# Design notes: assumptions, decisions, limits

These are notes to refer to in the live pitch.

## Who it's for
An AP clerk at a mid-size company who today opens every PDF, finds the PO in a spreadsheet, checks the numbers and
decides. The goal is **not** to replace their judgement. It handles the clean majority automatically and gives
them every exception already investigated: what's wrong, the evidence, who should act, and a draft email.

## Assumptions (where the brief was silent)
- **2-way match** (invoice ↔ PO). There's no goods-receipt data in scope; a 3-way match would add receipts as one more stage.
- PO amounts are **pre-tax**. Tax is validated for plausibility (≤ 15%, printed rate = computed rate), not against the PO.
- **Tolerances**: amounts ±2% or $25, whichever is larger; unit price ±2%. Auto-approval limit $25,000. All in `data/policy.json`.
- A PO can be **billed in several invoices** (split billing). Budget is used up only by *approved or paid* invoices; held ones don't.
- Slash dates are **US format** (MM/DD/YYYY): the buyer is a US company.
- USD only.
- The vendor's name is printed at the top of its own invoice. The buyer's own name is ignored when finding the vendor.

## What's AI, what's rules, why
| Step | How | Why |
|---|---|---|
| Read a scanned PDF | **AI**: offline OCR model (RapidOCR / ONNX) | Perception is what ML is good at. Free, no data leaves the machine |
| Extract fields | **Rules** with space-tolerant label patterns; **LLM fallback** if required fields are missing | Rules are instant, free and predictable on known layouts. The LLM covers the long tail |
| Validate, match, decide | **Rules only** | Money decisions must be auditable and repeatable. A model is never the reason a payment goes out |
| Check the reading | Arithmetic cross-check (lines → subtotal → tax → total) | A cheap, independent check on OCR or LLM output: a misread digit breaks the sums |

## Decision logic
- **REJECTED**: vendor blocked; exact duplicate; same invoice re-sent with a reformatted number (same digit core + vendor + amount).
- **NEEDS REVIEW**: anything a human must judge, e.g.:
  - missing fields, sums that don't add up, implausible tax, future or stale date
  - unknown vendor, tax-ID mismatch, request to pay new bank details
  - PO not found, closed or belonging to another vendor; no PO and no confident inference
  - price outside tolerance, quantity above ordered, PO balance exceeded
  - total above the auto-approval limit
- **APPROVED**: nothing above. It's scheduled for the payment run by the due date (the invoice's own terms, else the vendor's terms).
- Every finding names an **owner**: the PO's buyer for price, quantity or budget issues; the AP manager for fraud signals; procurement for unknown vendors; the AP clerk otherwise.

## The edge cases and why I picked them
1. **Split billing / over-billing (Brightline freight).** The brief says vendors split a PO into several invoices.
   The interesting part is that *same-amount invoices are normal here* (one invoice per truckload), so a naive
   "same amount = duplicate" rule would wrongly hold them. That was an actual bug in my first version.
   The guard is the **PO balance**: invoices are accepted while the PO has budget, and the one that exceeds it is held with the arithmetic shown.
2. **Disguised duplicate (Acme "417").** Duplicates rarely arrive identical. They come back as a "second notice" with a
   new date and a reformatted number. The process compares the number's **digit core** (`INV-00417` → `417`)
   together with vendor and amount. Paying an invoice twice is the most expensive everyday AP mistake.
3. **Missing PO number (Coastal).** "PO references might be implied." The vendor's open POs are scored on amount fit
   and line-item similarity. It auto-matches only if one PO clearly wins (≥ 75% and 15 points ahead), otherwise it
   holds and asks the vendor.
4. **Scanned image (Acme).** No text layer, so OCR. OCR can misread digits, so the invoice's own arithmetic is used to
   verify the reading. If the sums disagree, it holds instead of guessing.

Extra realistic cases: a blocked vendor asking for "new bank details" (the classic business-email-compromise fraud
pattern), and a price creeping 3.9% above the PO.

## How I know it works
- 13 unit tests: every sample reaches its intended decision; OCR-style text, date formats, "PO Box" not mistaken for a PO.
- A non-invoice PDF goes to NEEDS REVIEW ("missing invoice number, date, total"); it doesn't crash.
- Any stage that throws an error turns into a NEEDS REVIEW finding, so the process always ends in a decision.

## Limits / what I'd build next
- Rule extraction is tuned to common label wording. Truly novel layouts need the LLM fallback (or a vision model on the image).
- Line-item matching is fuzzy text similarity; real catalogues would match on SKU or item codes.
- No goods receipts (3-way match), no multi-currency, no approval hierarchy above the $25k limit.
- Intake is upload; production would watch the AP mailbox (e.g. Gmail API or n8n email trigger) and post to the ERP.
- Learn from reviewer overrides to tune tolerances per vendor.
