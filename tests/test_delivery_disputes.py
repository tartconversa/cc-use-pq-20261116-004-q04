from fastapi.testclient import TestClient

from carbon_trace.api import create_app
from common import ORDER, Scenario


class DeliveryDisputeTests(Scenario):
    def setUp(self):
        super().setUp()
        self.populated()
        self.app.create_order(ORDER)
        self.app.delivery.commit({"commitment_id": "C1", "shipment_id": "SH1", "order_id": "O1",
                                  "expected_revision": self.app.status()["revision"]})

    def short(self, check_id="D1", received="9", at="2026-02-03T01:00:00Z", **extra):
        payload = {"check_id": check_id, "shipment_id": "SH1", "checked_at": at,
                   "received_quantity": received, "condition": "accepted",
                   "note": "Synthetic receipt", "party": "logistics",
                   "evidence_ref": f"ticket-{check_id}"}
        payload.update(extra)
        return self.app.record_delivery_check(payload)

    def settle(self, settlement_id="S1", accepted="9", checks=("D1",), disputes=(),
               expected_revision=None, **extra):
        payload = {"settlement_id": settlement_id, "shipment_id": "SH1",
                   "expected_revision": (self.app.status()["revision"]
                                         if expected_revision is None else expected_revision),
                   "decided_at": "2026-02-05T00:00:00Z", "decided_by": "settlement-desk",
                   "note": "Synthetic settlement", "accepted_quantity": accepted,
                   "basis_check_ids": list(checks), "dispute_ids": list(disputes)}
        payload.update(extra)
        return self.app.settle_delivery(payload)

    def dispute(self, dispute_id="X1", claimed="1", at="2026-02-03T02:00:00Z"):
        return self.app.open_delivery_dispute({
            "dispute_id": dispute_id, "shipment_id": "SH1", "opened_by": "buyer",
            "opened_at": at, "claimed_quantity": claimed,
            "kind": "short_receipt", "reason": "Synthetic shortfall",
            "evidence_ref": f"weigh-ticket-{dispute_id}", "note": "Claimed against delivery"})

    def post_settlement_reweigh(self, dispute_id="X2", check_id="D2", received="9.5"):
        self.dispute(dispute_id=dispute_id, at="2026-02-05T06:00:00Z")
        return self.short(check_id=check_id, received=received,
                          at="2026-02-06T01:00:00Z", dispute_id=dispute_id)

    # -- provisional evidence -------------------------------------------------
    def test_short_receipt_is_provisional_until_settled(self):
        self.short()
        order = self.app.delivery.order("O1")
        self.assertEqual(order["received_tons"], "9")
        self.assertEqual(order["accepted_received_tons"], "9")
        self.assertEqual(order["settled_tons"], "0")
        self.assertEqual(order["unsettled_tons"], "10")
        self.assertEqual(order["shortfall_tons"], "1")
        self.assertEqual(order["settlement_status"], "pending")
        self.assertIn("unsettled_receipt", {risk["code"] for risk in order["unresolved_risks"]})
        shipment = self.app.delivery.shipment("SH1")
        self.assertEqual(shipment["conclusion"]["basis"], "provisional_check")
        self.assertTrue(shipment["conclusion"]["provisional"])

    def test_quantity_breakdown_separates_pending_and_damaged(self):
        self.app.record_delivery_check({
            "check_id": "D1", "shipment_id": "SH1", "checked_at": "2026-02-03T01:00:00Z",
            "received_quantity": "10", "party": "buyer", "evidence_ref": "reweigh-1",
            "note": "Mixed disposition", "accepted_quantity": "7",
            "damaged_quantity": "2", "pending_quantity": "1"})
        shipment = self.app.delivery.shipment("SH1")["conclusion"]
        self.assertEqual(shipment["accepted_quantity"], "7")
        self.assertEqual(shipment["damaged_quantity"], "2")
        self.assertEqual(shipment["pending_quantity"], "1")
        self.assertEqual(shipment["rejected_quantity"], "0")
        self.assertEqual(shipment["shortfall_quantity"], "0")

    def test_breakdown_must_reconcile_to_received(self):
        self.expect_domain(lambda: self.app.record_delivery_check({
            "check_id": "D1", "shipment_id": "SH1", "checked_at": "2026-02-03T01:00:00Z",
            "received_quantity": "10", "accepted_quantity": "7", "damaged_quantity": "2",
            "note": "Broken breakdown"}), "invalid_input")

    # -- disputes -------------------------------------------------------------
    def test_open_dispute_blocks_clean_settlement_status(self):
        self.short()
        self.dispute()
        order = self.app.delivery.order("O1")
        self.assertEqual(order["settlement_status"], "disputed")
        self.assertEqual(order["current_basis"]["open_dispute_ids"], ["X1"])
        self.assertIn("open_dispute", {risk["code"] for risk in order["unresolved_risks"]})

    def test_dispute_requires_evidence_and_resolution_is_idempotent(self):
        self.expect_domain(lambda: self.app.open_delivery_dispute({
            "dispute_id": "X1", "shipment_id": "SH1", "opened_by": "buyer",
            "opened_at": "2026-02-03T02:00:00Z", "claimed_quantity": "1",
            "kind": "short_receipt", "reason": "No proof", "note": "Missing ref"}),
            "invalid_input")
        self.dispute()
        payload = {"expected_revision": self.app.status()["revision"],
                   "resolved_at": "2026-02-04T00:00:00Z", "resolved_by": "settlement-desk",
                   "resolution": "accepted buyer reweigh", "evidence_ref": "joint-report-1"}
        first = self.app.resolve_delivery_dispute("X1", payload)
        replay = self.app.resolve_delivery_dispute("X1", payload)
        self.assertEqual(first["status"], "resolved")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["resolved_revision"], replay["resolved_revision"])

    def test_dispute_cannot_be_resolved_twice_with_different_evidence(self):
        self.dispute()
        payload = {"expected_revision": self.app.status()["revision"],
                   "resolved_at": "2026-02-04T00:00:00Z", "resolved_by": "settlement-desk",
                   "resolution": "accepted", "evidence_ref": "joint-report-1"}
        self.app.resolve_delivery_dispute("X1", payload)
        changed = {**payload, "expected_revision": self.app.status()["revision"],
                   "resolution": "something else"}
        self.expect_domain(lambda: self.app.resolve_delivery_dispute("X1", changed), "conflict")

    # -- settlements and repeated/crossed submissions -------------------------
    def test_settlement_is_final_and_replay_is_safe(self):
        self.short()
        result = self.settle()
        replay = self.settle()
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["settlement_id"], replay["settlement_id"])
        order = self.app.delivery.order("O1")
        self.assertEqual(order["settled_tons"], "9")
        self.assertEqual(order["unsettled_tons"], "1")
        self.assertEqual(order["settlement_status"], "settled")
        self.assertEqual(self.app.delivery.shipment("SH1")["conclusion"]["basis"], "settlement")

    def test_second_settlement_requires_explicit_supersession(self):
        self.short()
        self.settle()
        self.post_settlement_reweigh()
        self.expect_domain(lambda: self.settle(settlement_id="S2", accepted="9.5",
                                               received_quantity="9.5", checks=("D2",)),
                           "conflict")

    def test_supersession_is_versioned_and_preserves_history(self):
        self.short()
        self.settle()
        self.post_settlement_reweigh()
        second = self.settle(settlement_id="S2", accepted="9.5",
                             received_quantity="9.5", checks=("D2",),
                             supersedes_settlement_id="S1")
        self.assertEqual(second["version"], 2)
        shipments = self.app.delivery.shipment("SH1")
        self.assertEqual([item["status"] for item in shipments["settlements"]],
                         ["superseded", "active"])
        self.assertEqual(shipments["active_settlement"]["settlement_id"], "S2")
        self.assertEqual(self.app.delivery.order("O1")["settled_tons"], "9.5")

    def test_stale_settlement_does_not_replace_active_conclusion(self):
        self.short()
        self.settle()
        self.dispute()
        self.post_settlement_reweigh()
        self.expect_domain(lambda: self.settle(settlement_id="S2", accepted="9.5",
                                               received_quantity="9.5", checks=("D2",),
                                               supersedes_settlement_id="S1",
                                               expected_revision=0), "stale_revision")
        self.assertEqual(self.app.delivery.shipment("SH1")["active_settlement"]["settlement_id"],
                         "S1")

    def test_settlement_cannot_be_based_on_foreign_evidence(self):
        self.short()
        self.app.delivery.check({"check_id": "DX", "shipment_id": "SH1",
                                 "checked_at": "2026-02-03T05:00:00Z", "received_quantity": "9",
                                 "condition": "accepted", "note": "later same shipment"})
        self.expect_domain(lambda: self.settle(checks=("DX", "D-none")), "not_found")

    def test_late_reweigh_after_settlement_is_disclosed_not_applied(self):
        self.short()
        self.settle()
        self.post_settlement_reweigh()
        shipment = self.app.delivery.shipment("SH1")
        self.assertEqual(shipment["active_settlement"]["settlement_id"], "S1")
        self.assertEqual(shipment["conclusion"]["accepted_quantity"], "9")
        self.assertEqual(shipment["conclusion"]["late_check_ids"], ["D2"])
        self.assertTrue(shipment["conclusion"]["review_required"])
        order = self.app.delivery.order("O1")
        self.assertIn("check_after_settlement", {risk["code"] for risk in order["unresolved_risks"]})
        self.assertEqual(order["settled_tons"], "9")

    def test_out_of_order_recheck_does_not_override_later_weighing(self):
        self.short(check_id="D1", received="9", at="2026-02-03T05:00:00Z")
        self.short(check_id="D0", received="8", at="2026-02-03T01:00:00Z",
                   supersedes_check_id="D1")
        shipment = self.app.delivery.shipment("SH1")
        self.assertEqual(shipment["effective_check_id"], "D1")
        self.assertEqual(shipment["conclusion"]["accepted_quantity"], "9")
        self.assertEqual(shipment["conclusion"]["out_of_order_check_ids"], ["D0"])

    def test_recheck_without_evidence_is_rejected(self):
        self.short()
        self.expect_domain(lambda: self.app.record_delivery_check({
            "check_id": "D2", "shipment_id": "SH1", "checked_at": "2026-02-04T01:00:00Z",
            "received_quantity": "9.5", "condition": "accepted", "note": "no proof",
            "supersedes_check_id": "D1"}), "invalid_input")

    def test_post_settlement_check_without_open_dispute_is_rejected(self):
        self.short()
        self.settle()
        self.expect_domain(lambda: self.short(check_id="D2", received="9.5",
                                               at="2026-02-06T01:00:00Z"),
                           "business_invariant")
        self.assertEqual(self.app.delivery.order("O1")["settled_tons"], "9")

    def test_check_id_reuse_with_different_content_conflicts(self):
        self.short()
        self.expect_domain(lambda: self.short(received="8.5"), "conflict")

    def test_identical_check_retry_is_a_replay(self):
        first = self.short()
        replay = self.short()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["revision"], replay["revision"])
        self.assertEqual(self.app.status()["revision"], first["revision"])

    def test_settlement_cannot_use_an_open_dispute_as_resolution(self):
        self.short()
        self.dispute()
        settlement = self.settle(disputes=("X1",))
        self.assertTrue(settlement["provisional"])
        self.assertEqual(settlement["basis"]["open_dispute_ids"], ["X1"])

    # -- order and reconciliation alignment -----------------------------------
    def test_order_quantities_align_with_basis_and_risk(self):
        self.short()
        self.dispute()
        self.app.resolve_delivery_dispute("X1", {
            "expected_revision": self.app.status()["revision"],
            "resolved_at": "2026-02-04T00:00:00Z", "resolved_by": "settlement-desk",
            "resolution": "buyer reweigh accepted", "evidence_ref": "joint-report-1"})
        self.settle(accepted="9", disputes=("X1",))
        order = self.app.delivery.order("O1")
        self.assertEqual(order["ordered_tons"], "30")
        self.assertEqual(order["committed_tons"], "10")
        self.assertEqual(order["shipped_tons"], "10")
        self.assertEqual(order["received_tons"], "9")
        self.assertEqual(order["settled_tons"], "9")
        self.assertEqual(order["unsettled_tons"], "1")
        self.assertEqual(order["shortfall_tons"], "1")
        self.assertEqual(order["unreceived_tons"], "21")
        self.assertEqual(order["settlement_status"], "settled")
        self.assertEqual(order["current_basis"]["settlement_ids"], ["S1"])

    def test_sales_order_detail_exposes_basis_and_risks(self):
        self.short()
        detail = self.app.orders.detail("O1")
        self.assertEqual(detail["settlement_status"], "pending")
        self.assertEqual(detail["current_basis"]["unsettled_quantity"], "10")
        self.assertTrue(detail["unresolved_risks"])
        self.assertEqual(detail["settlement_basis"][0]["effective_check_id"], "D1")
        self.assertEqual(detail["uncovered_tons"], "20")

    def test_physical_shipped_tons_stay_stable_across_disputes_and_settlement(self):
        report_before = self.app.report("P1", "2026-02-01", "2026-02-02")
        published = self._publish("V1")
        frozen_before = self.app.publication.version(published["version"])
        self.short()
        self.dispute()
        self.app.resolve_delivery_dispute("X1", {
            "expected_revision": self.app.status()["revision"],
            "resolved_at": "2026-02-04T00:00:00Z", "resolved_by": "settlement-desk",
            "resolution": "resolved", "evidence_ref": "joint-report-1"})
        self.settle()
        report_after = self.app.report("P1", "2026-02-01", "2026-02-02")
        self.assertEqual(report_after["rows"][1]["shipped_tons"], "10")
        self.assertEqual(report_after["rows"], report_before["rows"])
        self.assertEqual(self.app.publication.version(frozen_before["version"])["rows"],
                         report_before["rows"])

    def test_reconciliation_explains_evidence_shortfall(self):
        self.short()
        result = self.app.reconciliation.plant("P1", "2026-02-02", "2026-02-02")["delivery"]
        self.assertEqual(result["physical_shipment_total_tons"], "10")
        self.assertEqual(result["received_tons"], "9")
        self.assertEqual(result["evidence_vs_physical_tons"], "-1")
        self.assertEqual(result["unsettled_tons"], "10")
        self.assertTrue(result["risks"])

    def test_source_reconciliation_explains_delivery_gap(self):
        self.short()
        evidence = self.app.reconciliation.source("factory")["delivery_evidence"]
        self.assertEqual(evidence["shipped_tons"], "10")
        self.assertEqual(evidence["received_tons"], "9")
        self.assertEqual(evidence["evidence_vs_physical_tons"], "-1")

    def test_api_dispute_and_settlement_endpoints(self):
        client = TestClient(create_app(self.path))
        self.short()
        dispute = client.post("/delivery/disputes", json={
            "dispute_id": "X1", "shipment_id": "SH1", "opened_by": "buyer",
            "opened_at": "2026-02-03T02:00:00Z", "claimed_quantity": "1",
            "kind": "short_receipt", "reason": "Synthetic shortfall",
            "evidence_ref": "weigh-ticket-2", "note": "Claimed"})
        self.assertEqual(dispute.status_code, 201)
        listed = client.get("/delivery/shipments/SH1/disputes").json()
        self.assertEqual(listed[0]["dispute_id"], "X1")
        settled = client.post("/delivery/settlements", json={
            "settlement_id": "S1", "shipment_id": "SH1",
            "expected_revision": self.app.status()["revision"],
            "decided_at": "2026-02-05T00:00:00Z", "decided_by": "settlement-desk",
            "note": "Final", "accepted_quantity": "9", "basis_check_ids": ["D1"],
            "dispute_ids": ["X1"]})
        self.assertEqual(settled.status_code, 201)
        self.assertTrue(settled.json()["provisional"])
        order = client.get("/orders/O1").json()
        self.assertEqual(order["settlement_status"], "disputed")

    def _publish(self, candidate_id):
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": candidate_id, "plant_id": "P1",
                          "from_day": "2026-02-01", "to_day": "2026-02-02",
                          "expected_head": self.app.status()["head"],
                          "expected_revision": revision})
        return self.app.publish(candidate_id, {"expected_head": self.app.status()["head"],
                                               "expected_revision": revision})
