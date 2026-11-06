"""SQLite store: the AP invoice register (for duplicates and PO consumption) and the run history."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT, invoice_number TEXT, number_core TEXT, invoice_date TEXT,
    po_number TEXT, subtotal REAL, total REAL, status TEXT, source TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, decision TEXT, headline TEXT,
    vendor TEXT, invoice_number TEXT, po_number TEXT, total REAL, extractor TEXT,
    duration_ms INTEGER, record_json TEXT);
"""

# Invoice statuses that use up a PO's budget. Held and rejected invoices don't.
CONSUMING = ("paid", "approved")


def number_core(invoice_number):
    """'INV-00417' and '417' share the core '417': how resubmitted duplicates get caught."""
    digits = re.sub(r"\D", "", invoice_number or "")
    return digits.lstrip("0") or digits


def connect(path=DB, ref=None):
    path = Path(path)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if fresh and ref is not None:
        reset(conn, ref)
    return conn


def reset(conn, ref):
    """Restore demo state: empty run history, invoice register = data/invoice_history.csv."""
    conn.execute("DELETE FROM invoices")
    conn.execute("DELETE FROM runs")
    for h in ref.history:
        conn.execute(
            "INSERT INTO invoices (vendor_id, invoice_number, number_core, invoice_date, po_number, subtotal,"
            " total, status, source) VALUES (?,?,?,?,?,?,?,?, 'history')",
            (h["vendor_id"], h["invoice_number"], number_core(h["invoice_number"]), h["invoice_date"],
             h["po_number"], float(h["subtotal"]), float(h["total"]), h["status"]))
    conn.commit()


def vendor_invoices(conn, vendor_id):
    return [dict(r) for r in conn.execute("SELECT * FROM invoices WHERE vendor_id=?", (vendor_id,))]


def po_consumed(conn, po_number):
    rows = conn.execute(f"SELECT invoice_number, subtotal FROM invoices WHERE po_number=? AND status IN "
                        f"({','.join('?' * len(CONSUMING))})", (po_number, *CONSUMING)).fetchall()
    return round(sum(r["subtotal"] for r in rows), 2), [r["invoice_number"] for r in rows]


def register_invoice(conn, run_id, vendor_id, f, po_number, status):
    conn.execute(
        "INSERT INTO invoices (vendor_id, invoice_number, number_core, invoice_date, po_number, subtotal, total,"
        " status, source, run_id) VALUES (?,?,?,?,?,?,?,?, 'run', ?)",
        (vendor_id, f.get("invoice_number"), number_core(f.get("invoice_number")), f.get("invoice_date"),
         po_number, f.get("subtotal"), f.get("total"), status, run_id))


def save_run(conn, record):
    f = record["fields"]
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (record["run_id"], record["created_at"], record["filename"], record["decision"], record["headline"],
         record.get("vendor_name") or f.get("vendor_name"), f.get("invoice_number"), record.get("po_number"),
         f.get("total"), record["extractor"], record["duration_ms"], json.dumps(record, default=str)))


def runs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at DESC")]


def run_record(conn, run_id):
    row = conn.execute("SELECT record_json FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["record_json"]) if row else None


def register(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id")]


def new_run_id():
    return datetime.now().strftime("RUN-%Y%m%d-%H%M%S-%f")[:-3]
