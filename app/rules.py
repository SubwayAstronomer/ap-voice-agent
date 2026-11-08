"""Business rules a finance user can change from the UI.

Defaults come from config.yaml. Changes are validated, applied to the live
config (CFG, shared by every stage) and saved in the settings table, so they
survive a restart. Every run uses the rules in force when it starts.
"""
import copy
import json
import os

from . import db
from .pipeline.common import CFG

DEFAULTS = copy.deepcopy(CFG)

# path, label, unit, min, max, step, help
EDITABLE = [
    ("matching.price_tolerance_pct", "Price tolerance over PO", "%", 0, 20, 0.1,
     "How far a unit price may exceed the PO price before the invoice is held for the buyer."),
    ("approval.auto_approve_limit_inr", "Auto-approve limit", "INR", 0, 100000000, 1000,
     "Clean invoices above this amount still need a manager's sign-off."),
    ("extraction.min_confidence", "Minimum read confidence", "0 to 1", 0.5, 0.99, 0.01,
     "A key field read with lower confidence puts the invoice on hold for the AP clerk."),
    ("validation.total_tolerance_inr", "Total rounding tolerance", "INR", 0, 1000, 0.5,
     "Allowed gap between the printed total and lines plus tax (rounding)."),
    ("duplicates.near_total_pct", "Near-duplicate amount window", "%", 0, 10, 0.1,
     "Same vendor, a different invoice number, and a total within this percentage is a possible duplicate."),
    ("duplicates.near_days", "Near-duplicate date window", "days", 0, 365, 1,
     "...and dated within this many days of the earlier invoice."),
    ("po_inference.min_score", "PO inference: minimum score", "0 to 100", 0, 100, 1,
     "With no PO on the invoice, the best open PO must score at least this much."),
    ("po_inference.min_margin", "PO inference: winning margin", "points", 0, 100, 1,
     "...and beat the runner-up by this many points, otherwise the buyer decides."),
    ("vendor.fuzzy_name_min", "Vendor name match threshold", "0 to 100", 50, 100, 1,
     "Similarity needed to match a vendor by name when the GSTIN is missing."),
    ("demo.stage_delay_seconds", "Pause between stages (demo only)", "seconds", 0, 5, 0.1,
     "Slows the live view so each stage is visible. Set 0 for full speed."),
]
_META = {p: (label, unit, lo, hi, step, help_) for p, label, unit, lo, hi, step, help_ in EDITABLE}


def _get(cfg, path):
    node = cfg
    for part in path.split("."):
        node = node[part]
    return node


def _set(path, value):
    node = CFG
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _saved():
    row = db.q("SELECT value FROM settings WHERE key='rules'", one=True)
    return json.loads(row["value"]) if row else {}


def load():
    """Apply saved overrides at startup. DEMO_STAGE_DELAY in the environment sets the
    demo pause unless it was changed in the UI."""
    saved = _saved()
    if "demo.stage_delay_seconds" not in saved and os.environ.get("DEMO_STAGE_DELAY"):
        try:
            _set("demo.stage_delay_seconds", float(os.environ["DEMO_STAGE_DELAY"]))
        except ValueError:
            pass
    for path, value in saved.items():
        if path in _META:
            _set(path, value)


def listing():
    out = []
    for path, label, unit, lo, hi, step, help_ in EDITABLE:
        out.append({"key": path, "label": label, "unit": unit, "min": lo, "max": hi, "step": step, "help": help_,
                    "value": _get(CFG, path), "default": _get(DEFAULTS, path)})
    return out


def update(changes):
    """Validate every change first; apply all or none. Returns (ok, errors)."""
    errors, clean = [], {}
    for path, raw in (changes or {}).items():
        if path not in _META:
            errors.append(f"{path}: not an editable rule")
            continue
        label, unit, lo, hi, step, _ = _META[path]
        try:
            v = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{label}: '{raw}' is not a number")
            continue
        if not (lo <= v <= hi):
            errors.append(f"{label}: must be between {lo} and {hi}")
            continue
        clean[path] = int(v) if float(step).is_integer() and v.is_integer() else v
    if errors:
        return False, errors
    saved = _saved()
    for path, v in clean.items():
        _set(path, v)
        if v == _get(DEFAULTS, path):
            saved.pop(path, None)
        else:
            saved[path] = v
    db.ex("INSERT OR REPLACE INTO settings (key, value) VALUES ('rules', ?)", (json.dumps(saved),))
    return True, []


def reset():
    for path in _META:
        _set(path, copy.deepcopy(_get(DEFAULTS, path)))
    db.ex("DELETE FROM settings WHERE key='rules'")
    load()
