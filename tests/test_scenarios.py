"""Runs all 10 sample invoices in order on a fresh database and checks each
decision and its key reason codes against data/expected.json.

    python -m pytest -q tests        (or)        python tests/test_scenarios.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("AP_DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("DEMO_STAGE_DELAY", "0")
os.environ.setdefault("AP_TODAY", "2026-11-04")

from app import db  # noqa: E402
from app.pipeline import runner  # noqa: E402
from app.pipeline.common import norm_invoice_no  # noqa: E402
from app.pipeline.extract import repair_gstin  # noqa: E402


def run_all():
    db.init(reset_reference=True)
    db.reset_runs()
    with open(os.path.join(ROOT, "data", "expected.json")) as f:
        expected = json.load(f)
    results = []
    for e in expected:
        rid = runner.create_run(os.path.join(ROOT, "data", "invoices", e["file"]), e["file"])
        runner.execute(rid)
        r = db.q("SELECT * FROM runs WHERE run_id=?", (rid,), one=True)
        codes = json.loads(r["reason_codes"] or "[]")
        ok = r["status"] == "done" and r["decision"] == e["decision"] and set(e["codes"]) <= set(codes)
        results.append((e, r, codes, ok))
    return results


def test_scenarios():
    results = run_all()
    bad = [(e["file"], r["decision"], codes, r.get("error")) for e, r, codes, ok in results if not ok]
    assert not bad, bad


def test_helpers():
    assert norm_invoice_no("INV/2026/0142") == norm_invoice_no("INV-2026-142")
    assert norm_invoice_no("AIS/26-27/0418") != norm_invoice_no("AIS/26-27/0436")
    assert repair_gstin("36AAJFD2271Q1ZZ") == ("36AAJFD2271Q1ZZ", False)
    assert repair_gstin("36AAJFD227lQ1ZZ".upper())[0] == "36AAJFD2271Q1ZZ"


def _fake_llm_json():
    return json.dumps({"doc_type": "invoice", "fields": {
        "vendor_name": {"value": "Apex Industrial Supplies Pvt Ltd", "confidence": 0.98, "source": "header"},
        "supplier_gstin": {"value": "21AAFCA3317K1Z2", "confidence": 0.97, "source": "GSTIN: 21AAFCA3317K1Z2"},
        "buyer_gstin": {"value": "21AAKCM4821H1ZP", "confidence": 0.97, "source": "Bill To GSTIN"},
        "invoice_no": {"value": "AIS/26-27/0418", "confidence": 0.99, "source": "Invoice No"},
        "invoice_date": {"value": "2026-10-12", "confidence": 0.99, "source": "12-Oct-2026"},
        "po_ref": {"value": "PO-1001", "confidence": 0.99, "source": "PO No: PO-1001"},
        "subtotal": {"value": "1,22,050.00", "confidence": 0.99, "source": "Sub Total"},
        "cgst": {"value": 10984.5, "confidence": 0.99, "source": "CGST"},
        "sgst": {"value": 10984.5, "confidence": 0.99, "source": "SGST"},
        "igst": {"value": None, "confidence": 1.0, "source": ""},
        "total": {"value": "144019.00", "confidence": 0.99, "source": "Grand Total"},
        "prices_include_tax": {"value": False, "confidence": 0.95, "source": ""},
        "bank_account": {"value": "50200012345678", "confidence": 0.99, "source": "A/c"},
        "bank_ifsc": {"value": "HDFC0001234", "confidence": 0.99, "source": "IFSC"}},
        "lines": [{"description": "Hydraulic hose assembly 1/2 in x 2 m", "hsn": "4009", "qty": 40, "uom": "Nos", "unit_price": 1850, "amount": 74000, "confidence": 0.98, "source": "row 1"},
                  {"description": "Pressure gauge 0-250 bar glycerine filled", "hsn": "9026", "qty": 12, "uom": "Nos", "unit_price": 2400, "amount": 28800, "confidence": 0.98, "source": "row 2"},
                  {"description": "Hex bolt M16 x 80 HT 8.8 with nut", "hsn": "7318", "qty": 500, "uom": "Nos", "unit_price": 38.5, "amount": 19250, "confidence": 0.98, "source": "row 3"}]})


def _run_one_with_llm(fake):
    from app.pipeline import extract as ex
    from app.pipeline import runner as rn
    old_env, old_call, old_rn = os.environ.get("ANTHROPIC_API_KEY"), ex._call_llm, rn._call_llm
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    ex._call_llm = fake
    rn._call_llm = lambda cfg, prompt, images: "Approved: matches PO-1001."
    try:
        db.init(reset_reference=True)
        db.reset_runs()
        rid = runner.create_run(os.path.join(ROOT, "data", "invoices", "01_happy_path.pdf"), "01_happy_path.pdf")
        runner.execute(rid)
        return db.q("SELECT * FROM runs WHERE run_id=?", (rid,), one=True)
    finally:
        ex._call_llm, rn._call_llm = old_call, old_rn
        if old_env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)


def test_llm_path_mocked():
    r = _run_one_with_llm(lambda cfg, prompt, images: "```json\n" + _fake_llm_json() + "\n```")
    assert r["decision"] == "APPROVE" and r["mode"].startswith("llm:anthropic"), (r["decision"], r["mode"], r["error"])


def test_llm_outage_falls_back():
    def boom(cfg, prompt, images):
        raise TimeoutError("simulated outage")
    r = _run_one_with_llm(boom)
    assert r["decision"] == "APPROVE" and "LLM_FALLBACK" in json.loads(r["reason_codes"]), (r["decision"], r["reason_codes"])


if __name__ == "__main__":
    out = run_all()
    for e, r, codes, ok in out:
        print(f"{'PASS' if ok else 'FAIL'}  {e['file']:26s} expected {e['decision']:7s} got {str(r['decision']):7s} "
              f"{r['duration_ms']:>5} ms  {','.join(codes)}")
        if r.get("error"):
            print(r["error"])
    print(f"\n{sum(1 for *_, ok in out if ok)}/{len(out)} scenarios pass")
