"""Master data the process checks invoices against: vendors, purchase orders, policy."""
import csv
import json
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"


@dataclass
class Reference:
    vendors: dict      # vendor_id -> row (+ "aliases" list)
    pos: dict          # po_number -> row (+ "lines" list, "total")
    policy: dict
    history: list      # invoices already in the AP system before this demo


def _rows(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load():
    vendors = {}
    for r in _rows("vendors.csv"):
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        r["payment_terms_days"] = int(r["payment_terms_days"])
        vendors[r["vendor_id"]] = r
    pos = {r["po_number"]: {**r, "lines": []} for r in _rows("purchase_orders.csv")}
    for r in _rows("po_lines.csv"):
        pos[r["po_number"]]["lines"].append(
            {"description": r["description"], "qty": float(r["qty"]), "unit_price": float(r["unit_price"])})
    for po in pos.values():
        po["total"] = round(sum(l["qty"] * l["unit_price"] for l in po["lines"]), 2)
    policy = json.loads((DATA / "policy.json").read_text())
    return Reference(vendors, pos, policy, _rows("invoice_history.csv"))
