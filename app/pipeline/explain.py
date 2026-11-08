"""Turning reason codes into words: explanation, questions for owners,
vendor email draft and the ERP posting payload."""
from datetime import datetime, timedelta

from .common import fmt_inr

QUESTIONS = {
    "CREDIT_NOTE": "This is a credit note. Apply it against the vendor's open invoices manually.",
    "UNREADABLE": "The scan could not be read. Ask the vendor for a clearer copy or key it in manually.",
    "LOW_CONFIDENCE": "Check the low-confidence fields against the PDF and correct them if needed.",
    "TOTAL_MISMATCH": "The printed total does not equal lines plus tax. Confirm the correct amount with the vendor.",
    "LINE_MATH": "A line's quantity x rate does not equal its amount. Confirm which figure is right.",
    "TAX_TYPE": "Tax type does not fit the supply (CGST+SGST within a state, IGST across states). Confirm with tax team.",
    "GSTIN_INVALID": "Supplier GSTIN fails the format or checksum. Verify it on the GST portal.",
    "DATE_INVALID": "The invoice date is in the future or before the PO date. Confirm the correct date.",
    "VENDOR_UNKNOWN": "Vendor is not in the master. Onboard them or confirm who sent this.",
    "VENDOR_GSTIN_MISMATCH": "Name matches a vendor but the GSTIN does not. Confirm which entity is billing.",
    "BANK_CHANGED": "Call the vendor on the phone number already in the vendor master (not one on the invoice) "
                    "to confirm the new bank account before updating it.",
    "POSSIBLE_DUPLICATE": "A similar invoice (same vendor, similar amount and date) exists. Confirm this is a new bill.",
    "PO_NOT_FOUND": "No matching PO. Raise or identify the PO, or confirm this is a non-PO purchase.",
    "PO_VENDOR_MISMATCH": "The PO quoted belongs to a different vendor. Confirm the correct PO.",
    "PO_AMBIGUOUS": "More than one open PO fits. Tell us which PO this invoice is against.",
    "PRICE_VARIANCE": "Unit price is above PO beyond tolerance. Approve the new price or ask for a revised invoice.",
    "QTY_EXCEEDS_GRN": "More is billed than has been received. Check for a pending GRN, or ask for a credit note.",
    "PO_OVERBILLED": "This invoice takes billing past the PO quantity. Amend the PO or reject the excess.",
    "UNMATCHED_LINE": "A line has no matching PO line (e.g. freight). Approve it as an extra or ask to remove it.",
    "ABOVE_LIMIT": "Clean invoice above the auto-approval limit. Manager sign-off needed.",
}


def explanation(ctx, decision):
    f = ctx.get("fields", {})
    inv_no = (f.get("invoice_no") or {}).get("value", "(no number)")
    vendor = (ctx.get("vendor") or {}).get("legal_name") or (f.get("vendor_name") or {}).get("value", "unknown vendor")
    total = (f.get("total") or {}).get("value")
    po = (ctx.get("po") or {}).get("po_number")
    flags = ctx["flags"]
    info = [x["message"] for x in flags if x["severity"] == "INFO"]
    holds = [x for x in flags if x["severity"] == "HOLD"]
    rejects = [x for x in flags if x["severity"] == "REJECT"]
    head = f"Invoice {inv_no} from {vendor} for {fmt_inr(total)}"
    if decision == "APPROVE":
        n = len(ctx.get("match_rows", []))
        s = f"Approved. {head} matches {po} and its goods receipts on all {n} line(s); vendor, bank, tax and duplicate checks passed."
    elif decision == "HOLD":
        owners = ", ".join(dict.fromkeys(x["owner"] for x in holds))
        s = f"On hold for {owners}. {head} needs a person on {len(holds)} point(s): " + \
            " ".join(x["message"] for x in holds)
    else:
        s = f"Rejected. {head} cannot be paid as submitted: " + " ".join(x["message"] for x in rejects)
        if holds:
            other = ", ".join(dict.fromkeys(x["code"] for x in holds))
            s += f" {len(holds)} other issue(s) were also found ({other}); see the stage details."
    if info:
        s += " Notes: " + " ".join(info)
    return s


def next_action(ctx, decision):
    f = ctx.get("fields", {})
    val = lambda k: (f.get(k) or {}).get("value")
    vendor = ctx.get("vendor") or {}
    if decision == "APPROVE":
        inv_date = val("invoice_date")
        due = None
        if inv_date:
            due = (datetime.strptime(inv_date, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
        tax = sum(val(k) or 0 for k in ("cgst", "sgst", "igst"))
        return {"type": "post_to_erp", "title": "Post to ERP and schedule payment",
                "payload": {"vendor_id": vendor.get("vendor_id"), "vendor_gstin": vendor.get("gstin"),
                            "invoice_no": val("invoice_no"), "invoice_date": inv_date,
                            "po_number": (ctx.get("po") or {}).get("po_number"),
                            "gross_amount": val("total"), "tax_amount": round(tax, 2),
                            "pay_to_account": "xxxx" + str(vendor.get("bank_account", ""))[-4:],
                            "payment_due": due, "terms": "Net 30"}}
    if decision == "HOLD":
        holds = [x for x in ctx["flags"] if x["severity"] == "HOLD"]
        return {"type": "exception_queue", "title": "Route to exception queue",
                "owners": list(dict.fromkeys(x["owner"] for x in holds)),
                "questions": [{"owner": x["owner"], "code": x["code"],
                               "question": QUESTIONS.get(x["code"], x["message"])} for x in holds]}
    rejects = [x for x in ctx["flags"] if x["severity"] == "REJECT"]
    name = vendor.get("legal_name") or val("vendor_name") or "Vendor"
    bullets = "\n".join(f"  - {x['message']}" for x in rejects)
    body = (f"Dear {name} accounts team,\n\n"
            f"We are unable to process invoice {val('invoice_no') or '(unnumbered)'} dated "
            f"{val('invoice_date') or '-'} for {fmt_inr(val('total'))} for the following reason(s):\n\n"
            f"{bullets}\n\n"
            "Please reply with a corrected invoice or the missing details. If this invoice was sent "
            "in error, no action is needed.\n\nRegards,\nAccounts Payable, Meridian Steel Works Pvt Ltd")
    return {"type": "vendor_email", "title": "Draft email to vendor (send after clerk review)",
            "subject": f"Invoice {val('invoice_no') or ''} - unable to process", "body": body}
