"""Shared helpers: config, flags, parsing and normalisation."""
import os
import re
from datetime import date, datetime

import yaml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(HERE, "config.yaml")) as _f:
    CFG = yaml.safe_load(_f)
try:  # DEMO_STAGE_DELAY in the environment sets the starting demo pause (the Rules page can change it)
    CFG["demo"]["stage_delay_seconds"] = float(os.environ["DEMO_STAGE_DELAY"])
except (KeyError, ValueError):
    pass

STAGES = [
    (1, "intake", "Intake and classify"),
    (2, "extract", "Extract fields"),
    (3, "validate", "Validate the invoice itself"),
    (4, "vendor", "Vendor and duplicate check"),
    (5, "po", "Find the PO"),
    (6, "match", "3-way match"),
    (7, "decide", "Decision engine"),
]

# Reason codes: severity and who acts on them.
CODES = {
    "NOT_INVOICE": ("REJECT", "Vendor"),
    "CREDIT_NOTE": ("HOLD", "AP clerk"),
    "UNREADABLE": ("HOLD", "AP clerk"),
    "MISSING_FIELD": ("REJECT", "Vendor"),
    "LOW_CONFIDENCE": ("HOLD", "AP clerk"),
    "TOTAL_MISMATCH": ("HOLD", "AP clerk"),
    "LINE_MATH": ("HOLD", "AP clerk"),
    "TAX_TYPE": ("HOLD", "AP clerk"),
    "GSTIN_INVALID": ("HOLD", "AP clerk"),
    "WRONG_BUYER": ("REJECT", "Vendor"),
    "DATE_INVALID": ("HOLD", "AP clerk"),
    "VENDOR_UNKNOWN": ("HOLD", "Vendor master"),
    "VENDOR_GSTIN_MISMATCH": ("HOLD", "Vendor master"),
    "BANK_CHANGED": ("HOLD", "Vendor master"),
    "DUPLICATE": ("REJECT", "Vendor"),
    "POSSIBLE_DUPLICATE": ("HOLD", "AP clerk"),
    "PO_NOT_FOUND": ("HOLD", "Buyer"),
    "PO_VENDOR_MISMATCH": ("HOLD", "Buyer"),
    "PO_AMBIGUOUS": ("HOLD", "Buyer"),
    "PRICE_VARIANCE": ("HOLD", "Buyer"),
    "QTY_EXCEEDS_GRN": ("HOLD", "Buyer"),
    "PO_OVERBILLED": ("HOLD", "Buyer"),
    "UNMATCHED_LINE": ("HOLD", "Buyer"),
    "ABOVE_LIMIT": ("HOLD", "Manager"),
    # informational: recorded and explained, never change the outcome
    "PO_INFERRED": ("INFO", None),
    "PRICE_WITHIN_TOLERANCE": ("INFO", None),
    "OCR_REPAIRED": ("INFO", None),
    "READERS_DISAGREE": ("INFO", None),
    "LLM_FALLBACK": ("INFO", None),
    "VENDOR_MATCHED_BY_NAME": ("INFO", None),
}


def flag(code, message, stage, **extra):
    sev, owner = CODES[code]
    f = {"code": code, "severity": sev, "owner": owner, "message": message, "stage": stage}
    f.update(extra)
    return f


MONEY_RE = re.compile(r"(?<![\d.,])\d[\d,]*\.\d{2}(?![\d])")


def money(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = MONEY_RE.search(str(s)) or re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def norm_invoice_no(s):
    """'INV/2026/0142' and 'INV-2026-142' both become 'INV2026142'."""
    if not s:
        return ""
    tokens = re.findall(r"[A-Za-z]+|\d+", str(s).upper())
    return "".join(str(int(t)) if t.isdigit() else t for t in tokens)


_DATE_FORMATS = ["%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y",
                 "%Y-%m-%d", "%d/%b/%Y", "%b %d, %Y", "%B %d, %Y"]


def parse_date(s):
    if not s:
        return None
    if isinstance(s, (date, datetime)):
        return s.strftime("%Y-%m-%d")
    s = re.sub(r"\s+", " ", str(s).strip().rstrip(".,"))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def today_iso():
    return os.environ.get("AP_TODAY") or date.today().isoformat()


def fmt_inr(x):
    if x is None:
        return "-"
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "INR " + whole + "." + frac
