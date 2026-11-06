"""The invoice process: 8 visible stages from PDF to a reasoned decision.

AI reads (OCR, optional LLM); rules decide. Every finding has a severity:
  reject -> REJECTED      review -> NEEDS REVIEW      note -> recorded, doesn't block
No reject/review findings -> APPROVED.
"""
import hashlib
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from . import store
from .extract import extract_rules, missing_required, squash
from .llm import configured as llm_configured, extract_llm
from .reader import MIN_TEXT_CHARS, ocr, read_text_layer

APPROVED, REVIEW, REJECTED = "APPROVED", "NEEDS REVIEW", "REJECTED"


@dataclass
class Finding:
    severity: str          # reject | review | note
    code: str
    message: str
    owner: str = ""        # who should act on it
    vendor_ask: str = ""   # what to ask the vendor, if anything


@dataclass
class Stage:
    key: str
    title: str
    status: str = "pass"   # pass | warn | fail
    summary: str = ""
    details: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    ms: int = 0


def usd(x):
    return f"${x:,.2f}" if x is not None else "—"


def similar(a, b):
    return SequenceMatcher(None, squash(a), squash(b)).ratio()


class InvoiceRun:
    def __init__(self, pdf_bytes, filename, ref, conn, extractor="auto", today=None):
        self.pdf, self.filename, self.ref, self.conn = pdf_bytes, filename, ref, conn
        self.policy = ref.policy
        self.mode = extractor
        self.today = today or date.today()
        self.run_id = store.new_run_id()
        self.stages, self.findings = [], []
        self.text, self.read_method, self.ocr_conf = "", "", None
        self.fields, self.extractor = {}, ""
        self.vendor, self.po, self.po_inferred = None, None, False
        self.record = None

    # -- helpers ---------------------------------------------------------------------------
    def _stage(self, key, title, fn):
        st = Stage(key, title)
        t0 = time.perf_counter()
        before = len(self.findings)
        try:
            fn(st)
        except Exception as e:   # a stage crash must still produce a decision, never a dead end
            self.findings.append(Finding("review", "internal_error", f"{title} failed: {e}", "AP clerk"))
            st.summary = f"Error: {e}"
        st.findings = self.findings[before:]
        sev = {f.severity for f in st.findings}
        st.status = "fail" if "reject" in sev else "warn" if "review" in sev else "pass"
        st.ms = int((time.perf_counter() - t0) * 1000)
        self.stages.append(st)
        return st

    def _add(self, *a, **kw):
        self.findings.append(Finding(*a, **kw))

    # -- the process -------------------------------------------------------------------------
    def steps(self):
        """Run stage by stage; yields each finished Stage so a UI can show it live."""
        t0 = time.perf_counter()
        plan = [("ingest", "Ingest document", self.ingest), ("read", "Read text", self.read),
                ("extract", "Extract fields", self.extract), ("validate", "Validate invoice", self.validate),
                ("vendor", "Vendor check", self.check_vendor), ("duplicate", "Duplicate check", self.check_duplicate),
                ("po", "PO match", self.match_po), ("decide", "Decide & record", self.decide)]
        for key, title, fn in plan:
            st = self._stage(key, title, fn)
            if key == "decide":   # save before showing the last stage, in one transaction
                self.record["stages"] = [asdict(s) for s in self.stages]
                self.record["duration_ms"] = int((time.perf_counter() - t0) * 1000)
                self._persist()
            yield st

    def run(self):
        for _ in self.steps():
            pass
        return self.record

    def ingest(self, st):
        pages, text = read_text_layer(self.pdf)
        self.text = text
        st.details = {"file": self.filename, "size_kb": round(len(self.pdf) / 1024, 1), "pages": pages,
                      "sha256": hashlib.sha256(self.pdf).hexdigest()[:16], "text_layer_chars": len(text.strip())}
        st.summary = f"{self.filename}: {pages} page(s), {st.details['size_kb']} KB"

    def read(self, st):
        if len(self.text.strip()) >= MIN_TEXT_CHARS:
            self.read_method = "pdf text layer"
            st.summary = f"Digital PDF: read embedded text ({len(self.text)} chars)"
        else:
            self.read_method = "OCR"
            try:
                self.text, mean, low = ocr(self.pdf)
            except Exception as e:
                self._add("review", "unreadable", f"Scanned PDF and OCR failed ({e}); key it in manually.",
                          "AP clerk")
                st.summary = "Scanned PDF, OCR unavailable"
                return
            self.ocr_conf = mean
            st.summary = f"Scanned image (no text layer): OCR read {len(self.text.splitlines())} lines, " \
                         f"mean confidence {mean:.0%}"
            if mean < self.policy["ocr_min_confidence"]:
                self._add("review", "ocr_low_confidence",
                          f"OCR confidence {mean:.0%} is below {self.policy['ocr_min_confidence']:.0%}; "
                          "a person should check the extracted values.", "AP clerk")
        st.details = {"method": self.read_method, "ocr_confidence": self.ocr_conf,
                      "text_preview": self.text.splitlines()[:30]}

    def extract(self, st):
        buyer = self.policy["buyer_company"]
        spec = llm_configured()
        f = {} if self.mode == "llm" and spec else extract_rules(self.text, buyer)
        self.extractor = "rules"
        missing = missing_required(f) if f else ["all"]
        if spec and (self.mode == "llm" or missing):
            try:
                g = extract_llm(self.text, spec, buyer)
                if not f:
                    f, self.extractor = g, f"LLM ({spec})"
                else:   # LLM only fills what the rules couldn't find
                    for k, v in g.items():
                        if not f.get(k) and v:
                            f[k] = v
                    self.extractor = f"rules + LLM ({spec})"
            except Exception as e:
                st.details["llm_error"] = str(e)
        self.fields = f
        missing = missing_required(f)
        if missing:
            self._add("review", "missing_fields", f"Invoice is missing: {', '.join(missing)}.", "AP clerk",
                      f"Please resend the invoice showing the {', '.join(missing)}.")
        n = len(f.get("lines") or [])
        st.summary = f"{self.extractor}: {len([k for k, v in f.items() if v not in (None, [], '')])} fields, " \
                     f"{n} line item(s)" + (f"; missing {', '.join(missing)}" if missing else "")
        st.details.update({"extractor": self.extractor,
                           "fields": {k: v for k, v in f.items() if k != "lines"}, "lines": f.get("lines") or []})

    def validate(self, st):
        f, checks = self.fields, []
        lines, sub, tax, total = f.get("lines") or [], f.get("subtotal"), f.get("tax") or 0.0, f.get("total")
        arithmetic_ok = True
        if lines and sub is not None:
            s = round(sum(l["amount"] for l in lines), 2)
            ok = abs(s - sub) <= 0.02
            checks.append(("Line items add up to subtotal", ok, f"{usd(s)} vs {usd(sub)}"))
            if not ok:
                arithmetic_ok = False
                self._add("review", "lines_sum", f"Line items add up to {usd(s)} but the subtotal says {usd(sub)}.",
                          "AP clerk", "The line items and subtotal on your invoice don't agree; please send a "
                                      "corrected invoice.")
        if sub is not None and total is not None:
            expected = sub if f.get("tax_inclusive") else round(sub + tax, 2)
            ok = abs(expected - total) <= 0.02
            checks.append(("Subtotal + tax = total", ok, f"{usd(sub)} + {usd(tax)} = {usd(expected)} vs {usd(total)}"))
            if not ok:
                arithmetic_ok = False
                self._add("review", "total_mismatch", f"Subtotal + tax = {usd(expected)} but total says {usd(total)}.",
                          "AP clerk", "The total on your invoice doesn't match subtotal plus tax; please correct it.")
            if sub:
                rate = tax / sub * 100
                ok = rate <= self.policy["max_tax_rate_pct"] and (
                    f.get("tax_rate") is None or abs(rate - f["tax_rate"]) < 0.05)
                checks.append(("Tax rate plausible", ok, f"{rate:.2f}%" + (f" (printed {f['tax_rate']}%)"
                                                                           if f.get("tax_rate") else "")))
                if not ok:
                    self._add("review", "tax_rate", f"Tax works out to {rate:.2f}% of the subtotal"
                              + (f", but the invoice prints {f['tax_rate']}%" if f.get("tax_rate") else "") + ".",
                              "AP clerk")
        d = f.get("invoice_date")
        if d:
            inv = date.fromisoformat(d)
            ok = inv <= self.today + timedelta(days=1)
            checks.append(("Invoice date not in the future", ok, d))
            if not ok:
                self._add("review", "future_date", f"Invoice is dated {d}, which is in the future.", "AP clerk")
            if (self.today - inv).days > 365:
                self._add("review", "stale", f"Invoice date {d} is over a year old.", "AP clerk")
            if f.get("due_date") and f["due_date"] < d:
                self._add("review", "due_before_issue", f"Due date {f['due_date']} is before invoice date {d}.",
                          "AP clerk")
        if self.read_method == "OCR" and checks:
            if arithmetic_ok:
                self._add("note", "ocr_verified", "Values were read by OCR; the arithmetic cross-checks all "
                          "agree, so the reading is self-consistent.")
            else:
                self._add("review", "ocr_uncertain", "Values were read by OCR and the arithmetic doesn't agree; "
                          "likely a misread. Check against the image.", "AP clerk")
        st.details = {"checks": [{"check": c, "ok": ok, "detail": det} for c, ok, det in checks]}
        st.summary = f"{sum(ok for _, ok, _ in checks)}/{len(checks)} checks passed"

    def check_vendor(self, st):
        f = self.fields
        header = squash(" ".join(self.text.splitlines()[:6]))
        best, score = None, 0.0
        for v in self.ref.vendors.values():
            for name in [v["name"]] + v["aliases"]:
                s = 1.0 if squash(name) and squash(name) in header else similar(f.get("vendor_name") or "", name)
                if s > score:
                    best, score = v, s
        if not best or score < 0.85:
            self._add("review", "vendor_unknown", f"'{f.get('vendor_name')}' is not in the vendor master.",
                      "Procurement (vendor onboarding)")
            st.summary = f"No match for '{f.get('vendor_name')}'"
            return
        self.vendor = best
        st.details = {"matched": best["name"], "vendor_id": best["vendor_id"], "match_score": round(score, 2),
                      "status": best["status"], "tax_id_on_file": best["tax_id"],
                      "tax_id_on_invoice": f.get("vendor_tax_id")}
        st.summary = f"{best['name']} ({best['vendor_id']}), status: {best['status']}"
        if best["status"] != "approved":
            self._add("reject", "vendor_blocked", f"{best['name']} is {best['status']}: {best['status_note']}.",
                      "AP manager", "We are unable to process invoices for your account at this time; our "
                                    "procurement team will contact you.")
        if f.get("vendor_tax_id") and f["vendor_tax_id"] != best["tax_id"]:
            self._add("review", "tax_id_mismatch", f"Tax ID on invoice ({f['vendor_tax_id']}) differs from the "
                      f"vendor master ({best['tax_id']}): possible impersonation.", "AP manager")
        if f.get("bank_change_notice"):
            self._add("review", "bank_change", f"Invoice asks for payment to new bank details "
                      f"(\"{f['bank_change_notice']}\"). Verify by phone with a known contact before paying; "
                      "this is the classic invoice-fraud pattern.", "AP manager")

    def check_duplicate(self, st):
        f = self.fields
        if not self.vendor or not f.get("invoice_number"):
            st.summary = "Skipped: needs a known vendor and an invoice number"
            return
        prior = store.vendor_invoices(self.conn, self.vendor["vendor_id"])
        num, core, total = f["invoice_number"], store.number_core(f["invoice_number"]), f.get("total")
        hit, split_note = None, None
        for p in prior:
            same_amount = total is not None and abs((p["total"] or 0) - total) <= 1.0
            if squash(p["invoice_number"]) == squash(num):
                hit = ("reject", "duplicate_exact", f"Exact duplicate of invoice {p['invoice_number']} "
                       f"({p['status']}, dated {p['invoice_date']}, {usd(p['total'])}).")
            elif core and p["number_core"] == core and same_amount:
                hit = ("reject", "duplicate_reformatted",
                       f"Probable duplicate: '{num}' is invoice {p['invoice_number']} re-sent with a reformatted "
                       f"number and a new date. Same vendor, same amount {usd(total)}; the original is "
                       f"already {p['status']} (dated {p['invoice_date']}).")
            elif same_amount and p["invoice_date"] and f.get("invoice_date") and abs(
                    (date.fromisoformat(p["invoice_date"]) - date.fromisoformat(f["invoice_date"])).days
            ) <= self.policy["duplicate_window_days"] and p.get("po_number") in (None, f.get("po_number")):
                if self._po_has_room(f.get("po_number")):
                    # Same-amount bills against a PO with budget left are normal split billing
                    # (one freight run = one invoice). The PO balance check is the guard there.
                    split_note = (f"Same amount as {p['invoice_number']}, but {f['po_number']} still has budget for "
                                  "it: treated as split billing, guarded by the PO balance check.")
                    continue
                hit = hit or ("review", "duplicate_possible",
                              f"Possible duplicate: same amount {usd(total)} as {p['invoice_number']} "
                              f"({p['status']}, {p['invoice_date']}) within {self.policy['duplicate_window_days']} days.")
            if hit and hit[0] == "reject":
                break
        if hit:
            self._add(*hit, owner="AP clerk", vendor_ask=(
                f"Our records show invoice {num} was already received/paid. Please confirm whether this is "
                "a reminder or a separate charge." if hit[0] == "reject" else ""))
            st.summary = hit[2]
        else:
            st.summary = f"No duplicate among {len(prior)} invoices from this vendor"
            if split_note:
                self._add("note", "same_amount_split", split_note)
        st.details = {"compared_against": len(prior), "number_core": core}

    def _po_has_room(self, po_number):
        po = self.ref.pos.get(po_number or "")
        if not po:
            return False
        consumed, _ = store.po_consumed(self.conn, po_number)
        pol = self.policy
        allowed = po["total"] + max(po["total"] * pol["amount_tolerance_pct"] / 100, pol["amount_tolerance_abs"])
        return consumed + (self.fields.get("subtotal") or 0) <= allowed

    def _score_po(self, po):
        f = self.fields
        sub = f.get("subtotal") or 0
        consumed, _ = store.po_consumed(self.conn, po["po_number"])
        remaining = po["total"] - consumed
        amount = max(0.0, 1 - abs(sub - remaining) / max(remaining, 1)) if remaining > 0 else 0.0
        lines = f.get("lines") or []
        line = (sum(max((similar(l["description"], p["description"]) for p in po["lines"]), default=0)
                    for l in lines) / len(lines)) if lines else 0.0
        return round(0.5 * amount + 0.5 * line, 3), remaining

    def match_po(self, st):
        f, pol = self.fields, self.policy
        if not self.vendor:
            st.summary = "Skipped: vendor not identified"
            return
        po_no = f.get("po_number")
        if po_no:
            po = self.ref.pos.get(po_no)
            if not po:
                self._add("review", "po_not_found", f"PO {po_no} does not exist.", "AP clerk",
                          f"We can't find purchase order {po_no}; please confirm the PO number.")
                st.summary = f"{po_no} not found"
                return
            if po["vendor_id"] != self.vendor["vendor_id"]:
                self._add("review", "po_vendor_mismatch", f"{po_no} belongs to {self.ref.vendors[po['vendor_id']]['name']},"
                          f" not {self.vendor['name']}.", po["buyer"])
            if po["status"] != "open":
                self._add("review", "po_closed", f"{po_no} is {po['status']}.", po["buyer"])
        else:
            cands = [p for p in self.ref.pos.values()
                     if p["vendor_id"] == self.vendor["vendor_id"] and p["status"] == "open"]
            scored = sorted(((self._score_po(p)[0], p) for p in cands), key=lambda x: -x[0])
            st.details["candidates"] = [{"po": p["po_number"], "score": s, "po_total": p["total"]} for s, p in scored]
            if not scored:
                self._add("review", "po_missing", "No PO number and no open PO for this vendor.", "Procurement",
                          "Please resend the invoice with the purchase order number.")
                st.summary = "No PO reference and nothing to infer from"
                return
            top_score, po = scored[0]
            margin = top_score - (scored[1][0] if len(scored) > 1 else 0)
            if top_score < pol["po_inference_min_score"] or margin < 0.15:
                self._add("review", "po_ambiguous", "No PO number on the invoice and no single open PO clearly "
                          f"matches (best {po['po_number']} at {top_score:.0%}).", "AP clerk",
                          "Please resend the invoice with the purchase order number.")
                st.summary = "No PO reference; could not infer one confidently"
                return
            self.po_inferred = True
            self._add("note", "po_inferred", f"No PO number on the invoice; matched to {po['po_number']} "
                      f"from vendor + amount + line items ({top_score:.0%} match, next best "
                      f"{scored[1][0]:.0%})." if len(scored) > 1 else
                      f"No PO number on the invoice; matched to the vendor's only open PO {po['po_number']} "
                      f"({top_score:.0%} match).")
        self.po = po

        # Line-level: is each invoiced item on the PO, at the PO price?
        tol = pol["unit_price_tolerance_pct"]
        rows = []
        for l in f.get("lines") or []:
            best = max(po["lines"], key=lambda p: similar(l["description"], p["description"]), default=None)
            sim = similar(l["description"], best["description"]) if best else 0
            if not best or sim < 0.55:
                self._add("review", "line_not_on_po", f"'{l['description']}' is not on {po['po_number']}.", po["buyer"])
                rows.append({"invoice_line": l["description"], "po_line": None})
                continue
            var = (l["unit_price"] - best["unit_price"]) / best["unit_price"] * 100
            rows.append({"invoice_line": l["description"], "po_line": best["description"], "qty": l["qty"],
                         "po_qty": best["qty"], "price": l["unit_price"], "po_price": best["unit_price"],
                         "variance_pct": round(var, 2)})
            if abs(var) > tol:
                self._add("review", "price_variance", f"Unit price for '{best['description']}' is {usd(l['unit_price'])}"
                          f" vs PO {usd(best['unit_price'])} ({var:+.1f}%, tolerance ±{tol:g}%).", po["buyer"],
                          f"The unit price for {best['description']} ({usd(l['unit_price'])}) doesn't match our PO "
                          f"price of {usd(best['unit_price'])}; please send a corrected invoice or the agreed change.")
            elif abs(var) > 0.001:
                self._add("note", "price_within_tolerance", f"'{best['description']}' price differs by {var:+.1f}% "
                          f"(within ±{tol:g}%).")
            if l["qty"] > best["qty"]:
                self._add("review", "qty_over_po", f"Invoiced quantity {l['qty']:g} of '{best['description']}' "
                          f"exceeds the {best['qty']:g} ordered.", po["buyer"])

        # PO-level: split invoices are fine until the PO is used up.
        consumed, prior = store.po_consumed(self.conn, po["po_number"])
        sub = f.get("subtotal") or 0
        after = round(consumed + sub, 2)
        allowed = po["total"] + max(po["total"] * pol["amount_tolerance_pct"] / 100, pol["amount_tolerance_abs"])
        st.details.update({"po": po["po_number"], "inferred": self.po_inferred, "buyer": po["buyer"],
                           "po_total": po["total"], "previously_invoiced": consumed, "prior_invoices": prior,
                           "this_invoice": sub, "after_this_invoice": after,
                           "pct_of_po_used": round(after / po["total"] * 100, 1), "lines": rows})
        if after > allowed:
            over = after - po["total"]
            self._add("review", "po_overbilled",
                      f"Over-billing: {po['po_number']} is for {usd(po['total'])}; {usd(consumed)} already invoiced "
                      f"({', '.join(prior) or 'none'}) + this {usd(sub)} = {usd(after)}, which is {usd(over)} "
                      f"({over / po['total']:.0%}) over the PO.", po["buyer"],
                      f"This invoice would take {po['po_number']} past its approved value ({usd(po['total'])} "
                      f"with {usd(consumed)} already billed). Please confirm what this charge covers.")
        elif prior:
            self._add("note", "split_invoice", f"Split billing: invoice {len(prior) + 1} against {po['po_number']}; "
                      f"cumulative {usd(after)} of {usd(po['total'])} ({after / po['total']:.0%}).")
        if (f.get("total") or 0) > pol["auto_approve_limit"]:
            self._add("review", "over_limit", f"Total {usd(f['total'])} is above the auto-approval limit "
                      f"{usd(pol['auto_approve_limit'])}.", "AP manager")
        st.summary = (f"{po['po_number']}{' (inferred)' if self.po_inferred else ''}: {usd(after)} of "
                      f"{usd(po['total'])} after this invoice ({after / po['total']:.0%})")

    def _persist(self):
        r, f = self.record, self.fields
        status = {APPROVED: "approved", REVIEW: "on_hold", REJECTED: "rejected"}[r["decision"]]
        with self.conn:   # both rows or neither
            if self.vendor and f.get("invoice_number"):
                store.register_invoice(self.conn, self.run_id, self.vendor["vendor_id"], f, r["po_number"], status)
            store.save_run(self.conn, r)

    def decide(self, st):
        sev = {f.severity for f in self.findings}
        decision = REJECTED if "reject" in sev else REVIEW if "review" in sev else APPROVED
        blocking = [f for f in self.findings if f.severity == ("reject" if decision == REJECTED else "review")]
        f = self.fields
        if decision == APPROVED:
            terms = f.get("terms_days") or self.vendor["payment_terms_days"]
            due = f.get("due_date") or (date.fromisoformat(f["invoice_date"]) + timedelta(days=terms)).isoformat()
            headline = f"Matches {self.po['po_number']}: pay {usd(f.get('total'))} to {self.vendor['name']} by {due}"
            action, owner = f"Queue {usd(f.get('total'))} for the payment run due {due}.", "Payments (automatic)"
        else:
            headline = blocking[0].message
            owner = blocking[0].owner or "AP clerk"
            action = ("Do not pay. " if decision == REJECTED else "Hold. ") + f"Route to {owner}."
        asks = [x.vendor_ask for x in blocking if x.vendor_ask]
        email = ""
        if asks:
            email = (f"Subject: Invoice {f.get('invoice_number') or '(no number)'} from "
                     f"{(self.vendor or {}).get('name') or f.get('vendor_name')}\n\nHello,\n\nThank you for your "
                     f"invoice {f.get('invoice_number') or ''} for {usd(f.get('total'))}. Before we can process it:\n\n"
                     + "\n".join(f"- {a}" for a in dict.fromkeys(asks))
                     + f"\n\nKind regards,\nAccounts Payable, {self.policy['buyer_company']}")
        st.summary = f"{decision}: {headline}"
        st.details = {"decision": decision, "owner": owner, "next_action": action}
        self.record = {
            "run_id": self.run_id, "created_at": datetime.now().isoformat(timespec="seconds"),
            "filename": self.filename, "decision": decision, "headline": headline, "next_action": action,
            "owner": owner, "vendor_email": email,
            "vendor_id": (self.vendor or {}).get("vendor_id"), "vendor_name": (self.vendor or {}).get("name"),
            "po_number": self.po["po_number"] if self.po else None, "po_inferred": self.po_inferred,
            "extractor": self.extractor, "read_method": self.read_method, "ocr_confidence": self.ocr_conf,
            "fields": f, "findings": [asdict(x) for x in self.findings],
        }
