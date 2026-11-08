# AP Autopilot - invoice PDF to decision

Zamp AI Solutions Analyst case study, PS-1 (Finance / AP).

The app takes a vendor invoice PDF (text or scanned) and returns **Approve**, **Hold** or
**Reject** with reason codes, a plain-English explanation and the next action: an ERP posting
payload, questions for a named owner, or a drafted email to the vendor. Every stage is visible
live while it runs, and a dashboard shows history across runs.

Design principle: **the LLM reads and explains; rules decide.** The same input always gives the
same decision, and every decision can be traced to a rule in `app/config.yaml`.

---

## Quick start (local)

Requires Python 3.10+ and Tesseract OCR (`brew install tesseract` / `apt install tesseract-ocr`).

```bash
pip install -r requirements.txt
python tests/test_scenarios.py        # runs all 10 scenarios against expected outcomes
uvicorn app.main:app --port 8000      # open http://localhost:8000
```

Optional LLM extraction (otherwise it runs offline with OCR + rules):

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # or OPENAI_API_KEY=... and LLM_PROVIDER=openai
export LLM_MODEL=claude-sonnet-5-5            # any vision-capable model name
```

The sidebar shows which extraction mode is active. If the LLM call fails or times out, the run
falls back to OCR + rules and records an `LLM_FALLBACK` note, so AP never stops on a vendor outage.

## Deploy (public link for the submission)

**Render (free):** push this folder to GitHub, then in Render choose *New > Blueprint* and pick
the repo. `render.yaml` builds the Dockerfile (which installs Tesseract). Add
`ANTHROPIC_API_KEY` in the service's environment if you want LLM mode. The free tier sleeps
after inactivity, so open the URL a few minutes before the interview. The SQLite file lives on
the container disk and resets on redeploy, which is fine for a demo (use *Reset demo data* anyway).

**Docker anywhere:** `docker build -t ap-autopilot . && docker run -p 8000:8000 ap-autopilot`

---

## The process (7 stages)

| # | Stage | What it does | Flags it can raise |
|---|---|---|---|
| 1 | Intake and classify | SHA-256 hash; text layer or scan (OCR); is it an invoice? | NOT_INVOICE (reject), CREDIT_NOTE, UNREADABLE |
| 2 | Extract fields | LLM (vision + JSON schema) or OCR + rules; value, confidence and source text per field | MISSING_FIELD (reject), LOW_CONFIDENCE |
| 3 | Validate the invoice | line maths, lines + tax = total, GSTIN checksum, CGST+SGST vs IGST by state, billed to our GSTIN, date | LINE_MATH, TOTAL_MISMATCH, GSTIN_INVALID, TAX_TYPE, WRONG_BUYER (reject), DATE_INVALID |
| 4 | Vendor and duplicates | vendor by GSTIN, else fuzzy name; bank details vs master; exact and near duplicates | VENDOR_UNKNOWN, VENDOR_GSTIN_MISMATCH, BANK_CHANGED, DUPLICATE (reject), POSSIBLE_DUPLICATE |
| 5 | Find the PO | PO quoted, or infer from the vendor's open POs (70% line descriptions, 30% amount) | PO_NOT_FOUND, PO_VENDOR_MISMATCH, PO_AMBIGUOUS |
| 6 | 3-way match | each line vs PO price and GRN quantity, with billed-to-date from earlier approved invoices | PRICE_VARIANCE, QTY_EXCEEDS_GRN, PO_OVERBILLED, UNMATCHED_LINE |
| 7 | Decision engine | any Reject flag -> REJECT; else any Hold flag -> HOLD; else APPROVE. Above INR 5 lakh -> manager | ABOVE_LIMIT |

Every check runs (no short-circuit), so the clerk and the vendor see all problems at once. The
only early stop is a fatal intake result (not an invoice, unreadable). Informational notes
(`PO_INFERRED`, `PRICE_WITHIN_TOLERANCE`, `OCR_REPAIRED`, `LLM_FALLBACK`) are explained but never
change the outcome.

Each Hold names an owner (AP clerk, Buyer, Vendor master, Manager) and a specific question. A
person can approve or reject from the exception queue; a reason is mandatory and the override
is logged and counted on the dashboard. An override to Approve counts towards billed-to-date.

## Scenarios (in `data/invoices`, expected results in `data/expected.json`)

Run them in order (*Run all 10 in order* on the New run page), because several depend on
earlier invoices.

| File | Format | Scenario | Result |
|---|---|---|---|
| 01_happy_path | text | Clean invoice, PO quoted, exact match | Approve |
| 02 / 03_split | text | Edge 1: PO-1043 billed in two deliveries (60 + 40) | Approve, Approve |
| 04_split_C_overbill | text | Edge 1: 20 more units on a fully billed PO | Hold: QTY_EXCEEDS_GRN, PO_OVERBILLED |
| 05_original_0142 | text | Original refractory invoice | Approve |
| 06_dup_scan_142 | scan | Edge 2: same invoice re-sent as a scan, number reformatted (INV/2026/0142 -> INV-2026-142) | Reject: DUPLICATE |
| 07_no_po_scan | scan | Edge 3: tax-inclusive prices, no PO; one clear match | Approve, PO inferred |
| 08_no_po_ambiguous | scan | Edge 3: no PO and two identical repeat-order POs | Hold: PO_AMBIGUOUS |
| 09_bank_changed | text | Edge 4: perfect match but a new bank account | Hold: BANK_CHANGED |
| 10_price_variance | text | Price 1.5% above PO, inside 2% tolerance | Approve, with a note |

Regenerate the data with `python scripts/generate_data.py` (4 vendor layouts; scans are rendered
and degraded with blur, rotation and noise).

## Design decisions worth discussing

- **Rules decide, the model reads.** Auditable and repeatable. The LLM does what rules do badly
  (reading messy documents) and, when a key is set, rewrites the reason codes as a short summary.
- **Duplicate detection on meaning, not bytes.** Invoice numbers are normalised (case,
  separators, leading zeros), so a rescanned copy with a reformatted number is still caught.
  Only non-rejected invoices count, so a corrected resubmission of a rejected invoice is allowed.
- **Checksums beat confidence scores.** A GSTIN that passes its mod-36 checksum is trusted even
  if OCR confidence is low; common OCR confusions (O/0, I/1, Z/2) are repaired by position and
  accepted only if the checksum then verifies.
- **Billed-to-date is state.** Matching uses the quantity already approved on each PO line, which
  is how split and over-billed invoices are caught.
- **A clean match is not "safe to pay".** Changed bank details always hold, whatever else passes.
- **Ask when unsure.** PO inference proceeds only if the best PO beats the next by 10 points.

## Project layout

```
app/
  config.yaml          all thresholds (tolerances, limits, confidence)
  main.py              FastAPI: runs, SSE stream, dashboard, queue, reference, reset
  db.py                SQLite schema + seed loading
  gst.py               GSTIN checksum and state codes
  pipeline/
    extract.py         PDF/OCR reading, offline parser, LLM extractor
    stages.py          the 7 stages
    explain.py         explanation, owner questions, vendor email, ERP payload
    runner.py          orchestration, one run_events row per stage (live view + audit log)
  static/              single-page UI (no build step)
data/seed/             vendor master, POs, GRNs, buyer
data/invoices/         the 10 sample PDFs
scripts/generate_data.py
tests/test_scenarios.py   10 scenarios + helpers + mocked LLM path + LLM outage fallback
```

## Assumptions and limitations

- India context: INR and GST invoices; buyer is a fictional Odisha company (state code 21).
- ERP is simulated by SQLite seed data; posting to ERP is a payload, not a live integration.
- One invoice maps to one PO; multi-PO invoices would be held for the buyer.
- Offline extraction is tuned to common Indian invoice layouts; LLM mode handles arbitrary ones.
- Credit notes are detected and routed, not processed. Invoices longer than 3 pages: first 3 read.
- The 0.5 s pause between stages (`DEMO_STAGE_DELAY`) is only there so the live view is watchable.

Next steps: email-inbox intake, learning vendor layouts from clerk corrections, ERP connectors
(SAP / Tally / Oracle), and approval routing by cost centre.
