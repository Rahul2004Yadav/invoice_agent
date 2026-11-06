#!/usr/bin/env python3
"""Process invoices from the terminal (the same pipeline the web app runs).

  python run.py samples/1_happy_path_delta.pdf [more.pdf ...]
  python run.py --reset            # restore demo data (invoice register + empty run history)
"""
import sys
from pathlib import Path

from agent import store
from agent.pipeline import InvoiceRun
from agent.reference import load

ICON = {"pass": "✅", "warn": "⚠️ ", "fail": "❌"}


def main(argv):
    ref = load()
    conn = store.connect(ref=ref)
    if "--reset" in argv:
        store.reset(conn, ref)
        print("Demo data reset.")
        argv = [a for a in argv if a != "--reset"]
    for path in argv:
        run = InvoiceRun(Path(path).read_bytes(), Path(path).name, ref, conn)
        print(f"\n=== {Path(path).name}")
        for st in run.steps():
            print(f"{ICON[st.status]} {st.title:<17} {st.summary}  ({st.ms} ms)")
        r = run.record
        print(f"--> {r['decision']}: {r['headline']}")
        for f in r["findings"]:
            if f["severity"] != "note":
                continue
            print(f"    note: {f['message']}")
        print(f"    next: {r['next_action']}")


if __name__ == "__main__":
    main(sys.argv[1:])
