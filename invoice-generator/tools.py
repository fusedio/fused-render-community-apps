from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import shutil
import sys
import time
from datetime import date

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import invoice as backend  # noqa: E402

FLAG_FIELDS = {"status", "sent", "sent_date", "paid", "paid_date"}
MANAGED_FIELDS = {"id", "created", "modified"}
IMAGE_FIELDS = ("logo", "signature")
STATUSES = ("draft", "final")
THEMES = ("minimal", "band", "boxed")
DISCOUNT_MODES = ("pct", "flat")
DATE_FIELDS = ("issue_date", "due_date", "sent_date", "paid_date")
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FILTERS = {
    "all": lambda r: True,
    "outstanding": lambda r: not r["paid"],
    "awaiting": lambda r: r["sent"] and not r["paid"],
    "unsent": lambda r: not r["sent"] and not r["paid"],
    "paid": lambda r: r["paid"],
    "draft": lambda r: r["status"] != "final",
    "final": lambda r: r["status"] == "final",
}
_UNSET = object()


def _client_slugs():
    if not os.path.isdir(backend.CLIENTS):
        return []
    return sorted(s for s in os.listdir(backend.CLIENTS)
                  if os.path.isfile(os.path.join(backend.CLIENTS, s, "client.json")))


def _client_name(slug):
    return backend._read_json(os.path.join(backend.CLIENTS, slug, "client.json")).get("name", slug)


def _resolve_client(client):
    ref = (client or "").strip()
    if not ref:
        raise ValueError("client is required: pass a client slug or name from list_clients")
    slugs = _client_slugs()
    if ref in slugs:
        return ref
    for slug in slugs:
        if _client_name(slug).casefold() == ref.casefold():
            return slug
    try:
        slug = backend._slug(ref)
    except ValueError:
        slug = ""
    if slug in slugs:
        return slug
    raise ValueError(f"no client matches {ref!r}; see list_clients")


def _scope(client):
    return [_resolve_client(client)] if (client or "").strip() else _client_slugs()


def _locate(invoice, client=""):
    ref = (invoice or "").strip()
    if not ref:
        raise ValueError("invoice is required: pass an invoice id or number from list_invoices")
    hits = [(slug, doc) for slug in _scope(client) for doc in backend._invoices(slug)
            if ref == doc["id"] or ref.casefold() == doc["number"].casefold()]
    if not hits:
        raise ValueError(f"no invoice matches {ref!r}; see list_invoices")
    if len(hits) > 1:
        owners = ", ".join(slug for slug, _ in hits)
        raise ValueError(f"{ref!r} matches invoices of several clients ({owners}); pass client")
    return hits[0]


def _locked(doc):
    return ValueError(f"invoice {doc['number']} is final and locked; "
                      "set status to 'draft' with update_invoice first")


def _unlocked(invoice, client):
    slug, doc = _locate(invoice, client)
    if doc["status"] == "final":
        raise _locked(doc)
    return slug, doc


def _read_image(path, field):
    path = os.path.expanduser((path or "").strip())
    mime = IMAGE_MIME.get(os.path.splitext(path)[1].lower())
    if not mime or not os.path.isfile(path):
        raise ValueError(f"{field} must be an existing png, jpg, webp, gif or svg file, got {path!r}")
    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    with open(path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode()


def _with_images(changes):
    changes = dict(changes)
    for key in IMAGE_FIELDS:
        value = changes.get(key, "")
        if not value or (isinstance(value, str) and value.startswith("data:image/")):
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a local image path, a data:image URL, or '' to remove it")
        mime, b64 = _read_image(value, key)
        changes[key] = f"data:{mime};base64,{b64}"
    return changes


def _hide_images(obj):
    for key in IMAGE_FIELDS:
        if obj[key]:
            obj[key] = f"(image set, {len(obj[key]) * 3 // 4 // 1024} KB)"
    return obj


def _number(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    if isinstance(value, (int, float)):
        return value
    try:
        n = float(str(value).strip() or 0)
    except ValueError:
        raise ValueError(f"{field} must be a number, got {value!r}") from None
    return int(n) if n.is_integer() else n


def _date(value, field):
    if value in ("", None):
        return ""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{field} must be a YYYY-MM-DD date, got {value!r}") from None


def _currency(value, field="currency"):
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"{field} must be a 3-letter ISO currency code, got {value!r}")
    return code


def _rows(value, schema, field):
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise ValueError(f"{field} must be a list of objects with keys {sorted(schema)}")
    for i, row in enumerate(value):
        extra = set(row) - set(schema)
        if extra:
            raise ValueError(f"{field}[{i}] has unknown keys {sorted(extra)}; allowed: {sorted(schema)}")


def _check_contacts(obj, parties):
    _rows(obj["payment"], backend.KV, "payment")
    for party in parties:
        _rows(obj[party]["custom"], backend.KV, f"{party}.custom")


def _merge(base, changes, where=""):
    for key, value in changes.items():
        if key not in base:
            raise ValueError(f"unknown field {where}{key!r}; allowed: {sorted(base)}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"{where}{key} must be an object with keys {sorted(base[key])}")
            _merge(base[key], value, f"{where}{key}.")
        else:
            base[key] = copy.deepcopy(value)


def _fx(currency, quote):
    if quote is None or quote == "":
        return None
    if isinstance(quote, dict):
        quote = quote.get("quote")
    quote = _currency(quote, "fx")
    if quote == currency:
        raise ValueError(f"fx currency must differ from the invoice currency ({currency})")
    r = backend.main(action="fx_rate", base=currency, quote=quote)
    return {"quote": quote, "rate": r["rate"], "date": r["date"]}


def _check_doc(doc, changes):
    if doc["status"] not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    if doc["theme"] not in THEMES:
        raise ValueError(f"theme must be one of {list(THEMES)}")
    if doc["discount"]["mode"] not in DISCOUNT_MODES:
        raise ValueError(f"discount.mode must be one of {list(DISCOUNT_MODES)}")
    if not str(doc["number"]).strip():
        raise ValueError("number cannot be empty")
    doc["currency"] = _currency(doc["currency"])
    for field in DATE_FIELDS:
        doc[field] = _date(doc[field], field)
    for field in ("sent", "paid", "attach_list"):
        if not isinstance(doc[field], bool):
            raise ValueError(f"{field} must be true or false")
    _rows(doc["items"], backend.ITEM, "items")
    for i, item in enumerate(doc["items"]):
        for key in ("qty", "rate"):
            if key in item:
                item[key] = _number(item[key], f"items[{i}].{key}")
    _check_contacts(doc, ("billed_by", "billed_to"))
    doc["tax_pct"] = _number(doc["tax_pct"], "tax_pct")
    doc["shipping"] = _number(doc["shipping"], "shipping")
    doc["text_scale"] = _number(doc["text_scale"], "text_scale")
    doc["discount"]["value"] = _number(doc["discount"]["value"], "discount.value")
    explicit = changes.get("item_columns") or {}
    for col in ("hsn", "unit"):
        if col not in explicit and any(str(it.get(col) or "").strip() for it in doc["items"]):
            doc["item_columns"][col] = True


def _apply_flags(old, doc, changes):
    today = date.today().isoformat()
    if changes.get("sent") is False and changes.get("paid") is True:
        raise ValueError("an invoice cannot be paid without being sent")
    if old["sent"] and not doc["sent"]:
        doc["paid"] = False
    if doc["paid"] and not old["paid"] and not doc["paid_date"]:
        doc["paid_date"] = today
    if doc["sent"] and not old["sent"] and not doc["sent_date"]:
        doc["sent_date"] = today
    if not doc["paid"]:
        doc["paid_date"] = ""
    if not doc["sent"]:
        doc["sent_date"] = ""


def _edit(old, changes):
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object of invoice fields")
    if "attachments" in changes:
        raise ValueError("attachments are managed with attach_file and remove_attachment")
    managed = MANAGED_FIELDS & changes.keys()
    if managed:
        raise ValueError(f"{sorted(managed)} are managed by the app and cannot be set")
    unknown = changes.keys() - backend.DOC_SCHEMA.keys()
    if unknown:
        allowed = sorted(backend.DOC_SCHEMA.keys() - MANAGED_FIELDS - {"attachments"})
        raise ValueError(f"unknown fields {sorted(unknown)}; allowed: {allowed}")
    if old["status"] == "final" and changes.get("status", "final") == "final" and changes.keys() - FLAG_FIELDS:
        raise _locked(old)
    changes = _with_images(changes)
    fx = changes.pop("fx", _UNSET)
    doc = copy.deepcopy(old)
    _merge(doc, changes)
    _check_doc(doc, changes)
    _apply_flags(old, doc, changes)
    if fx is not _UNSET:
        doc["fx"] = _fx(doc["currency"], fx)
    elif doc["fx"] and doc["currency"] != old["currency"]:
        doc["fx"] = _fx(doc["currency"], doc["fx"].get("quote"))
    return doc


def _totals(doc):
    t = backend._totals(doc)
    out = {"currency": doc["currency"], **{k: round(v, 2) for k, v in t.items()}}
    fx = doc["fx"] or {}
    if fx.get("rate"):
        out["fx"] = {"currency": fx["quote"], "rate": fx["rate"], "date": fx["date"],
                     "total": round(t["total"] * fx["rate"], 2)}
    return out


def _invoice_view(slug, doc):
    return {"client": slug, "client_name": _client_name(slug), "invoice": _hide_images(copy.deepcopy(doc)),
            "totals": _totals(doc), "attach_dir": backend._attach_dir(slug, doc["id"])}


def _save(slug, doc):
    backend.main(action="save_invoice", client=slug, doc=json.dumps(doc))
    return _invoice_view(slug, backend.main(action="load_invoice", client=slug, id=doc["id"])["doc"])


def get_settings():
    return {"settings": _hide_images(backend.main(action="get_settings")["settings"])}


def update_settings(changes: dict):
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty object of settings fields")
    s = backend.main(action="get_settings")["settings"]
    _merge(s, _with_images(changes))
    _check_contacts(s, ("billed_by",))
    s["home_currency"] = _currency(s["home_currency"], "home_currency")
    backend.main(action="save_settings", settings=json.dumps(s))
    return get_settings()


def list_clients():
    return backend.main(action="list_clients")


def create_client(name: str):
    return backend.main(action="create_client", name=name)


def rename_client(client: str, name: str):
    return backend.main(action="rename_client", client=_resolve_client(client), name=name)


def delete_client(client: str):
    slug = _resolve_client(client)
    count = len(backend._invoices(slug))
    backend.main(action="delete_client", client=slug)
    return {"ok": True, "deleted": slug, "invoices_deleted": count}


def list_invoices(client: str = "", show: str = "all"):
    key = (show or "all").strip().lower()
    if key not in FILTERS:
        raise ValueError(f"show must be one of {list(FILTERS)}")
    rows = []
    for slug in _scope(client):
        name = _client_name(slug)
        rows += [{"client": slug, "client_name": name, **r}
                 for r in backend.main(action="list_invoices", client=slug)["invoices"]
                 if FILTERS[key](r)]
    rows.sort(key=lambda r: (r["issue_date"], r["modified"]), reverse=True)
    sums = {}
    for r in rows:
        sums[r["currency"]] = round(sums.get(r["currency"], 0) + r["total"], 2)
    return {"invoices": rows, "total_by_currency": sums}


def get_invoice(invoice: str, client: str = ""):
    return _invoice_view(*_locate(invoice, client))


def create_invoice(client: str, changes: dict = None):
    slug = _resolve_client(client)
    draft = backend.main(action="new_invoice", client=slug)["doc"]
    return _save(slug, _edit(draft, changes))


def update_invoice(invoice: str, changes: dict, client: str = ""):
    if not changes:
        raise ValueError("changes is empty: pass the invoice fields to set")
    slug, old = _locate(invoice, client)
    return _save(slug, _edit(old, changes))


def duplicate_invoice(invoice: str, client: str = "", changes: dict = None):
    slug, src = _locate(invoice, client)
    r = backend.main(action="duplicate_invoice", client=slug, id=src["id"])
    try:
        doc = _edit(r["doc"], changes)
    except Exception:
        shutil.rmtree(r["attach_dir"], ignore_errors=True)
        raise
    return _save(slug, doc)


def delete_invoice(invoice: str, client: str = ""):
    slug, doc = _locate(invoice, client)
    backend.main(action="delete_invoice", client=slug, id=doc["id"])
    return {"ok": True, "client": slug, "deleted": doc["id"], "number": doc["number"]}


def attach_file(invoice: str, path: str, label: str = "", client: str = ""):
    slug, doc = _unlocked(invoice, client)
    path = os.path.expanduser((path or "").strip())
    if path.lower().endswith(".pdf"):
        raise ValueError("PDFs are split into pages by the app in the browser; add PDFs from the "
                         "invoice's Attachments panel, or attach each page as an image")
    mime, b64 = _read_image(path, "path")
    att_id = f"att-{int(time.time() * 1000):x}{secrets.token_hex(2)}"
    r = backend.main(action="attach_page", client=slug, id=doc["id"], att=att_id,
                     data=json.dumps({"index": 1, "mime": mime, "b64": b64, "w": 0, "h": 0}))
    name = os.path.basename(path)
    doc["attachments"].append({"id": att_id, "label": (label or "").strip() or os.path.splitext(name)[0],
                               "filename": name, "kind": "image", "pages": [r["page"]]})
    return _save(slug, doc)


def remove_attachment(invoice: str, attachment: str, client: str = ""):
    slug, doc = _unlocked(invoice, client)
    ref = str(attachment or "").strip()
    atts = doc["attachments"]
    index = next((i for i, a in enumerate(atts)
                  if ref in (a["id"], str(i + 1)) or (a["label"] and ref.casefold() == a["label"].casefold())),
                 None)
    if index is None:
        have = [f"{i + 1}: {a['label'] or a['filename']} ({a['id']})" for i, a in enumerate(atts)]
        raise ValueError(f"no attachment matches {ref!r}; this invoice has {have or 'none'}")
    atts.pop(index)
    return _save(slug, doc)


def fx_rate(base: str, quote: str):
    return backend.main(action="fx_rate", base=base, quote=quote)
