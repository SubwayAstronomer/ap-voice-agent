"""Runs the seven stages in order, writing one run_events row per stage
(the live view and the audit log read these rows)."""
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime

from .. import db
from .common import CFG, STAGES
from .extract import _call_llm, llm_config
from .stages import STAGE_FUNCS

_queue_lock = threading.Lock()


def now():
    return datetime.now().isoformat(timespec="milliseconds")


def _delay():
    return float(CFG["demo"]["stage_delay_seconds"])   # editable on the Rules page; env sets the start value


def create_run(src_path, file_name, copy=True):
    """Register the invoice and an empty run; return run_id."""
    path = src_path
    if copy:
        os.makedirs(os.path.join(db.DATA, "uploads"), exist_ok=True)
        path = os.path.join(db.DATA, "uploads", f"{int(time.time() * 1000)}_{file_name}")
        shutil.copyfile(src_path, path)
    inv_id = db.ex("INSERT INTO invoices (file_name, file_path, uploaded_at) VALUES (?,?,?)", (file_name, path, now()))
    return _new_run(inv_id)


def create_rerun(run_id):
    """A new run of the same invoice with the rules now in force. When it finishes, the
    earlier run is marked superseded, so it no longer counts for billing or duplicates."""
    old = db.q("SELECT invoice_id FROM runs WHERE run_id=?", (run_id,), one=True)
    return _new_run(old["invoice_id"]) if old else None


def _new_run(inv_id):
    run_id = db.ex("INSERT INTO runs (invoice_id, started_at, status) VALUES (?,?,?)", (inv_id, now(), "queued"))
    for no, key, name in STAGES:
        db.ex("INSERT INTO run_events (run_id, stage_no, stage, status, summary) VALUES (?,?,?,?,?)",
              (run_id, no, key, "waiting", name))
    return run_id


def _event(run_id, stage, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE run_events SET {sets} WHERE run_id=? AND stage=?", (*kw.values(), run_id, stage))


def _llm_summary(ctx):
    cfg = llm_config()
    default = "0" if cfg and cfg["model"].endswith(":free") else "1"   # free tier: save the daily quota
    if not cfg or os.environ.get("EXPLAIN_WITH_LLM", default) == "0":
        return None
    facts = {"decision": ctx["decision"], "flags": [{k: f[k] for k in ("code", "severity", "owner", "message")}
                                                     for f in ctx["flags"]], "facts_text": ctx["explanation"]}
    prompt = ("Write 2-3 plain sentences for an accounts-payable clerk explaining this invoice decision. Use only the "
              "facts given; do not add numbers or reasons that are not in them. Start with the decision.\n" + json.dumps(facts))
    try:
        return _call_llm(cfg, prompt, []).strip()
    except Exception:
        return None


def execute(run_id):
    run = db.q("SELECT * FROM runs WHERE run_id=?", (run_id,), one=True)
    inv = db.q("SELECT * FROM invoices WHERE invoice_id=?", (run["invoice_id"],), one=True)
    ctx = {"run_id": run_id, "invoice_id": inv["invoice_id"], "file_path": inv["file_path"], "file_name": inv["file_name"], "flags": [], "fields": {},
           "lines": [], "mode": "", "stop": False}
    t0 = time.time()
    db.ex("UPDATE runs SET status='running', started_at=? WHERE run_id=?", (now(), run_id))
    try:
        for no, key, name in STAGES:
            if ctx["stop"] and key != "decide":
                _event(run_id, key, status="skipped", summary="Skipped: an earlier stage stopped the run")
                continue
            start = time.time()
            _event(run_id, key, status="running", started_at=now())
            time.sleep(_delay())
            before = len(ctx["flags"])
            summary, detail = STAGE_FUNCS[key](ctx)
            new = ctx["flags"][before:]
            blocking = [f for f in new if f["severity"] != "INFO"]
            status = "flag" if blocking else "pass"
            if key == "decide":
                status = ctx["decision"].lower()
            _event(run_id, key, status=status, ended_at=now(), duration_ms=int((time.time() - start) * 1000),
                   summary=summary, detail=json.dumps({"stage_flags": new, **detail}, default=str))
        summary = _llm_summary(ctx)
        f = ctx["fields"]
        val = lambda k: (f.get(k) or {}).get("value")
        db.ex("""UPDATE invoices SET file_hash=?, vendor_id=?, invoice_no=?, invoice_no_norm=?, invoice_date=?, total=?,
                 extracted=? WHERE invoice_id=?""",
              (ctx.get("file_hash"), (ctx.get("vendor") or {}).get("vendor_id"), val("invoice_no"), ctx.get("invoice_no_norm"),
               val("invoice_date"), val("total"), json.dumps({"fields": f, "lines": ctx["lines"]}, default=str), inv["invoice_id"]))
        for m in ctx.get("line_matches", []):
            db.ex("INSERT INTO line_matches VALUES (?,?,?,?,?)", (run_id, m["po_number"], m["po_line_no"], m["qty"], m["amount"]))
        codes = list(dict.fromkeys(x["code"] for x in ctx["flags"]))
        db.ex("""UPDATE runs SET status='done', ended_at=?, mode=?, decision=?, final_decision=?, reason_codes=?, flags=?,
                 explanation=?, owner=?, next_action=?, vendor_id=?, vendor_name=?, po_number=?, invoice_no=?, total=?,
                 duration_ms=? WHERE run_id=?""",
              (now(), ctx["mode"], ctx["decision"], ctx["decision"], json.dumps(codes), json.dumps(ctx["flags"]),
               json.dumps({"facts": ctx["explanation"], "summary": summary}), ctx["owner"],
               json.dumps(ctx["next_action"]), (ctx.get("vendor") or {}).get("vendor_id"),
               (ctx.get("vendor") or {}).get("legal_name") or val("vendor_name"), (ctx.get("po") or {}).get("po_number"),
               val("invoice_no"), val("total"), int((time.time() - t0) * 1000), run_id))
        db.ex("UPDATE runs SET status='superseded' WHERE invoice_id=? AND run_id<>? AND status='done'",
              (inv["invoice_id"], run_id))
    except Exception as e:
        tb = traceback.format_exc()
        db.ex("UPDATE run_events SET status='error', summary=? WHERE run_id=? AND status='running'", (f"Error: {e}", run_id))
        db.ex("UPDATE runs SET status='error', ended_at=?, error=?, duration_ms=? WHERE run_id=?",
              (now(), tb[-2000:], int((time.time() - t0) * 1000), run_id))
    return run_id


def execute_serial(run_ids):
    """Runs one at a time so that billed-to-date and duplicate checks see earlier results."""
    with _queue_lock:
        for rid in run_ids:
            execute(rid)


def start(run_ids):
    threading.Thread(target=execute_serial, args=(list(run_ids),), daemon=True).start()
