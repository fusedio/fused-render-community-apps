# /// script
# dependencies = []
# ///
"""Backend for invoice_generator — a local invoice manager (clients, invoice
documents, numbering, FX reference rates).

One bare `main(action=...)` dispatcher (the fused-render contract; bare on
purpose — see the note at the definition). Pure stdlib: documents are plain
JSON files on disk, one invoice per file, grouped per client. Every action
returns a dict so an AI agent can drive the whole app headlessly exactly as
the UI does.

Actions
  health                       -> {ok: true}
  get_settings                 -> {settings}  (schema-filled)
  save_settings(settings)      -> {ok: true}
  list_clients                 -> {clients:[{slug, name, invoice_count, modified}]}
  create_client(name)          -> {slug, name}
  rename_client(client, name)  -> {slug, name}  (slug unchanged)
  delete_client(client)        -> {ok: true}
  list_invoices(client)        -> {invoices:[{id, number, issue_date, status,
                                              total, currency, modified}]}
  new_invoice(client)          -> {doc}  (composed, not saved)
  duplicate_invoice(client,id) -> {doc}  (fresh id/number/date, not saved)
  load_invoice(client, id)     -> {doc}
  save_invoice(client, doc)    -> {ok, id, modified}
  delete_invoice(client, id)   -> {ok: true}
  fx_rate(base, quote)         -> {rate, date, cached}
"""
import copy
import json
import os
import re
import secrets
import shutil
from datetime import date, datetime

# NOTE: bare `def main` (no @fused.udf) is deliberate — under the built-in
# executor the worker calls main() by its own signature; @fused.udf hides that
# signature and triggers a hosted-auth flow that times out.

DATA_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "data", "invoice_generator"))
CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "invoice_generator"))
SETTINGS = os.path.join(DATA_ROOT, "settings.json")
CLIENTS = os.path.join(DATA_ROOT, "clients")
FX_DIR = os.path.join(CACHE_ROOT, "fx")

KV = {"label": "", "value": ""}
ITEM = {"desc": "", "qty": 1, "rate": 0, "hsn": "", "unit": ""}

SETTINGS_SCHEMA = {
    "billed_by": {"name": "", "email": "", "phone": "", "address": "", "custom": []},
    "logo": "",
    "signature": "",
    "home_currency": "USD",
    "default_terms": "",
    "payment": [],
}

DOC_SCHEMA = {
    "id": "",
    "number": "",
    "status": "draft",
    "issue_date": "",
    "due_date": "",
    "po": "",
    "currency": "USD",
    "fx": None,
    "theme": "minimal",
    "text_scale": 1,
    "labels": {"billed_by": "Billed by", "billed_to": "Billed to"},
    "billed_by": {"name": "", "email": "", "phone": "", "address": "", "custom": []},
    "billed_to": {"name": "", "email": "", "phone": "", "address": "", "custom": []},
    "logo": "",
    "items": [ITEM],
    "item_columns": {"hsn": False, "unit": False},
    "discount": {"mode": "pct", "value": 0},
    "tax_pct": 0,
    "shipping": 0,
    "notes": "",
    "terms": "",
    "payment": [],
    "signature": "",
    "created": "",
    "modified": "",
}


# ---------------------------------------------------------------------- helpers
def _now():
    return datetime.now().isoformat(timespec="seconds")


def _fill(default, value):
    if isinstance(default, dict) and isinstance(value, dict):
        return {k: _fill(v, value[k]) if k in value else copy.deepcopy(v)
                for k, v in default.items()}
    return copy.deepcopy(default if value is None else value)


def _fill_settings(s):
    s = _fill(SETTINGS_SCHEMA, s or {})
    s["billed_by"]["custom"] = [_fill(KV, e) for e in s["billed_by"]["custom"]]
    s["payment"] = [_fill(KV, e) for e in s["payment"]]
    return s


def _fill_doc(doc):
    doc = _fill(DOC_SCHEMA, doc or {})
    doc["items"] = [_fill(ITEM, it) for it in doc["items"]]
    for party in ("billed_by", "billed_to"):
        doc[party]["custom"] = [_fill(KV, e) for e in doc[party]["custom"]]
    doc["payment"] = [_fill(KV, e) for e in doc["payment"]]
    return doc


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not s:
        raise ValueError("client name needs at least one letter or number")
    return s


def _client_dir(client):
    d = os.path.join(CLIENTS, client)
    if not os.path.isfile(os.path.join(d, "client.json")):
        raise ValueError(f"no such client: {client}")
    return d


def _invoice_path(client, inv_id):
    p = os.path.join(_client_dir(client), "invoices", inv_id + ".json")
    if not os.path.isfile(p):
        raise ValueError(f"no such invoice: {inv_id}")
    return p


def _invoices(client):
    d = os.path.join(_client_dir(client), "invoices")
    if not os.path.isdir(d):
        return []
    return [_fill_doc(_read_json(os.path.join(d, n)))
            for n in sorted(os.listdir(d)) if n.endswith(".json")]


def _new_id():
    return f"inv-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(2)}"


def _next_number(client_name, docs):
    word = (client_name or "").split()[0] if (client_name or "").split() else ""
    prefix = re.sub(r"[^A-Za-z0-9]", "", word).upper()[:6] or "INV"
    seq = 0
    for doc in docs:
        m = re.search(r"-(\d+)$", doc.get("number") or "")
        if m:
            seq = max(seq, int(m.group(1)))
    return f"{prefix}-{date.today().year}-{seq + 1:03d}"


def _total(doc):
    subtotal = sum(float(it.get("qty") or 0) * float(it.get("rate") or 0)
                   for it in doc["items"])
    disc = doc["discount"]
    value = float(disc.get("value") or 0)
    discount = subtotal * value / 100 if disc.get("mode") == "pct" else value
    taxable = subtotal - discount
    tax = taxable * float(doc.get("tax_pct") or 0) / 100
    return taxable + tax + float(doc.get("shipping") or 0)


# --------------------------------------------------------------------- actions
def _get_settings():
    s = _read_json(SETTINGS) if os.path.isfile(SETTINGS) else {}
    return {"settings": _fill_settings(s)}


def _save_settings(settings):
    _write_json(SETTINGS, _fill_settings(json.loads(settings)))
    return {"ok": True}


def _list_clients():
    clients = []
    if os.path.isdir(CLIENTS):
        for slug in os.listdir(CLIENTS):
            cpath = os.path.join(CLIENTS, slug, "client.json")
            if not os.path.isfile(cpath):
                continue
            info = _read_json(cpath)
            inv_dir = os.path.join(CLIENTS, slug, "invoices")
            files = [os.path.join(inv_dir, n) for n in os.listdir(inv_dir)
                     if n.endswith(".json")] if os.path.isdir(inv_dir) else []
            mtime = max([os.path.getmtime(cpath)] + [os.path.getmtime(p) for p in files])
            clients.append({"slug": slug, "name": info.get("name", slug),
                            "invoice_count": len(files),
                            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds")})
    clients.sort(key=lambda c: c["modified"], reverse=True)
    return {"clients": clients}


def _create_client(name):
    name = (name or "").strip()
    slug = _slug(name)
    d = os.path.join(CLIENTS, slug)
    if os.path.isfile(os.path.join(d, "client.json")):
        raise ValueError(f'a client named "{name}" already exists')
    _write_json(os.path.join(d, "client.json"),
                {"name": name, "slug": slug, "created": _now()})
    return {"slug": slug, "name": name}


def _rename_client(client, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("client name cannot be empty")
    cpath = os.path.join(_client_dir(client), "client.json")
    info = _read_json(cpath)
    info["name"] = name
    _write_json(cpath, info)
    return {"slug": client, "name": name}


def _delete_client(client):
    shutil.rmtree(_client_dir(client))
    return {"ok": True}


def _list_invoices(client):
    rows = [{"id": d["id"], "number": d["number"], "issue_date": d["issue_date"],
             "status": d["status"], "total": round(_total(d), 2),
             "currency": d["currency"], "modified": d["modified"]}
            for d in _invoices(client)]
    rows.sort(key=lambda r: (r["issue_date"], r["modified"]), reverse=True)
    return {"invoices": rows}


def _new_invoice(client):
    info = _read_json(os.path.join(_client_dir(client), "client.json"))
    docs = _invoices(client)
    if docs:
        doc = max(docs, key=lambda d: d["modified"])
        doc["items"] = [copy.deepcopy(ITEM)]
        doc["shipping"] = 0
    else:
        doc = _fill_doc({})
        s = _get_settings()["settings"]
        doc["billed_by"] = s["billed_by"]
        doc["logo"] = s["logo"]
        doc["signature"] = s["signature"]
        doc["terms"] = s["default_terms"]
        doc["payment"] = s["payment"]
        doc["currency"] = s["home_currency"]
        doc["billed_to"]["name"] = info["name"]
    doc["id"] = _new_id()
    doc["number"] = _next_number(info["name"], docs)
    doc["issue_date"] = date.today().isoformat()
    doc["due_date"] = ""
    doc["po"] = ""
    doc["notes"] = ""
    doc["status"] = "draft"
    doc["created"] = doc["modified"] = _now()
    return {"doc": doc}


def _duplicate_invoice(client, inv_id):
    info = _read_json(os.path.join(_client_dir(client), "client.json"))
    doc = _fill_doc(_read_json(_invoice_path(client, inv_id)))
    doc["id"] = _new_id()
    doc["number"] = _next_number(info["name"], _invoices(client))
    doc["issue_date"] = date.today().isoformat()
    doc["due_date"] = ""
    doc["status"] = "draft"
    doc["created"] = doc["modified"] = _now()
    return {"doc": doc}


def _load_invoice(client, inv_id):
    return {"doc": _fill_doc(_read_json(_invoice_path(client, inv_id)))}


def _save_invoice(client, doc):
    d = _fill_doc(json.loads(doc))
    if not d["id"]:
        raise ValueError("invoice has no id")
    for other in _invoices(client):
        if other["id"] != d["id"] and other["number"] == d["number"]:
            raise ValueError(f'invoice number "{d["number"]}" is already used')
    d["modified"] = _now()
    _write_json(os.path.join(_client_dir(client), "invoices", d["id"] + ".json"), d)
    return {"ok": True, "id": d["id"], "modified": d["modified"]}


def _delete_invoice(client, inv_id):
    os.remove(_invoice_path(client, inv_id))
    return {"ok": True}


def _fx_rate(base, quote):
    base, quote = (base or "").upper(), (quote or "").upper()
    if not base or not quote:
        raise ValueError("fx_rate needs base and quote currencies")
    today_path = os.path.join(FX_DIR, f"{date.today().isoformat()}-{base}-{quote}.json")
    if os.path.isfile(today_path):
        cached = _read_json(today_path)
        return {"rate": cached["rate"], "date": cached["date"], "cached": True}
    import urllib.request
    url = f"https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}"
    # frankfurter 403s the default Python-urllib agent
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render-invoice/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
    except OSError:
        suffix = f"-{base}-{quote}.json"
        old = sorted(n for n in (os.listdir(FX_DIR) if os.path.isdir(FX_DIR) else [])
                     if n.endswith(suffix))
        if not old:
            raise ValueError("Exchange rate unavailable offline")
        cached = _read_json(os.path.join(FX_DIR, old[-1]))
        return {"rate": cached["rate"], "date": cached["date"], "cached": True}
    rate = data.get("rates", {}).get(quote)
    if rate is None:
        raise ValueError(f"no exchange rate for {base} to {quote}")
    _write_json(today_path, {"rate": rate, "date": data["date"]})
    return {"rate": rate, "date": data["date"], "cached": False}


# ----------------------------------------------------------------- dispatcher
def main(
    action: str = "health",
    client: str = "",
    id: str = "",
    name: str = "",
    doc: str = "",
    settings: str = "",
    base: str = "",
    quote: str = "",
):
    if action == "health":
        return {"ok": True}
    if action == "get_settings":
        return _get_settings()
    if action == "save_settings":
        return _save_settings(settings)
    if action == "list_clients":
        return _list_clients()
    if action == "create_client":
        return _create_client(name)
    if action == "rename_client":
        return _rename_client(client, name)
    if action == "delete_client":
        return _delete_client(client)
    if action == "list_invoices":
        return _list_invoices(client)
    if action == "new_invoice":
        return _new_invoice(client)
    if action == "duplicate_invoice":
        return _duplicate_invoice(client, id)
    if action == "load_invoice":
        return _load_invoice(client, id)
    if action == "save_invoice":
        return _save_invoice(client, doc)
    if action == "delete_invoice":
        return _delete_invoice(client, id)
    if action == "fx_rate":
        return _fx_rate(base, quote)
    raise ValueError(f"unknown action {action!r}")
