"""The seven stages. Each takes the run context, appends flags, and returns
(summary, detail). Stages never decide the outcome; stage 7 does, by rule."""
import hashlib
import re
from datetime import datetime

from rapidfuzz import fuzz

from .. import db, gst
from . import explain
from .common import CFG, CODES, flag, fmt_inr, norm_invoice_no, today_iso
from .extract import classify, llm_config, llm_extract, offline_extract, read_pdf, reconcile


def _v(ctx, key):
    return (ctx["fields"].get(key) or {}).get("value")


def _check(checks, name, ok, detail):
    checks.append({"check": name, "result": "pass" if ok else "flag", "detail": detail})


# ------------------------------------------------------------ 1 intake
def intake(ctx):
    with open(ctx["file_path"], "rb") as fh:
        ctx["file_hash"] = hashlib.sha256(fh.read()).hexdigest()
    doc = read_pdf(ctx["file_path"])
    ctx["doc"] = doc
    doc_type = classify(doc["text"])
    ctx["doc_type"] = doc_type
    detail = {"file": ctx["file_name"], "sha256": ctx["file_hash"][:16] + "...", "pages": doc["pages"],
              "format": "scanned image (OCR)" if doc["is_scan"] else "text PDF", "characters_read": len(doc["text"]),
              "document_type": doc_type}
    if doc["is_scan"] and len(doc["text"].strip()) < 40:
        ctx["flags"].append(flag("UNREADABLE", "The scan is unreadable even after OCR.", "intake"))
        ctx["stop"] = True
    elif doc_type == "credit_note":
        ctx["flags"].append(flag("CREDIT_NOTE", "This is a credit note, not an invoice; routed for manual application.", "intake"))
        ctx["stop"] = True
    elif doc_type != "invoice":
        ctx["flags"].append(flag("NOT_INVOICE", f"The document is a {doc_type.replace('_', ' ')}, not a tax invoice.", "intake"))
        ctx["stop"] = True
    kind = "Scanned image, read with OCR" if doc["is_scan"] else "Text PDF"
    return f"{kind}; {doc['pages']} page(s); classified as {doc_type.replace('_', ' ')}", detail


# ------------------------------------------------------------ 2 extract
def extract(ctx):
    buyer_gstin = db.buyer()["gstin"]
    cfg = llm_config()
    # Reader 1 (always): the PDF text layer, or OCR for scans, parsed with rules.
    offline = offline_extract(ctx["doc"], buyer_gstin)
    result = None
    if cfg:
        try:
            # Reader 2: the vision LLM. The two readings are reconciled field by field.
            llm = llm_extract(ctx["file_path"], ctx["doc"], cfg)
            result = reconcile(llm, offline, ctx["doc"]["is_scan"])
            ctx["mode"] = f"llm+ocr:{cfg['provider']}:{cfg.get('used_model') or cfg['model']}"
        except Exception as e:  # vendor outage must not stop AP
            ctx["flags"].append(flag("LLM_FALLBACK", f"LLM extraction failed ({str(e)[:220] or type(e).__name__}); used OCR and rules instead.", "extract"))
    if result is None:
        result = offline
        ctx["mode"] = "offline:ocr+rules"
    fields, lines = result["fields"], result["lines"]

    # A GSTIN whose checksum verifies is trusted even when OCR confidence is low:
    # a misread that still passes a mod-36 checksum is very unlikely.
    for k in ("supplier_gstin", "buyer_gstin"):
        if k in fields and gst.is_valid(str(fields[k]["value"])):
            repaired = any("repaired" in n and str(fields[k]["value"]) in n for n in result["notes"])
            fields[k]["confidence"] = max(fields[k]["confidence"], 0.90 if repaired else 0.97)
            fields[k]["verified"] = "checksum"
    for n in result["notes"]:
        code = "READERS_DISAGREE" if n.startswith("LLM and") else "OCR_REPAIRED"
        ctx["flags"].append(flag(code, n + ".", "extract"))

    ctx["fields"], ctx["lines"] = fields, lines
    missing = [k for k in CFG["extraction"]["mandatory_fields"] if fields.get(k, {}).get("value") in (None, "")]
    if missing:
        ctx["flags"].append(flag("MISSING_FIELD", "Mandatory field(s) missing: " + ", ".join(m.replace("_", " ") for m in missing) + ".",
                                 "extract", fields=missing))
    thr = CFG["extraction"]["min_confidence"]
    low = [(k, fields[k]["confidence"]) for k in CFG["extraction"]["key_fields"]
           if k in fields and fields[k]["confidence"] < thr]
    if low:
        ctx["flags"].append(flag("LOW_CONFIDENCE", "Low read confidence on " +
                                 ", ".join(f"{k.replace('_', ' ')} ({c:.2f})" for k, c in low) + f"; threshold {thr}.",
                                 "extract", fields=[k for k, _ in low]))
    if not lines:
        ctx["flags"].append(flag("LOW_CONFIDENCE", "No line items could be read.", "extract"))
    if not fields:
        ctx["stop"] = True
    detail = {"mode": ctx["mode"], "fields": fields, "lines": lines, "threshold": thr,
              "lines_from": result.get("lines_from")}
    return f"{len(fields)} fields and {len(lines)} line(s) read via {ctx['mode'].split(':')[0].upper()}", detail


# ------------------------------------------------------------ 3 validate
def validate(ctx):
    checks = []
    lines = ctx["lines"]
    tol = CFG["validation"]["total_tolerance_inr"]
    incl = bool(_v(ctx, "prices_include_tax"))
    ctx["inclusive"] = incl

    bad = [i + 1 for i, l in enumerate(lines)
           if l["unit_price"] is not None and l["amount"] is not None
           and abs(l["qty"] * l["unit_price"] - l["amount"]) > CFG["validation"]["line_tolerance_inr"]]
    _check(checks, "Line arithmetic (qty x rate = amount)", not bad,
           "all lines add up" if not bad else f"line(s) {bad} do not add up")
    if bad:
        ctx["flags"].append(flag("LINE_MATH", f"Line(s) {bad}: quantity x rate does not equal the amount.", "validate"))

    lines_sum = round(sum(l["amount"] or 0 for l in lines), 2)
    tax = round(sum(_v(ctx, k) or 0 for k in ("cgst", "sgst", "igst")), 2)
    total = _v(ctx, "total")
    if total is not None and lines:
        expect = lines_sum if incl else round(lines_sum + tax, 2)
        ok = abs(expect - total) <= tol
        how = "lines (tax-inclusive)" if incl else "lines + tax"
        _check(checks, "Invoice total", ok, f"{how} = {fmt_inr(expect)} vs stated {fmt_inr(total)}")
        if not ok:
            ctx["flags"].append(flag("TOTAL_MISMATCH", f"Stated total {fmt_inr(total)} differs from {how} {fmt_inr(expect)}.", "validate"))
    ctx["ex_tax_subtotal"] = round(total - tax, 2) if incl and total else lines_sum

    sup = _v(ctx, "supplier_gstin")
    if sup:
        ok = gst.is_valid(sup)
        _check(checks, "Supplier GSTIN format and checksum", ok, sup)
        if not ok:
            ctx["flags"].append(flag("GSTIN_INVALID", f"Supplier GSTIN {sup} fails the format/checksum test.", "validate"))
    buyer = db.buyer()["gstin"]
    bg = _v(ctx, "buyer_gstin")
    if bg:
        ok = bg == buyer
        _check(checks, "Billed to our GSTIN", ok, bg)
        if not ok:
            ctx["flags"].append(flag("WRONG_BUYER", f"Invoice is addressed to GSTIN {bg}, not ours ({buyer}).", "validate"))

    if sup and gst.is_valid(sup):
        same = gst.state_code(sup) == CFG["buyer_gstin_state"]
        has_cs = bool(_v(ctx, "cgst") or _v(ctx, "sgst"))
        has_i = bool(_v(ctx, "igst"))
        expected = "CGST + SGST" if same else "IGST"
        ok = (has_cs and not has_i) if same else (has_i and not has_cs)
        st = gst.STATES.get(gst.state_code(sup), gst.state_code(sup))
        _check(checks, "Tax type fits place of supply", ok,
               f"supplier in {st}, buyer in {gst.STATES.get(CFG['buyer_gstin_state'])}: expect {expected}")
        if not ok:
            ctx["flags"].append(flag("TAX_TYPE", f"Supplier is in {st}; expected {expected} but the invoice shows "
                                     f"{'CGST/SGST' if has_cs else ''}{' and ' if has_cs and has_i else ''}{'IGST' if has_i else ''}"
                                     f"{'no tax' if not (has_cs or has_i) else ''}.", "validate"))

    d = _v(ctx, "invoice_date")
    if d:
        ok = d <= today_iso()
        _check(checks, "Invoice date not in the future", ok, d)
        if not ok:
            ctx["flags"].append(flag("DATE_INVALID", f"Invoice date {d} is in the future.", "validate"))
    n_flag = sum(1 for c in checks if c["result"] == "flag")
    return f"{len(checks) - n_flag} of {len(checks)} checks passed", {"checks": checks, "prices_include_tax": incl}


# ------------------------------------------------------------ 4 vendor + duplicates
def vendor(ctx):
    checks = []
    sup = _v(ctx, "supplier_gstin")
    name = _v(ctx, "vendor_name") or ""
    vendors = db.vendors()
    v = next((x for x in vendors if sup and x["gstin"] == sup), None)
    how = "GSTIN exact match" if v else None
    if not v and name:
        best, score = None, 0
        for x in vendors:
            s = max(fuzz.token_set_ratio(name.lower(), n.lower()) for n in [x["legal_name"]] + x["aliases"])
            if s > score:
                best, score = x, s
        if score >= CFG["vendor"]["fuzzy_name_min"]:
            v, how = best, f"name match ({score:.0f})"
            if sup:
                ctx["flags"].append(flag("VENDOR_GSTIN_MISMATCH", f"Name matches {best['legal_name']} but GSTIN {sup} "
                                         f"differs from master {best['gstin']}.", "vendor"))
            else:
                ctx["flags"].append(flag("VENDOR_MATCHED_BY_NAME", f"Vendor matched by name ({score:.0f}).", "vendor"))
    _check(checks, "Vendor in master", bool(v), f"{v['legal_name']} ({how})" if v else "no match")
    if not v:
        ctx["flags"].append(flag("VENDOR_UNKNOWN", f"No vendor in the master matches '{name}' / {sup}.", "vendor"))
    ctx["vendor"] = v

    acct, ifsc = _v(ctx, "bank_account"), _v(ctx, "bank_ifsc")
    if v and (acct or ifsc):
        changed = (acct and acct != v["bank_account"]) or (ifsc and ifsc != v["bank_ifsc"])
        _check(checks, "Bank details match master", not changed,
               f"invoice xxxx{str(acct)[-4:]} / {ifsc} vs master xxxx{v['bank_account'][-4:]} / {v['bank_ifsc']}")
        if changed:
            ctx["flags"].append(flag("BANK_CHANGED", f"Bank details on the invoice (a/c xxxx{str(acct)[-4:]}, {ifsc}) differ from "
                                     f"the vendor master (a/c xxxx{v['bank_account'][-4:]}, {v['bank_ifsc']}). Possible "
                                     "payment-redirection fraud; never auto-approve.", "vendor"))

    # duplicates: compare with earlier invoices that were not rejected
    norm = norm_invoice_no(_v(ctx, "invoice_no"))
    ctx["invoice_no_norm"] = norm
    prior = db.q("""SELECT i.*, r.run_id, r.final_decision, r.started_at AS run_at FROM invoices i
                    JOIN runs r ON r.invoice_id = i.invoice_id
                    WHERE r.run_id <> ? AND r.invoice_id <> ? AND r.status = 'done'
                    AND COALESCE(r.final_decision,'') <> 'REJECT'""",
                 (ctx["run_id"], ctx["invoice_id"]))
    dups, near = [], []
    total, d = _v(ctx, "total"), _v(ctx, "invoice_date")
    for p in prior:
        if p["file_hash"] == ctx["file_hash"]:
            dups.append((p, "identical file"))
            continue
        if v and p["vendor_id"] != v["vendor_id"]:
            continue
        if norm and p["invoice_no_norm"] == norm:
            dups.append((p, f"same invoice number ({p['invoice_no']} vs {_v(ctx, 'invoice_no')} after normalising)"))
        elif total and p["total"] and abs(p["total"] - total) <= total * CFG["duplicates"]["near_total_pct"] / 100 \
                and d and p["invoice_date"] and \
                abs((datetime.fromisoformat(d) - datetime.fromisoformat(p["invoice_date"])).days) <= CFG["duplicates"]["near_days"]:
            near.append(p)
    _check(checks, "Not a duplicate", not dups and not near, f"compared with {len(prior)} earlier invoice(s)")
    for p, why in dups[:1]:
        ctx["flags"].append(flag("DUPLICATE", f"Duplicate of invoice {p['invoice_no']} already processed in run #{p['run_id']} "
                                 f"({ {'APPROVE': 'approved', 'HOLD': 'on hold'}.get(p['final_decision'], 'processed')} {p['run_at'][:10]}): {why}.", "vendor",
                                 original_run=p["run_id"]))
    if not dups:
        for p in near[:1]:
            ctx["flags"].append(flag("POSSIBLE_DUPLICATE", f"Similar to invoice {p['invoice_no']} (run #{p['run_id']}): same vendor, "
                                     f"total {fmt_inr(p['total'])}, dated {p['invoice_date']}.", "vendor", original_run=p["run_id"]))
    detail = {"checks": checks, "vendor": v and {k: v[k] for k in ("vendor_id", "legal_name", "gstin", "bank_ifsc")},
              "normalised_invoice_no": norm, "compared_with": len(prior)}
    return (f"{v['legal_name']} ({how})" if v else "Vendor not found") + \
        ("; duplicate found" if dups else "; no duplicate"), detail


# ------------------------------------------------------------ 5 PO
def _remaining_value(po, exclude_invoice=None):
    value = sum(l["qty"] * l["unit_price"] for l in po["lines"])
    return value, value - db.billed_value(po["po_number"], exclude_invoice=exclude_invoice)


def find_po(ctx):
    raw_ref = _v(ctx, "po_ref")
    v = ctx.get("vendor")
    m = re.search(r"PO[-\s]?(\d{3,6})", str(raw_ref or ""), re.IGNORECASE)
    ref = f"PO-{m.group(1)}" if m else None
    if raw_ref and not ref:
        ctx["flags"].append(flag("READERS_DISAGREE", f"PO reference '{str(raw_ref)[:40]}' is not a PO number; "
                                 "treated as no PO reference.", "po"))
    if ref:
        po = db.po(ref)
        if not po:
            ctx["flags"].append(flag("PO_NOT_FOUND", f"PO {ref} quoted on the invoice does not exist.", "po"))
            return f"PO {ref} not found", {"method": "explicit", "po_ref": ref}
        if v and po["vendor_id"] != v["vendor_id"]:
            ctx["flags"].append(flag("PO_VENDOR_MISMATCH", f"PO {ref} belongs to vendor {po['vendor_id']}, not {v['vendor_id']}.", "po"))
        ctx["po"] = po
        return f"{ref} quoted on the invoice", {"method": "explicit", "po": po}
    if not v:
        ctx["flags"].append(flag("PO_NOT_FOUND", "No PO reference and no known vendor to search POs for.", "po"))
        return "No PO reference; cannot search", {"method": "none"}

    cands = []
    for po in db.pos_for_vendor(v["vendor_id"]):
        value, remaining = _remaining_value(po, ctx["invoice_id"])
        if remaining <= 1:
            continue
        descs = [l["description"].lower() for l in po["lines"]]
        desc_score = sum(max(fuzz.token_set_ratio(il["description"].lower(), d) for d in descs)
                         for il in ctx["lines"]) / max(1, len(ctx["lines"]))
        amt_score = 100 * (1 - min(1.0, abs(ctx["ex_tax_subtotal"] - remaining) / remaining))
        score = round(0.7 * desc_score + 0.3 * amt_score, 1)
        cands.append({"po_number": po["po_number"], "po_date": po["po_date"], "open_value": round(remaining, 2),
                      "description_score": round(desc_score, 1), "amount_score": round(amt_score, 1), "score": score, "_po": po})
    cands.sort(key=lambda c: -c["score"])
    table = [{k: c[k] for k in c if k != "_po"} for c in cands]
    if not cands or cands[0]["score"] < CFG["po_inference"]["min_score"]:
        ctx["flags"].append(flag("PO_NOT_FOUND", f"No PO reference, and none of the vendor's {len(cands)} open PO(s) resembles this invoice.", "po"))
        return "No PO reference; no open PO fits", {"method": "inferred", "candidates": table}
    top = cands[0]
    second = cands[1] if len(cands) > 1 else None
    if second and top["score"] - second["score"] < CFG["po_inference"]["min_margin"]:
        ctx["flags"].append(flag("PO_AMBIGUOUS", f"No PO reference; {top['po_number']} ({top['score']}) and {second['po_number']} "
                                 f"({second['score']}) fit equally well.", "po", candidates=[top["po_number"], second["po_number"]]))
        return f"Ambiguous: {top['po_number']} vs {second['po_number']}", {"method": "inferred", "candidates": table}
    ctx["po"] = top["_po"]
    runner = f" vs {second['score']} for {second['po_number']}" if second else ""
    ctx["flags"].append(flag("PO_INFERRED", f"No PO on the invoice; inferred {top['po_number']} from line items and amount "
                             f"(score {top['score']}{runner}).", "po"))
    return f"Inferred {top['po_number']} (score {top['score']}{runner})", {"method": "inferred", "candidates": table, "po": top["_po"]}


# ------------------------------------------------------------ 6 3-way match
def match(ctx):
    po = ctx.get("po")
    if not po:
        return "Skipped: no PO to match against", {"rows": [], "skipped": True}
    rows, matches = [], []
    tol = CFG["matching"]["price_tolerance_pct"]
    d = _v(ctx, "invoice_date")
    if d and d < po["po_date"]:
        ctx["flags"].append(flag("DATE_INVALID", f"Invoice date {d} is before the PO date {po['po_date']}.", "match"))
    for i, il in enumerate(ctx["lines"], 1):
        best, score = None, 0
        for pl in po["lines"]:
            s = fuzz.token_set_ratio(il["description"].lower(), pl["description"].lower())
            if il.get("hsn") and il["hsn"] == pl["hsn"]:
                s = min(100, s + 10)
            if s > score:
                best, score = pl, s
        row = {"line": i, "description": il["description"], "qty": il["qty"], "issues": []}
        if not best or score < CFG["matching"]["line_match_min"]:
            row.update({"po_line": None, "status": "flag", "issues": ["no matching PO line"]})
            ctx["flags"].append(flag("UNMATCHED_LINE", f"Line {i} '{il['description']}' has no matching line on {po['po_number']}.", "match"))
            rows.append(row)
            continue
        price = il["unit_price"] or 0
        ex = price / (1 + best["gst_rate"] / 100) if ctx.get("inclusive") else price
        var = (ex - best["unit_price"]) / best["unit_price"] * 100
        grn = db.grn_qty(po["po_number"], best["line_no"])
        billed = db.billed_qty(po["po_number"], best["line_no"], exclude_invoice=ctx["invoice_id"])
        row.update({"po_line": best["line_no"], "po_description": best["description"], "match_score": round(score),
                    "price_ex_tax": round(ex, 2), "po_price": best["unit_price"], "variance_pct": round(var, 2),
                    "grn_qty": grn, "billed_before": billed, "po_qty": best["qty"]})
        if var > tol + 1e-9:
            row["issues"].append(f"price +{var:.1f}%")
            ctx["flags"].append(flag("PRICE_VARIANCE", f"Line {i} unit price {fmt_inr(ex)} is {var:.1f}% above PO "
                                     f"{fmt_inr(best['unit_price'])} (tolerance {tol}%).", "match"))
        elif var > 0.005:
            ctx["flags"].append(flag("PRICE_WITHIN_TOLERANCE", f"Line {i} priced {var:.1f}% above PO, within the {tol}% tolerance.", "match"))
        if billed + il["qty"] > grn + 1e-6:
            row["issues"].append("qty over GRN")
            ctx["flags"].append(flag("QTY_EXCEEDS_GRN", f"Line {i}: {billed:g} already billed + {il['qty']:g} on this invoice exceeds "
                                     f"{grn:g} received.", "match"))
        if billed + il["qty"] > best["qty"] + 1e-6:
            row["issues"].append("over PO qty")
            ctx["flags"].append(flag("PO_OVERBILLED", f"Line {i}: billing would reach {billed + il['qty']:g} of {best['qty']:g} "
                                     f"ordered on {po['po_number']}.", "match"))
        row["status"] = "flag" if row["issues"] else "pass"
        rows.append(row)
        matches.append({"po_number": po["po_number"], "po_line_no": best["line_no"], "qty": il["qty"],
                        "amount": round(il["qty"] * ex, 2)})
    ctx["match_rows"], ctx["line_matches"] = rows, matches
    ok = sum(1 for r in rows if r["status"] == "pass")
    return f"{ok} of {len(rows)} line(s) match {po['po_number']} and GRN", {"po_number": po["po_number"], "rows": rows}


# ------------------------------------------------------------ 7 decide
def decide(ctx):
    sev = {x["severity"] for x in ctx["flags"]}
    total = _v(ctx, "total") if ctx.get("fields") else None
    if "REJECT" not in sev and total and total > CFG["approval"]["auto_approve_limit_inr"]:
        ctx["flags"].append(flag("ABOVE_LIMIT", f"Total {fmt_inr(total)} is above the auto-approval limit of "
                                 f"{fmt_inr(CFG['approval']['auto_approve_limit_inr'])}.", "decide"))
        sev.add("HOLD")
    decision = "REJECT" if "REJECT" in sev else "HOLD" if "HOLD" in sev else "APPROVE"
    ctx.setdefault("fields", {})
    ctx["decision"] = decision
    ctx["explanation"] = explain.explanation(ctx, decision)
    ctx["next_action"] = explain.next_action(ctx, decision)
    holds = [x for x in ctx["flags"] if x["severity"] == "HOLD"]
    ctx["owner"] = holds[0]["owner"] if decision == "HOLD" else ("Vendor" if decision == "REJECT" else None)
    counted = [x for x in ctx["flags"] if x["severity"] != "INFO"]
    rule = "any Reject flag -> REJECT; else any Hold flag -> HOLD; else APPROVE"
    return f"{decision} ({len(counted)} blocking flag(s))", {
        "decision": decision, "rule": rule, "explanation": ctx["explanation"], "next_action": ctx["next_action"],
        "flags": ctx["flags"]}


STAGE_FUNCS = {"intake": intake, "extract": extract, "validate": validate, "vendor": vendor,
               "po": find_po, "match": match, "decide": decide}
