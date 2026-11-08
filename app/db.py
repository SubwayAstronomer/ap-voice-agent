"""SQLite storage: ERP stand-in tables (vendors, POs, GRNs) plus the
process tables (invoices, runs, run_events, line matches)."""
import json
import os
import sqlite3
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("AP_DB_PATH", os.path.join(DATA, "ap.db"))

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  vendor_id TEXT PRIMARY KEY, legal_name TEXT, aliases TEXT, gstin TEXT, state_code TEXT,
  bank_account TEXT, bank_ifsc TEXT, bank_name TEXT, address TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor_id TEXT, po_date TEXT, status TEXT, currency TEXT);
CREATE TABLE IF NOT EXISTS po_lines (
  po_number TEXT, line_no INTEGER, item_code TEXT, description TEXT, hsn TEXT, qty REAL,
  uom TEXT, unit_price REAL, gst_rate REAL, PRIMARY KEY (po_number, line_no));
CREATE TABLE IF NOT EXISTS grns (
  grn_number TEXT, po_number TEXT, line_no INTEGER, qty_received REAL, received_date TEXT);
CREATE TABLE IF NOT EXISTS invoices (
  invoice_id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT, file_path TEXT, file_hash TEXT,
  uploaded_at TEXT, vendor_id TEXT, invoice_no TEXT, invoice_no_norm TEXT, invoice_date TEXT,
  total REAL, extracted TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, started_at TEXT, ended_at TEXT,
  status TEXT, mode TEXT, decision TEXT, final_decision TEXT, reason_codes TEXT, flags TEXT,
  explanation TEXT, owner TEXT, next_action TEXT, vendor_id TEXT, vendor_name TEXT,
  po_number TEXT, invoice_no TEXT, total REAL, duration_ms INTEGER, override TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, stage_no INTEGER, stage TEXT,
  status TEXT, started_at TEXT, ended_at TEXT, duration_ms INTEGER, summary TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS line_matches (
  run_id INTEGER, po_number TEXT, po_line_no INTEGER, qty REAL, amount REAL);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def q(sql, args=(), one=False):
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with _lock:
        with connect() as conn:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid


def _load(name):
    with open(os.path.join(DATA, "seed", name)) as f:
        return json.load(f)


def init(reset_reference=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock:
        with connect() as conn:
            conn.executescript(SCHEMA)
            have = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
            if have and not reset_reference:
                return
            conn.executescript("DELETE FROM vendors; DELETE FROM purchase_orders; DELETE FROM po_lines; DELETE FROM grns;")
            for v in _load("vendors.json"):
                conn.execute("INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (v["vendor_id"], v["legal_name"], json.dumps(v["aliases"]), v["gstin"], v["state_code"],
                              v["bank_account"], v["bank_ifsc"], v["bank_name"], v["address"], v["status"]))
            for p in _load("purchase_orders.json"):
                conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?)",
                             (p["po_number"], p["vendor_id"], p["po_date"], p["status"], p["currency"]))
                for l in p["lines"]:
                    conn.execute("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?,?)",
                                 (p["po_number"], l["line_no"], l["item_code"], l["description"], l["hsn"], l["qty"],
                                  l["uom"], l["unit_price"], l["gst_rate"]))
            for g in _load("grns.json"):
                conn.execute("INSERT INTO grns VALUES (?,?,?,?,?)",
                             (g["grn_number"], g["po_number"], g["line_no"], g["qty_received"], g["received_date"]))
            conn.commit()


def reset_runs():
    with _lock:
        with connect() as conn:
            conn.executescript("DELETE FROM invoices; DELETE FROM runs; DELETE FROM run_events; DELETE FROM line_matches;"
                               "DELETE FROM sqlite_sequence WHERE name IN ('invoices','runs','run_events');")
            conn.commit()


def buyer():
    return _load("buyer.json")


# ------------------------------------------------------------ reference reads
def vendors():
    rows = q("SELECT * FROM vendors ORDER BY vendor_id")
    for r in rows:
        r["aliases"] = json.loads(r["aliases"] or "[]")
    return rows


def po(po_number):
    head = q("SELECT * FROM purchase_orders WHERE po_number=?", (po_number,), one=True)
    if not head:
        return None
    head["lines"] = q("SELECT * FROM po_lines WHERE po_number=? ORDER BY line_no", (po_number,))
    return head


def pos_for_vendor(vendor_id):
    return [po(r["po_number"]) for r in
            q("SELECT po_number FROM purchase_orders WHERE vendor_id=? AND status='open' ORDER BY po_number", (vendor_id,))]


def grn_qty(po_number, line_no):
    r = q("SELECT COALESCE(SUM(qty_received),0) AS s FROM grns WHERE po_number=? AND line_no=?",
          (po_number, line_no), one=True)
    return r["s"]


def billed_qty(po_number, line_no, exclude_invoice=None):
    """Quantity on invoices whose current final decision is APPROVE (overrides included).
    Superseded runs and other runs of the same invoice (re-runs) do not count."""
    r = q("""SELECT COALESCE(SUM(m.qty),0) AS s FROM line_matches m JOIN runs r ON r.run_id=m.run_id
             WHERE m.po_number=? AND m.po_line_no=? AND r.final_decision='APPROVE' AND r.status='done'
             AND r.invoice_id<>?""",
          (po_number, line_no, exclude_invoice or -1), one=True)
    return r["s"]


def billed_value(po_number, exclude_invoice=None):
    r = q("""SELECT COALESCE(SUM(m.amount),0) AS s FROM line_matches m JOIN runs r ON r.run_id=m.run_id
             WHERE m.po_number=? AND r.final_decision='APPROVE' AND r.status='done' AND r.invoice_id<>?""",
          (po_number, exclude_invoice or -1), one=True)
    return r["s"]
