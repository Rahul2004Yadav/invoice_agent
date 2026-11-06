"""Every demo invoice must reach its intended decision.  Run: python -m unittest discover tests"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import store  # noqa: E402
from agent.extract import extract_rules, parse_date  # noqa: E402
from agent.pipeline import APPROVED, REJECTED, REVIEW, InvoiceRun  # noqa: E402
from agent.reference import load  # noqa: E402

TODAY = date(2026, 11, 6)
S = ROOT / "samples"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ref = load()
        self.conn = store.connect(Path(self.tmp.name) / "t.db", ref=self.ref)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_pdf(self, name):
        return InvoiceRun((S / name).read_bytes(), name, self.ref, self.conn, today=TODAY).run()

    def codes(self, r):
        return {f["code"] for f in r["findings"]}


class Decisions(Base):
    def test_happy_path(self):
        r = self.run_pdf("1_happy_path_delta.pdf")
        self.assertEqual(r["decision"], APPROVED)
        self.assertEqual(r["po_number"], "PO-2026-1004")
        self.assertEqual(r["fields"]["total"], 6235.20)

    def test_split_billing_then_overbill(self):
        self.assertEqual(self.run_pdf("2a_split_invoice_ok_brightline.pdf")["decision"], APPROVED)
        r = self.run_pdf("2b_split_invoice_overbill_brightline.pdf")
        self.assertEqual(r["decision"], REVIEW)
        self.assertIn("po_overbilled", self.codes(r))

    def test_overbill_even_without_the_split_invoice(self):
        r = self.run_pdf("2b_split_invoice_overbill_brightline.pdf")
        self.assertEqual(r["decision"], REVIEW)
        self.assertIn("po_overbilled", self.codes(r))

    def test_disguised_duplicate(self):
        r = self.run_pdf("3_duplicate_resubmitted_acme.pdf")
        self.assertEqual(r["decision"], REJECTED)
        self.assertIn("duplicate_reformatted", self.codes(r))

    def test_missing_po_is_inferred(self):
        r = self.run_pdf("4_missing_po_coastal.pdf")
        self.assertEqual(r["decision"], APPROVED)
        self.assertTrue(r["po_inferred"])
        self.assertEqual(r["po_number"], "PO-2026-1003")

    def test_scanned_invoice_via_ocr(self):
        r = self.run_pdf("5_scanned_image_acme.pdf")
        self.assertEqual(r["read_method"], "OCR")
        self.assertEqual(r["decision"], APPROVED)
        self.assertIn("ocr_verified", self.codes(r))
        self.assertEqual(r["fields"]["invoice_number"], "INV-00452")

    def test_blocked_vendor(self):
        r = self.run_pdf("6_extra_blocked_vendor_evergreen.pdf")
        self.assertEqual(r["decision"], REJECTED)
        self.assertTrue({"vendor_blocked", "bank_change"} <= self.codes(r))

    def test_price_variance(self):
        r = self.run_pdf("7_extra_price_variance_coastal.pdf")
        self.assertEqual(r["decision"], REVIEW)
        self.assertIn("price_variance", self.codes(r))

    def test_same_invoice_twice_is_rejected(self):
        self.run_pdf("1_happy_path_delta.pdf")
        r = self.run_pdf("1_happy_path_delta.pdf")
        self.assertEqual(r["decision"], REJECTED)
        self.assertIn("duplicate_exact", self.codes(r))

    def test_every_run_is_recorded(self):
        self.run_pdf("1_happy_path_delta.pdf")
        self.assertEqual(len(store.runs(self.conn)), 1)


class Extraction(unittest.TestCase):
    def test_ocr_style_text_without_spaces(self):
        f = extract_rules("AcmeIndustrial SuppliesInc.\nInvoiceNumber:INV-9\nInvoiceDate:November3,2026\n"
                          "PONumber:PO-2026-1008\nTotal Due (USD) 10.00")
        self.assertEqual((f["invoice_number"], f["invoice_date"], f["po_number"], f["total"]),
                         ("INV-9", "2026-11-03", "PO-2026-1008", 10.0))

    def test_po_box_is_not_a_po(self):
        self.assertIsNone(extract_rules("Acme\nPO Box 2290\nInvoice # A-1")["po_number"])

    def test_date_formats(self):
        for s in ("October 27, 2026", "27 Oct 2026", "10/27/2026", "2026-10-27"):
            self.assertEqual(parse_date(s), "2026-10-27")


if __name__ == "__main__":
    unittest.main()
