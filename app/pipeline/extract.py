"""Reading the document.

Two extractors produce the same structure:
  * llm_extract     - a vision LLM reads page images (+ text layer) into a JSON schema
  * offline_extract - OCR (tesseract) or the PDF text layer, parsed with rules

The LLM is used when an API key is configured; otherwise, or if the LLM call
fails, the offline extractor runs so the process never stops on a vendor outage.
Each field carries value, confidence (0-1) and source_text.
"""
import base64
import io
import json
import os
import re
import time

import httpx
import numpy as np
import pymupdf
import pytesseract
from PIL import Image

from .. import gst
from .common import MONEY_RE, money, parse_date

# Windows installs Tesseract outside PATH; honour TESSERACT_CMD or the default install folder.
_TESS = os.environ.get("TESSERACT_CMD") or (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.name == "nt" and os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None)
if _TESS:
    pytesseract.pytesseract.tesseract_cmd = _TESS

UOMS = r"(Nos|No|Pcs|Pairs|Pair|Bags|Bag|Sets|Set|Kg|Kgs|Mtr|Mtrs|Ltr|Box|Units|Unit|Rolls|Roll)"
LINE_RE = re.compile(
    r"^\W*(\d{1,3})\s*[.|]?\s+(.+?)\s+(?:(\d{4,8})\s+)?(\d+(?:\.\d+)?)\s+" + UOMS + r"\.?\s+[|]?\s*("
    + MONEY_RE.pattern + r")\s+[|]?\s*(" + MONEY_RE.pattern + r")", re.IGNORECASE)


# ------------------------------------------------------------------- reading
def _long_runs(mask, min_len):
    """Boolean mask of horizontal runs of True at least min_len long (vectorised per row)."""
    out = np.zeros_like(mask)
    padded = np.pad(mask.astype(np.int8), ((0, 0), (1, 1)))
    d = np.diff(padded, axis=1)
    for y in np.where(mask.sum(axis=1) >= min_len)[0]:
        starts, ends = np.where(d[y] == 1)[0], np.where(d[y] == -1)[0]
        for s, e in zip(starts, ends):
            if e - s >= min_len:
                out[y, s:e] = True
    return out


def _remove_rules(img):
    """Whiten long horizontal and vertical lines (table borders) so OCR reads the cells as text rows."""
    a = np.array(img.convert("L"))
    dark = a < 150
    lines = _long_runs(dark, 120) | _long_runs(dark.T, 60).T
    a[lines] = 255
    return Image.fromarray(a)


def read_pdf(path):
    """Return dict(text, lines=[(text, conf)], is_scan, pages)."""
    doc = pymupdf.open(path)
    pages = len(doc)
    text = "\n".join(p.get_text() for p in doc)
    if len(text.strip()) > 80:
        # Text layer present: pdfplumber keeps table rows on one line.
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:3])
        doc.close()
        return {"text": text, "lines": [(l, 0.99) for l in text.splitlines() if l.strip()],
                "is_scan": False, "pages": pages}
    lines = []
    for page in list(doc)[:3]:
        img = _remove_rules(Image.open(io.BytesIO(page.get_pixmap(dpi=300).tobytes("png"))))
        data = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
        rows = {}
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            rows.setdefault(key, []).append((word, float(data["conf"][i])))
        for key in sorted(rows):
            words = rows[key]
            confs = [c for _, c in words if c >= 0]
            lines.append((" ".join(w for w, _ in words), (sum(confs) / len(confs) / 100.0) if confs else 0.5))
    doc.close()
    return {"text": "\n".join(l for l, _ in lines), "lines": lines, "is_scan": True, "pages": pages}


def classify(text):
    up = text.upper()
    if "CREDIT NOTE" in up:
        return "credit_note"
    if "QUOTATION" in up or "PROFORMA" in up:
        return "quote"
    if "STATEMENT OF ACCOUNT" in up:
        return "statement"
    if "PURCHASE ORDER" in up and "INVOICE" not in up:
        return "purchase_order"
    if "INVOICE" in up or re.search(r"\b(?:BILL|INV)\.?\s*(?:NO|DATE)\b", up):
        return "invoice"
    if gst.GSTIN_RE.search(up) and re.search(r"\b(?:GRAND TOTAL|AMOUNT DUE|AMOUNT PAYABLE|TOTAL)\b", up):
        return "invoice"          # a GST document with a payable total and none of the non-invoice markers
    return "other"


# ------------------------------------------------------------- GSTIN repair
_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"}
_TO_ALPHA = {v: k for k, v in {"O": "0", "I": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}.items()}


def repair_gstin(raw):
    """Fix common OCR confusions by position, accept only if the checksum passes.
    Returns (gstin or None, repaired: bool)."""
    s = re.sub(r"[^0-9A-Z]", "", (raw or "").upper())
    if len(s) != 15:
        return None, False
    if gst.is_valid(s):
        return s, False
    shape = "DDLLLLLDDDDLXZX"   # D digit, L letter, X either, Z literal
    out = []
    for ch, kind in zip(s, shape):
        if kind == "D":
            ch = _TO_DIGIT.get(ch, ch)
        elif kind == "L":
            ch = _TO_ALPHA.get(ch, ch)
        elif kind == "Z":
            ch = "Z" if ch in "Z2" else ch
        out.append(ch)
    fixed = "".join(out)
    if gst.is_valid(fixed) and sum(a != b for a, b in zip(s, fixed)) <= 2:
        return fixed, True
    return None, False


# ------------------------------------------------------------ offline rules
def _field(value, conf, source):
    return {"value": value, "confidence": round(conf, 2), "source": source}


def _find(lines, pattern, group=1, flags=re.IGNORECASE):
    rx = re.compile(pattern, flags)
    for text, conf in lines:
        m = rx.search(text)
        if m:
            return m.group(group), conf, text.strip()
    return None, 0.0, None


DATE_PAT = (r"(\d{1,2}[\-/. ](?:\d{1,2}|[A-Za-z]{3,9})[\-/. ]\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
            r"|\d{4}-\d{2}-\d{2})")
SKIP_LINE = re.compile(r"^\W*(?:sub\s*total|subtotal|total|grand|taxable|cgst|sgst|igst|output|add|less|round|amount|"
                       r"net|balance|tax|of which)\b", re.IGNORECASE)
NUM = re.compile(r"^\d[\d,]*(?:\.\d+)?$")


def _find_kv(lines, label, value, ok):
    """A labelled value either on the same line ("Invoice No: X") or, as in Tally-style
    boxes, on the next line under the label ("Invoice No.  Dated" / "EH/1123  23-Oct-2026")."""
    rx_same = re.compile(label + r"\s*[:\-#]?\s*" + value, re.IGNORECASE)
    rx_label = re.compile(label, re.IGNORECASE)
    rx_value = re.compile(value, re.IGNORECASE)
    for i, (text, conf) in enumerate(lines):
        m = rx_same.search(text)
        if m and ok(m.group(1)):
            return m.group(1), conf, text.strip()
    for i, (text, conf) in enumerate(lines[:-1]):
        lm = rx_label.search(text)
        if not lm:
            continue
        nxt, nconf = lines[i + 1]
        # labels before ours on the label line tell us which value on the next line is ours
        before = len(re.findall(r"(?:No\.?|Date|Dated|Number|Order)\b", text[:lm.start()], re.IGNORECASE))
        cands = [m.group(1) for m in rx_value.finditer(nxt) if ok(m.group(1))]
        if cands:
            pick = cands[before] if before < len(cands) else cands[0]
            return pick, nconf * 0.95, text.strip() + " / " + nxt.strip()
    return None, 0.0, None


def parse_line(text):
    """Find a line item by arithmetic, not by column layout: some quantity x some rate = some amount
    on the same row. Works whatever the column order, with or without serial numbers, units, HSN or a
    'per' column. If the quantity itself is unreadable (OCR) but amount / rate is a whole number, the
    quantity is inferred and flagged. Falls back to the column regex, so genuine maths errors on an
    invoice are still caught by validation."""
    if SKIP_LINE.match(text):
        return None
    toks = [t.strip("|:;") for t in text.replace("|", " ").split()]
    toks = [t for t in toks if t]
    if len(toks) < 3:
        return None
    serial = 1 if re.fullmatch(r"\d{1,3}\.?", toks[0]) and len(toks) > 4 else 0
    money_idx = [k for k, t in enumerate(toks) if MONEY_RE.fullmatch(t)]
    for a in range(len(money_idx)):
        for b in range(a + 1, len(money_idx)):
            ri, ai = money_idx[a], money_idx[b]
            rate, amount = money(toks[ri]), money(toks[ai])
            if not rate or not amount:
                continue
            # nearest number before the rate, skipping up to 2 unit / junk tokens
            qi, k, skipped = None, ri - 1, 0
            while k > serial and skipped <= 2:
                if NUM.fullmatch(toks[k]):
                    qi = k
                    break
                skipped += 1
                k -= 1
            inferred = False
            if qi is not None:
                qty = float(toks[qi].replace(",", ""))
                if abs(qty * rate - amount) > max(1.0, amount * 0.001):
                    qi = None
            if qi is None:
                q = amount / rate
                if abs(q - round(q)) < 0.001 and round(q) >= 1:
                    qty, inferred = float(round(q)), True
                    k = ri - 1
                    while k > serial and not re.search(r"[A-Za-z]{2,}", toks[k]):
                        k -= 1
                    qi = k + 1
                else:
                    continue
            desc_toks = toks[serial:qi]
            uom = ""
            hsn = None
            between = toks[qi + 1:ri]
            for t in between:
                if re.fullmatch(r"[A-Za-z]{1,6}\.?", t):
                    uom = t.rstrip(".")
            if desc_toks and re.fullmatch(r"\d{4,8}", desc_toks[-1]):
                hsn = desc_toks.pop()
            if inferred and desc_toks and not re.search(r"[A-Za-z]", desc_toks[-1]):
                desc_toks = desc_toks[:-1]
            desc = " ".join(desc_toks).strip(" -|")
            if len(re.findall(r"[A-Za-z]", desc)) < 3:
                continue
            return {"description": desc, "hsn": hsn, "qty": qty, "uom": uom, "unit_price": rate, "amount": amount,
                    "qty_inferred": inferred}
    m = LINE_RE.search(text)
    if m:
        return {"description": m.group(2).strip(" |"), "hsn": m.group(3), "qty": float(m.group(4)), "uom": m.group(5),
                "unit_price": money(m.group(6)), "amount": money(m.group(7)), "qty_inferred": False}
    return None


def offline_extract(doc, buyer_gstin):
    lines = doc["lines"]
    f = {}
    notes = []

    # vendor name: first line that is not a document title
    for text, conf in lines:
        t = text.strip(" .:|")
        if t and re.search(r"[A-Za-z]{3}", t) and t.upper() not in ("TAX INVOICE", "INVOICE", "ORIGINAL FOR RECIPIENT"):
            f["vendor_name"] = _field(re.sub(r"\s+[.:|]+$", "", t), conf, text)
            break

    # GSTINs (with OCR repair)
    gstins = []
    for text, conf in lines:
        for m in re.finditer(r"\b[0-9A-Z]{15}\b", text.upper()):
            g, repaired = repair_gstin(m.group(0))
            if g:
                gstins.append((g, conf * (0.92 if repaired else 1.0), text.strip(), repaired, m.group(0)))
    sup = [x for x in gstins if x[0] != buyer_gstin]
    if sup:
        g, conf, src, repaired, raw = sup[0]
        f["supplier_gstin"] = _field(g, conf, src)
        if repaired:
            notes.append(f"GSTIN read as {raw}, repaired to {g} (checksum verified)")
    by = [x for x in gstins if x[0] == buyer_gstin]
    if by:
        f["buyer_gstin"] = _field(by[0][0], by[0][1], by[0][2])
    elif len(gstins) > 1:
        f["buyer_gstin"] = _field(gstins[1][0], gstins[1][1], gstins[1][2])

    v, c, s = _find_kv(lines, r"(?:Invoice\s*(?:No\.?|Number|#)|Inv\.?\s*No\.?|Bill\s*No\.?)",
                       r"([A-Z0-9][A-Z0-9/\-]{2,})", lambda x: bool(re.search(r"\d", x)) and not parse_date(x))
    if v:
        f["invoice_no"] = _field(v.strip("-/"), c, s)
    v, c, s = _find_kv(lines, r"(?:Invoice\s*Date|Date\s*of\s*Invoice|Inv\.?\s*Date|Bill\s*Date|Dated|Date)", DATE_PAT,
                       lambda x: parse_date(x) is not None)
    if v:
        f["invoice_date"] = _field(parse_date(v), c, s)
    v, c, s = _find_kv(lines, r"(?:P\.?\s?O\.?|Purchase\s+Order|Buyer'?s\s+Order)\s*(?:No\.?|Ref\.?|Number)?",
                       r"(PO[-\s]?\d{3,6})", lambda x: True)
    if not v:   # a PO number printed anywhere else ("your PO PO-3004"), with lower confidence
        v, c, s = _find(lines, r"\b(PO[-\s]?\d{3,6})\b")
        c *= 0.95
    if v:
        f["po_ref"] = _field(re.sub(r"PO[\s]?", "PO-", v.upper()).replace("--", "-"), c, s)

    for key, labels in (("total", ["Grand Total", "Invoice Total", "Total Invoice Value", "Net Amount Payable",
                                   "Amount Payable", "Total Amount", "Amount Due", "Total Due", "Total Payable",
                                   "Invoice Value", "Total"]),
                        ("subtotal", ["Sub Total", "Subtotal", "Taxable Value", "Taxable Amount"])):
        for lab in labels:
            hit = False
            for text, conf in lines:
                if re.match(r"^\W*" + re.escape(lab) + r"\b", text.strip(), re.IGNORECASE):
                    ms = MONEY_RE.findall(text)
                    if ms:
                        f[key] = _field(money(ms[-1]), conf, text.strip())
                        hit = True
                        break
            if hit:
                break

    for tax in ("CGST", "SGST", "IGST"):
        for text, conf in lines:
            if re.match(r"^\W*(?:Add:?\s*|Output\s+|Of which\s+|Less:?\s*)?" + tax + r"\b", text.strip(), re.IGNORECASE):
                ms = MONEY_RE.findall(text)
                if ms:
                    f[tax.lower()] = _field(money(ms[-1]), conf, text.strip())
                    break

    incl = re.search(r"inclusive\s+of\s+(?:GST|tax)", doc["text"], re.IGNORECASE)
    f["prices_include_tax"] = _field(bool(incl), 0.95, incl.group(0) if incl else "no inclusive-of-tax statement")

    v, c, s = _find(lines, r"(?:A/c|Account)\s*No\.?\s*[:\-]?\s*(\d{9,18})")
    if v:
        f["bank_account"] = _field(v, c, s)
    v, c, s = _find(lines, r"IFSC\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4}[0O][A-Z0-9]{6,7})")
    if v:
        v = v.upper()
        if len(v) == 12 and v[4] in "O0" and v[5] in "O0":
            v = v[:4] + v[5:]                                   # OCR doubled the zero
        f["bank_ifsc"] = _field(v[:4] + "0" + v[5:11], c, s)   # 5th char of an IFSC is always zero

    items = []
    for text, conf in lines:
        item = parse_line(text)
        if item:
            if item.pop("qty_inferred", False):
                notes.append(f"Quantity on '{item['description'][:30]}' was unreadable; inferred {item['qty']:g} "
                             "from amount / rate")
                conf = min(conf, 0.84)
            item.update({"confidence": round(conf, 2), "source": text.strip()})
            items.append(item)
    return {"doc_type": classify(doc["text"]), "fields": f, "lines": items, "notes": notes}


# ---------------------------------------------------------------- LLM path
SCHEMA_PROMPT = """You are an accounts-payable extraction engine. Read this vendor document and return ONLY a JSON object:
{
 "doc_type": "invoice" | "credit_note" | "quote" | "statement" | "purchase_order" | "other",
 "fields": {
   "<name>": {"value": <value or null>, "confidence": <0..1>, "source": "<exact text you read it from>"}
 },
 "lines": [{"description": str, "hsn": str|null, "qty": number, "uom": str, "unit_price": number,
            "amount": number, "confidence": 0..1, "source": "<the row as printed>"}]
}
Field names: vendor_name, supplier_gstin, buyer_gstin, invoice_no, invoice_date (YYYY-MM-DD), po_ref,
subtotal, cgst, sgst, igst, total, prices_include_tax (true/false), bank_account, bank_ifsc.
Rules: copy invoice_no and po_ref exactly as printed. Amounts are plain numbers without commas.
unit_price and amount are as printed on the row (do not remove tax). If a field is absent, set value null
and confidence 1.0. Lower confidence when the image is blurred or a character is uncertain. No prose."""


OPENROUTER_DEFAULT_MODEL = "google/gemma-4-31b-it:free"   # free, accepts images
OPENROUTER_FREE_FALLBACKS = ["google/gemma-4-26b-a4b-it:free", "openrouter/free"]   # API allows 3 models in total


def openrouter_models(primary):
    """Primary model first, then free vision fallbacks (override with LLM_FALLBACK_MODELS=a,b,c)."""
    extra = os.environ.get("LLM_FALLBACK_MODELS")
    rest = [m.strip() for m in extra.split(",") if m.strip()] if extra is not None else OPENROUTER_FREE_FALLBACKS
    if not primary.endswith(":free") and primary != "openrouter/free":
        rest = []                                        # paid primary: no silent model switching
    out = []
    for m in [primary] + rest:
        if m not in out:
            out.append(m)
    return out[:3]                                       # OpenRouter rejects more than 3


def llm_config():
    """Pick the provider from the environment. An OpenRouter key (sk-or-...) is recognised
    in OPENROUTER_API_KEY or in any of the other key variables."""
    keys = {k: os.environ.get(k, "").strip() for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    provider = os.environ.get("LLM_PROVIDER", "").lower().strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    or_key = keys["OPENROUTER_API_KEY"] or next((v for v in keys.values() if v.startswith("sk-or-")), "")
    if provider in ("", "openrouter") and or_key:
        return {"provider": "openrouter", "model": model or OPENROUTER_DEFAULT_MODEL, "key": or_key}
    if provider in ("", "anthropic") and keys["ANTHROPIC_API_KEY"]:
        return {"provider": "anthropic", "model": model or "claude-sonnet-5-5", "key": keys["ANTHROPIC_API_KEY"]}
    if provider in ("", "openai") and keys["OPENAI_API_KEY"]:
        return {"provider": "openai", "model": model or "gpt-4.1-mini", "key": keys["OPENAI_API_KEY"]}
    return None


def _page_images(path, max_pages=3):
    doc = pymupdf.open(path)
    out = [base64.b64encode(p.get_pixmap(dpi=150).tobytes("png")).decode() for p in list(doc)[:max_pages]]
    doc.close()
    return out


class LLMError(RuntimeError):
    pass


def _check(r):
    """Raise LLMError with the provider's own error message (e.g. bad key, no credit, unknown model)."""
    if r.status_code < 400:
        return
    meta = {}
    try:
        body = r.json()
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        meta = (err.get("metadata") or {}) if isinstance(err, dict) else {}
    except ValueError:
        msg = r.text[:300]
    if meta.get("raw"):
        msg += " | " + (meta.get("provider_name") or "provider") + ": " + str(meta["raw"])[:200]
    hints = {401: "API key is wrong or revoked", 403: "key lacks permission", 404: "model name not found",
             402: "no credit on the account", 529: "provider overloaded"}
    hint = hints.get(r.status_code, "")
    if r.status_code == 429:
        if r.headers.get("X-RateLimit-Remaining") == "0" or "per-day" in msg.lower() or "free-models-per" in msg.lower():
            hint = "your free quota is used up (20 per minute, 50 per day); wait or add credit"
        else:
            hint = "the free model is busy upstream; not your quota, retry shortly or use another model"
    raise LLMError(f"HTTP {r.status_code}{' (' + hint + ')' if hint else ''}: {msg}")


def _budget(cfg, default):
    """Seconds this HTTP call may take. llm_extract sets cfg['deadline'] so one invoice never waits on
    the model for more than LLM_DEADLINE seconds in total; after that the run continues on OCR."""
    if not cfg.get("deadline"):
        return default
    left = cfg["deadline"] - time.time()
    if left < 5:
        raise LLMError(f"model did not answer within the {os.environ.get('LLM_DEADLINE', '100')} s time budget")
    return min(default, left)


def _call_llm(cfg, prompt, images):
    timeout = _budget(cfg, float(os.environ.get("LLM_TIMEOUT", "60")))
    if cfg["provider"] == "anthropic":
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in images]
        content.append({"type": "text", "text": prompt})
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                       headers={"x-api-key": cfg["key"], "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": cfg["model"], "max_tokens": 4000, "messages": [{"role": "user", "content": content}]})
        _check(r)
        return "".join(b.get("text", "") for b in r.json()["content"])
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}} for b in images]
    content.append({"type": "text", "text": prompt})
    if cfg["provider"] == "openrouter":
        # OpenAI-compatible API. No response_format: not every free model supports it; the prompt asks for JSON.
        # "models" lets OpenRouter fall back to the next model when one is rate-limited or down.
        # Only free models are listed, so a fallback can never bill the account.
        payload = {"model": cfg["model"], "models": openrouter_models(cfg["model"]), "max_tokens": 4000,
                   "temperature": 0, "messages": [{"role": "user", "content": content}]}
        tries = int(os.environ.get("LLM_RETRIES", "2"))
        for attempt in range(tries + 1):
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=_budget(cfg, float(os.environ.get("LLM_TIMEOUT", "90"))),
                           headers={"Authorization": "Bearer " + cfg["key"], "X-Title": "AP Autopilot"}, json=payload)
            if r.status_code not in (429, 502, 503) or attempt == tries:
                break
            if r.headers.get("X-RateLimit-Remaining") == "0":
                break                                   # daily quota gone: retrying will not help
            wait = r.headers.get("Retry-After")
            pause = min(20.0, float(wait)) if wait and wait.replace(".", "").isdigit() else 4.0 * (attempt + 1)
            if cfg.get("deadline") and time.time() + pause > cfg["deadline"] - 5:
                break                                   # no time left in the budget for another try
            time.sleep(pause)
        _check(r)
        body = r.json()
        cfg["used_model"] = body.get("model") or cfg["model"]
        if body.get("error"):
            err = body["error"]
            raise LLMError(f"OpenRouter error: {err.get('message') if isinstance(err, dict) else err}")
        msg = (body.get("choices") or [{}])[0].get("message", {}).get("content")
        if isinstance(msg, list):
            msg = "".join(part.get("text", "") for part in msg if isinstance(part, dict))
        if not msg:
            raise LLMError("OpenRouter returned an empty reply (the free model may be busy; try again)")
        return msg
    r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=timeout,
                   headers={"Authorization": "Bearer " + cfg["key"]},
                   json={"model": cfg["model"], "response_format": {"type": "json_object"},
                         "messages": [{"role": "user", "content": content}]})
    _check(r)
    return r.json()["choices"][0]["message"]["content"]


def parse_llm_json(raw):
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    return json.loads(s[start:end + 1])


def normalise_llm(data):
    """Coerce LLM output into the same shape and types as offline_extract."""
    fields = {}
    notes = []
    for name, f in (data.get("fields") or {}).items():
        if not isinstance(f, dict):
            f = {"value": f, "confidence": 0.8, "source": ""}
        val = f.get("value")
        if val in (None, "", "null"):
            continue
        if name in ("subtotal", "cgst", "sgst", "igst", "total"):
            val = money(val)
        elif name == "invoice_date":
            val = parse_date(val)
        elif name == "prices_include_tax":
            val = val if isinstance(val, bool) else str(val).lower() in ("true", "yes", "1")
        elif name in ("supplier_gstin", "buyer_gstin"):
            g, repaired = repair_gstin(str(val))
            if repaired:
                notes.append(f"GSTIN read as {val}, repaired to {g} (checksum verified)")
            val = g or str(val).upper()
        elif name == "bank_ifsc":
            v = re.sub(r"\s", "", str(val).upper())
            val = v[:4] + "0" + v[5:] if len(v) == 11 else v
        if val is None:
            continue
        fields[name] = {"value": val, "confidence": float(f.get("confidence", 0.8)), "source": str(f.get("source", ""))}
    lines = []
    for l in data.get("lines") or []:
        try:
            lines.append({"description": str(l.get("description", "")), "hsn": l.get("hsn"),
                          "qty": float(money(l.get("qty")) or 0), "uom": l.get("uom") or "",
                          "unit_price": money(l.get("unit_price")), "amount": money(l.get("amount")),
                          "confidence": float(l.get("confidence", 0.8)), "source": str(l.get("source", ""))})
        except (TypeError, ValueError):
            continue
    return {"doc_type": data.get("doc_type", "invoice"), "fields": fields, "lines": lines, "notes": notes}


def verify_against_text(fields, text, source_name):
    """Raise confidence on LLM values that a second, independent reader (the PDF text layer
    or OCR) also found. Two readers agreeing is stronger evidence than a self-reported score."""
    up = re.sub(r"\s+", "", text.upper())
    amounts = {money(m) for m in MONEY_RE.findall(text)}
    dates = set()
    for m in re.finditer(r"\d{1,2}[\-/. ](?:\d{1,2}|[A-Za-z]{3,9})[\-/. ]\d{4}", text):
        d = parse_date(m.group(0))
        if d:
            dates.add(d)
    for k, f in fields.items():
        v = f.get("value")
        ok = False
        if k in ("subtotal", "cgst", "sgst", "igst", "total") and isinstance(v, (int, float)):
            ok = any(a is not None and abs(a - v) < 0.01 for a in amounts)
        elif k == "invoice_date" and v:
            ok = v in dates
        elif k in ("invoice_no", "po_ref", "supplier_gstin", "buyer_gstin", "bank_account", "bank_ifsc") and v:
            ok = re.sub(r"\s+", "", str(v).upper()) in up
        if ok:
            f["confidence"] = max(float(f.get("confidence") or 0), 0.95)
            f["verified"] = f.get("verified") or source_name
    return fields


def llm_extract(path, doc, cfg):
    cfg["deadline"] = time.time() + float(os.environ.get("LLM_DEADLINE", "100"))
    prompt = SCHEMA_PROMPT
    if not doc["is_scan"]:
        prompt += "\n\nText layer of the PDF (use it to confirm digits):\n" + doc["text"][:6000]
    raw = _call_llm(cfg, prompt, _page_images(path))
    try:
        out = normalise_llm(parse_llm_json(raw))
        verify_against_text(out["fields"], doc["text"], "matches OCR" if doc["is_scan"] else "matches PDF text")
        return out
    except (ValueError, KeyError):
        # one retry asking for strict JSON
        raw = _call_llm(cfg, prompt + "\n\nYour previous reply was not valid JSON. Reply with JSON only.",
                        _page_images(path))
        out = normalise_llm(parse_llm_json(raw))
        verify_against_text(out["fields"], doc["text"], "matches OCR" if doc["is_scan"] else "matches PDF text")
        return out


# ------------------------------------------------------------ two-reader reconciliation
def _same(a, b, key=""):
    if key == "vendor_name":
        from rapidfuzz import fuzz
        norm = lambda x: re.sub(r"[^A-Z0-9 ]", " ", str(x).upper().replace("&", " AND "))
        return fuzz.token_set_ratio(norm(a), norm(b)) >= 90
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.01
    return re.sub(r"[\s]", "", str(a).upper()) == re.sub(r"[\s]", "", str(b).upper())


def _plausible(key, v):
    """Format checks a correct value must pass, used to break ties between readers."""
    s = str(v)
    if key in ("supplier_gstin", "buyer_gstin"):
        return gst.is_valid(s)
    if key == "bank_ifsc":
        return bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", s))
    if key == "bank_account":
        return bool(re.fullmatch(r"\d{9,18}", s))
    if key == "po_ref":
        return bool(re.fullmatch(r"PO-\d{3,6}", s.upper()))
    return v not in (None, "")


def lines_reconcile(lines, fields, tol=1.0):
    """True when every line adds up and the lines add up to the stated total."""
    if not lines:
        return False
    for l in lines:
        if l.get("unit_price") is None or l.get("amount") is None or abs(l["qty"] * l["unit_price"] - l["amount"]) > tol:
            return False
    total = (fields.get("total") or {}).get("value")
    if total is None:
        return False
    s = sum(l["amount"] for l in lines)
    if (fields.get("prices_include_tax") or {}).get("value"):
        return abs(s - total) <= tol
    tax = sum((fields.get(k) or {}).get("value") or 0 for k in ("cgst", "sgst", "igst"))
    return abs(s + tax - total) <= tol


def reconcile(llm, off, is_scan):
    """Merge the LLM reading with the text-layer / OCR reading.

    - Both agree            -> accept, high confidence ("LLM and OCR agree").
    - Text PDF, disagree    -> the PDF text layer wins: it is the document's own characters.
    - Scan, disagree        -> the value that passes format checks wins; if both pass, the more
                               confident reader wins and the confidence is capped so a human looks.
    - Only one reader found -> keep it as read.
    Lines: use the set that reconciles (qty x rate = amount, lines + tax = total)."""
    other = "OCR" if is_scan else "PDF text"
    fields, notes = {}, list(llm.get("notes", [])) + list(off.get("notes", []))
    disagreements = []
    for k in set(llm["fields"]) | set(off["fields"]):
        a, b = llm["fields"].get(k), off["fields"].get(k)
        if a and b:
            if _same(a["value"], b["value"], k):
                f = dict(a)
                f["confidence"] = max(float(a.get("confidence") or 0), float(b.get("confidence") or 0), 0.97)
                f["verified"] = "LLM and " + other + " agree"
                f["source"] = b.get("source") or a.get("source")
            else:
                if not is_scan:
                    f = dict(b)
                    f["verified"] = "PDF text (LLM read " + str(a["value"])[:30] + ")"
                else:
                    pa, pb = _plausible(k, a["value"]), _plausible(k, b["value"])
                    if pa != pb:
                        f = dict(a if pa else b)
                    else:
                        f = dict(b if float(b.get("confidence") or 0) >= float(a.get("confidence") or 0) else a)
                        f["confidence"] = min(float(f.get("confidence") or 0), 0.84)
                    f["verified"] = "readers disagree (LLM: " + str(a["value"])[:24] + ", OCR: " + str(b["value"])[:24] + ")"
                disagreements.append(k)
        else:
            f = dict(a or b)
            if b and not a:
                f["verified"] = f.get("verified") or (other + " only")
            elif a and not b and not _plausible(k, a["value"]):
                notes.append(f"LLM read {k.replace('_', ' ')} as '{str(a['value'])[:40]}', which is not a valid "
                             f"{k.replace('_', ' ')} and no other reader found one; ignored")
                continue
        fields[k] = f
    llm_ok, off_ok = lines_reconcile(llm["lines"], fields), lines_reconcile(off["lines"], fields)
    if off_ok and (not llm_ok or not is_scan):
        lines, used = off["lines"], other
    elif llm_ok:
        lines, used = llm["lines"], "LLM"
    else:
        lines, used = (llm["lines"] or off["lines"]) if is_scan else (off["lines"] or llm["lines"]), "best available"
    if disagreements:
        notes.append("LLM and " + other + " disagreed on " + ", ".join(sorted(disagreements)) +
                     "; resolved by rule (see the field table)")
    return {"doc_type": llm.get("doc_type") or off.get("doc_type"), "fields": fields, "lines": lines,
            "notes": notes, "lines_from": used, "disagreements": disagreements}
