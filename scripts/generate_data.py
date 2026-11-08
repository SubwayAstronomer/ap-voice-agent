"""Generate the demo dataset: vendor master, POs, GRNs and 10 invoice PDFs.

Four vendor templates with different layouts. Three invoices are rendered
as phone-style scans (image-only PDFs) to exercise OCR / vision extraction.

Usage:  python scripts/generate_data.py
"""
import io
import json
import os
import random
import sys

from PIL import Image, ImageFilter
import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.gst import make_gstin  # noqa: E402

SEED = os.path.join(ROOT, "data", "seed")
INV = os.path.join(ROOT, "data", "invoices")

BUYER = {
    "name": "Meridian Steel Works Pvt Ltd",
    "address": "Plot 14, Industrial Estate, Kalinganagar, Odisha 755026",
    "gstin": make_gstin("21", "AAKCM4821H"),
}

VENDORS = [
    {"vendor_id": "V001", "legal_name": "Apex Industrial Supplies Pvt Ltd", "aliases": ["Apex Industrial", "AIS"],
     "gstin": make_gstin("21", "AAFCA3317K"), "bank_account": "50200012345678", "bank_ifsc": "HDFC0001234",
     "bank_name": "HDFC Bank, Bhubaneswar", "address": "22 Rasulgarh Industrial Area, Bhubaneswar, Odisha 751010"},
    {"vendor_id": "V002", "legal_name": "Bharat Bearings and Transmission Co", "aliases": ["Bharat Bearings", "BBT"],
     "gstin": make_gstin("27", "AABFB6620M"), "bank_account": "001105022871", "bank_ifsc": "ICIC0000011",
     "bank_name": "ICICI Bank, Pune", "address": "Gat No 118, Chakan MIDC, Pune, Maharashtra 410501"},
    {"vendor_id": "V003", "legal_name": "Coastal Refractories Ltd", "aliases": ["Coastal Refractories"],
     "gstin": make_gstin("19", "AACCC9054P"), "bank_account": "3398201004471", "bank_ifsc": "CNRB0003398",
     "bank_name": "Canara Bank, Durgapur", "address": "NH-2, Bamunara Industrial Park, Durgapur, West Bengal 713212"},
    {"vendor_id": "V004", "legal_name": "Deccan Safety and Consumables", "aliases": ["Deccan Safety", "DSC"],
     "gstin": make_gstin("36", "AAJFD2271Q"), "bank_account": "62118870021", "bank_ifsc": "SBIN0020871",
     "bank_name": "State Bank of India, Hyderabad", "address": "4-1-77 Balanagar, Hyderabad, Telangana 500037"},
    {"vendor_id": "V005", "legal_name": "Eastern Hydraulics Pvt Ltd", "aliases": ["Eastern Hydraulics"],
     "gstin": make_gstin("21", "AADCE5512L"), "bank_account": "918020055501", "bank_ifsc": "UTIB0000102",
     "bank_name": "Axis Bank, Cuttack", "address": "Jagatpur Industrial Estate, Cuttack, Odisha 754021"},
    {"vendor_id": "V006", "legal_name": "Falcon Electricals", "aliases": ["Falcon Electric"],
     "gstin": make_gstin("20", "AAHFF7734D"), "bank_account": "1120001093340", "bank_ifsc": "PUNB0112000",
     "bank_name": "Punjab National Bank, Jamshedpur", "address": "Adityapur Industrial Area, Jamshedpur, Jharkhand 831013"},
]
for v in VENDORS:
    v["state_code"] = v["gstin"][:2]
    v["status"] = "active"
VMAP = {v["vendor_id"]: v for v in VENDORS}


def line(no, code, desc, hsn, qty, uom, price, gst=18):
    return {"line_no": no, "item_code": code, "description": desc, "hsn": hsn, "qty": qty, "uom": uom,
            "unit_price": price, "gst_rate": gst}


POS = [
    {"po_number": "PO-1001", "vendor_id": "V001", "po_date": "2026-09-22", "lines": [
        line(1, "HYD-HOSE-12", "Hydraulic hose assembly 1/2 in x 2 m", "4009", 40, "Nos", 1850.00),
        line(2, "GAUGE-250", "Pressure gauge 0-250 bar glycerine filled", "9026", 12, "Nos", 2400.00),
        line(3, "BOLT-M16", "Hex bolt M16 x 80 HT 8.8 with nut", "7318", 500, "Nos", 38.50)]},
    {"po_number": "PO-1002", "vendor_id": "V001", "po_date": "2026-09-25", "lines": [
        line(1, "GRS-EP2-18", "Grease lithium complex EP2 18 kg bucket", "2710", 20, "Nos", 4200.00),
        line(2, "SLING-20", "Wire rope sling 20 mm x 3 m", "7312", 10, "Nos", 3150.00)]},
    {"po_number": "PO-1003", "vendor_id": "V001", "po_date": "2026-09-30", "lines": [
        line(1, "SOL-VLV-24", "Pneumatic solenoid valve 1/2 in 24 VDC", "8481", 15, "Nos", 5600.00),
        line(2, "PU-TUBE-8", "PU tube 8 mm x 100 m roll", "3917", 6, "Nos", 2750.00)]},
    {"po_number": "PO-1043", "vendor_id": "V002", "po_date": "2026-09-15", "lines": [
        line(1, "BRG-6310", "Deep groove ball bearing 6310 2RS", "8482", 100, "Nos", 2450.00),
        line(2, "SLV-H310", "Bearing adapter sleeve H310", "8483", 100, "Nos", 640.00)]},
    {"po_number": "PO-2001", "vendor_id": "V003", "po_date": "2026-09-10", "lines": [
        line(1, "MC-BRK-150", "Magnesia carbon brick MC-10 150 mm", "6902", 1200, "Nos", 168.00),
        line(2, "CAST-HA70", "High alumina castable 70 pct 25 kg bag", "3816", 80, "Bags", 1450.00)]},
    {"po_number": "PO-3001", "vendor_id": "V004", "po_date": "2026-09-28", "lines": [
        line(1, "PPE-HLM", "Safety helmet with ratchet ISI marked", "6506", 200, "Nos", 250.00),
        line(2, "PPE-GLV", "Leather safety gloves", "4203", 300, "Pairs", 140.00),
        line(3, "PPE-GGL", "Safety goggles anti-fog", "9004", 150, "Nos", 120.00)]},
    {"po_number": "PO-3002", "vendor_id": "V004", "po_date": "2026-10-01", "lines": [
        line(1, "CW-WHT-25", "Cotton waste white 25 kg bale", "5202", 40, "Nos", 1100.00),
        line(2, "WG-SH11", "Welding glass shade 11", "7007", 500, "Nos", 18.00)]},
    {"po_number": "PO-3003", "vendor_id": "V004", "po_date": "2026-10-03", "lines": [
        line(1, "CW-WHT-25", "Cotton waste white 25 kg bale", "5202", 40, "Nos", 1100.00),
        line(2, "WG-SH11", "Welding glass shade 11", "7007", 500, "Nos", 18.00)]},
    {"po_number": "PO-3004", "vendor_id": "V004", "po_date": "2026-10-05", "lines": [
        line(1, "FE-ABC-6", "Fire extinguisher ABC 6 kg", "8424", 20, "Nos", 2650.00)]},
    {"po_number": "PO-4001", "vendor_id": "V005", "po_date": "2026-10-02", "lines": [
        line(1, "HYD-PMP-40", "Hydraulic gear pump 40 cc", "8413", 4, "Nos", 38500.00)]},
    {"po_number": "PO-5001", "vendor_id": "V006", "po_date": "2026-10-06", "lines": [
        line(1, "CBL-4C-16", "Armoured cable 4 core 16 sq mm", "8544", 500, "Mtr", 412.00)]},
]
for p in POS:
    p["status"] = "open"
    p["currency"] = "INR"
PMAP = {p["po_number"]: p for p in POS}

GRNS = []


def grn(no, po, date, qtys):
    for ln, q in qtys.items():
        GRNS.append({"grn_number": no, "po_number": po, "line_no": ln, "qty_received": q, "received_date": date})


grn("GRN-5001", "PO-1001", "2026-10-09", {1: 40, 2: 12, 3: 500})
grn("GRN-5002", "PO-1002", "2026-10-14", {1: 20, 2: 10})
grn("GRN-5003", "PO-1003", "2026-10-17", {1: 15, 2: 6})
grn("GRN-5101", "PO-1043", "2026-10-06", {1: 60, 2: 60})
grn("GRN-5102", "PO-1043", "2026-10-18", {1: 40, 2: 40})
grn("GRN-5201", "PO-2001", "2026-10-03", {1: 1200, 2: 80})
grn("GRN-5301", "PO-3001", "2026-10-13", {1: 200, 2: 300, 3: 150})
grn("GRN-5302", "PO-3002", "2026-10-16", {1: 40, 2: 500})
grn("GRN-5303", "PO-3003", "2026-10-19", {1: 40, 2: 500})
grn("GRN-5304", "PO-3004", "2026-10-20", {1: 20})


# ---------------------------------------------------------------- formatting
def inr(x):
    """Indian digit grouping: 144019.5 -> 1,44,019.50"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + whole + "." + frac


def west(x):
    return f"{x:,.2f}"


def r2(x):
    return round(x + 1e-9, 2)


STY = {
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, leading=18),
    "h1c": ParagraphStyle("h1c", fontName="Helvetica-Bold", fontSize=14, leading=17),
    "p": ParagraphStyle("p", fontName="Helvetica", fontSize=9, leading=12),
    "pc": ParagraphStyle("pc", fontName="Helvetica", fontSize=9.5, leading=12.5),
    "t": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=12, leading=15),
    "tc": ParagraphStyle("tc", fontName="Helvetica-Bold", fontSize=12, leading=15),
}


def P(text, s="p"):
    return Paragraph(text, STY[s])


def build(path, story):
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    doc.build(story)


GRID = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
    ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
])


def kv_table(rows, widths, font="Helvetica", size=9):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), size),
                           ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    return t


# ---------------------------------------------------------------- templates
def template_a(path, inv):
    """Apex: clean table, PO in header, CGST + SGST summary, Indian grouping."""
    v = VMAP[inv["vendor_id"]]
    lines = inv["lines"]
    sub = r2(sum(l["qty"] * l["rate"] for l in lines))
    cg = r2(sub * 0.09)
    total = r2(sub + 2 * cg)
    st = [P(v["legal_name"], "h1"), P(v["address"]), P(f"GSTIN: {v['gstin']}"), Spacer(1, 6),
          P("TAX INVOICE", "t"), Spacer(1, 4)]
    hdr = [[P(f"<b>Bill To:</b> {BUYER['name']}<br/>{BUYER['address']}<br/>GSTIN: {BUYER['gstin']}"),
            P(f"Invoice No: {inv['invoice_no']}<br/>Invoice Date: {inv['date_text']}<br/>PO No: {inv['po_ref']}")]]
    t = Table(hdr, colWidths=[110 * mm, 68 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    st += [t, Spacer(1, 8)]
    rows = [["Sl", "Description", "HSN", "Qty", "UoM", "Rate (INR)", "Amount (INR)"]]
    for i, l in enumerate(lines, 1):
        rows.append([str(i), l["desc"], l["hsn"], str(l["qty"]), l["uom"], inr(l["rate"]), inr(r2(l["qty"] * l["rate"]))])
    t = Table(rows, colWidths=[9 * mm, 72 * mm, 14 * mm, 13 * mm, 12 * mm, 26 * mm, 32 * mm])
    t.setStyle(GRID)
    st += [t, Spacer(1, 6)]
    st.append(kv_table([["Sub Total", inr(sub)], ["CGST @ 9%", inr(cg)], ["SGST @ 9%", inr(cg)],
                        ["Grand Total", inr(total)]], [50 * mm, 32 * mm]))
    st += [Spacer(1, 4), P(f"Amount in words: see Grand Total above"), Spacer(1, 8)]
    bank = inv.get("bank_override") or {"account": v["bank_account"], "ifsc": v["bank_ifsc"], "bank": v["bank_name"]}
    if inv.get("bank_note"):
        st.append(P(f"<b>{inv['bank_note']}</b>"))
    st.append(P(f"Bank Details: {bank['bank']} | A/c No: {bank['account']} | IFSC: {bank['ifsc']}"))
    st += [Spacer(1, 14), P(f"For {v['legal_name']}"), Spacer(1, 16), P("Authorised Signatory")]
    build(path, st)
    return {"subtotal": sub, "tax": r2(2 * cg), "total": total}


def template_b(path, inv):
    """Bharat: itemised with IGST per line, western grouping, 'Buyer's Order No.'"""
    v = VMAP[inv["vendor_id"]]
    lines = inv["lines"]
    st = [P("TAX INVOICE", "t"), P(v["legal_name"], "h1"), P(v["address"]), P(f"GSTIN/UIN: {v['gstin']}"),
          Spacer(1, 6)]
    hdr = [[P(f"Inv. No.: {inv['invoice_no']}"), P(f"Dated: {inv['date_text']}")],
           [P(f"Buyer's Order No.: {inv['po_ref']}"), P("Mode of Despatch: Road (LR 22871)")],
           [P(f"Buyer: {BUYER['name']}, GSTIN {BUYER['gstin']}"), P("Place of Supply: Odisha (21)")]]
    t = Table(hdr, colWidths=[110 * mm, 68 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey)]))
    st += [t, Spacer(1, 8)]
    rows = [["Sl", "Item Description", "HSN", "Qty", "UoM", "Rate", "Taxable", "IGST", "IGST Amt"]]
    sub = tax = 0.0
    for i, l in enumerate(lines, 1):
        val = r2(l["qty"] * l["rate"])
        ig = r2(val * 0.18)
        sub += val
        tax += ig
        rows.append([str(i), l["desc"], l["hsn"], str(l["qty"]), l["uom"], west(l["rate"]), west(val), "18%", west(ig)])
    sub, tax = r2(sub), r2(tax)
    total = r2(sub + tax)
    t = Table(rows, colWidths=[8 * mm, 56 * mm, 13 * mm, 11 * mm, 11 * mm, 19 * mm, 23 * mm, 12 * mm, 22 * mm])
    t.setStyle(GRID)
    st += [t, Spacer(1, 6)]
    st.append(kv_table([["Taxable Value", west(sub)], ["IGST", west(tax)], ["Invoice Total", west(total)]],
                       [50 * mm, 32 * mm]))
    st += [Spacer(1, 8), P(f"Our Bankers: {v['bank_name']}, A/c No: {v['bank_account']}, IFSC: {v['bank_ifsc']}"),
           Spacer(1, 6), P("Subject to Pune jurisdiction. E. and O.E."), Spacer(1, 16),
           P(f"for {v['legal_name']}")]
    build(path, st)
    return {"subtotal": sub, "tax": tax, "total": total}


def template_c(path, inv):
    """Coastal: no HSN column, tax only in the footer, 'Invoice Number'."""
    v = VMAP[inv["vendor_id"]]
    lines = inv["lines"]
    sub = r2(sum(l["qty"] * l["rate"] for l in lines))
    ig = r2(sub * 0.18)
    total = r2(sub + ig)
    st = [P(v["legal_name"], "h1"), P(v["address"] + f" | GSTIN {v['gstin']}"), Spacer(1, 10),
          P("INVOICE", "t"), Spacer(1, 4),
          P(f"Invoice Number: {inv['invoice_no']}"), P(f"Date: {inv['date_text']}"),
          P(f"Purchase Order: {inv['po_ref']}"), Spacer(1, 4),
          P(f"Consignee: {BUYER['name']}, {BUYER['address']} (GSTIN {BUYER['gstin']})"), Spacer(1, 8)]
    rows = [["Sl", "Description of Goods", "", "Qty", "Unit", "Rate", "Amount"]]
    for i, l in enumerate(lines, 1):
        rows.append([str(i), l["desc"], "", str(l["qty"]), l["uom"], inr(l["rate"]), inr(r2(l["qty"] * l["rate"]))])
    t = Table(rows, colWidths=[9 * mm, 80 * mm, 2 * mm, 15 * mm, 13 * mm, 24 * mm, 35 * mm])
    t.setStyle(GRID)
    st += [t, Spacer(1, 6)]
    st.append(kv_table([["Sub Total", inr(sub)], ["Add: IGST @ 18%", inr(ig)], ["Total Invoice Value", inr(total)]],
                       [55 * mm, 35 * mm]))
    st += [Spacer(1, 10), P(f"Please remit to {v['bank_name']}, Account No: {v['bank_account']}, IFSC: {v['bank_ifsc']}"),
           Spacer(1, 16), P("Authorised Signatory")]
    build(path, st)
    return {"subtotal": sub, "tax": ig, "total": total}


def template_d(path, inv):
    """Deccan: upper-case letterhead, prices inclusive of GST, 'Bill No', often no PO."""
    v = VMAP[inv["vendor_id"]]
    lines = inv["lines"]
    total = r2(sum(l["qty"] * l["rate_incl"] for l in lines))
    tax = r2(total - total / 1.18)
    st = [P(v["legal_name"].upper().replace(" AND ", " & "), "h1c"), P(v["address"], "pc"),
          P(f"GSTIN: {v['gstin']}", "pc"), Spacer(1, 8), P("TAX INVOICE", "tc"), Spacer(1, 4),
          P(f"Bill No: {inv['invoice_no']}", "pc"), P(f"Bill Date: {inv['date_text']}", "pc"),
          P(f"To: {BUYER['name']}", "pc"), P(f"GSTIN: {BUYER['gstin']}", "pc"),
          P(f"Ref: {inv['ref_text']}", "pc"), Spacer(1, 8)]
    rows = [["Sl", "Particulars", "", "Qty", "Unit", "Rate", "Amount"]]
    for i, l in enumerate(lines, 1):
        rows.append([str(i), l["desc"], "", str(l["qty"]), l["uom"], inr(l["rate_incl"]), inr(r2(l["qty"] * l["rate_incl"]))])
    t = Table(rows, colWidths=[9 * mm, 76 * mm, 2 * mm, 14 * mm, 14 * mm, 24 * mm, 34 * mm])
    style = TableStyle(GRID.getCommands())
    style.add("FONTNAME", (0, 1), (-1, -1), "Helvetica")
    style.add("FONTSIZE", (0, 0), (-1, -1), 9)
    t.setStyle(style)
    st += [t, Spacer(1, 6), P("All prices are inclusive of GST @ 18%", "pc"),
           P(f"Of which IGST @ 18%: {inr(tax)}", "pc"), P(f"Net Amount Payable: {inr(total)}", "pc"),
           Spacer(1, 8), P(f"Bank: {v['bank_name']} A/c No: {v['bank_account']} IFSC: {v['bank_ifsc']}", "pc"),
           Spacer(1, 16), P("Thank you for your business", "pc")]
    build(path, st)
    return {"subtotal": r2(total - tax), "tax": tax, "total": total}


TEMPLATES = {"A": template_a, "B": template_b, "C": template_c, "D": template_d}


def scanify(path, seed):
    """Render a PDF page to an image and degrade it like a phone scan."""
    rnd = random.Random(seed)
    doc = pymupdf.open(path)
    pix = doc[0].get_pixmap(dpi=200)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    doc.close()
    img = img.rotate(rnd.uniform(-0.9, 0.9), expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.002)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = rnd.randrange(90, 200)
    img = img.point(lambda p: min(255, int(p * 0.94 + 10)))
    img.save(path, "PDF", resolution=200)


def pl(po, ln, qty=None, rate=None):
    src = PMAP[po]["lines"][ln - 1]
    q = src["qty"] if qty is None else qty
    r = src["unit_price"] if rate is None else rate
    return {"desc": src["description"], "hsn": src["hsn"], "qty": q, "uom": src["uom"], "rate": r,
            "rate_incl": r2(r * 1.18)}


INVOICES = [
    {"file": "01_happy_path.pdf", "tpl": "A", "vendor_id": "V001", "invoice_no": "AIS/26-27/0418",
     "date_text": "12-Oct-2026", "po_ref": "PO-1001", "lines": [pl("PO-1001", 1), pl("PO-1001", 2), pl("PO-1001", 3)],
     "scenario": "Happy path: clean text PDF, explicit PO, exact match",
     "expect": {"decision": "APPROVE", "codes": []}},
    {"file": "02_split_A.pdf", "tpl": "B", "vendor_id": "V002", "invoice_no": "BBT-7731", "date_text": "08.10.2026",
     "po_ref": "PO-1043", "lines": [pl("PO-1043", 1, 60), pl("PO-1043", 2, 60)],
     "scenario": "Edge 1a: PO-1043 first delivery, 60 of 100 units",
     "expect": {"decision": "APPROVE", "codes": []}},
    {"file": "03_split_B.pdf", "tpl": "B", "vendor_id": "V002", "invoice_no": "BBT-7764", "date_text": "20.10.2026",
     "po_ref": "PO-1043", "lines": [pl("PO-1043", 1, 40), pl("PO-1043", 2, 40)],
     "scenario": "Edge 1b: PO-1043 second delivery, remaining 40 units",
     "expect": {"decision": "APPROVE", "codes": []}},
    {"file": "04_split_C_overbill.pdf", "tpl": "B", "vendor_id": "V002", "invoice_no": "BBT-7790",
     "date_text": "27.10.2026", "po_ref": "PO-1043", "lines": [pl("PO-1043", 1, 20)],
     "scenario": "Edge 1c: 20 more bearings on a PO that is already fully billed",
     "expect": {"decision": "HOLD", "codes": ["QTY_EXCEEDS_GRN", "PO_OVERBILLED"]}},
    {"file": "05_original_0142.pdf", "tpl": "C", "vendor_id": "V003", "invoice_no": "INV/2026/0142",
     "date_text": "05/10/2026", "po_ref": "PO-2001", "lines": [pl("PO-2001", 1), pl("PO-2001", 2)],
     "scenario": "Edge 2a: the original refractory invoice (run before 06)",
     "expect": {"decision": "APPROVE", "codes": []}},
    {"file": "06_dup_scan_142.pdf", "tpl": "C", "vendor_id": "V003", "invoice_no": "INV-2026-142",
     "date_text": "05/10/2026", "po_ref": "PO-2001", "lines": [pl("PO-2001", 1), pl("PO-2001", 2)], "scan": True,
     "scenario": "Edge 2b: same invoice re-sent as a scan with the number reformatted",
     "expect": {"decision": "REJECT", "codes": ["DUPLICATE"]}},
    {"file": "07_no_po_scan.pdf", "tpl": "D", "vendor_id": "V004", "invoice_no": "DSC/1187", "date_text": "15 Oct 2026",
     "ref_text": "As per telephonic discussion with stores", "po_ref": None, "scan": True,
     "lines": [pl("PO-3001", 1), pl("PO-3001", 2), pl("PO-3001", 3)],
     "scenario": "Edge 3a: scanned, tax-inclusive, no PO reference; one clear PO",
     "expect": {"decision": "APPROVE", "codes": ["PO_INFERRED"]}},
    {"file": "08_no_po_ambiguous.pdf", "tpl": "D", "vendor_id": "V004", "invoice_no": "DSC/1203",
     "date_text": "22 Oct 2026", "ref_text": "Monthly consumables supply", "po_ref": None, "scan": True,
     "lines": [pl("PO-3002", 1), pl("PO-3002", 2)],
     "scenario": "Edge 3b: no PO reference and two identical repeat-order POs",
     "expect": {"decision": "HOLD", "codes": ["PO_AMBIGUOUS"]}},
    {"file": "09_bank_changed.pdf", "tpl": "A", "vendor_id": "V001", "invoice_no": "AIS/26-27/0436",
     "date_text": "18-Oct-2026", "po_ref": "PO-1002", "lines": [pl("PO-1002", 1), pl("PO-1002", 2)],
     "bank_override": {"account": "91802004417730", "ifsc": "ICIC0004417", "bank": "ICICI Bank, Kolkata"},
     "bank_note": "Please note: we have changed our bankers. Kindly update and remit to the account below.",
     "scenario": "Edge 4: perfect PO and GRN match, but a new bank account",
     "expect": {"decision": "HOLD", "codes": ["BANK_CHANGED"]}},
    {"file": "10_price_variance.pdf", "tpl": "A", "vendor_id": "V001", "invoice_no": "AIS/26-27/0441",
     "date_text": "21-Oct-2026", "po_ref": "PO-1003", "lines": [pl("PO-1003", 1, rate=5684.00), pl("PO-1003", 2)],
     "scenario": "Backup: valve priced 1.5% above PO, inside the 2% tolerance",
     "expect": {"decision": "APPROVE", "codes": ["PRICE_WITHIN_TOLERANCE"]}},
]


def main():
    os.makedirs(SEED, exist_ok=True)
    os.makedirs(INV, exist_ok=True)
    with open(os.path.join(SEED, "vendors.json"), "w") as f:
        json.dump(VENDORS, f, indent=2)
    with open(os.path.join(SEED, "purchase_orders.json"), "w") as f:
        json.dump(POS, f, indent=2)
    with open(os.path.join(SEED, "grns.json"), "w") as f:
        json.dump(GRNS, f, indent=2)
    with open(os.path.join(SEED, "buyer.json"), "w") as f:
        json.dump(BUYER, f, indent=2)
    expected = []
    for i, inv in enumerate(INVOICES):
        path = os.path.join(INV, inv["file"])
        totals = TEMPLATES[inv["tpl"]](path, inv)
        if inv.get("scan"):
            scanify(path, seed=100 + i)
        expected.append({"file": inv["file"], "scenario": inv["scenario"], "template": inv["tpl"],
                         "format": "scan" if inv.get("scan") else "text", "invoice_no": inv["invoice_no"],
                         "total": totals["total"], "decision": inv["expect"]["decision"],
                         "codes": inv["expect"]["codes"]})
        print(f"{inv['file']:28s} total {totals['total']:>12,.2f}  expect {inv['expect']['decision']}")
    with open(os.path.join(ROOT, "data", "expected.json"), "w") as f:
        json.dump(expected, f, indent=2)


if __name__ == "__main__":
    main()
