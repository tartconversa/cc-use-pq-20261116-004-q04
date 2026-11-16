import json
from datetime import datetime, timezone
from decimal import Decimal

from .errors import Conflict, Invariant, Stale, require
from .store import Store
from .values import as_number, canonical, decimal, digest, identifier, instant, text

ZERO = Decimal(0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_decimal(payload: dict, key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    return decimal(value, key, nonnegative=True)


def _optional_text(payload: dict, key: str, maximum: int = 400) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return text(value, key, max_length=maximum)


class Delivery:
    CONDITIONS = {"pending", "accepted", "damaged", "rejected"}
    DISPUTE_KINDS = {"short_receipt", "damaged", "rejected", "quality", "quantity", "other"}

    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------ commitments
    def commit(self, payload: dict) -> dict:
        commitment_id = identifier(payload.get("commitment_id"), "commitment_id")
        shipment_id = identifier(payload.get("shipment_id"), "shipment_id")
        order_id = identifier(payload.get("order_id"), "order_id")
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0, "invalid expected_revision")
        with self.store.write() as db:
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed")
            shipment = Store.one(db, "SELECT shipments.*,lines.plant_id,lots.grade FROM shipments "
                                 "JOIN lots ON lots.lot_id=shipments.lot_id "
                                 "JOIN lines ON lines.line_id=lots.line_id WHERE shipment_id=?",
                                 (shipment_id,), "shipment")
            order = Store.one(db, "SELECT * FROM orders WHERE order_id=?", (order_id,), "order")
            if order["status"] != "open":
                raise Invariant("order is not open")
            if order["plant_id"] != shipment["plant_id"] or order["grade"] != shipment["grade"]:
                raise Invariant("shipment and order scope differ")
            if shipment["local_day"] > order["due_day"]:
                raise Invariant("shipment occurs after order due day")
            if db.execute("SELECT 1 FROM commitments WHERE shipment_id=? OR commitment_id=?",
                          (shipment_id, commitment_id)).fetchone():
                raise Conflict("shipment or commitment already matched")
            claimed = sum((decimal(row["quantity"], "quantity") for row in db.execute(
                "SELECT quantity FROM commitments WHERE order_id=?", (order_id,))), ZERO)
            quantity = decimal(shipment["quantity"], "quantity")
            if claimed + quantity > decimal(order["quantity"], "order quantity"):
                raise Invariant("shipments exceed order demand")
            revision = Store.advance(db, "delivery_commitment", commitment_id)
            db.execute("INSERT INTO commitments VALUES (?,?,?,?,?)",
                       (commitment_id, order_id, shipment_id, as_number(quantity), revision))
            return {"commitment_id": commitment_id, "quantity": as_number(quantity),
                    "revision": revision}

    # ------------------------------------------------------------------ checks
    def _check_digest(self, payload: dict, *, shipment_id: str, checked_at: str) -> str:
        return digest({
            "shipment_id": shipment_id, "checked_at": checked_at,
            "received_quantity": as_number(payload["_received"]),
            "condition": payload["_condition"],
            "note": payload["_note"], "party": payload["_party"],
            "evidence_ref": payload["_evidence_ref"],
            "accepted_quantity": as_number(payload["_accepted"]),
            "damaged_quantity": as_number(payload["_damaged"]),
            "rejected_quantity": as_number(payload["_rejected"]),
            "pending_quantity": as_number(payload["_pending"]),
            "supersedes_check_id": payload.get("_supersedes"),
            "dispute_id": payload.get("_dispute_id"),
        })

    @staticmethod
    def _parse_breakdown(payload: dict) -> dict:
        received = decimal(payload.get("received_quantity"), "received_quantity", nonnegative=True)
        raw = {key: _optional_decimal(payload, key) for key in
               ("accepted_quantity", "damaged_quantity", "rejected_quantity", "pending_quantity")}
        condition = payload.get("condition")
        explicit = any(value is not None for value in raw.values())
        if explicit:
            if condition is not None:
                require(condition in Delivery.CONDITIONS, "invalid delivery condition")
            quantities = {key: raw[key] if raw[key] is not None else ZERO for key in raw}
            require(sum(quantities.values(), ZERO) == received,
                    "delivery quantity breakdown must equal received_quantity")
            if condition is None:
                for candidate in ("pending", "damaged", "rejected", "accepted"):
                    if quantities[f"{candidate}_quantity"] > 0:
                        condition = candidate
                        break
                else:
                    condition = "accepted"
        else:
            require(condition in Delivery.CONDITIONS, "invalid delivery condition")
            quantities = {f"{name}_quantity": (received if condition == name else ZERO)
                          for name in Delivery.CONDITIONS}
        return {"received": received, "condition": condition,
                "accepted": quantities["accepted_quantity"],
                "damaged": quantities["damaged_quantity"],
                "rejected": quantities["rejected_quantity"],
                "pending": quantities["pending_quantity"]}

    def check(self, payload: dict) -> dict:
        check_id = identifier(payload.get("check_id"), "check_id")
        shipment_id = identifier(payload.get("shipment_id"), "shipment_id")
        checked_at = instant(payload.get("checked_at"), "checked_at")
        note = text(payload.get("note"), "note", max_length=400)
        party = text(payload.get("party", "logistics"), "party", max_length=80)
        evidence_ref = _optional_text(payload, "evidence_ref")
        supersedes = payload.get("supersedes_check_id")
        if supersedes is not None:
            supersedes = identifier(supersedes, "supersedes_check_id")
            require(evidence_ref is not None,
                    "a recheck that supersedes an earlier check needs evidence_ref",
                    field="evidence_ref")
        breakdown = self._parse_breakdown(payload)
        dispute_id = payload.get("dispute_id")
        if dispute_id is not None:
            dispute_id = identifier(dispute_id, "dispute_id")
        normalized = {"_received": breakdown["received"], "_condition": breakdown["condition"],
                      "_note": note, "_party": party, "_evidence_ref": evidence_ref,
                      "_accepted": breakdown["accepted"], "_damaged": breakdown["damaged"],
                      "_rejected": breakdown["rejected"], "_pending": breakdown["pending"],
                      "_supersedes": supersedes, "_dispute_id": dispute_id}
        payload_digest = self._check_digest(normalized, shipment_id=shipment_id, checked_at=checked_at)
        with self.store.write() as db:
            previous = db.execute("SELECT * FROM delivery_checks WHERE check_id=?", (check_id,)).fetchone()
            if previous:
                if previous["payload_digest"] is None:
                    same = (previous["shipment_id"] == shipment_id
                            and previous["checked_at"] == checked_at
                            and decimal(previous["received_quantity"], "received_quantity") ==
                            breakdown["received"]
                            and previous["note"] == note)
                else:
                    same = previous["payload_digest"] == payload_digest
                if not same:
                    raise Conflict("check_id reused with different evidence")
                return {**self._check_view(previous),
                        "revision": previous["accepted_revision"], "replayed": True}
            shipment = Store.one(db, "SELECT * FROM shipments WHERE shipment_id=?",
                                 (shipment_id,), "shipment")
            if checked_at < shipment["shipped_at"]:
                raise Invariant("delivery check precedes shipment")
            shipped = decimal(shipment["quantity"], "shipment quantity")
            received = breakdown["received"]
            if received > shipped:
                raise Invariant("received quantity exceeds shipped quantity")
            if supersedes is not None:
                prior = Store.one(db, "SELECT * FROM delivery_checks WHERE check_id=?",
                                  (supersedes,), "superseded check")
                if prior["shipment_id"] != shipment_id:
                    raise Invariant("superseded check belongs to another shipment")
                if db.execute("SELECT 1 FROM delivery_checks WHERE supersedes_check_id=?",
                              (supersedes,)).fetchone():
                    raise Conflict("check was already superseded")
                active = self._active_settlement(db, shipment_id)
                if active and supersedes in self._basis_check_ids(active):
                    raise Invariant("cannot supersede a check used by the active settlement")
            rows = db.execute("SELECT * FROM delivery_checks WHERE shipment_id=? "
                              "ORDER BY sequence,accepted_revision", (shipment_id,)).fetchall()
            active = self._active_settlement(db, shipment_id)
            if dispute_id is not None:
                dispute = Store.one(db, "SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                    (dispute_id,), "dispute")
                if dispute["shipment_id"] != shipment_id:
                    raise Invariant("dispute belongs to another shipment")
                if dispute["status"] != "open":
                    raise Invariant("check cannot cite a closed dispute")
            if active is not None:
                live_open = db.execute("SELECT 1 FROM delivery_disputes WHERE shipment_id=? "
                                       "AND status='open' LIMIT 1", (shipment_id,)).fetchone()
                if live_open is None:
                    raise Invariant("active settlement already closes this shipment; "
                                    "open a dispute before submitting further delivery evidence")
                if dispute_id is None:
                    raise Invariant("delivery evidence after a settlement must cite the open dispute "
                                    "it responds to")
            elif dispute_id is not None:
                dispute = Store.one(db, "SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                    (dispute_id,), "dispute")
                if dispute["status"] == "open" and checked_at < dispute["opened_at"]:
                    raise Invariant("check precedes the dispute it cites")
            sequence = max((row["sequence"] for row in rows), default=0) + 1
            latest_business = max((row["checked_at"] for row in rows), default=None)
            out_of_order = int(latest_business is not None and checked_at <= latest_business)
            late = int(active is not None)
            revision = Store.advance(db, "delivery_check", check_id)
            db.execute("INSERT INTO delivery_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (check_id, shipment_id, checked_at, as_number(received),
                        breakdown["condition"], note, revision, party, evidence_ref, sequence,
                        as_number(breakdown["accepted"]), as_number(breakdown["damaged"]),
                        as_number(breakdown["rejected"]), as_number(breakdown["pending"]),
                        dispute_id, payload_digest, late, out_of_order,
                        supersedes, _now()))
            return {"check_id": check_id, "shipment_id": shipment_id, "revision": revision,
                    "sequence": sequence, "received_quantity": as_number(received),
                    "condition": breakdown["condition"],
                    "accepted_quantity": as_number(breakdown["accepted"]),
                    "damaged_quantity": as_number(breakdown["damaged"]),
                    "rejected_quantity": as_number(breakdown["rejected"]),
                    "pending_quantity": as_number(breakdown["pending"]),
                    "supersedes_check_id": supersedes, "late": bool(late),
                    "out_of_order": bool(out_of_order), "replayed": False}

    # ---------------------------------------------------------------- disputes
    def _dispute_digest(self, payload: dict) -> str:
        return digest({key: payload[key] for key in
                       ("shipment_id", "opened_by", "opened_at", "claimed_quantity",
                        "kind", "reason", "evidence_ref", "note")})

    def open_dispute(self, payload: dict) -> dict:
        dispute_id = identifier(payload.get("dispute_id"), "dispute_id")
        shipment_id = identifier(payload.get("shipment_id"), "shipment_id")
        opened_by = text(payload.get("opened_by"), "opened_by", max_length=80)
        opened_at = instant(payload.get("opened_at"), "opened_at")
        claimed = decimal(payload.get("claimed_quantity"), "claimed_quantity", nonnegative=True)
        kind = payload.get("kind")
        require(kind in self.DISPUTE_KINDS, "invalid dispute kind")
        reason = text(payload.get("reason"), "reason", max_length=400)
        evidence_ref = text(payload.get("evidence_ref"), "evidence_ref", max_length=400)
        note = text(payload.get("note"), "note", max_length=400)
        normalized = {"shipment_id": shipment_id, "opened_by": opened_by,
                      "opened_at": opened_at, "claimed_quantity": as_number(claimed),
                      "kind": kind, "reason": reason, "evidence_ref": evidence_ref, "note": note}
        payload_digest = self._dispute_digest(normalized)
        with self.store.write() as db:
            previous = db.execute("SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                  (dispute_id,)).fetchone()
            if previous:
                if previous["payload_digest"] != payload_digest:
                    raise Conflict("dispute_id reused with different evidence")
                return {**self._dispute_view(previous), "replayed": True}
            shipment = Store.one(db, "SELECT * FROM shipments WHERE shipment_id=?",
                                 (shipment_id,), "shipment")
            if opened_at < shipment["shipped_at"]:
                raise Invariant("dispute precedes shipment")
            revision = Store.advance(db, "delivery_dispute", dispute_id)
            db.execute("INSERT INTO delivery_disputes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (dispute_id, shipment_id, opened_by, opened_at, as_number(claimed), kind,
                        reason, evidence_ref, note, "open", revision, None, None, None, None,
                        None, None, payload_digest))
            return {"dispute_id": dispute_id, "shipment_id": shipment_id, "status": "open",
                    "claimed_quantity": as_number(claimed), "revision": revision,
                    "replayed": False}

    def resolve_dispute(self, dispute_id: str, payload: dict) -> dict:
        dispute_id = identifier(dispute_id, "dispute_id")
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0,
                "invalid expected_revision")
        resolved_at = instant(payload.get("resolved_at"), "resolved_at")
        resolved_by = text(payload.get("resolved_by"), "resolved_by", max_length=80)
        resolution = text(payload.get("resolution"), "resolution", max_length=400)
        evidence_ref = text(payload.get("evidence_ref"), "evidence_ref", max_length=400)
        resolution_digest = digest({"dispute_id": dispute_id, "resolved_at": resolved_at,
                                    "resolved_by": resolved_by, "resolution": resolution,
                                    "evidence_ref": evidence_ref})
        with self.store.write() as db:
            dispute = Store.one(db, "SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                (dispute_id,), "dispute")
            if dispute["resolution_payload_digest"] == resolution_digest:
                return {**dict(dispute), "replayed": True}
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed")
            if dispute["status"] != "open":
                raise Conflict("dispute is no longer open")
            if resolved_at < dispute["opened_at"]:
                raise Invariant("dispute resolution precedes opening")
            revision = Store.advance(db, "delivery_dispute", dispute_id)
            db.execute("UPDATE delivery_disputes SET status='resolved',resolved_revision=?,"
                       "resolution=?,resolved_at=?,resolved_by=?,resolution_evidence_ref=?,"
                       "resolution_payload_digest=? WHERE dispute_id=?",
                       (revision, resolution, resolved_at, resolved_by, evidence_ref,
                        resolution_digest, dispute_id))
            row = db.execute("SELECT * FROM delivery_disputes WHERE dispute_id=?",
                             (dispute_id,)).fetchone()
            return {**dict(row), "replayed": False}

    def withdraw_dispute(self, dispute_id: str, payload: dict) -> dict:
        dispute_id = identifier(dispute_id, "dispute_id")
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0,
                "invalid expected_revision")
        resolved_at = instant(payload.get("resolved_at"), "resolved_at")
        resolved_by = text(payload.get("resolved_by"), "resolved_by", max_length=80)
        resolution = text(payload.get("resolution", "withdrawn"), "resolution", max_length=400)
        evidence_ref = text(payload.get("evidence_ref"), "evidence_ref", max_length=400)
        resolution_digest = digest({"dispute_id": dispute_id, "resolved_at": resolved_at,
                                    "resolved_by": resolved_by, "resolution": resolution,
                                    "evidence_ref": evidence_ref, "withdrawn": True})
        with self.store.write() as db:
            dispute = Store.one(db, "SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                (dispute_id,), "dispute")
            if dispute["resolution_payload_digest"] == resolution_digest:
                return {**dict(dispute), "replayed": True}
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed")
            if dispute["status"] != "open":
                raise Conflict("dispute is no longer open")
            if resolved_at < dispute["opened_at"]:
                raise Invariant("dispute withdrawal precedes opening")
            revision = Store.advance(db, "delivery_dispute", dispute_id)
            db.execute("UPDATE delivery_disputes SET status='withdrawn',resolved_revision=?,"
                       "resolution=?,resolved_at=?,resolved_by=?,resolution_evidence_ref=?,"
                       "resolution_payload_digest=? WHERE dispute_id=?",
                       (revision, resolution, resolved_at, resolved_by, evidence_ref,
                        resolution_digest, dispute_id))
            row = db.execute("SELECT * FROM delivery_disputes WHERE dispute_id=?",
                             (dispute_id,)).fetchone()
            return {**dict(row), "replayed": False}

    # ------------------------------------------------------------- settlements
    @staticmethod
    def _active_settlement(db, shipment_id: str):
        return db.execute("SELECT * FROM delivery_settlements WHERE shipment_id=? AND status='active' "
                          "ORDER BY version DESC LIMIT 1", (shipment_id,)).fetchone()

    @staticmethod
    def _basis_check_ids(settlement) -> set[str]:
        try:
            basis = json.loads(settlement["basis_json"])
        except (TypeError, ValueError):
            return set()
        return {item["check_id"] for item in basis.get("checks", [])}

    def _settlement_digest(self, payload: dict, normalized: dict) -> str:
        return digest({"settlement_id": payload["settlement_id"], **normalized})

    def settle(self, payload: dict) -> dict:
        settlement_id = identifier(payload.get("settlement_id"), "settlement_id")
        shipment_id = identifier(payload.get("shipment_id"), "shipment_id")
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0,
                "invalid expected_revision")
        decided_at = instant(payload.get("decided_at"), "decided_at")
        decided_by = text(payload.get("decided_by"), "decided_by", max_length=80)
        note = text(payload.get("note"), "note", max_length=400)
        evidence_ref = _optional_text(payload, "evidence_ref")
        accepted = decimal(payload.get("accepted_quantity"), "accepted_quantity", nonnegative=True)
        damaged = decimal(payload.get("damaged_quantity", 0), "damaged_quantity", nonnegative=True)
        rejected = decimal(payload.get("rejected_quantity", 0), "rejected_quantity", nonnegative=True)
        pending = decimal(payload.get("pending_quantity", 0), "pending_quantity", nonnegative=True)
        check_ids = payload.get("basis_check_ids")
        require(isinstance(check_ids, list) and bool(check_ids) and len(check_ids) <= 100,
                "basis_check_ids must list 1-100 checks")
        check_ids = [identifier(item, "basis_check_ids") for item in check_ids]
        require(len(set(check_ids)) == len(check_ids), "basis_check_ids contains duplicates")
        dispute_ids = payload.get("dispute_ids", [])
        require(isinstance(dispute_ids, list) and len(dispute_ids) <= 100,
                "dispute_ids must list 0-100 disputes")
        dispute_ids = [identifier(item, "dispute_ids") for item in dispute_ids]
        require(len(set(dispute_ids)) == len(dispute_ids), "dispute_ids contains duplicates")
        supersedes = payload.get("supersedes_settlement_id")
        if supersedes is not None:
            supersedes = identifier(supersedes, "supersedes_settlement_id")
        received_declared = _optional_decimal(payload, "received_quantity")
        normalized = {"shipment_id": shipment_id, "decided_at": decided_at,
                      "decided_by": decided_by, "note": note, "evidence_ref": evidence_ref,
                      "accepted_quantity": as_number(accepted), "damaged_quantity": as_number(damaged),
                      "rejected_quantity": as_number(rejected),
                      "pending_quantity": as_number(pending),
                      "basis_check_ids": sorted(check_ids), "dispute_ids": sorted(dispute_ids),
                      "received_quantity": as_number(received_declared) if received_declared is not None else None,
                      "supersedes_settlement_id": supersedes}
        payload_digest = self._settlement_digest(payload, normalized)
        with self.store.write() as db:
            previous = db.execute("SELECT * FROM delivery_settlements WHERE settlement_id=?",
                                  (settlement_id,)).fetchone()
            if previous:
                if previous["payload_digest"] != payload_digest:
                    raise Conflict("settlement_id reused with different decision")
                return {**dict(previous), "basis": json.loads(previous["basis_json"]),
                        "replayed": True}
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed before settlement")
            shipment = Store.one(db, "SELECT * FROM shipments WHERE shipment_id=?",
                                 (shipment_id,), "shipment")
            if decided_at < shipment["shipped_at"]:
                raise Invariant("settlement precedes shipment")
            checks = []
            for check_id in check_ids:
                row = Store.one(db, "SELECT * FROM delivery_checks WHERE check_id=?",
                                (check_id,), "basis check")
                if row["shipment_id"] != shipment_id:
                    raise Invariant("basis check belongs to another shipment")
                checks.append(row)
            disputes = []
            for dispute_id in dispute_ids:
                row = Store.one(db, "SELECT * FROM delivery_disputes WHERE dispute_id=?",
                                (dispute_id,), "basis dispute")
                if row["shipment_id"] != shipment_id:
                    raise Invariant("basis dispute belongs to another shipment")
                disputes.append(row)
            for row in checks:
                if db.execute("SELECT 1 FROM delivery_checks WHERE supersedes_check_id=?",
                              (row["check_id"],)).fetchone():
                    raise Invariant("basis check was superseded by a later check")
            authoritative = max(checks, key=lambda row: (row["checked_at"], row["sequence"]))
            received = decimal(authoritative["received_quantity"], "received_quantity")
            if received_declared is not None:
                require(received_declared in {decimal(row["received_quantity"], "received_quantity")
                                              for row in checks},
                        "settlement received_quantity must match a basis check",
                        field="received_quantity")
                received = received_declared
            require(accepted + damaged + rejected + pending == received,
                    "settlement accepted/damaged/rejected/pending tons must add up to "
                    "the received quantity named by the basis checks")
            require(received <= decimal(shipment["quantity"], "shipment quantity"),
                    "settlement received quantity exceeds shipment")
            active = self._active_settlement(db, shipment_id)
            if supersedes is not None:
                if active is None or active["settlement_id"] != supersedes:
                    raise Conflict("superseded settlement is not the active settlement")
            elif active is not None:
                raise Conflict("shipment already has an active settlement; "
                               "explicit supersedes_settlement_id is required")
            open_referenced = [row["dispute_id"] for row in disputes if row["status"] == "open"]
            version = db.execute("SELECT coalesce(max(version),0) FROM delivery_settlements "
                                 "WHERE shipment_id=?", (shipment_id,)).fetchone()[0] + 1
            revision = Store.advance(db, "delivery_settlement", settlement_id)
            basis = {
                "received_quantity": as_number(received),
                "checks": [{"check_id": row["check_id"], "checked_at": row["checked_at"],
                            "sequence": row["sequence"], "received_quantity": row["received_quantity"],
                            "condition": row["condition"],
                            "accepted_quantity": row["accepted_quantity"],
                            "damaged_quantity": row["damaged_quantity"],
                            "rejected_quantity": row["rejected_quantity"],
                            "pending_quantity": row["pending_quantity"],
                            "party": row["party"], "evidence_ref": row["evidence_ref"],
                            "late": bool(row["late"]),
                            "out_of_order": bool(row["out_of_order"])} for row in checks],
                "disputes": [{"dispute_id": row["dispute_id"], "status": row["status"],
                              "kind": row["kind"], "claimed_quantity": row["claimed_quantity"],
                              "evidence_ref": row["evidence_ref"],
                              "resolution": row["resolution"]} for row in disputes],
                "open_dispute_ids": open_referenced,
            }
            settled = accepted + damaged
            db.execute("INSERT INTO delivery_settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (settlement_id, shipment_id, version, as_number(accepted),
                        as_number(damaged), as_number(rejected), as_number(pending),
                        as_number(settled), canonical(basis), evidence_ref, decided_by, decided_at,
                        note, "active", revision, supersedes, None, payload_digest))
            if supersedes is not None:
                db.execute("UPDATE delivery_settlements SET status='superseded',superseded_revision=? "
                           "WHERE settlement_id=?", (revision, supersedes))
            return {"settlement_id": settlement_id, "shipment_id": shipment_id,
                    "version": version, "accepted_quantity": as_number(accepted),
                    "damaged_quantity": as_number(damaged), "rejected_quantity": as_number(rejected),
                    "pending_quantity": as_number(pending), "received_quantity": as_number(received),
                    "settled_quantity": as_number(settled),
                    "shortfall_quantity": as_number(decimal(shipment["quantity"], "shipment quantity") - received),
                    "provisional": bool(open_referenced), "basis": basis,
                    "supersedes_settlement_id": supersedes, "revision": revision,
                    "replayed": False}

    # ------------------------------------------------------------------ views
    @staticmethod
    def _check_view(row) -> dict:
        return {"check_id": row["check_id"], "shipment_id": row["shipment_id"],
                "checked_at": row["checked_at"], "received_quantity": row["received_quantity"],
                "condition": row["condition"], "note": row["note"],
                "party": row["party"], "evidence_ref": row["evidence_ref"],
                "sequence": row["sequence"],
                "accepted_quantity": row["accepted_quantity"],
                "damaged_quantity": row["damaged_quantity"],
                "rejected_quantity": row["rejected_quantity"],
                "pending_quantity": row["pending_quantity"],
                "dispute_id": row["dispute_id"], "late": bool(row["late"]),
                "out_of_order": bool(row["out_of_order"]),
                "supersedes_check_id": row["supersedes_check_id"],
                "recorded_at": row["recorded_at"],
                "accepted_revision": row["accepted_revision"]}

    @staticmethod
    def _settlement_view(row) -> dict:
        basis = json.loads(row["basis_json"])
        return {"settlement_id": row["settlement_id"], "shipment_id": row["shipment_id"],
                "version": row["version"],
                "accepted_quantity": row["accepted_quantity"],
                "damaged_quantity": row["damaged_quantity"],
                "rejected_quantity": row["rejected_quantity"],
                "pending_quantity": row["pending_quantity"],
                "settled_quantity": row["settled_quantity"],
                "basis": basis, "evidence_ref": row["evidence_ref"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "note": row["note"], "status": row["status"],
                "accepted_revision": row["accepted_revision"],
                "supersedes_settlement_id": row["supersedes_settlement_id"],
                "superseded_revision": row["superseded_revision"]}

    @staticmethod
    def _dispute_view(row) -> dict:
        return {"dispute_id": row["dispute_id"], "shipment_id": row["shipment_id"],
                "opened_by": row["opened_by"], "opened_at": row["opened_at"],
                "claimed_quantity": row["claimed_quantity"], "kind": row["kind"],
                "reason": row["reason"], "evidence_ref": row["evidence_ref"],
                "note": row["note"], "status": row["status"],
                "opened_revision": row["opened_revision"],
                "resolved_revision": row["resolved_revision"],
                "resolution": row["resolution"], "resolved_at": row["resolved_at"],
                "resolved_by": row["resolved_by"],
                "resolution_evidence_ref": row["resolution_evidence_ref"]}

    def _accounting(self, db, shipment) -> dict:
        shipment_id = shipment["shipment_id"]
        shipped = decimal(shipment["quantity"], "shipment quantity")
        checks = db.execute("SELECT * FROM delivery_checks WHERE shipment_id=? "
                            "ORDER BY sequence,accepted_revision", (shipment_id,)).fetchall()
        settlement = self._active_settlement(db, shipment_id)
        # Authority is business time, then acceptance sequence.  A recheck that
        # arrives late with an earlier weighing time never silently replaces the
        # most recent weighing; it remains visible as evidence with late=True.
        effective = max(checks, key=lambda row: (row["checked_at"], row["sequence"])) if checks else None
        if settlement is not None:
            basis_json = json.loads(settlement["basis_json"])
            basis = "settlement"
            received = decimal(basis_json["received_quantity"], "received_quantity")
            accepted = decimal(settlement["accepted_quantity"], "accepted")
            damaged = decimal(settlement["damaged_quantity"], "damaged")
            rejected = decimal(settlement["rejected_quantity"], "rejected")
            pending = decimal(settlement["pending_quantity"], "pending")
            settled = accepted + damaged
            provisional = bool(basis_json.get("open_dispute_ids"))
            settlement_id = settlement["settlement_id"]
            settlement_revision = settlement["accepted_revision"]
        elif effective is not None:
            basis = "provisional_check"
            received = decimal(effective["received_quantity"], "received_quantity")
            accepted = decimal(effective["accepted_quantity"], "accepted")
            damaged = decimal(effective["damaged_quantity"], "damaged")
            rejected = decimal(effective["rejected_quantity"], "rejected")
            pending = decimal(effective["pending_quantity"], "pending")
            settled = ZERO
            provisional = True
            settlement_id = None
            settlement_revision = None
        else:
            basis = "none"
            received = accepted = damaged = rejected = settled = ZERO
            pending = shipped
            provisional = True
            settlement_id = None
            settlement_revision = None
        shortfall = shipped - received if basis != "none" else ZERO
        open_disputes = db.execute("SELECT * FROM delivery_disputes WHERE shipment_id=? "
                                   "AND status='open' ORDER BY opened_at,dispute_id",
                                   (shipment_id,)).fetchall()
        if settlement is not None:
            # A settlement is only an accounting conclusion for the evidence named
            # in its basis.  A check that arrived afterwards is disclosed but never
            # silently folded into an already-valid conclusion.  Superseded checks
            # that existed before the settlement are history, not a late arrival.
            basis_ids = self._basis_check_ids(settlement)
            late_ids = [row["check_id"] for row in checks
                        if row["check_id"] not in basis_ids
                        and (row["late"] or row["accepted_revision"] > settlement["accepted_revision"])]
        else:
            late_ids = [row["check_id"] for row in checks if row["late"]]
        return {
            "shipment_id": shipment_id, "shipped_quantity": shipped,
            "received_quantity": received, "accepted_quantity": accepted,
            "damaged_quantity": damaged, "rejected_quantity": rejected,
            "pending_quantity": pending, "settled_quantity": settled,
            "shortfall_quantity": shortfall,
            "unsettled_quantity": shipped - settled,
            "basis": basis, "provisional": provisional,
            "settlement_id": settlement_id, "settlement_revision": settlement_revision,
            "effective_check_id": effective["check_id"] if effective else None,
            "checks": checks, "settlement": settlement,
            "open_disputes": open_disputes,
            "late_check_ids": late_ids,
            "out_of_order_check_ids": [row["check_id"] for row in checks if row["out_of_order"]],
        }

    def shipment(self, shipment_id: str) -> dict:
        shipment_id = identifier(shipment_id, "shipment_id")
        with self.store.read() as db:
            shipment = Store.one(db, "SELECT * FROM shipments WHERE shipment_id=?",
                                 (shipment_id,), "shipment")
            accounting = self._accounting(db, shipment)
            checks = [self._check_view(row) for row in accounting["checks"]]
            settlements = [self._settlement_view(row) for row in db.execute(
                "SELECT * FROM delivery_settlements WHERE shipment_id=? ORDER BY version",
                (shipment_id,))]
            disputes = [self._dispute_view(row) for row in db.execute(
                "SELECT * FROM delivery_disputes WHERE shipment_id=? ORDER BY opened_at,dispute_id",
                (shipment_id,))]
            commitment = db.execute("SELECT * FROM commitments WHERE shipment_id=?",
                                    (shipment_id,)).fetchone()
            effective = next((row for row in checks
                          if row["check_id"] == accounting["effective_check_id"]), None)
            return {"shipment": dict(shipment), "checks": checks,
                    "commitment": dict(commitment) if commitment else None,
                    "latest_condition": effective["condition"] if effective else "pending",
                    "effective_check_id": accounting["effective_check_id"],
                    "settlements": settlements, "disputes": disputes,
                    "open_disputes": [item for item in disputes if item["status"] == "open"],
                    "active_settlement": self._settlement_view(accounting["settlement"])
                    if accounting["settlement"] else None,
                    "conclusion": {
                        "basis": accounting["basis"], "provisional": accounting["provisional"],
                        "received_quantity": as_number(accounting["received_quantity"]),
                        "accepted_quantity": as_number(accounting["accepted_quantity"]),
                        "damaged_quantity": as_number(accounting["damaged_quantity"]),
                        "rejected_quantity": as_number(accounting["rejected_quantity"]),
                        "pending_quantity": as_number(accounting["pending_quantity"]),
                        "settled_quantity": as_number(accounting["settled_quantity"]),
                        "shortfall_quantity": as_number(accounting["shortfall_quantity"]),
                        "open_dispute_ids": [row["dispute_id"]
                                             for row in accounting["open_disputes"]],
                        "late_check_ids": accounting["late_check_ids"],
                        "out_of_order_check_ids": accounting["out_of_order_check_ids"],
                        "review_required": bool(accounting["late_check_ids"]
                                                or accounting["out_of_order_check_ids"]),
                    },
                    "source_revision": Store.revision(db)}

    def order(self, order_id: str) -> dict:
        order_id = identifier(order_id, "order_id")
        with self.store.read() as db:
            order = Store.one(db, "SELECT * FROM orders WHERE order_id=?", (order_id,), "order")
            commitment_rows = db.execute("SELECT * FROM commitments WHERE order_id=? "
                                         "ORDER BY accepted_revision", (order_id,)).fetchall()
            basis_rows, risks = [], []
            totals = {key: ZERO for key in ("committed", "shipped", "received", "accepted", "damaged",
                                            "rejected", "pending", "settled", "shortfall",
                                            "unsettled", "provisional")}
            unresolved = False
            for commitment in commitment_rows:
                shipment = Store.one(db, "SELECT * FROM shipments WHERE shipment_id=?",
                                     (commitment["shipment_id"],), "shipment")
                accounting = self._accounting(db, shipment)
                for total_key, accounting_key in (("shipped", "shipped_quantity"),
                                                  ("received", "received_quantity"),
                                                  ("accepted", "accepted_quantity"),
                                                  ("damaged", "damaged_quantity"),
                                                  ("rejected", "rejected_quantity"),
                                                  ("pending", "pending_quantity"),
                                                  ("settled", "settled_quantity"),
                                                  ("shortfall", "shortfall_quantity"),
                                                  ("unsettled", "unsettled_quantity")):
                    totals[total_key] += accounting[accounting_key]
                totals["committed"] += decimal(commitment["quantity"], "committed quantity")
                if accounting["basis"] != "settlement":
                    totals["provisional"] += accounting["received_quantity"]
                unresolved = unresolved or accounting["provisional"]
                basis_rows.append({
                    "shipment_id": shipment["shipment_id"],
                    "commitment_id": commitment["commitment_id"],
                    "committed_quantity": commitment["quantity"],
                    "shipped_quantity": as_number(accounting["shipped_quantity"]),
                    "basis": accounting["basis"],
                    "provisional": accounting["provisional"],
                    "settlement_id": accounting["settlement_id"],
                    "settlement_revision": accounting["settlement_revision"],
                    "effective_check_id": accounting["effective_check_id"],
                    "received_quantity": as_number(accounting["received_quantity"]),
                    "accepted_quantity": as_number(accounting["accepted_quantity"]),
                    "damaged_quantity": as_number(accounting["damaged_quantity"]),
                    "rejected_quantity": as_number(accounting["rejected_quantity"]),
                    "pending_quantity": as_number(accounting["pending_quantity"]),
                    "settled_quantity": as_number(accounting["settled_quantity"]),
                    "shortfall_quantity": as_number(accounting["shortfall_quantity"]),
                    "unsettled_quantity": as_number(accounting["unsettled_quantity"]),
                    "open_dispute_ids": [row["dispute_id"] for row in accounting["open_disputes"]],
                    "late_check_ids": accounting["late_check_ids"],
                    "out_of_order_check_ids": accounting["out_of_order_check_ids"],
                })
                self._collect_risks(accounting, order, risks)
            if order["status"] == "cancelled" and commitment_rows:
                risks.append({"code": "commitment_on_cancelled_order", "severity": "major",
                              "shipment_id": None,
                              "detail": "order was cancelled after delivery commitments were recorded"})
            unsettled = [row for row in basis_rows if row["basis"] != "settlement"]
            if not commitment_rows:
                settlement_status = "no_commitments"
            elif any(row["open_dispute_ids"] for row in basis_rows):
                settlement_status = "disputed"
            elif unsettled:
                settlement_status = "pending"
            elif unresolved:
                # A settlement exists and no dispute is open, but it was recorded
                # while a dispute was still unresolved: its finality is qualified.
                settlement_status = "provisional"
            else:
                settlement_status = "settled"
            current_basis = {
                "status": settlement_status,
                "settlement_ids": [row["settlement_id"] for row in basis_rows if row["settlement_id"]],
                "unsettled_quantity": as_number(totals["unsettled"]),
                "provisional_quantity": as_number(totals["provisional"]),
                "open_dispute_ids": sorted({dispute_id for row in basis_rows
                                            for dispute_id in row["open_dispute_ids"]}),
                "pending_review_shipment_ids": [row["shipment_id"] for row in basis_rows
                                                if row["late_check_ids"]],
            }
            committed = totals["committed"]
            accepted = totals["accepted"]
            ordered = decimal(order["quantity"], "order quantity")
            return {"order_id": order_id, "order_status": order["status"],
                    "ordered_tons": order["quantity"],
                    "committed_tons": as_number(committed),
                    "shipped_tons": as_number(totals["shipped"]),
                    "received_tons": as_number(totals["received"]),
                    "accepted_received_tons": as_number(accepted),
                    "damaged_tons": as_number(totals["damaged"]),
                    "rejected_tons": as_number(totals["rejected"]),
                    "pending_receipt_tons": as_number(totals["pending"]),
                    "settled_tons": as_number(totals["settled"]),
                    "unsettled_tons": as_number(totals["unsettled"]),
                    "shortfall_tons": as_number(totals["shortfall"]),
                    "provisional_tons": as_number(totals["provisional"]),
                    "unreceived_tons": as_number(ordered - accepted),
                    "settlement_status": settlement_status,
                    "current_basis": current_basis,
                    "settlement_basis": basis_rows,
                    "unresolved_risks": risks,
                    "commitments": [dict(row) for row in commitment_rows],
                    "source_revision": Store.revision(db)}

    @staticmethod
    def _shipment_risks(accounting: dict) -> list[dict]:
        shipment_id = accounting["shipment_id"]
        risks: list[dict] = []
        if accounting["basis"] == "none":
            risks.append({"code": "missing_receipt", "severity": "major",
                          "shipment_id": shipment_id,
                          "detail": "committed shipment has no delivery evidence"})
        if accounting["basis"] == "provisional_check":
            risks.append({"code": "unsettled_receipt", "severity": "major",
                          "shipment_id": shipment_id,
                          "detail": "receipt is supported by a check but has no final settlement"})
        if accounting["pending_quantity"] > 0:
            risks.append({"code": "pending_quantity", "severity": "major",
                          "shipment_id": shipment_id,
                          "detail": f"{as_number(accounting['pending_quantity'])} tons await confirmation"})
        if accounting["shortfall_quantity"] > 0:
            risks.append({"code": "quantity_shortfall", "severity": "critical",
                          "shipment_id": shipment_id,
                          "detail": f"{as_number(accounting['shortfall_quantity'])} tons shipped but not evidenced as received"})
        if accounting["damaged_quantity"] > 0:
            risks.append({"code": "damaged_quantity", "severity": "major",
                          "shipment_id": shipment_id,
                          "detail": f"{as_number(accounting['damaged_quantity'])} tons were settled as damaged"})
        if accounting["rejected_quantity"] > 0:
            risks.append({"code": "rejected_quantity", "severity": "critical",
                          "shipment_id": shipment_id,
                          "detail": f"{as_number(accounting['rejected_quantity'])} tons were rejected"})
        for dispute in accounting["open_disputes"]:
            risks.append({"code": "open_dispute", "severity": "critical",
                          "shipment_id": shipment_id,
                          "detail": f"dispute {dispute['dispute_id']} ({dispute['kind']}) is unresolved",
                          "dispute_id": dispute["dispute_id"],
                          "claimed_quantity": dispute["claimed_quantity"]})
        if accounting["late_check_ids"]:
            risks.append({"code": "check_after_settlement", "severity": "major",
                          "shipment_id": shipment_id,
                          "detail": "delivery evidence arrived after the active settlement and is not reflected",
                          "check_ids": accounting["late_check_ids"]})
        if accounting["out_of_order_check_ids"]:
            risks.append({"code": "out_of_order_check", "severity": "minor",
                          "shipment_id": shipment_id,
                          "detail": "a recheck arrived with an earlier weighing time than one already recorded",
                          "check_ids": accounting["out_of_order_check_ids"]})
        return risks

    @classmethod
    def _collect_risks(cls, accounting: dict, order, risks: list[dict]) -> None:
        risks.extend(cls._shipment_risks(accounting))
        if order["status"] != "open":
            risks.append({"code": "order_not_open", "severity": "major",
                          "shipment_id": accounting["shipment_id"],
                          "detail": f"order status is {order['status']}"})
