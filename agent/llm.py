"""Optional LLM extraction for layouts the rules can't read. One string picks the model:

  INVOICE_LLM=gemini:gemini-2.5-flash   (GEMINI_API_KEY)     INVOICE_LLM=anthropic:<model> (ANTHROPIC_API_KEY)
  INVOICE_LLM=groq:<model>              (GROQ_API_KEY)       INVOICE_LLM=openai:<model>    (OPENAI_API_KEY)
  INVOICE_LLM=ollama:llama3.1           (local)              INVOICE_LLM=custom:<model> + LLM_BASE_URL, LLM_API_KEY

The model only extracts. It never decides: its output goes through the same validation and rules.
"""
import json
import os
import re
import urllib.request

from .extract import FIELDS, parse_date

OPENAI_COMPATIBLE = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "ollama": ("http://localhost:11434/v1/chat/completions", None),
    "custom": (os.environ.get("LLM_BASE_URL", ""), "LLM_API_KEY"),
}

PROMPT = """Extract this vendor invoice into JSON with exactly these keys:
vendor_name, vendor_tax_id, invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD), po_number,
terms_days (int), subtotal, tax, tax_rate (percent number), total,
lines (list of {{description, qty, unit_price, amount}}).
Use null for anything not printed on the invoice. Never guess or compute a value that is not printed.
The buyer is {buyer}; vendor_name is the company that ISSUED the invoice.

INVOICE TEXT:
{text}"""


def configured():
    spec = os.environ.get("INVOICE_LLM", "")
    if not spec:
        return None
    provider = spec.split(":", 1)[0]
    if provider == "anthropic":
        return spec if os.environ.get("ANTHROPIC_API_KEY") else None
    if provider in OPENAI_COMPATIBLE:
        key_env = OPENAI_COMPATIBLE[provider][1]
        return spec if key_env is None or os.environ.get(key_env) else None
    return None


def _post(url, payload, headers):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def complete(spec, prompt):
    provider, _, model = spec.partition(":")
    if provider == "anthropic":
        data = _post("https://api.anthropic.com/v1/messages",
                     {"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in data["content"])
    url, key_env = OPENAI_COMPATIBLE[provider]
    headers = {"Authorization": f"Bearer {os.environ[key_env]}"} if key_env else {}
    data = _post(url, {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": prompt}]}, headers)
    return data["choices"][0]["message"]["content"]


def _float(v):
    try:
        return float(str(v).replace(",", "").replace("$", "")) if v not in (None, "") else None
    except ValueError:
        return None


def extract_llm(text, spec, buyer=""):
    raw = complete(spec, PROMPT.format(buyer=buyer, text=text[:12000]))
    data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    f = {k: data.get(k) for k in FIELDS}
    for k in ("subtotal", "tax", "tax_rate", "total"):
        f[k] = _float(f[k])
    for k in ("invoice_date", "due_date"):
        f[k] = parse_date(f[k]) if f[k] else None
    po = re.search(r"(\d{4}-\d{3,4})", str(f.get("po_number") or ""))
    f["po_number"] = f"PO-{po.group(1)}" if po else None
    f["terms_days"] = int(_float(f["terms_days"])) if _float(f["terms_days"]) else None
    f["lines"] = [{"description": str(l.get("description", "")), "qty": _float(l.get("qty")) or 0,
                   "unit_price": _float(l.get("unit_price")) or 0, "amount": _float(l.get("amount")) or 0}
                  for l in (data.get("lines") or []) if isinstance(l, dict)]
    return f
