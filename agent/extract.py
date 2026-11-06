"""Turn invoice text into structured fields.

Rules first: they're instant, free and handle the label variations vendors actually use.
Every label pattern tolerates missing spaces, because OCR drops them ("InvoiceNumber:INV-00452").
"""
import re
from datetime import datetime

MONEY = r"\$?\s*(\d{1,3}(?:,?\d{3})*\.\d{2})"
DATE_VALUE = (r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s*\d{1,2},?\s*\d{4}"
              r"|\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{4})")

INVOICE_NO = re.compile(r"(?:invoice\s*(?:number|no\.?|#)|inv\.?\s*no\.?)\s*[:#]?\s*"
                        r"([A-Z0-9/-]*\d[A-Z0-9/-]*)", re.I)
INVOICE_DATE = re.compile(r"(?:invoice\s*date|issue\s*date|dated|(?<!due )(?<!due)\bdate)\s*:?\s*" + DATE_VALUE, re.I)
DUE_DATE = re.compile(r"(?:due\s*date|due\s*by|payment\s*due)\s*:?\s*" + DATE_VALUE, re.I)
PO_REF = re.compile(r"(?:\bP\.?\s?O\.?(?!\s*Box)\s*(?:number|no\.?|#|ref(?:erence)?)?|purchase\s*order(?:\s*no\.?)?"
                    r"|order\s*ref(?:erence)?)\s*[:#]?\s*(?:P\.?\s?O\.?)?\s*-?\s*(\d{4}-\d{3,4})", re.I)
TAX_ID = re.compile(r"(?:EIN|tax\s*id|TIN)\s*:?\s*(\d{2}-\d{7})", re.I)
TERMS = re.compile(r"net\s*(\d{1,3})\b", re.I)
BANK_CHANGE = re.compile(r"(?:new|updated|changed?)\s*bank|bank\s*details|remit\s*to\s*account", re.I)
LINE_DESC_FIRST = re.compile(rf"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+{MONEY}\s+{MONEY}$")
LINE_QTY_FIRST = re.compile(rf"^(?P<qty>\d+(?:\.\d+)?)\s+(?P<desc>.+?)\s+{MONEY}\s+{MONEY}$")

DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%Y-%m-%d")

FIELDS = ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date", "po_number",
          "terms_days", "subtotal", "tax", "tax_rate", "total", "lines"]


def _num(s):
    return float(s.replace(",", "").replace("$", "").strip())


def parse_date(s):
    if not s:
        return None
    s = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)   # "November3,2026" -> "November 3,2026"
    s = re.sub(r"[,.]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _last_money(line):
    found = re.findall(MONEY, line)
    return _num(found[-1]) if found else None


def extract_rules(text, buyer_name=""):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    f = {k: None for k in FIELDS}
    f["lines"] = []

    header = [l for l in lines[:3] if squash(buyer_name) not in squash(l)]
    if header:   # vendors put their name first; drop a trailing "INVOICE" title
        f["vendor_name"] = re.sub(r"\s*\b(tax\s*)?invoice\b\s*$", "", header[0], flags=re.I).strip()

    for pattern, key in ((INVOICE_NO, "invoice_number"), (PO_REF, "po_number"), (TAX_ID, "vendor_tax_id")):
        m = pattern.search(text)
        if m:
            f[key] = m.group(1).strip().rstrip("-/")
    if f["po_number"]:
        f["po_number"] = "PO-" + f["po_number"]
    m = INVOICE_DATE.search(text)
    f["invoice_date"] = parse_date(m.group(1)) if m else None
    m = DUE_DATE.search(text)
    f["due_date"] = parse_date(m.group(1)) if m else None
    m = TERMS.search(text)
    f["terms_days"] = int(m.group(1)) if m else None
    m = BANK_CHANGE.search(text)
    f["bank_change_notice"] = m and next(l for l in lines if BANK_CHANGE.search(l))

    totals = {}
    for line in lines:
        key = squash(line)
        amount = _last_money(line)
        lm = LINE_DESC_FIRST.match(line) or LINE_QTY_FIRST.match(line)
        if lm:
            qty = float(lm.group("qty"))
            money = re.findall(MONEY, line)
            price, amt = _num(money[-2]), _num(money[-1])
            if abs(qty * price - amt) <= 0.02:      # only accept rows whose own maths works
                f["lines"].append({"description": lm.group("desc").strip(" -|"), "qty": qty,
                                   "unit_price": price, "amount": amt})
                continue
        if amount is None:
            continue
        if key.startswith(("subtotal", "netamount")):
            totals.setdefault("subtotal", amount)
        elif re.search(r"(salestax|^tax|vat|gst)", key) and "taxid" not in key:
            totals.setdefault("tax", amount)
            rate = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", line)
            if rate:
                f["tax_rate"] = float(rate.group(1))
            if "incl" in key:
                f["tax_inclusive"] = True
        for label in ("amountdue", "totaldue", "grandtotal", "balancedue", "invoicetotal"):
            if key.startswith(label):
                totals.setdefault("total", amount)
        if key.startswith("total") and "total" not in totals:
            totals["total"] = amount

    f.update({k: totals.get(k) for k in ("subtotal", "tax", "total")})
    if f["subtotal"] is None and f["lines"]:
        f["subtotal"] = round(sum(l["amount"] for l in f["lines"]), 2)
    if f["tax"] is None and f["subtotal"] is not None and f["total"] is not None:
        f["tax"] = round(f["total"] - f["subtotal"], 2)
    return f


def missing_required(f):
    req = {"vendor_name": "vendor name", "invoice_number": "invoice number", "invoice_date": "invoice date",
           "total": "total amount"}
    return [label for k, label in req.items() if not f.get(k)]
