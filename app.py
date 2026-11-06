"""AP Invoice Agent: Streamlit UI.  Run: streamlit run app.py"""
import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

from agent import store
from agent.llm import configured as llm_configured
from agent.pipeline import APPROVED, REJECTED, REVIEW, InvoiceRun, usd
from agent.reader import render_preview
from agent.reference import load

ROOT = Path(__file__).resolve().parent
SAMPLES = {
    "1_happy_path_delta.pdf": "Happy path: Delta Precision, clean digital invoice",
    "2a_split_invoice_ok_brightline.pdf": "Edge 1a: split invoice, shipment 3 of 3 (fits the PO)",
    "2b_split_invoice_overbill_brightline.pdf": "Edge 1b: split invoice that over-bills the PO",
    "3_duplicate_resubmitted_acme.pdf": "Edge 2: duplicate re-sent with a reformatted number",
    "4_missing_po_coastal.pdf": "Edge 3: no PO number on the invoice",
    "5_scanned_image_acme.pdf": "Edge 4: scanned image, no text layer (OCR)",
    "6_extra_blocked_vendor_evergreen.pdf": "Extra: blocked vendor asking for new bank details",
    "7_extra_price_variance_coastal.pdf": "Extra: unit price above the PO",
}
STAGES = ["Ingest document", "Read text", "Extract fields", "Validate invoice", "Vendor check",
          "Duplicate check", "PO match", "Decide & record"]
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
SEV_ICON = {"reject": "❌", "review": "⚠️", "note": "ℹ️"}
COLORS = {APPROVED: ("#0f7b3f", "#e8f6ee"), REVIEW: ("#9a6200", "#fff6e0"), REJECTED: ("#b42318", "#fdecea")}

st.set_page_config(page_title="AP Invoice Agent", page_icon="🧾", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 1.6rem;}
.card {border-radius: 10px; padding: 16px 20px; margin: 8px 0 14px; border: 1px solid rgba(0,0,0,.08);}
.card .dec {font-size: 1.5rem; font-weight: 700; letter-spacing: .02em;}
.card .head {font-size: 1.02rem; margin: 4px 0 8px;}
.card .meta {font-size: .9rem; opacity: .85;}
.pending {opacity: .45; padding: 6px 2px;}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def resources():
    ref = load()
    return ref, store.connect(ref=ref)


ref, conn = resources()


def md(text):
    """Streamlit markdown treats $...$ as LaTeX; escape dollar signs in data."""
    return str(text).replace("$", "\\$")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🧾 AP Invoice Agent")
    st.caption(f"Accounts payable for **{ref.policy['buyer_company']}**: invoice PDF in, reasoned decision out.")
    page = st.radio("Go to", ["▶️ Process invoice", "📊 Dashboard", "📚 Master data", "🗺️ How it works"],
                    label_visibility="collapsed")
    st.divider()
    spec = llm_configured()
    st.markdown("**Reading engines**")
    st.markdown("- PDF text layer: on\n- OCR (offline RapidOCR): on\n"
                f"- LLM fallback: {'`' + spec + '`' if spec else 'off (set `INVOICE_LLM`)'}")
    st.divider()
    if st.button("↺ Reset demo data", width="stretch",
                 help="Clears run history and restores the invoice register to its starting state."):
        store.reset(conn, ref)
        st.session_state.pop("last", None)
        st.toast("Demo data reset")


# ---------------------------------------------------------------- shared renderers
def decision_card(r):
    fg, bg = COLORS[r["decision"]]
    f = r["fields"]
    st.markdown(f"""<div class="card" style="background:{bg}; border-left: 6px solid {fg};">
      <div class="dec" style="color:{fg}">{r['decision']}</div>
      <div class="head">{r['headline']}</div>
      <div class="meta"><b>Next action:</b> {r['next_action']} &nbsp;·&nbsp; <b>Owner:</b> {r['owner']}</div>
      <div class="meta">{r.get('vendor_name') or f.get('vendor_name') or '?'} · invoice {f.get('invoice_number') or '?'}
       · {usd(f.get('total'))} · PO {r.get('po_number') or '—'}{' (inferred)' if r.get('po_inferred') else ''}
       · read by {r['read_method']} · extracted by {r['extractor']} · {r['duration_ms']} ms</div>
    </div>""", unsafe_allow_html=True)
    blocking = [x for x in r["findings"] if x["severity"] != "note"]
    notes = [x for x in r["findings"] if x["severity"] == "note"]
    if blocking:
        st.markdown("**Why**")
        for x in blocking:
            st.markdown(f"{SEV_ICON[x['severity']]} {md(x['message'])}" + (f"  \n&nbsp;&nbsp;&nbsp;&nbsp;→ *{x['owner']}*"
                                                                      if x["owner"] else ""))
    if notes:
        st.markdown("**Notes**")
        for x in notes:
            st.markdown(f"ℹ️ {md(x['message'])}")


def stage_body(s):
    d = s["details"] or {}
    k = s["key"]
    if k == "read" and d.get("text_preview"):
        if d.get("ocr_confidence") is not None:
            st.metric("OCR confidence", f"{d['ocr_confidence']:.0%}")
        st.code("\n".join(d["text_preview"]), language=None)
    elif k == "extract":
        st.caption(f"Extractor: {d.get('extractor')}")
        st.dataframe(pd.DataFrame([{"field": a, "value": "" if b is None else str(b)}
                                   for a, b in (d.get("fields") or {}).items()]), hide_index=True,
                     width="stretch")
        if d.get("lines"):
            st.dataframe(pd.DataFrame(d["lines"]), hide_index=True, width="stretch")
        if d.get("llm_error"):
            st.warning(f"LLM fallback failed: {d['llm_error']}")
    elif k == "validate":
        st.dataframe(pd.DataFrame([{"": "✅" if c["ok"] else "❌", "check": c["check"], "detail": c["detail"]}
                                   for c in d.get("checks", [])]), hide_index=True, width="stretch")
    elif k == "po":
        if d.get("candidates"):
            st.caption("No PO number on the invoice: scored the vendor's open POs (amount fit + line match)")
            st.dataframe(pd.DataFrame(d["candidates"]), hide_index=True, width="stretch")
        if d.get("po"):
            c = st.columns(4)
            c[0].metric("PO total", usd(d["po_total"]))
            c[1].metric("Already invoiced", usd(d["previously_invoiced"]))
            c[2].metric("This invoice", usd(d["this_invoice"]))
            c[3].metric("PO used after", f"{d['pct_of_po_used']:.0f}%")
            if d.get("lines"):
                st.dataframe(pd.DataFrame(d["lines"]), hide_index=True, width="stretch")
    elif d:
        st.json(d, expanded=False)
    for x in s["findings"]:
        st.markdown(f"{SEV_ICON[x['severity']]} {md(x['message'])}")


def stage_label(i, s):
    return f"{ICON[s['status']]} **{i}. {s['title']}** · {md(s['summary'])} · `{s['ms']} ms`"


def record_tabs(r):
    t1, t2, t3 = st.tabs(["Stage log", "Vendor email draft", "Raw record (JSON)"])
    with t1:
        for i, s in enumerate(r["stages"], 1):
            with st.expander(stage_label(i, s)):
                stage_body(s)
    with t2:
        if r.get("vendor_email"):
            st.text_area("Ready to send after review", r["vendor_email"], height=260)
        else:
            st.caption("No vendor action needed for this decision.")
    with t3:
        st.json(r, expanded=False)


# ---------------------------------------------------------------- pages
def page_process():
    st.header("Process an invoice")
    left, right = st.columns([1, 1.1], gap="large")
    with left:
        source = st.radio("Input", ["Sample invoice", "Upload a PDF"], horizontal=True)
        if source == "Sample invoice":
            name = st.selectbox("Sample", list(SAMPLES), format_func=lambda n: SAMPLES[n])
            data = (ROOT / "samples" / name).read_bytes()
        else:
            up = st.file_uploader("Invoice PDF", type=["pdf"])
            name, data = (up.name, up.getvalue()) if up else (None, None)
        modes = {"Auto: rules first, LLM if fields are missing": "auto", "Rules only": "rules"}
        if spec:
            modes["LLM extraction"] = "llm"
        mode = modes[st.selectbox("Extraction", list(modes))]
        pace = st.slider("Demo pace (seconds per stage)", 0.0, 1.5, 0.5, 0.1)
        if st.session_state.get("last", {}).get("filename") != name:
            st.session_state.pop("last", None)   # don't show a previous invoice's decision
        go = st.button("▶ Run process", type="primary", disabled=data is None, width="stretch")
    with right:
        if data:
            try:
                st.image(render_preview(data), width="stretch")
            except Exception as e:
                st.error(f"Can't render this PDF: {e}")

    if go and data:
        st.subheader("Live run")
        bar = st.progress(0.0, text="Starting…")
        slots = [st.empty() for _ in STAGES]
        for i, t in enumerate(STAGES):
            slots[i].markdown(f"<div class='pending'>⏳ {i + 1}. {t}</div>", unsafe_allow_html=True)
        run = InvoiceRun(data, name, ref, conn, extractor=mode)
        slots[0].markdown(f"🔄 **1. {STAGES[0]}**: running…")
        for i, s in enumerate(run.steps()):
            s = asdict(s)
            with slots[i].container():
                with st.expander(stage_label(i + 1, s), expanded=s["status"] != "pass"):
                    stage_body(s)
            bar.progress((i + 1) / len(STAGES), text=f"{i + 1}/{len(STAGES)} stages")
            if i + 1 < len(STAGES):
                slots[i + 1].markdown(f"🔄 **{i + 2}. {STAGES[i + 1]}**: running…")
                time.sleep(pace)
        bar.empty()
        st.session_state["last"] = run.record

    r = st.session_state.get("last")
    if r:
        st.subheader("Decision")
        decision_card(r)
        t1, t2 = st.tabs(["Vendor email draft", "Raw record (JSON)"])
        with t1:
            if r.get("vendor_email"):
                st.text_area("Ready to send after review", r["vendor_email"], height=260)
            else:
                st.caption("No vendor action needed for this decision.")
        with t2:
            st.json(r, expanded=False)


def page_dashboard():
    st.header("Dashboard")
    rows = store.runs(conn)
    if not rows:
        st.info("No runs yet. Process an invoice to see it here.")
        return
    df = pd.DataFrame(rows)
    n = len(df)
    by = df["decision"].value_counts()
    approved = df[df.decision == APPROVED]
    c = st.columns(6)
    c[0].metric("Invoices processed", n)
    c[1].metric("Auto-approved", f"{by.get(APPROVED, 0)}", f"{by.get(APPROVED, 0) / n:.0%} touchless",
                delta_color="off")
    c[2].metric("Needs review", by.get(REVIEW, 0))
    c[3].metric("Rejected", by.get(REJECTED, 0))
    c[4].metric("Approved value", usd(approved.total.fillna(0).sum()))
    c[5].metric("Avg. processing", f"{df.duration_ms.mean() / 1000:.1f} s")

    g1, g2 = st.columns(2)
    with g1:
        st.caption("Decisions")
        st.bar_chart(by.reindex([APPROVED, REVIEW, REJECTED]).fillna(0))
    with g2:
        st.caption("Exception queue: who needs to act")
        q = []
        for r in rows:
            if r["decision"] != APPROVED:
                rec = json.loads(r["record_json"])
                q.append({"invoice": r["invoice_number"], "vendor": r["vendor"], "decision": r["decision"],
                          "owner": rec["owner"], "reason": r["headline"][:90]})
        if q:
            st.dataframe(pd.DataFrame(q), hide_index=True, width="stretch")
        else:
            st.caption("Nothing waiting 🎉")

    st.subheader("Run history")
    pick = st.multiselect("Filter", [APPROVED, REVIEW, REJECTED], default=[APPROVED, REVIEW, REJECTED])
    view = df[df.decision.isin(pick)][["created_at", "filename", "vendor", "invoice_number", "po_number", "total",
                                      "decision", "headline", "extractor", "duration_ms", "id"]]
    st.dataframe(view, hide_index=True, width="stretch",
                 column_config={"total": st.column_config.NumberColumn(format="$%.2f"),
                                "headline": st.column_config.TextColumn(width="large")})
    rid = st.selectbox("Open a run", view["id"].tolist(),
                       format_func=lambda i: f"{df.set_index('id').loc[i, 'filename']}  ·  "
                                             f"{df.set_index('id').loc[i, 'decision']}  ·  {i}")
    if rid:
        rec = store.run_record(conn, rid)
        decision_card(rec)
        record_tabs(rec)


def page_master():
    st.header("Master data the process checks against")
    t1, t2, t3, t4 = st.tabs(["Vendors", "Purchase orders", "Invoice register", "Policy"])
    with t1:
        st.dataframe(pd.DataFrame([{k: (", ".join(v) if isinstance(v, list) else v) for k, v in x.items()}
                                   for x in ref.vendors.values()]), hide_index=True, width="stretch")
    with t2:
        rows = []
        for po in ref.pos.values():
            used, invs = store.po_consumed(conn, po["po_number"])
            rows.append({"po_number": po["po_number"], "vendor": ref.vendors[po["vendor_id"]]["name"],
                         "status": po["status"], "buyer": po["buyer"], "po_total": po["total"],
                         "invoiced": used, "remaining": round(po["total"] - used, 2), "invoices": ", ".join(invs)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("PO lines")
        st.dataframe(pd.DataFrame([{"po_number": p, **l} for p, po in ref.pos.items() for l in po["lines"]]),
                     hide_index=True, width="stretch")
    with t3:
        st.caption("Every invoice the AP system knows about: history plus everything processed here. "
                   "Used for duplicate detection; approved and paid ones use up PO budget.")
        st.dataframe(pd.DataFrame(store.register(conn)), hide_index=True, width="stretch")
    with t4:
        st.json(ref.policy)


def page_how():
    st.header("How it works")
    st.graphviz_chart("""digraph { rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef3fb",
      fontname="Helvetica", fontsize=11]; edge [color="#888"];
      pdf [label="Invoice PDF", fillcolor="#fff"]; a [label="1 Ingest"]; b [label="2 Read\\ntext layer / OCR"];
      c [label="3 Extract\\nrules (+LLM fallback)"]; d [label="4 Validate\\nmaths, dates, tax"];
      e [label="5 Vendor\\nmaster, status, fraud"]; f [label="6 Duplicates\\nnumber core, amount"];
      g [label="7 PO match\\nexplicit / inferred,\\nprice, qty, balance"];
      h [label="8 Decide", fillcolor="#fff3d6"];
      ok [label="APPROVED\\n→ payment run", fillcolor="#e8f6ee"]; rv [label="NEEDS REVIEW\\n→ named owner", fillcolor="#fff6e0"];
      rj [label="REJECTED\\n→ vendor email", fillcolor="#fdecea"];
      pdf->a->b->c->d->e->f->g->h; h->ok; h->rv; h->rj; }""")
    st.markdown("""
**Design principle: AI reads, rules decide.** Perception (OCR, optional LLM) turns a messy PDF into fields.
Every approve/hold/reject comes from explicit rules a finance team can audit, and every finding says who must act.

| Rule | Outcome |
|---|---|
| Vendor blocked, exact duplicate, or same invoice re-sent with a reformatted number + same amount | **REJECTED** |
| Missing required field, maths don't add up, implausible tax, future/stale date | **NEEDS REVIEW** (AP clerk) |
| Unknown vendor, tax ID mismatch, request to change bank details | **NEEDS REVIEW** (procurement / AP manager) |
| PO not found, closed, wrong vendor, ambiguous inference | **NEEDS REVIEW** |
| Unit price outside ±2%, quantity above ordered, PO balance exceeded (beyond 2% / $25) | **NEEDS REVIEW** (PO buyer) |
| Total above $25,000 auto-approval limit | **NEEDS REVIEW** (AP manager) |
| None of the above | **APPROVED**, scheduled by due date |

**Edge cases built in**
1. **Split billing / over-billing.** One PO is billed in several invoices. Same-amount invoices are accepted while the PO
   has budget; the one that pushes it over is held with the arithmetic shown.
2. **Disguised duplicate.** `417` vs `INV-00417`, new date, "second notice". Matched on the number's digit core + vendor + amount.
3. **Missing PO number.** The vendor's open POs are scored on amount fit and line-item similarity; auto-matched only if one
   clearly wins, otherwise held.
4. **Scanned invoice.** No text layer, so offline OCR. OCR can misread, so the invoice's own arithmetic (lines → subtotal → tax → total)
   is used as a check on the reading.
""")


{"▶️ Process invoice": page_process, "📊 Dashboard": page_dashboard, "📚 Master data": page_master,
 "🗺️ How it works": page_how}[page]()
