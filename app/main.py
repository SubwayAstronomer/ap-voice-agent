"""FastAPI app: API for runs, live stream, dashboard, exception queue,
reference data, plus the single-page UI under /."""
import asyncio
import json
import os
import tempfile
from datetime import datetime

try:  # load .env (API keys, DEMO_STAGE_DELAY) if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, rules
from .pipeline import runner
from .pipeline.common import CFG, CODES, STAGES
from .pipeline.extract import llm_config

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(db.DATA, "invoices")

app = FastAPI(title="AP Invoice Agent")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Make browsers re-check the UI files so fixes show up without a hard refresh."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
db.init()
rules.load()


def _expected():
    with open(os.path.join(db.DATA, "expected.json")) as f:
        return json.load(f)


def _run_row(r):
    if not r:
        return None
    for k in ("reason_codes", "flags", "next_action", "override", "explanation"):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except (TypeError, ValueError):
                pass
    return r


def _run_full(run_id):
    r = _run_row(db.q("SELECT * FROM runs WHERE run_id=?", (run_id,), one=True))
    if not r:
        raise HTTPException(404, "run not found")
    r["invoice"] = db.q("SELECT invoice_id, file_name, uploaded_at FROM invoices WHERE invoice_id=?", (r["invoice_id"],), one=True)
    events = db.q("SELECT * FROM run_events WHERE run_id=? ORDER BY stage_no", (run_id,))
    names = {k: n for _, k, n in STAGES}
    for e in events:
        e["name"] = names.get(e["stage"], e["stage"])
        e["detail"] = json.loads(e["detail"]) if e.get("detail") else None
    r["events"] = events
    return r


@app.get("/api/health")
def health():
    cfg = llm_config()
    return {"ok": True, "extraction": f"LLM ({cfg['provider']}, {cfg['model']})" if cfg else "Offline (OCR + rules)",
            "llm": bool(cfg), "stage_delay": runner._delay(), "approval_limit": CFG["approval"]["auto_approve_limit_inr"]}


@app.get("/api/llm-check")
def llm_check():
    """One tiny text call to the configured LLM, returning the exact error if it fails."""
    from .pipeline.extract import _call_llm
    cfg = llm_config()
    if not cfg:
        return {"ok": False, "configured": False,
                "message": "No API key found. Put ANTHROPIC_API_KEY=... in the .env file next to requirements.txt and restart."}
    try:
        reply = _call_llm(cfg, "Reply with the single word OK.", [])
        return {"ok": True, "configured": True, "provider": cfg["provider"], "model": cfg["model"],
                "key_hint": cfg["key"][:7] + "..." + cfg["key"][-4:], "message": "Connected via " + cfg.get("used_model", cfg["model"]) + ". Model replied: " + reply.strip()[:40]}
    except Exception as e:
        return {"ok": False, "configured": True, "provider": cfg["provider"], "model": cfg["model"],
                "key_hint": cfg["key"][:7] + "..." + cfg["key"][-4:], "message": str(e)[:400] or type(e).__name__}


@app.get("/api/samples")
def samples():
    return _expected()


@app.get("/api/samples/{name}")
def sample_file(name: str):
    path = os.path.join(SAMPLES, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


class SampleReq(BaseModel):
    file: str


@app.post("/api/runs/sample")
def run_sample(req: SampleReq):
    path = os.path.join(SAMPLES, os.path.basename(req.file))
    if not os.path.exists(path):
        raise HTTPException(404, "sample not found")
    rid = runner.create_run(path, req.file)
    runner.start([rid])
    return {"run_id": rid}


@app.post("/api/runs/samples/all")
def run_all_samples():
    ids = [runner.create_run(os.path.join(SAMPLES, e["file"]), e["file"]) for e in _expected()]
    runner.start(ids)
    return {"run_ids": ids}


@app.post("/api/runs")
async def upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF invoice.")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "File is larger than 15 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "This file is not a valid PDF.")
    tmp = os.path.join(tempfile.mkdtemp(), "upload.pdf")
    with open(tmp, "wb") as f:
        f.write(data)
    rid = runner.create_run(tmp, os.path.basename(file.filename))
    runner.start([rid])
    return {"run_id": rid}


@app.get("/api/runs")
def list_runs():
    rows = db.q("""SELECT r.run_id, r.started_at, r.ended_at, r.status, r.mode, r.decision, r.final_decision, r.reason_codes,
                   r.owner, r.vendor_name, r.po_number, r.invoice_no, r.total, r.duration_ms, r.override, i.file_name
                   FROM runs r JOIN invoices i ON i.invoice_id=r.invoice_id ORDER BY r.run_id DESC""")
    return [_run_row(r) for r in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    return _run_full(run_id)


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: int):
    r = db.q("SELECT i.file_path FROM runs r JOIN invoices i ON i.invoice_id=r.invoice_id WHERE r.run_id=?", (run_id,), one=True)
    if not r or not os.path.exists(r["file_path"]):
        raise HTTPException(404)
    return FileResponse(r["file_path"], media_type="application/pdf")


@app.get("/api/runs/{run_id}/preview.png")
def run_preview(run_id: int, page: int = 0):
    """PNG render of a page, so the preview works in every browser (including ones without a PDF viewer)."""
    import pymupdf
    from fastapi.responses import Response
    r = db.q("SELECT i.file_path FROM runs r JOIN invoices i ON i.invoice_id=r.invoice_id WHERE r.run_id=?", (run_id,), one=True)
    if not r or not os.path.exists(r["file_path"]):
        raise HTTPException(404)
    doc = pymupdf.open(r["file_path"])
    png = doc[min(max(page, 0), len(doc) - 1)].get_pixmap(dpi=110).tobytes("png")
    doc.close()
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: int):
    async def gen():
        last = None
        for _ in range(2400):        # ~10 minutes max
            snap = _run_full(run_id)
            body = json.dumps(snap, default=str)
            if body != last:
                yield f"data: {body}\n\n"
                last = body
            if snap["status"] in ("done", "error", "superseded"):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class OverrideReq(BaseModel):
    action: str
    comment: str = ""
    by: str = "AP clerk"


@app.post("/api/runs/{run_id}/rerun")
def rerun(run_id: int):
    """Process the same invoice again with the rules now in force (e.g. after changing a tolerance)."""
    r = db.q("SELECT status FROM runs WHERE run_id=?", (run_id,), one=True)
    if not r:
        raise HTTPException(404, "run not found")
    if r["status"] in ("queued", "running"):
        raise HTTPException(400, "This run is still in progress.")
    new_id = runner.create_rerun(run_id)
    runner.start([new_id])
    return {"run_id": new_id}


EVAL_FIELDS = ["invoice_no", "invoice_date", "supplier_gstin", "po_ref", "total", "tax", "line_count", "bank_account"]


def _extracted_value(ext, key):
    f = ext.get("fields", {})
    val = lambda k: (f.get(k) or {}).get("value")
    if key == "tax":
        return round(sum(val(k) or 0 for k in ("cgst", "sgst", "igst")), 2)
    if key == "line_count":
        return len(ext.get("lines") or [])
    if key == "po_ref":
        import re as _re
        m = _re.search(r"PO[-\s]?(\d{3,6})", str(val("po_ref") or ""), _re.IGNORECASE)
        return f"PO-{m.group(1)}" if m else None
    return val(key)


def _match(key, got, want):
    if key in ("total", "tax"):
        return got is not None and want is not None and abs(float(got) - float(want)) < 0.01
    if key == "invoice_no":
        return str(got or "").strip().upper() == str(want or "").strip().upper()
    return (got or None) == (want or None)


@app.get("/api/evaluation")
def evaluation():
    """Scores the latest run of each sample invoice against the ground truth written by the
    generator that produced the PDFs: the decision, its key reason codes, and 8 extracted fields."""
    rows, field_hits, field_total = [], {k: 0 for k in EVAL_FIELDS}, 0
    for e in _expected():
        r = db.q("""SELECT r.*, i.extracted FROM runs r JOIN invoices i ON i.invoice_id = r.invoice_id
                    WHERE i.file_name = ? AND r.status = 'done' ORDER BY r.run_id DESC LIMIT 1""", (e["file"],), one=True)
        if not r:
            rows.append({"file": e["file"], "format": e["format"], "scored": False})
            continue
        ext = json.loads(r["extracted"] or "{}")
        codes = json.loads(r["reason_codes"] or "[]")
        fields = {}
        for k in EVAL_FIELDS:
            got, want = _extracted_value(ext, k), e["truth"].get(k)
            ok = _match(k, got, want)
            fields[k] = {"ok": ok, "got": got, "want": want}
            field_hits[k] += ok
        field_total += 1
        rows.append({"file": e["file"], "format": e["format"], "scored": True, "run_id": r["run_id"], "mode": r["mode"],
                     "duration_ms": r["duration_ms"], "expected": e["decision"], "got": r["decision"],
                     "decision_ok": r["decision"] == e["decision"] and set(e["codes"]) <= set(codes),
                     "fields": fields})
    scored = [x for x in rows if x["scored"]]
    n_fields = field_total * len(EVAL_FIELDS)
    changed = [r["label"] for r in rules.listing() if r["value"] != r["default"] and not r["key"].startswith("demo.")]
    return {"rows": rows, "fields": EVAL_FIELDS, "scored": len(scored), "total": len(rows), "rules_changed": changed,
            "decisions_ok": sum(1 for x in scored if x["decision_ok"]),
            "field_accuracy": round(100 * sum(field_hits.values()) / n_fields, 1) if n_fields else None,
            "per_field": {k: (round(100 * v / field_total, 1) if field_total else None) for k, v in field_hits.items()}}


@app.get("/api/rules")
def get_rules():
    return rules.listing()


@app.put("/api/rules")
def put_rules(changes: dict):
    ok, errors = rules.update(changes)
    if not ok:
        raise HTTPException(400, "; ".join(errors))
    return rules.listing()


@app.post("/api/rules/reset")
def reset_rules():
    rules.reset()
    return rules.listing()


@app.post("/api/runs/{run_id}/override")
def override(run_id: int, req: OverrideReq):
    action = req.action.upper()
    if action not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "action must be APPROVE or REJECT")
    if not req.comment.strip():
        raise HTTPException(400, "A comment is required for every override (audit trail).")
    r = db.q("SELECT * FROM runs WHERE run_id=?", (run_id,), one=True)
    if not r or r["status"] != "done":
        raise HTTPException(404, "run not found or not finished")
    ov = {"action": action, "comment": req.comment.strip(), "by": req.by or "AP clerk",
          "at": datetime.now().isoformat(timespec="seconds"), "previous": r["final_decision"]}
    db.ex("UPDATE runs SET final_decision=?, override=? WHERE run_id=?", (action, json.dumps(ov), run_id))
    return _run_full(run_id)


@app.get("/api/dashboard")
def dashboard():
    runs = [_run_row(r) for r in db.q("SELECT * FROM runs WHERE status='done'")]
    n = len(runs)
    by = {"APPROVE": 0, "HOLD": 0, "REJECT": 0}
    for r in runs:
        by[r["final_decision"]] = by.get(r["final_decision"], 0) + 1
    stp = sum(1 for r in runs if r["decision"] == "APPROVE" and not r.get("override"))
    reasons = {}
    for r in runs:
        for code in r.get("reason_codes") or []:
            sev = CODES.get(code, ("INFO",))[0]
            if sev != "INFO":
                reasons[code] = reasons.get(code, 0) + 1
    durations = [r["duration_ms"] for r in runs if r.get("duration_ms")]
    value = sum(r["total"] or 0 for r in runs)
    open_holds = sum(1 for r in runs if r["final_decision"] == "HOLD")
    return {"processed": n, "by_decision": by, "straight_through": stp,
            "straight_through_rate": round(100 * stp / n, 1) if n else 0,
            "avg_ms": int(sum(durations) / len(durations)) if durations else 0, "value": value,
            "open_holds": open_holds, "reasons": sorted(reasons.items(), key=lambda x: -x[1]),
            "overrides": sum(1 for r in runs if r.get("override"))}


@app.get("/api/reference")
def reference():
    pos = []
    for h in db.q("SELECT * FROM purchase_orders ORDER BY po_number"):
        p = db.po(h["po_number"])
        for l in p["lines"]:
            l["grn_qty"] = db.grn_qty(p["po_number"], l["line_no"])
            l["billed_qty"] = db.billed_qty(p["po_number"], l["line_no"])
        p["value"] = sum(l["qty"] * l["unit_price"] for l in p["lines"])
        pos.append(p)
    return {"buyer": db.buyer(), "vendors": db.vendors(), "purchase_orders": pos,
            "grns": db.q("SELECT * FROM grns ORDER BY grn_number, line_no"), "config": CFG}


@app.post("/api/reset")
def reset():
    db.reset_runs()
    up = os.path.join(db.DATA, "uploads")
    if os.path.isdir(up):
        for f in os.listdir(up):
            try:
                os.remove(os.path.join(up, f))
            except OSError:
                pass
    return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
