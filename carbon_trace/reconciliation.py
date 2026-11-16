from collections import Counter
from decimal import Decimal

from .catalog import Catalog
from .errors import require
from .quality import Quality
from .store import Store
from .values import as_number, day, decimal, identifier


class Reconciliation:
    def __init__(self, store: Store):
        self.store = store
        self.quality = Quality()

    def plant(self, plant_id: str, from_day: str, to_day: str) -> dict:
        plant_id = identifier(plant_id, "plant_id")
        from_day = day(from_day, "from_day")
        to_day = day(to_day, "to_day")
        require(from_day <= to_day, "date interval is inverted")
        with self.store.read() as db:
            Catalog.plant(db, plant_id)
            lots = db.execute("SELECT lots.* FROM lots JOIN lines ON lines.line_id=lots.line_id "
                              "WHERE lines.plant_id=? AND lots.local_day BETWEEN ? AND ? "
                              "ORDER BY lots.local_day,lots.lot_id", (plant_id, from_day, to_day)).fetchall()
            result = [self._lot(db, lot) for lot in lots]
            exceptions = [item for item in result if item["issues"]]
            produced = sum((decimal(item["produced_tons"], "produced") for item in result), Decimal(0))
            shipped = sum((decimal(item["shipped_tons"], "shipped") for item in result), Decimal(0))
            reserved = sum((decimal(item["reserved_tons"], "reserved") for item in result), Decimal(0))
            issues = Counter(code for item in exceptions for code in item["issues"])
            delivery = self._delivery(db, plant_id, from_day, to_day)
            # Physical shipment and delivery evidence are separate ledgers: the
            # tons reported here stay on the shipment's own local day, so a later
            # receipt, dispute or reweigh never moves historical shipped tonnage.
            return {"plant_id": plant_id, "from_day": from_day, "to_day": to_day,
                    "source_revision": Store.revision(db), "lot_count": len(result),
                    "exception_count": len(exceptions), "issue_counts": dict(sorted(issues.items())),
                    "produced_tons": as_number(produced), "shipped_tons": as_number(shipped),
                    "reserved_tons": as_number(reserved), "exceptions": exceptions,
                    "delivery": delivery}

    def _lot(self, db, lot) -> dict:
        quality = self.quality.classify(db, lot)
        shipments = db.execute("SELECT * FROM shipments WHERE lot_id=? ORDER BY shipped_at,shipment_id",
                               (lot["lot_id"],)).fetchall()
        reservations = db.execute("SELECT reservations.*,orders.status AS order_status FROM reservations "
                                  "JOIN orders ON orders.order_id=reservations.order_id "
                                  "WHERE reservations.lot_id=? ORDER BY reservations.created_revision",
                                  (lot["lot_id"],)).fetchall()
        produced = decimal(lot["quantity"], "quantity")
        shipped = sum((decimal(row["quantity"], "shipment quantity") for row in shipments), Decimal(0))
        reserved = sum((decimal(row["quantity"], "reservation quantity") for row in reservations
                        if row["status"] == "active"), Decimal(0))
        issues = []
        if quality["status"] == "unmeasured":
            issues.append("missing_assay")
        if quality["status"] == "failed":
            issues.append("quality_failed")
        if shipments and quality["status"] != "passed":
            issues.append("shipped_without_pass")
        if reserved and quality["status"] != "passed":
            issues.append("reserved_without_pass")
        if any(row["status"] == "active" and row["order_status"] != "open" for row in reservations):
            issues.append("reservation_on_cancelled_order")
        if shipped + reserved > produced:
            issues.append("overcommitted")
        if any(row["shipped_at"] < lot["produced_at"] for row in shipments):
            issues.append("shipment_before_production")
        if any(row["sampled_at"] < lot["produced_at"] for row in db.execute(
                "SELECT sampled_at FROM assays WHERE lot_id=?", (lot["lot_id"],))):
            issues.append("assay_before_production")
        return {"lot_id": lot["lot_id"], "local_day": lot["local_day"],
                "quality_status": quality["status"], "produced_tons": as_number(produced),
                "shipped_tons": as_number(shipped), "reserved_tons": as_number(reserved),
                "remaining_tons": as_number(produced - shipped - reserved), "issues": issues}

    def source(self, source: str, from_revision: int = 0) -> dict:
        source = identifier(source, "source")
        require(type(from_revision) is int and from_revision >= 0, "invalid from_revision")
        with self.store.read() as db:
            receipts = db.execute("SELECT event_kind,count(*) AS total,min(accepted_revision) AS first_revision,"
                                  "max(accepted_revision) AS last_revision FROM receipts WHERE source=? "
                                  "AND accepted_revision>=? GROUP BY event_kind ORDER BY event_kind",
                                  (source, from_revision)).fetchall()
            rejected = db.execute("SELECT error_code,count(*) AS total FROM rejections WHERE source=? "
                                  "GROUP BY error_code ORDER BY error_code", (source,)).fetchall()
            batches = db.execute("SELECT batch_id,completed_revision FROM batches WHERE source=? "
                                 "AND completed_revision>=? ORDER BY completed_revision,batch_id",
                                 (source, from_revision)).fetchall()
            return {"source": source, "from_revision": from_revision,
                    "current_revision": Store.revision(db), "accepted_by_kind": [dict(row) for row in receipts],
                    "rejected_by_code": [dict(row) for row in rejected],
                    "batches": [dict(row) for row in batches],
                    "delivery_evidence": self._source_delivery(db, source)}

    def _delivery(self, db, plant_id: str, from_day: str, to_day: str) -> dict:
        from .delivery import Delivery
        handler = Delivery(self.store)
        shipments = db.execute("SELECT shipments.* FROM shipments JOIN lots ON lots.lot_id=shipments.lot_id "
                               "JOIN lines ON lines.line_id=lots.line_id WHERE lines.plant_id=? "
                               "AND shipments.local_day BETWEEN ? AND ? ORDER BY shipments.local_day,shipments.shipment_id",
                               (plant_id, from_day, to_day)).fetchall()
        totals = {key: Decimal(0) for key in ("shipped", "received", "accepted", "damaged",
                                              "rejected", "pending", "settled", "shortfall",
                                              "unsettled")}
        rows, risks = [], []
        for shipment in shipments:
            accounting = handler._accounting(db, shipment)
            for key, column in (("shipped", "shipped_quantity"), ("received", "received_quantity"),
                                ("accepted", "accepted_quantity"), ("damaged", "damaged_quantity"),
                                ("rejected", "rejected_quantity"), ("pending", "pending_quantity"),
                                ("settled", "settled_quantity"), ("shortfall", "shortfall_quantity"),
                                ("unsettled", "unsettled_quantity")):
                totals[key] += accounting[column]
            rows.append({"shipment_id": shipment["shipment_id"], "local_day": shipment["local_day"],
                         "lot_id": shipment["lot_id"], "destination": shipment["destination"],
                         "shipped_tons": as_number(accounting["shipped_quantity"]),
                         "received_tons": as_number(accounting["received_quantity"]),
                         "accepted_tons": as_number(accounting["accepted_quantity"]),
                         "damaged_tons": as_number(accounting["damaged_quantity"]),
                         "rejected_tons": as_number(accounting["rejected_quantity"]),
                         "pending_tons": as_number(accounting["pending_quantity"]),
                         "settled_tons": as_number(accounting["settled_quantity"]),
                         "shortfall_tons": as_number(accounting["shortfall_quantity"])
                         if accounting["basis"] != "none" else None,
                         "basis": accounting["basis"], "provisional": accounting["provisional"],
                         "open_dispute_ids": [row["dispute_id"] for row in accounting["open_disputes"]],
                         "late_check_ids": accounting["late_check_ids"],
                         "out_of_order_check_ids": accounting["out_of_order_check_ids"]})
            for issue in handler._shipment_risks(accounting):
                risks.append({"shipment_id": shipment["shipment_id"], **issue})
        evidenced = any(row["basis"] != "none" for row in rows)
        return {"shipment_count": len(rows),
                **{f"{key}_tons": as_number(value) for key, value in totals.items()},
                "physical_shipment_total_tons": as_number(totals["shipped"]),
                "evidence_vs_physical_tons": as_number(totals["received"] - totals["shipped"])
                if evidenced else None,
                "unsettled_tons": as_number(totals["unsettled"]),
                "risk_count": len(risks), "risks": risks, "shipments": rows}

    def _source_delivery(self, db, source: str) -> dict:
        """Explain the gap between the source's shipment feed and delivery evidence."""
        from .delivery import Delivery
        handler = Delivery(self.store)
        shipments = db.execute("SELECT * FROM shipments WHERE source=? ORDER BY local_day,shipment_id",
                               (source,)).fetchall()
        shipped = received = settled = unsettled = Decimal(0)
        dispositions = {"accepted": Decimal(0), "damaged": Decimal(0),
                        "rejected": Decimal(0), "pending": Decimal(0)}
        open_disputes = 0
        rows = []
        for shipment in shipments:
            accounting = handler._accounting(db, shipment)
            shipped += accounting["shipped_quantity"]
            received += accounting["received_quantity"]
            settled += accounting["settled_quantity"]
            unsettled += accounting["unsettled_quantity"]
            for key in dispositions:
                dispositions[key] += accounting[f"{key}_quantity"]
            open_disputes += len(accounting["open_disputes"])
            rows.append({"shipment_id": shipment["shipment_id"],
                         "shipped_tons": as_number(accounting["shipped_quantity"]),
                         "received_tons": as_number(accounting["received_quantity"]),
                         "settled_tons": as_number(accounting["settled_quantity"]),
                         "shortfall_tons": as_number(accounting["shortfall_quantity"])
                         if accounting["basis"] != "none" else None,
                         "basis": accounting["basis"],
                         "open_dispute_count": len(accounting["open_disputes"]),
                         "late_check_count": len(accounting["late_check_ids"])})
        return {"shipment_count": len(rows),
                "shipped_tons": as_number(shipped), "received_tons": as_number(received),
                "settled_tons": as_number(settled), "unsettled_tons": as_number(unsettled),
                "accepted_tons": as_number(dispositions["accepted"]),
                "damaged_tons": as_number(dispositions["damaged"]),
                "rejected_tons": as_number(dispositions["rejected"]),
                "pending_tons": as_number(dispositions["pending"]),
                "evidence_vs_physical_tons": as_number(received - shipped) if rows else None,
                "open_dispute_count": open_disputes, "shipments": rows}
