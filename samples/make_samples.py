"""Generate the demo invoice PDFs: three vendor layouts plus one scanned image.

Run: python samples/make_samples.py
"""
import io
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent

BUYER = ["Northwind Manufacturing", "1200 Harbor Blvd", "Oakland, CA 94607"]

INVOICES = [
    # file, layout, vendor block, fields, lines (desc, qty, price), tax_rate, notes
    dict(file="1_happy_path_delta.pdf", layout="A",
         vendor=["Delta Precision Parts Co.", "88 Foundry Road", "Cleveland, OH 44114", "EIN 45-6789012"],
         number="DP-5521", date="October 27, 2026", due="December 11, 2026", po="PO-2026-1004",
         lines=[("Precision drive shaft", 200, 12.75), ("Bearing housing", 150, 21.40)], tax=8.25),
    dict(file="2a_split_invoice_ok_brightline.pdf", layout="B",
         vendor=["Brightline Logistics LLC", "4100 W Ogden Ave, Chicago, IL 60623"],
         number="FRT-2264", date="11/02/2026", terms="Net 15", po="PO-2026-1002",
         lines=[("Freight: Chicago IL to Dallas TX (FTL) - shipment 3 of 3", 1, 1450.00)], tax=None),
    dict(file="2b_split_invoice_overbill_brightline.pdf", layout="B",
         vendor=["Brightline Logistics LLC", "4100 W Ogden Ave, Chicago, IL 60623"],
         number="FRT-2270", date="11/04/2026", terms="Net 15", po="PO-2026-1002",
         lines=[("Freight: Chicago IL to Dallas TX (FTL) - shipments 3 and 4", 2, 1450.00)], tax=None),
    dict(file="3_duplicate_resubmitted_acme.pdf", layout="C",
         vendor=["ACME INDUSTRIAL SUPPLIES INC.", "PO Box 2290, Houston, TX 77002", "Tax ID 12-3456789"],
         number="417", date="2 Nov 2026", po="P.O. 2026-1001",
         lines=[("Nitrile safety gloves (box of 100)", 40, 18.50), ("Hard hat Class E", 60, 24.00),
                ("Hi-vis safety vest", 80, 9.75)], tax=8.25, stamp="SECOND NOTICE - PLEASE REMIT"),
    dict(file="4_missing_po_coastal.pdf", layout="B",
         vendor=["Coastal Office Solutions", "19 Pier Street, San Diego, CA 92101"],
         number="CO-88140", date="10/30/2026", terms="Net 30", po=None,
         lines=[("Ergonomic task chair", 12, 289.00), ("Sit-stand desk", 6, 540.00)], tax=8.0),
    dict(file="5_scanned_image_acme.pdf", layout="A", scan=True,
         vendor=["Acme Industrial Supplies Inc.", "PO Box 2290", "Houston, TX 77002", "EIN 12-3456789"],
         number="INV-00452", date="November 3, 2026", due="December 3, 2026", po="PO-2026-1008",
         lines=[("Anti-fog safety goggles", 120, 7.40), ("First aid kit 50-person", 10, 46.00)], tax=8.25),
    dict(file="6_extra_blocked_vendor_evergreen.pdf", layout="C",
         vendor=["EVERGREEN PACKAGING LTD", "7 Mill Lane, Portland, OR 97209", "Tax ID 56-7890123"],
         number="EVP-3301", date="28 Oct 2026", po="P.O. 2026-1006",
         lines=[("Corrugated shipping boxes", 1000, 1.10)], tax=None,
         stamp="NEW BANK DETAILS: First Coastal Bank, Acct 99102877"),
    dict(file="7_extra_price_variance_coastal.pdf", layout="B",
         vendor=["Coastal Office Solutions", "19 Pier Street, San Diego, CA 92101"],
         number="CO-88177", date="11/03/2026", terms="Net 30", po="PO-2026-1005",
         lines=[("Laser toner cartridge", 20, 66.50)], tax=8.0),
]


def money(x):
    return f"{x:,.2f}"


def totals(inv):
    sub = round(sum(q * p for _, q, p in inv["lines"]), 2)
    tax = round(sub * inv["tax"] / 100, 2) if inv.get("tax") else 0.0
    return sub, tax, round(sub + tax, 2)


def _styles():
    return {
        "h": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=16, leading=19),
        "t": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=22, leading=26, alignment=2),
        "n": ParagraphStyle("n", fontName="Helvetica", fontSize=10, leading=13),
        "r": ParagraphStyle("r", fontName="Helvetica", fontSize=10, leading=13, alignment=2),
        "red": ParagraphStyle("red", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=colors.red),
    }


def layout_a(inv, S):
    """Formal: vendor top-left, INVOICE title right, labelled fields, Description | Qty | Unit Price | Amount."""
    sub, tax, total = totals(inv)
    head = Table([[Paragraph("<br/>".join([f"<b>{inv['vendor'][0]}</b>"] + inv["vendor"][1:]), S["n"]),
                   Paragraph("INVOICE", S["t"])]], colWidths=[3.8 * inch, 3 * inch])
    fields = [f"Invoice Number: {inv['number']}", f"Invoice Date: {inv['date']}", f"Due Date: {inv['due']}",
              f"PO Number: {inv['po']}"]
    meta = Table([[Paragraph("<b>Bill To</b><br/>" + "<br/>".join(BUYER), S["n"]),
                   Paragraph("<br/>".join(fields), S["n"])]], colWidths=[3.4 * inch, 3.4 * inch])
    rows = [["Description", "Qty", "Unit Price", "Amount"]]
    rows += [[d, str(q), money(p), money(q * p)] for d, q, p in inv["lines"]]
    rows += [["", "", "Subtotal", money(sub)], ["", "", f"Sales Tax ({inv['tax']}%)", money(tax)],
             ["", "", "Total Due (USD)", money(total)]]
    t = Table(rows, colWidths=[3.4 * inch, 0.8 * inch, 1.3 * inch, 1.3 * inch])
    n = len(inv["lines"])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b5c")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                           ("LINEBELOW", (0, n), (-1, n), 0.8, colors.grey),
                           ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold")]))
    return [head, Spacer(1, 18), meta, Spacer(1, 18), t, Spacer(1, 24),
            Paragraph(f"Payment terms: due by {inv['due']}. Please quote the invoice number with payment.", S["n"])]


def layout_b(inv, S):
    """Compact: centred vendor, 'Invoice #', US date, 'Item | Quantity | Rate | Line Total', 'Amount Due'."""
    sub, tax, total = totals(inv)
    out = [Paragraph(inv["vendor"][0], ParagraphStyle("c", parent=S["h"], alignment=1)),
           Paragraph(inv["vendor"][1], ParagraphStyle("c2", parent=S["n"], alignment=1)), Spacer(1, 16)]
    left = [f"Invoice # {inv['number']}", f"Date: {inv['date']}", f"Terms: {inv['terms']}"]
    if inv.get("po"):
        left.append(f"PO Ref: {inv['po']}")
    out.append(Table([[Paragraph("<br/>".join(left), S["n"]),
                       Paragraph("<b>Customer</b><br/>" + "<br/>".join(BUYER), S["n"])]],
                     colWidths=[3.4 * inch, 3.4 * inch]))
    out.append(Spacer(1, 16))
    rows = [["Item", "Quantity", "Rate", "Line Total"]]
    rows += [[d, str(q), f"${money(p)}", f"${money(q * p)}"] for d, q, p in inv["lines"]]
    rows += [["", "", "Sub-total", f"${money(sub)}"]]
    if inv.get("tax"):
        rows += [["", "", f"Tax {inv['tax']:g}%", f"${money(tax)}"]]
    rows += [["", "", "Amount Due", f"${money(total)}"]]
    t = Table(rows, colWidths=[3.6 * inch, 0.9 * inch, 1.1 * inch, 1.2 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                           ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold")]))
    out += [t, Spacer(1, 20), Paragraph("Thank you for your business.", S["n"])]
    return out


def layout_c(inv, S):
    """Older style: caps vendor, 'Inv. No.', '2 Nov 2026', qty-first columns, 'Grand Total'."""
    sub, tax, total = totals(inv)
    out = [Paragraph(inv["vendor"][0], S["h"]), Paragraph("<br/>".join(inv["vendor"][1:]), S["n"]),
           Spacer(1, 10), Paragraph("TAX INVOICE", S["h"]), Spacer(1, 6)]
    if inv.get("stamp"):
        out += [Paragraph(inv["stamp"], S["red"]), Spacer(1, 6)]
    out.append(Paragraph(f"Inv. No.: {inv['number']}<br/>Dated: {inv['date']}<br/>Order Ref: {inv['po']}<br/>"
                         f"To: {', '.join(BUYER)}", S["n"]))
    out.append(Spacer(1, 14))
    rows = [["Qty", "Description", "Price", "Total"]]
    rows += [[str(q), d, money(p), money(q * p)] for d, q, p in inv["lines"]]
    rows += [["", "", "Net amount", money(sub)]]
    if inv.get("tax"):
        rows += [["", "", f"Sales tax {inv['tax']}%", money(tax)]]
    rows += [["", "", "Grand Total", money(total)]]
    t = Table(rows, colWidths=[0.7 * inch, 3.6 * inch, 1.2 * inch, 1.3 * inch])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, len(inv["lines"])), 0.5, colors.grey),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("ALIGN", (2, 0), (-1, -1), "RIGHT")]))
    out.append(t)
    return out


def build(inv, path):
    S = _styles()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.7 * inch, title=f"Invoice {inv['number']}")
    doc.build({"A": layout_a, "B": layout_b, "C": layout_c}[inv["layout"]](inv, S))


def rasterize(pdf_path):
    """Turn a PDF into an image-only 'scan': render, rotate slightly, blur, add speckle."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "150", "-png", "-singlefile", str(pdf_path), f"{tmp}/p"], check=True)
        img = Image.open(f"{tmp}/p.png").convert("L")
    rnd = random.Random(7)
    img = img.rotate(0.6, expand=False, fillcolor=255).filter(ImageFilter.GaussianBlur(0.5))
    px = img.load()
    for _ in range(4000):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = rnd.choice((150, 200))
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    pdf_path.write_bytes(buf.getvalue())


if __name__ == "__main__":
    for inv in INVOICES:
        path = OUT / inv["file"]
        build(inv, path)
        if inv.get("scan"):
            rasterize(path)
        print("wrote", path.name, "total", money(totals(inv)[2]))
