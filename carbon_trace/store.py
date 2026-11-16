import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import Missing


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    head INTEGER NOT NULL
);
INSERT OR IGNORE INTO state VALUES (1,0,0);
CREATE TABLE IF NOT EXISTS plants (
    plant_id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL,
    active_from TEXT NOT NULL, active_to TEXT, created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
    line_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    daily_capacity TEXT NOT NULL, active_from TEXT NOT NULL, active_to TEXT,
    created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS specifications (
    spec_id TEXT PRIMARY KEY, grade TEXT NOT NULL, valid_from TEXT NOT NULL,
    valid_to TEXT, sulfur_min TEXT NOT NULL, sulfur_max TEXT NOT NULL,
    ash_max TEXT NOT NULL, created_revision INTEGER NOT NULL,
    UNIQUE(grade,valid_from)
);
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY, line_id TEXT NOT NULL REFERENCES lines(line_id),
    grade TEXT NOT NULL, produced_at TEXT NOT NULL, local_day TEXT NOT NULL,
    quantity TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assays (
    assay_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    sampled_at TEXT NOT NULL, sulfur TEXT NOT NULL, ash TEXT NOT NULL,
    source TEXT NOT NULL, external_id TEXT NOT NULL, accepted_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS assays_order ON assays(lot_id,sampled_at,accepted_revision);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    shipped_at TEXT NOT NULL, local_day TEXT NOT NULL, quantity TEXT NOT NULL,
    destination TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_lot ON shipments(lot_id);
CREATE TABLE IF NOT EXISTS receipts (
    source TEXT NOT NULL, external_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL, event_kind TEXT NOT NULL,
    PRIMARY KEY(source, external_id)
);
CREATE TABLE IF NOT EXISTS rejections (
    batch_id TEXT NOT NULL, row_index INTEGER NOT NULL, source TEXT NOT NULL,
    external_id TEXT, error_code TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(batch_id,row_index)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, source TEXT NOT NULL, request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL, completed_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    revision INTEGER PRIMARY KEY, cause TEXT NOT NULL, ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    from_day TEXT NOT NULL, to_day TEXT NOT NULL, base_revision INTEGER NOT NULL,
    expected_head INTEGER NOT NULL, rows_json TEXT NOT NULL, rows_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','published'))
);
CREATE TABLE IF NOT EXISTS publications (
    version INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
    plant_id TEXT NOT NULL, from_day TEXT NOT NULL, to_day TEXT NOT NULL,
    source_revision INTEGER NOT NULL, rows_digest TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_rows (
    version INTEGER NOT NULL REFERENCES publications(version), day TEXT NOT NULL,
    data_json TEXT NOT NULL, PRIMARY KEY(version,day)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    grade TEXT NOT NULL, due_day TEXT NOT NULL, quantity TEXT NOT NULL,
    destination TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','cancelled')),
    created_revision INTEGER NOT NULL, cancelled_revision INTEGER
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id), quantity TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_revision INTEGER NOT NULL, released_revision INTEGER,
    UNIQUE(order_id,lot_id,status)
);
CREATE INDEX IF NOT EXISTS reservations_lot ON reservations(lot_id,status);
CREATE TABLE IF NOT EXISTS source_policies (
    source TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    allowed_kinds TEXT NOT NULL, max_batch_size INTEGER NOT NULL,
    updated_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    line_id TEXT NOT NULL REFERENCES lines(line_id),
    started_at TEXT NOT NULL, ended_at TEXT,
    start_day TEXT NOT NULL, end_day TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
    opened_revision INTEGER NOT NULL, closed_revision INTEGER
);
CREATE INDEX IF NOT EXISTS incidents_scope ON incidents(plant_id,start_day,end_day);
CREATE TABLE IF NOT EXISTS delivery_checks (
    check_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    checked_at TEXT NOT NULL, received_quantity TEXT NOT NULL,
    condition TEXT NOT NULL CHECK(condition IN ('pending','accepted','damaged','rejected')),
    note TEXT NOT NULL, accepted_revision INTEGER NOT NULL,
    party TEXT NOT NULL DEFAULT 'logistics', evidence_ref TEXT,
    sequence INTEGER NOT NULL DEFAULT 0,
    accepted_quantity TEXT NOT NULL DEFAULT '0',
    damaged_quantity TEXT NOT NULL DEFAULT '0',
    rejected_quantity TEXT NOT NULL DEFAULT '0',
    pending_quantity TEXT NOT NULL DEFAULT '0',
    dispute_id TEXT, payload_digest TEXT,
    late INTEGER NOT NULL DEFAULT 0, out_of_order INTEGER NOT NULL DEFAULT 0,
    supersedes_check_id TEXT, recorded_at TEXT
);
CREATE INDEX IF NOT EXISTS delivery_checks_shipment ON delivery_checks(shipment_id,checked_at);
CREATE INDEX IF NOT EXISTS delivery_checks_sequence ON delivery_checks(shipment_id,sequence);
CREATE TABLE IF NOT EXISTS delivery_disputes (
    dispute_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    opened_by TEXT NOT NULL, opened_at TEXT NOT NULL, claimed_quantity TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('short_receipt','damaged','rejected','quality','quantity','other')),
    reason TEXT NOT NULL, evidence_ref TEXT NOT NULL, note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','withdrawn')),
    opened_revision INTEGER NOT NULL, resolved_revision INTEGER, resolution TEXT,
    resolved_at TEXT, resolved_by TEXT, resolution_evidence_ref TEXT,
    resolution_payload_digest TEXT, payload_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_disputes_shipment ON delivery_disputes(shipment_id,status);
CREATE TABLE IF NOT EXISTS delivery_settlements (
    settlement_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    version INTEGER NOT NULL,
    accepted_quantity TEXT NOT NULL, damaged_quantity TEXT NOT NULL,
    rejected_quantity TEXT NOT NULL, pending_quantity TEXT NOT NULL,
    settled_quantity TEXT NOT NULL,
    basis_json TEXT NOT NULL, evidence_ref TEXT,
    decided_by TEXT NOT NULL, decided_at TEXT NOT NULL, note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    accepted_revision INTEGER NOT NULL,
    supersedes_settlement_id TEXT, superseded_revision INTEGER,
    payload_digest TEXT NOT NULL,
    UNIQUE(shipment_id,version)
);
CREATE INDEX IF NOT EXISTS delivery_settlements_shipment ON delivery_settlements(shipment_id,status);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
    shipment_id TEXT NOT NULL UNIQUE REFERENCES shipments(shipment_id),
    quantity TEXT NOT NULL, accepted_revision INTEGER NOT NULL
);
"""


# Columns added after the first delivery ledger shipped.  Databases created by an
# older binary are upgraded in place; no event, physical shipment or published
# revision is rewritten by this migration.
DELIVERY_CHECK_COLUMNS = {
    "party": "TEXT NOT NULL DEFAULT 'logistics'",
    "evidence_ref": "TEXT",
    "sequence": "INTEGER NOT NULL DEFAULT 0",
    "accepted_quantity": "TEXT NOT NULL DEFAULT '0'",
    "damaged_quantity": "TEXT NOT NULL DEFAULT '0'",
    "rejected_quantity": "TEXT NOT NULL DEFAULT '0'",
    "dispute_id": "TEXT",
    "payload_digest": "TEXT",
    "late": "INTEGER NOT NULL DEFAULT 0",
    "out_of_order": "INTEGER NOT NULL DEFAULT 0",
    "supersedes_check_id": "TEXT",
    "recorded_at": "TEXT",
}

DELIVERY_CHECK_DEFINITION = """
    check_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    checked_at TEXT NOT NULL, received_quantity TEXT NOT NULL,
    condition TEXT NOT NULL CHECK(condition IN ('pending','accepted','damaged','rejected')),
    note TEXT NOT NULL, accepted_revision INTEGER NOT NULL,
    party TEXT NOT NULL DEFAULT 'logistics', evidence_ref TEXT,
    sequence INTEGER NOT NULL DEFAULT 0,
    accepted_quantity TEXT NOT NULL DEFAULT '0',
    damaged_quantity TEXT NOT NULL DEFAULT '0',
    rejected_quantity TEXT NOT NULL DEFAULT '0',
    pending_quantity TEXT NOT NULL DEFAULT '0',
    dispute_id TEXT, payload_digest TEXT,
    late INTEGER NOT NULL DEFAULT 0, out_of_order INTEGER NOT NULL DEFAULT 0,
    supersedes_check_id TEXT, recorded_at TEXT
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        return db

    def initialize(self):
        db = self.connect()
        try:
            self._migrate_delivery_checks(db)
            db.executescript(SCHEMA)
            if self.path != ":memory:":
                db.execute("PRAGMA journal_mode=WAL")
        finally:
            db.close()

    @staticmethod
    def _migrate_delivery_checks(db):
        """Upgrade a pre-dispute delivery ledger without touching its evidence."""
        existing = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_checks'"
                              ).fetchone()
        if existing is None:
            return
        definition = existing[0] or ""
        if "'pending'" in definition and "evidence_ref" in definition:
            return
        columns = {row["name"] for row in db.execute("PRAGMA table_info(delivery_checks)")}
        kept = [name for name in ("check_id", "shipment_id", "checked_at", "received_quantity",
                                  "condition", "note", "accepted_revision") if name in columns]
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP INDEX IF EXISTS delivery_checks_shipment")
        db.execute("DROP INDEX IF EXISTS delivery_checks_sequence")
        db.execute("ALTER TABLE delivery_checks RENAME TO delivery_checks_legacy")
        db.execute("CREATE TABLE delivery_checks (" + DELIVERY_CHECK_DEFINITION.strip() + ")")
        target = [name for name in kept]
        for name in DELIVERY_CHECK_COLUMNS:
            if name not in columns:
                target.append(name)
        defaults = {"party": "'logistics'", "evidence_ref": "NULL", "sequence": "0",
                    "accepted_quantity": "'0'", "damaged_quantity": "'0'",
                    "rejected_quantity": "'0'", "dispute_id": "NULL", "payload_digest": "NULL",
                    "late": "0", "out_of_order": "0", "supersedes_check_id": "NULL",
                    "recorded_at": "NULL"}
        db.execute("INSERT INTO delivery_checks (" + ",".join(target) + ") SELECT " +
                   ",".join(name if name in columns else defaults[name] for name in target) +
                   " FROM delivery_checks_legacy")
        if columns and "accepted_quantity" not in columns:
            db.execute("UPDATE delivery_checks SET accepted_quantity=received_quantity "
                       "WHERE condition='accepted'")
        db.execute("DROP TABLE delivery_checks_legacy")
        db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        db = self.connect()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def revision(db) -> int:
        return db.execute("SELECT revision FROM state WHERE singleton=1").fetchone()[0]

    @staticmethod
    def head(db) -> int:
        return db.execute("SELECT head FROM state WHERE singleton=1").fetchone()[0]

    @staticmethod
    def advance(db, cause: str, ref: str) -> int:
        db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
        revision = Store.revision(db)
        db.execute("INSERT INTO audit VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                   (revision, cause, ref))
        return revision

    @staticmethod
    def one(db, sql: str, params: tuple, entity: str):
        row = db.execute(sql, params).fetchone()
        if row is None:
            raise Missing(f"{entity} not found")
        return row

    def status(self) -> dict:
        with self.read() as db:
            return {"revision": self.revision(db), "head": self.head(db),
                    "plants": db.execute("SELECT count(*) FROM plants").fetchone()[0],
                    "lots": db.execute("SELECT count(*) FROM lots").fetchone()[0]}
