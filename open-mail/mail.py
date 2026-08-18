# /// script
# dependencies = [
#     "google-api-python-client",
#     "google-auth-oauthlib",
# ]
# ///
# NOTE: deliberately NO @fused.udf here. Where the fused SDK is importable in
# the run venv, a decorated udf executes on the configured REMOTE Fused env
# (no local filesystem, no subprocess) — useless for a mail client that owns
# local token files and spawns a local OAuth browser flow. A bare main() keeps
# the builtin engine's child on the user's own machine. Run the server with
# FUSED_RENDER_ENGINE=builtin (the fused engine never calls a bare main()).
def main(
    op: str = "accounts",
    account: str = "",
    label: str = "INBOX",
    q: str = "",
    page_token: str = "",
    thread: str = "",
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    body: str = "",
    reply_to_message: str = "",
    add_labels: list = None,
    remove_labels: list = None,
    attachments: list = None,
    message_id: str = "",
    attachment_id: str = "",
    filename: str = "",
    mark_read: bool = False,
    app_password: str = "",
    config: dict = None,
    ids: list = None,
    cache: dict = None,
) -> dict:
    # engine may execute the body isolated from module globals — everything lives in here
    import base64
    import json
    import os
    import subprocess
    import sys
    import time

    MAIL_DIR = os.path.expanduser("~/.fused-mail")

    # ------------------------------------------------------------------ paths
    # Everything the app ships (this script, add_account.py, vendor_imaplib.py,
    # an optional bundled credentials.json) is resolved against APP_DIR, never
    # the process cwd — the engine may run the child from anywhere. The engine
    # may also exec this body isolated from module globals, so __file__ can be
    # missing; its preamble puts the script's directory at sys.path[0].
    try:
        APP_DIR = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        APP_DIR = os.path.abspath(sys.path[0])

    # OAuth client: a user-supplied one wins, else the client shipped next to this
    # script (installed-app secrets are not confidential — see readme "Sharing").
    creds_path = os.path.join(MAIL_DIR, "credentials.json")
    if not os.path.exists(creds_path):
        bundled = os.path.join(APP_DIR, "credentials.json")
        if os.path.exists(bundled):
            creds_path = bundled
    accounts_path = os.path.join(MAIL_DIR, "accounts.json")
    tokens_dir = os.path.join(MAIL_DIR, "tokens")
    downloads_dir = os.path.join(MAIL_DIR, "downloads")
    auth_status_path = os.path.join(MAIL_DIR, "auth_status.json")
    demo_state_path = os.path.join(MAIL_DIR, "demo_state.json")
    cache_dir = os.path.join(MAIL_DIR, "cache")
    for d in (MAIL_DIR, tokens_dir, downloads_dir, cache_dir):
        os.makedirs(d, exist_ok=True)

    def read_json(path, fallback):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return fallback

    def write_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------- demo data
    DEMO_THREADS = [
        {
            "id": "demo-t1",
            "subject": "Q3 planning doc — comments by Friday",
            "messages": [
                {
                    "id": "demo-m1",
                    "from": "Sara Chen <sara@acme.io>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785226000000,
                    "body_text": "Hey,\n\nQ3 planning doc is up. Please drop comments by Friday EOD — especially on the infra budget section.\n\nSara",
                    "body_html": "",
                    "attachments": [],
                },
                {
                    "id": "demo-m2",
                    "from": "Marcus Webb <marcus@acme.io>",
                    "to": "you@demo.test",
                    "cc": "sara@acme.io",
                    "date": 1785236000000,
                    "body_text": "Left comments on section 2. The GPU line item looks underestimated by ~30%.",
                    "body_html": "",
                    "attachments": [],
                },
            ],
            "labels": ["INBOX", "UNREAD"],
        },
        {
            "id": "demo-t2",
            "subject": "Your invoice #4821 from Linear",
            "messages": [
                {
                    "id": "demo-m3",
                    "from": "Linear <billing@linear.app>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785196000000,
                    "body_text": "Your invoice for July 2026 is attached.\n\nTotal: $128.00\n\nThanks,\nLinear",
                    "body_html": "<p>Your invoice for <b>July 2026</b> is attached.</p><p>Total: <b>$128.00</b></p><p>Thanks,<br>Linear</p>",
                    "attachments": [
                        {"id": "demo-a1", "filename": "invoice-4821.pdf", "size": 48213, "mime": "application/pdf"}
                    ],
                }
            ],
            "labels": ["INBOX"],
        },
        {
            "id": "demo-t3",
            "subject": "Re: dinner saturday?",
            "messages": [
                {
                    "id": "demo-m4",
                    "from": "Priya Nair <priya.n@gmail.com>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785256000000,
                    "body_text": "8pm works! Booking the usual place. Bring the board game.",
                    "body_html": "",
                    "attachments": [],
                }
            ],
            "labels": ["INBOX", "UNREAD", "STARRED"],
        },
        {
            "id": "demo-t4",
            "subject": "[GitHub] PR #308 merged: permission approval bridge",
            "messages": [
                {
                    "id": "demo-m5",
                    "from": "GitHub <notifications@github.com>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785176000000,
                    "body_text": "Merged #308 into main.\n\n-- \nReply to this email directly or view it on GitHub.",
                    "body_html": "<p>Merged <a href='#'>#308</a> into <code>main</code>.</p>",
                    "attachments": [],
                }
            ],
            "labels": ["INBOX"],
        },
        {
            "id": "demo-t5",
            "subject": "Flight confirmation — DPS → SIN, Aug 12",
            "messages": [
                {
                    "id": "demo-m6",
                    "from": "Singapore Airlines <noreply@singaporeair.com>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785136000000,
                    "body_text": "Booking confirmed. SQ 941 departs DPS 09:35, arrives SIN 12:15. Ref: XK4P2Q.",
                    "body_html": "",
                    "attachments": [
                        {"id": "demo-a2", "filename": "eticket-XK4P2Q.pdf", "size": 102400, "mime": "application/pdf"}
                    ],
                }
            ],
            "labels": ["INBOX", "STARRED"],
        },
        {
            "id": "demo-t6",
            "subject": "Weekly metrics digest",
            "messages": [
                {
                    "id": "demo-m7",
                    "from": "Metabase <reports@metabase.demo>",
                    "to": "you@demo.test",
                    "cc": "",
                    "date": 1785086000000,
                    "body_text": "WAU up 4.2% week over week. Activation rate flat at 31%. Churn down 0.3pt.",
                    "body_html": "",
                    "attachments": [],
                }
            ],
            "labels": ["INBOX", "UNREAD"],
        },
    ]

    def demo_state():
        st = read_json(demo_state_path, None)
        if st is None:
            st = {"labels": {t["id"]: t["labels"] for t in DEMO_THREADS}, "sent": []}
            write_json(demo_state_path, st)
        return st

    def demo_thread_labels(st, tid):
        return st["labels"].get(tid, [])

    def demo_list():
        st = demo_state()
        rows = []
        all_threads = DEMO_THREADS + st["sent"]
        for t in all_threads:
            labels = demo_thread_labels(st, t["id"]) if t["id"] in st["labels"] else t.get("labels", [])
            if label and label != "ALL" and label not in labels:
                continue
            if q:
                blob = (t["subject"] + " " + " ".join(m["body_text"] + " " + m["from"] for m in t["messages"])).lower()
                if q.lower() not in blob:
                    continue
            last = t["messages"][-1]
            rows.append(
                {
                    "id": t["id"],
                    "subject": t["subject"],
                    "from": last["from"],
                    "date": last["date"],
                    "snippet": last["body_text"][:120].replace("\n", " "),
                    "unread": "UNREAD" in labels,
                    "starred": "STARRED" in labels,
                    "msg_count": len(t["messages"]),
                }
            )
        rows.sort(key=lambda r: -r["date"])
        return {"threads": rows, "next_page_token": ""}

    def demo_get(tid):
        st = demo_state()
        for t in DEMO_THREADS + st["sent"]:
            if t["id"] == tid:
                labels = demo_thread_labels(st, tid) if tid in st["labels"] else t.get("labels", [])
                if mark_read and "UNREAD" in labels:
                    st["labels"][tid] = [x for x in labels if x != "UNREAD"]
                    write_json(demo_state_path, st)
                    labels = st["labels"][tid]
                return {
                    "id": tid,
                    "subject": t["subject"],
                    "labels": labels,
                    "messages": [dict(m, labels=labels) for m in t["messages"]],
                }
        return {"error": "thread not found"}

    def demo_modify(tid, add, remove):
        st = demo_state()
        cur = set(demo_thread_labels(st, tid))
        cur |= set(add or [])
        cur -= set(remove or [])
        st["labels"][tid] = sorted(cur)
        write_json(demo_state_path, st)
        return {"ok": True}

    def demo_send():
        st = demo_state()
        tid = "demo-sent-%d" % int(time.time() * 1000)
        msg = {
            "id": tid + "-m",
            "from": "you@demo.test",
            "to": to,
            "cc": cc,
            "date": int(time.time() * 1000),
            "body_text": body,
            "body_html": "",
            "attachments": [
                {"id": "", "filename": a.get("filename", "file"), "size": len(a.get("data_b64", "")) * 3 // 4, "mime": a.get("mime", "")}
                for a in (attachments or [])
            ],
        }
        if reply_to_message and thread:
            for t in DEMO_THREADS + st["sent"]:
                if t["id"] == thread:
                    t2 = dict(t)
                    t2["messages"] = t["messages"] + [msg]
                    if t["id"] in [x["id"] for x in st["sent"]]:
                        st["sent"] = [t2 if x["id"] == t["id"] else x for x in st["sent"]]
                    else:
                        # replies to fixture threads only persist the sent copy
                        st["sent"].append({"id": tid, "subject": "Re: " + t["subject"], "messages": [msg]})
                        st["labels"][tid] = ["SENT"]
                    write_json(demo_state_path, st)
                    return {"ok": True, "id": tid}
        st["sent"].append({"id": tid, "subject": subject or "(no subject)", "messages": [msg]})
        st["labels"][tid] = ["SENT"]
        write_json(demo_state_path, st)
        return {"ok": True, "id": tid}

    DEMO_LABELS = [
        {"id": "INBOX", "name": "Inbox", "type": "system"},
        {"id": "STARRED", "name": "Starred", "type": "system"},
        {"id": "SENT", "name": "Sent", "type": "system"},
        {"id": "TRASH", "name": "Trash", "type": "system"},
    ]

    # ------------------------------------------------------------ oauth glue
    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

    def get_service(email):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_path = os.path.join(tokens_dir, email + ".json")
        if not os.path.exists(token_path):
            raise RuntimeError("auth_error: no token for " + email)
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise RuntimeError("auth_error: token invalid for " + email)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def header(headers, name):
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    def walk_parts(payload, out_bodies, out_atts):
        mime = payload.get("mimeType", "")
        fname = payload.get("filename", "")
        b = payload.get("body", {})
        if fname and b.get("attachmentId"):
            out_atts.append(
                {"id": b["attachmentId"], "filename": fname, "size": b.get("size", 0), "mime": mime}
            )
        elif mime in ("text/plain", "text/html") and b.get("data"):
            text = base64.urlsafe_b64decode(b["data"] + "===").decode("utf-8", "replace")
            out_bodies.append((mime, text))
        for p in payload.get("parts", []) or []:
            walk_parts(p, out_bodies, out_atts)

    def message_to_dict(msg):
        headers = msg.get("payload", {}).get("headers", [])
        bodies, atts = [], []
        walk_parts(msg.get("payload", {}), bodies, atts)
        body_html = next((t for m, t in bodies if m == "text/html"), "")
        body_text = next((t for m, t in bodies if m == "text/plain"), "")
        return {
            "id": msg["id"],
            "from": header(headers, "From"),
            "to": header(headers, "To"),
            "cc": header(headers, "Cc"),
            "date": int(msg.get("internalDate", "0")),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": atts,
            "labels": msg.get("labelIds", []),
            "rfc_message_id": header(headers, "Message-ID"),
            "references": header(headers, "References"),
            "subject": header(headers, "Subject"),
        }

    def build_mime(from_email):
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email import encoders

        if attachments:
            m = MIMEMultipart()
            m.attach(MIMEText(body, "plain", "utf-8"))
            for a in attachments:
                part = MIMEBase(*(a.get("mime") or "application/octet-stream").split("/", 1))
                part.set_payload(base64.b64decode(a["data_b64"]))
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=a.get("filename", "file"))
                m.attach(part)
        else:
            m = MIMEText(body, "plain", "utf-8")
        m["To"] = to
        if cc:
            m["Cc"] = cc
        if bcc:
            m["Bcc"] = bcc
        m["From"] = from_email
        return m

    # ------------------------------------------------------------- imap glue
    # Gmail over IMAP/SMTP with an app password (myaccount.google.com/apppasswords).
    # Gmail's IMAP extensions carry what the REST API would give us:
    #   X-GM-THRID (thread id), X-GM-LABELS (label ops), X-GM-RAW (Gmail search syntax).
    IMAP_FOLDERS = {
        "INBOX": "INBOX",
        "STARRED": "[Gmail]/Starred",
        "SENT": "[Gmail]/Sent Mail",
        "TRASH": "[Gmail]/Trash",
        "SPAM": "[Gmail]/Spam",
        "DRAFT": "[Gmail]/Drafts",
        "ALL": "[Gmail]/All Mail",
    }
    IMAP_SYS_LABELS = [
        {"id": "INBOX", "name": "Inbox", "type": "system"},
        {"id": "STARRED", "name": "Starred", "type": "system"},
        {"id": "SENT", "name": "Sent", "type": "system"},
        {"id": "TRASH", "name": "Trash", "type": "system"},
    ]
    # X-GM-LABELS speaks Gmail's own vocabulary; the UI speaks label ids.
    IMAP_LABEL_MAP = {
        "\\Inbox": "INBOX", "\\Sent": "SENT", "\\Draft": "DRAFT", "\\Drafts": "DRAFT",
        "\\Trash": "TRASH", "\\Spam": "SPAM", "\\Junk": "SPAM",
        "\\Starred": "STARRED", "\\Flagged": "STARRED", "\\Important": "IMPORTANT",
    }
    IMAP_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                   "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

    def load_imaplib():
        # The FusedRender.app builtin engine runs on a py2app-pruned stdlib that
        # ships smtplib but not imaplib — fall back to the vendored copy
        # (vendor_imaplib.py, CPython 3.12 stdlib) shipped next to this script.
        try:
            import imaplib
            return imaplib
        except ModuleNotFoundError:
            import importlib.util
            path = os.path.join(APP_DIR, "vendor_imaplib.py")
            spec = importlib.util.spec_from_file_location("imaplib", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["imaplib"] = mod
            spec.loader.exec_module(mod)
            return mod

    def imap_pw(email):
        tok = read_json(os.path.join(tokens_dir, email + ".json"), {})
        pw = tok.get("app_password")
        if not pw:
            raise RuntimeError("auth_error: no app password stored for " + email)
        return pw

    def imap_conn(email):
        imaplib = load_imaplib()

        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(email, imap_pw(email))
        return M

    def imap_quote(folder):
        return '"' + folder + '"'

    def decode_hdr(raw):
        import re
        from email.header import decode_header

        if not raw:
            return ""
        out = []
        for chunk, enc in decode_header(raw):
            if isinstance(chunk, bytes):
                out.append(chunk.decode(enc or "utf-8", "replace"))
            else:
                out.append(chunk)
        # Long headers arrive folded, so the decoded subject carried real
        # newlines into the UI ("OperationalError:\n (psycopg…") — they showed
        # up in list rows, the <h2> and the document title.
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def imap_date_ms(internaldate_str):
        """IMAP INTERNALDATE ('03-Aug-2026 11:14:20 +0000') → epoch ms.

        Parsed by hand instead of strptime("%d-%b-%Y…"): %b resolves month
        names through the process locale, so a non-English LC_TIME in the
        engine's child would silently zero every date on this machine only."""
        import re
        from datetime import datetime, timedelta, timezone

        m = re.search(
            r"(\d{1,2})-([A-Za-z]{3})-(\d{4})[ T](\d{1,2}):(\d{2}):(\d{2})\s*([+-]\d{4})?",
            internaldate_str or "",
        )
        if not m:
            return 0
        mon = IMAP_MONTHS.get(m.group(2).lower())
        if not mon:
            return 0
        off = m.group(7) or "+0000"
        delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        tz = timezone(-delta if off[0] == "-" else delta)
        try:
            dt = datetime(int(m.group(3)), mon, int(m.group(1)),
                          int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz)
            return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return 0

    def hdr_date_ms(raw):
        """RFC 5322 `Date:` header → epoch ms, 0 when absent or unparseable."""
        from datetime import timezone
        from email.utils import parsedate_to_datetime

        if not raw:
            return 0
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            return 0
        if dt is None:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)   # floating time: assume UTC
        try:
            return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return 0

    def imap_parse_labels(raw):
        """X-GM-LABELS payload → set of UI label ids. Bare tokens are Gmail's
        own (\\Inbox); quoted ones are the user's."""
        import re

        out = set()
        for quoted, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', raw or ""):
            # Gmail quotes AND escapes its own labels — the wire carries
            # X-GM-LABELS ("\\Inbox"), so the backslash has to be unescaped
            # before it can match \Inbox. Without this every thread came back
            # with no labels at all and the header chips never rendered.
            tok = re.sub(r"\\(.)", r"\1", quoted) if quoted else bare
            if not tok:
                continue
            mapped = IMAP_LABEL_MAP.get(tok)
            if mapped:
                out.add(mapped)
            elif not tok.startswith("\\"):
                out.add(tok.replace("\\\\", "\\"))
        return out

    def clean_snippet(raw):
        """First ~400 readable characters of a message body, for the list row's
        preview line. Real IMAP rows previously had no snippet at all (the
        field was hard-coded ""), so every real row lost Gmail's third line."""
        import quopri
        import re

        try:
            txt = (raw or b"").decode("utf-8", "replace")
            if not txt.strip():
                return ""
            # BODY[1] of a nested multipart arrives with its own MIME headers
            if re.match(r"^(--|Content-[A-Za-z-]+:)", txt.lstrip()):
                bits = re.split(r"\r?\n\r?\n", txt, 1)
                txt = bits[1] if len(bits) > 1 else ""
            compact = re.sub(r"\s+", "", txt)
            # a base64 body is one unbroken token — decode it rather than
            # previewing the encoding
            if len(compact) > 48 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact[:200]):
                try:
                    txt = base64.b64decode(compact + "===").decode("utf-8", "replace")
                except Exception:
                    return ""
            elif "=3D" in txt or re.search(r"=\r?\n", txt):
                txt = quopri.decodestring(txt.encode("utf-8", "replace")).decode("utf-8", "replace")
            txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", txt)
            # The fetch is capped at 900 bytes, so a <style> block is usually
            # still OPEN when the text ends — without this, newsletter previews
            # read "@media only screen and (max-device-width: 480px)…".
            txt = re.sub(r"(?is)<(script|style)[^>]*>.*", " ", txt)
            txt = re.sub(r"(?is)<!(doctype|--).*?>", " ", txt)
            txt = re.sub(r"<[^>]*>", " ", txt)
            for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                            ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
                txt = txt.replace(ent, ch)
            txt = re.sub(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]{2,8});", " ", txt)
            # A tracking URL is 200 characters of nothing and ate the whole
            # preview line on every newsletter.
            txt = re.sub(r"\(?\s*https?://\S+\s*\)?", " ", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            return txt[:400]
        except Exception:
            return ""   # a preview is never worth failing the whole list for

    def imap_fetch_meta(M, uid_list):
        """One FETCH round-trip: thread id, flags, date, subject/from headers."""
        import email as email_mod
        import re

        if not uid_list:
            return []
        resp_meta = []
        typ, data = M.uid(
            "FETCH",
            ",".join(uid_list),
            "(X-GM-THRID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])",
        )
        for item in data:
            if not isinstance(item, tuple):
                continue
            head = item[0].decode("latin-1", "replace")
            uid_m = re.search(r"UID (\d+)", head)
            thrid_m = re.search(r"X-GM-THRID (\d+)", head)
            flags_m = re.search(r"FLAGS \(([^)]*)\)", head)
            date_m = re.search(r'INTERNALDATE "([^"]+)"', head)
            hdr = email_mod.message_from_bytes(item[1])
            # Two clocks on purpose. `recv` (INTERNALDATE) is when Gmail got it
            # — unspoofable, so it orders the list. `date` (the Date: header) is
            # what the sender claims and what the thread view shows, so both
            # views agree on the string. Either one alone leaves gaps.
            recv = imap_date_ms(date_m.group(1)) if date_m else 0
            resp_meta.append(
                {
                    "uid": uid_m.group(1) if uid_m else "",
                    "thrid": thrid_m.group(1) if thrid_m else "",
                    "flags": flags_m.group(1) if flags_m else "",
                    "recv": recv,
                    "date": hdr_date_ms(hdr.get("Date", "")) or recv,
                    "subject": decode_hdr(hdr.get("Subject", "")) or "(no subject)",
                    "from": decode_hdr(hdr.get("From", "")),
                }
            )
        return resp_meta

    def imap_snippets(M, uid_by_thread):
        """One extra batched FETCH to give real rows Gmail's preview line."""
        import re

        out = {}
        uids = sorted({u for u in uid_by_thread.values() if u})
        if not uids:
            return out
        try:
            typ, data = M.uid("FETCH", ",".join(uids), "(BODY.PEEK[1]<0.2400>)")
        except Exception:
            return out          # preview is a nicety; the list still ships
        by_uid = {}
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            um = re.search(r"UID (\d+)", item[0].decode("latin-1", "replace"))
            if um:
                by_uid[um.group(1)] = item[1] or b""
        for thrid, uid in uid_by_thread.items():
            out[thrid] = clean_snippet(by_uid.get(uid, b""))
        return out

    def imap_labels(email):
        M = imap_conn(email)
        try:
            typ, data = M.list()
            user = []
            for line in data or []:
                s = line.decode("utf-8", "replace")
                # (\HasNoChildren) "/" "Folder Name"
                name = s.rsplit(' "/" ', 1)[-1].strip().strip('"')
                if name.startswith("[Gmail]") or name == "INBOX" or "\\Noselect" in s:
                    continue
                user.append({"id": name, "name": name, "type": "user"})
            user.sort(key=lambda l: l["name"].lower())
            return {"labels": IMAP_SYS_LABELS + user}
        finally:
            M.logout()

    def imap_list(email):
        M = imap_conn(email)
        try:
            if q:
                M.select(imap_quote(IMAP_FOLDERS["ALL"]), readonly=True)
                typ, data = M.uid("SEARCH", None, "X-GM-RAW", '"' + q.replace('"', "'") + '"')
            else:
                folder = IMAP_FOLDERS.get(label, label)
                M.select(imap_quote(folder), readonly=True)
                typ, data = M.uid("SEARCH", None, "ALL")
            uids = (data[0] or b"").split()
            recent = [u.decode() for u in uids[-100:]]  # newest window; threads built from it
            metas = imap_fetch_meta(M, recent)
            threads = {}
            for m in metas:
                t = threads.setdefault(
                    m["thrid"],
                    {"id": m["thrid"], "subject": m["subject"], "from": m["from"], "date": 0,
                     "snippet": "", "unread": False, "starred": False, "msg_count": 0,
                     "_recv": 0, "_uid": ""},
                )
                t["msg_count"] += 1
                if m["recv"] >= t["_recv"]:
                    t["_recv"], t["_uid"] = m["recv"], m["uid"]
                    t["date"], t["subject"], t["from"] = m["date"], m["subject"], m["from"]
                if "\\Seen" not in m["flags"]:
                    t["unread"] = True
                if "\\Flagged" in m["flags"]:
                    t["starred"] = True
            rows = sorted(threads.values(), key=lambda r: -r["_recv"])[:25]
            snips = imap_snippets(M, {r["id"]: r["_uid"] for r in rows})
            for r in rows:
                r["snippet"] = snips.get(r["id"], "")
                r["date"] = r["date"] or r["_recv"]
                r.pop("_recv", None)
                r.pop("_uid", None)
            return {"threads": rows, "next_page_token": ""}
        finally:
            M.logout()

    def imap_thread_uids(M, thrid):
        typ, data = M.uid("SEARCH", None, "X-GM-THRID", thrid)
        return [u.decode() for u in (data[0] or b"").split()]

    def imap_get(email, thrid):
        import email as email_mod
        import re

        M = imap_conn(email)
        try:
            M.select(imap_quote(IMAP_FOLDERS["ALL"]))
            uids = imap_thread_uids(M, thrid)
            if not uids:
                return {"error": "thread not found"}
            msgs = []
            subject = "(no subject)"
            tlabels = set()
            unseen = False
            for uid in uids:
                # BODY.PEEK[], NOT RFC822 — this one item was the "1 Jan 1970"
                # bug. Gmail answers an RFC822 fetch by emitting the literal
                # FIRST and trailing INTERNALDATE *after* it, so it never
                # appeared in the tuple head this code used to regex: every
                # message parsed as epoch 0. PEEK puts INTERNALDATE back in the
                # head, and (bonus) stops a plain read from setting \Seen —
                # marking read is now only ever the explicit STORE below.
                typ, data = M.uid("FETCH", uid, "(INTERNALDATE FLAGS X-GM-LABELS BODY.PEEK[])")
                raw = next((it[1] for it in data if isinstance(it, tuple)), None)
                if raw is None:
                    continue
                # Scan the WHOLE response, not just the tuple head: a server may
                # legally put non-literal items on either side of the literal.
                meta = " ".join(
                    (it[0] if isinstance(it, tuple) else it).decode("latin-1", "replace")
                    for it in data if it
                )
                date_m = re.search(r'INTERNALDATE "([^"]+)"', meta)
                lbl_m = re.search(r"X-GM-LABELS \(([^)]*)\)", meta)
                flg_m = re.search(r"FLAGS \(([^)]*)\)", meta)
                tlabels |= imap_parse_labels(lbl_m.group(1) if lbl_m else "")
                if flg_m and "\\Seen" not in flg_m.group(1):
                    unseen = True
                em = email_mod.message_from_bytes(raw)
                body_text, body_html, atts = "", "", []
                for i, part in enumerate(em.walk()):
                    ctype = part.get_content_type()
                    fname = part.get_filename()
                    if fname:
                        payload = part.get_payload(decode=True) or b""
                        atts.append(
                            {"id": str(i), "filename": decode_hdr(fname), "size": len(payload), "mime": ctype}
                        )
                    elif ctype == "text/plain" and not body_text:
                        body_text = (part.get_payload(decode=True) or b"").decode(
                            part.get_content_charset() or "utf-8", "replace"
                        )
                    elif ctype == "text/html" and not body_html:
                        body_html = (part.get_payload(decode=True) or b"").decode(
                            part.get_content_charset() or "utf-8", "replace"
                        )
                subj = decode_hdr(em.get("Subject", ""))
                if subj:
                    subject = subj
                msgs.append(
                    {
                        "id": uid,
                        "from": decode_hdr(em.get("From", "")),
                        "to": decode_hdr(em.get("To", "")),
                        "cc": decode_hdr(em.get("Cc", "")),
                        # Date: header first (what the sender stamped, what
                        # Gmail shows), INTERNALDATE second, thread-wide
                        # fallback third — see the backfill below.
                        "date": hdr_date_ms(em.get("Date", ""))
                        or (imap_date_ms(date_m.group(1)) if date_m else 0),
                        "body_text": body_text,
                        "body_html": body_html,
                        "attachments": atts,
                        "labels": [],
                        "rfc_message_id": em.get("Message-ID", ""),
                        "references": em.get("References", ""),
                        "subject": subj,
                    }
                )
            if mark_read:
                M.uid("STORE", ",".join(uids), "+FLAGS", r"(\Seen)")
                unseen = False
            # Last line of defence: a message must never render as 1 Jan 1970.
            # Anything still unknown inherits the newest date in the thread —
            # the same value the list row shows for it.
            known = [m["date"] for m in msgs if m["date"]]
            fill = max(known) if known else int(time.time() * 1000)
            for m in msgs:
                if not m["date"]:
                    m["date"] = fill
            if unseen:
                tlabels.add("UNREAD")
            msgs.sort(key=lambda m: m["date"])
            return {"id": thrid, "subject": subject, "labels": sorted(tlabels), "messages": msgs}
        finally:
            M.logout()

    def imap_send(email):
        import smtplib

        m = build_mime(email)
        if op == "reply" and thread and reply_to_message:
            M = imap_conn(email)
            try:
                M.select(imap_quote(IMAP_FOLDERS["ALL"]), readonly=True)
                typ, data = M.uid(
                    "FETCH", reply_to_message, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT)])"
                )
                raw = next((it[1] for it in data if isinstance(it, tuple)), b"")
                import email as email_mod

                oh = email_mod.message_from_bytes(raw)
                mid = oh.get("Message-ID", "")
                osubj = decode_hdr(oh.get("Subject", ""))
                m["Subject"] = osubj if osubj.lower().startswith("re:") else "Re: " + osubj
                if mid:
                    m["In-Reply-To"] = mid
                    m["References"] = (oh.get("References", "") + " " + mid).strip()
            finally:
                M.logout()
        else:
            m["Subject"] = subject
        # Submission port first, implicit-TLS second: many networks (corporate
        # wifi, VPNs) silently blackhole 465 while leaving 587 open — it looked
        # like "sending is broken" rather than a blocked port.
        pw = imap_pw(email)
        errors = []
        # 12s per port, not 30: two 30s hangs exceed the engine's 60s cap, so the
        # call was killed as a bare "timeout" instead of returning which port failed.
        for port in (587, 465):
            try:
                if port == 587:
                    S = smtplib.SMTP("smtp.gmail.com", 587, timeout=12)
                    S.ehlo()
                    S.starttls()
                    S.ehlo()
                else:
                    S = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=12)
            except Exception as e:
                errors.append("port %d unreachable (%s)" % (port, type(e).__name__))
                continue
            try:
                S.login(email, pw)
                S.send_message(m)  # Gmail SMTP auto-saves a copy to Sent
                return {"ok": True, "id": "", "port": port}
            except Exception as e:
                msg = e.args[0] if e.args else e
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                errors.append("port %d: %s" % (port, str(msg)[:150]))
            finally:
                try:
                    S.quit()
                except Exception:
                    pass
        return {"error": "SMTP send failed — " + "; ".join(errors)}

    def imap_modify(email, thrid, add, remove):
        M = imap_conn(email)
        try:
            M.select(imap_quote(IMAP_FOLDERS["ALL"]))
            uids = imap_thread_uids(M, thrid)
            if not uids:
                return {"error": "thread not found"}
            uidset = ",".join(uids)
            # Beyond the three special cases, any other id is a Gmail label —
            # system ones map to their backslash form, user ones go quoted.
            GM = {"INBOX": r"\Inbox", "SPAM": r"\Spam", "TRASH": r"\Trash", "IMPORTANT": r"\Important"}
            gm_label = lambda l: GM.get(l, '"' + l.replace("\\", "\\\\").replace('"', '\\"') + '"')
            for lbl in add or []:
                if lbl == "STARRED":
                    M.uid("STORE", uidset, "+FLAGS", r"(\Flagged)")
                elif lbl == "UNREAD":
                    M.uid("STORE", uidset, "-FLAGS", r"(\Seen)")
                else:
                    M.uid("STORE", uidset, "+X-GM-LABELS", "(" + gm_label(lbl) + ")")
            for lbl in remove or []:
                if lbl == "STARRED":
                    M.uid("STORE", uidset, "-FLAGS", r"(\Flagged)")
                elif lbl == "UNREAD":
                    M.uid("STORE", uidset, "+FLAGS", r"(\Seen)")
                else:
                    M.uid("STORE", uidset, "-X-GM-LABELS", "(" + gm_label(lbl) + ")")
            return {"ok": True}
        finally:
            M.logout()

    def imap_trash(email, thrid):
        M = imap_conn(email)
        try:
            M.select(imap_quote(IMAP_FOLDERS["ALL"]))
            uids = imap_thread_uids(M, thrid)
            if not uids:
                return {"error": "thread not found"}
            M.uid("COPY", ",".join(uids), imap_quote(IMAP_FOLDERS["TRASH"]))
            return {"ok": True}
        finally:
            M.logout()

    def imap_attachment(email, uid, part_idx, fname):
        import email as email_mod

        M = imap_conn(email)
        try:
            M.select(imap_quote(IMAP_FOLDERS["ALL"]), readonly=True)
            typ, data = M.uid("FETCH", uid, "(BODY.PEEK[])")   # PEEK: downloading ≠ reading
            raw = next((it[1] for it in data if isinstance(it, tuple)), None)
            if raw is None:
                return {"error": "message not found"}
            em = email_mod.message_from_bytes(raw)
            for i, part in enumerate(em.walk()):
                if str(i) == part_idx:
                    dest = os.path.join(downloads_dir, os.path.basename(fname or "attachment"))
                    with open(dest, "wb") as f:
                        f.write(part.get_payload(decode=True) or b"")
                    return {"path": dest}
            return {"error": "attachment not found"}
        finally:
            M.logout()

    # ----------------------------------------------------- manage board config
    # The Manage board's categories live in a user-editable JSON file. Each
    # category is a tab: `prompt` feeds the AI classifier, `dest` is where the
    # confirm button sends the batch (TRASH / SPAM / ARCHIVE / KEEP / any label
    # id). `auto: "rsvp"` marks the category the RSVP-subject heuristic feeds.
    manage_config_path = os.path.join(MAIL_DIR, "manage_config.json")
    MANAGE_CONFIG_DEFAULT = {
        "version": 1,
        "categories": [
            {"id": "calendar", "name": "Calendar RSVPs", "dest": "TRASH", "auto": "rsvp",
             "prompt": "calendar responses — subjects starting Accepted:/Declined:/Tentative:/"
                       "Updated invitation:/Canceled event: — pure notification, no action possible"},
            {"id": "cold", "name": "Cold Outreach", "dest": "TRASH",
             "prompt": "unsolicited sales or outreach from strangers pitching services, agencies, "
                       "lead generation, PR placements — no prior relationship"},
            {"id": "newsletters", "name": "Newsletters", "dest": "ARCHIVE",
             "prompt": "recurring newsletters, digests, and editorial content the user subscribed to"},
            {"id": "promo", "name": "Promotions", "dest": "ARCHIVE",
             "prompt": "marketing, product announcements, sales, and discount offers from companies"},
            {"id": "events", "name": "Event Invites", "dest": "ARCHIVE",
             "prompt": "event, webinar, and conference invitations from companies"},
            {"id": "receipts", "name": "Receipts", "dest": "ARCHIVE",
             "prompt": "receipts, invoices already paid, payment confirmations, charge and renewal notifications"},
            {"id": "reports", "name": "Reports", "dest": "ARCHIVE",
             "prompt": "automated usage or monitoring reports, dashboards, scheduled summaries, CI notifications"},
            {"id": "alerts", "name": "Alerts", "dest": "ARCHIVE",
             "prompt": "security alerts, login warnings, incident and outage notifications, anything time-sensitive from a machine"},
            {"id": "keep", "name": "Keep", "dest": "KEEP",
             "prompt": "real conversations with people, anything asking the user to act or decide, "
                       "documents needing review"},
        ],
    }

    # ------------------------------------------------------- on-disk mailbox cache
    # A page reload used to start completely cold: every list page and every
    # thread body had to come back over IMAP/Gmail before anything painted.
    # The same stale-while-revalidate data the page held in memory now also
    # lives under ~/.fused-mail/cache/<account>/ so a refresh paints instantly
    # and the network fetch only has to reconcile what actually changed.
    #
    # Layout — list pages in one small file (read whole on boot), thread bodies
    # one file each (big, and only a slice is ever needed at once):
    #   cache/<account>/lists.json          {labels: [...], lists: {key: {...}}}
    #   cache/<account>/threads/<hash>.json  one get_thread result
    #
    # Nothing here is kept forever: anything untouched for CACHE_TTL_S is
    # deleted, so a mailbox you stop opening stops living on this disk. The
    # clock is LAST USE, not first write — reads touch the file — because a
    # thread you still open now and then is exactly the one worth keeping, and
    # every cached copy is revalidated against the server on open anyway.
    CACHE_MAX_THREADS = 600      # ~ a few hundred MB worst case; pruned by mtime
    CACHE_TTL_S = 30 * 24 * 3600  # 30 days since last use

    def cache_paths(acct):
        safe = "".join(c if (c.isalnum() or c in "@.-_") else "_" for c in (acct or "none"))
        base = os.path.join(cache_dir, safe or "none")
        return base, os.path.join(base, "lists.json"), os.path.join(base, "threads")

    def thread_file(tdir, tid):
        import hashlib
        return os.path.join(tdir, hashlib.sha1(str(tid).encode()).hexdigest() + ".json")

    def write_cache_json(path, data):
        # write_json's fixed "<path>.tmp" is not safe here: cache writes are the
        # one op deliberately run concurrently (key:null), and two flushes
        # racing on the same temp name made the loser fail its os.replace with
        # FileNotFoundError. A unique temp per call keeps each write atomic.
        import tempfile
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def rm(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def prune_threads(tdir):
        # Two limits, in order: anything past the TTL goes, then the newest
        # CACHE_MAX_THREADS survive whatever is left.
        try:
            files = [os.path.join(tdir, f) for f in os.listdir(tdir) if f.endswith(".json")]
        except OSError:
            return 0
        cutoff = time.time() - CACHE_TTL_S
        gone = 0
        fresh = []
        for p in files:
            try:
                stale = os.path.getmtime(p) < cutoff
            except OSError:
                continue
            if stale:
                gone += rm(p)
            else:
                fresh.append(p)
        if len(fresh) > CACHE_MAX_THREADS:
            # Oldest touch first — cache_get/cache_threads stamp the files they use.
            fresh.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
            for p in fresh[: len(fresh) - CACHE_MAX_THREADS]:
                gone += rm(p)
        return gone

    def expire_lists(blob):
        # List pages carry their own stamp (they are entries in one file, so
        # there is no mtime per page). An expired page is simply dropped —
        # the next fetch repopulates it.
        lists = blob.get("lists") or {}
        stamps = blob.get("lists_at") or {}
        cutoff = time.time() - CACHE_TTL_S
        for k in [k for k in lists if stamps.get(k, 0) < cutoff]:
            lists.pop(k, None)
            stamps.pop(k, None)
        blob["lists"] = lists
        blob["lists_at"] = stamps
        return blob

    def sweep_all_accounts():
        # An account you stop opening never runs its own cache_get, so the
        # boot sweep walks EVERY cached account, not just the active one —
        # otherwise a stale mailbox could sit on disk indefinitely.
        try:
            names = os.listdir(cache_dir)
        except OSError:
            return 0
        gone = 0
        for name in names:
            base = os.path.join(cache_dir, name)
            if not os.path.isdir(base):
                continue
            gone += prune_threads(os.path.join(base, "threads"))
            lp = os.path.join(base, "lists.json")
            blob = read_json(lp, None)
            if blob:
                before = len(blob.get("lists") or {})
                blob = expire_lists(blob)
                if len(blob.get("lists") or {}) != before:
                    try:
                        write_cache_json(lp, blob)
                    except OSError:
                        pass
            # A directory with nothing left in it is just litter.
            try:
                if not os.listdir(os.path.join(base, "threads")) and not (blob or {}).get("lists"):
                    import shutil
                    shutil.rmtree(base)
            except OSError:
                pass
        return gone

    if op == "cache_get":
        # Boot read: every list page (small) plus the most recently touched
        # thread bodies. The rest stay on disk and are pulled by cache_threads
        # as the warm queue reaches them.
        # This is also the TTL sweep point — once per page load, across every
        # account, so expiry happens even in a session that never writes.
        expired = sweep_all_accounts()
        base, lists_path, tdir = cache_paths(account)
        blob = expire_lists(read_json(lists_path, {}) or {})
        out = {"lists": blob.get("lists") or {}, "labels": blob.get("labels") or [],
               "expired": expired}
        threads = {}
        try:
            files = [os.path.join(tdir, f) for f in os.listdir(tdir) if f.endswith(".json")]
        except OSError:
            files = []
        files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        now = time.time()
        for p in files[:80]:
            t = read_json(p, None)
            if t and t.get("id"):
                threads[t["id"]] = t
                try:
                    os.utime(p, (now, now))   # handed to the page = in use, restart its clock
                except OSError:
                    pass
        out["threads"] = threads
        out["on_disk"] = len(files)
        return out

    if op == "cache_threads":
        # Batch disk read for the warm queue: whatever is cached comes back
        # here, and only the misses cost a network round trip.
        base, lists_path, tdir = cache_paths(account)
        threads = {}
        now = time.time()
        for tid in (ids or []):
            p = thread_file(tdir, tid)
            t = read_json(p, None)
            if t and t.get("id"):
                threads[t["id"]] = t
                try:
                    os.utime(p, (now, now))   # touch: keeps hot threads off the prune list
                except OSError:
                    pass
        return {"threads": threads}

    if op == "cache_put":
        # Merge-write. The page batches its dirty entries and flushes here, so
        # this is a handful of calls per session, not one per mutation.
        if not isinstance(cache, dict):
            return {"error": "cache dict required"}
        base, lists_path, tdir = cache_paths(account)
        os.makedirs(tdir, exist_ok=True)
        # lists.json is read-modify-written, so two writers (a second window,
        # or a flush overlapping one from another tab) would silently drop one
        # side's pages. Hold an exclusive lock across the whole cycle.
        import fcntl
        lock = open(os.path.join(base, ".lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            pass
        blob = expire_lists(read_json(lists_path, {}) or {})
        lists = blob.get("lists") or {}
        stamps = blob.get("lists_at") or {}
        now = time.time()
        if isinstance(cache.get("lists"), dict):
            lists.update(cache["lists"])
            for k in cache["lists"]:
                stamps[k] = now          # written now → its TTL starts here
        for k in (cache.get("drop_lists") or []):
            lists.pop(k, None)
            stamps.pop(k, None)
        if cache.get("clear_lists"):
            lists = {}
            stamps = {}
            if isinstance(cache.get("lists"), dict):
                lists.update(cache["lists"])
                for k in cache["lists"]:
                    stamps[k] = now
        blob["lists"] = lists
        blob["lists_at"] = stamps
        if isinstance(cache.get("labels"), list):
            blob["labels"] = cache["labels"]
        blob["saved_at"] = time.time()
        try:
            write_cache_json(lists_path, blob)
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()
        for tid, t in (cache.get("threads") or {}).items():
            if t:
                write_cache_json(thread_file(tdir, tid), t)
        for tid in (cache.get("drop_threads") or []):
            try:
                os.remove(thread_file(tdir, tid))
            except OSError:
                pass
        prune_threads(tdir)
        return {"ok": True}

    if op == "cache_clear":
        # Disconnecting an account takes its cached mail with it.
        import shutil
        base, _, _ = cache_paths(account)
        try:
            shutil.rmtree(base)
        except OSError:
            pass
        return {"ok": True}

    if op == "manage_config":
        cfg = read_json(manage_config_path, None)
        if not cfg or not isinstance(cfg.get("categories"), list):
            cfg = MANAGE_CONFIG_DEFAULT
            write_json(manage_config_path, cfg)
        return {"config": cfg, "path": manage_config_path}

    if op == "save_manage_config":
        if not config or not isinstance(config.get("categories"), list):
            return {"error": "config with a categories list required"}
        write_json(manage_config_path, config)
        return {"ok": True}

    # -------------------------------------------------------------- dispatch
    accounts = read_json(accounts_path, [])

    if op == "accounts":
        status = read_json(auth_status_path, {})
        return {
            "accounts": [{"email": "demo", "provider": "demo"}] + accounts,
            "has_credentials": os.path.exists(creds_path),
            "auth_status": status,
        }

    if op == "add_imap_account":
        imaplib = load_imaplib()

        email = account.strip()
        pw = app_password.replace(" ", "")
        if not email or not pw:
            return {"error": "email and app password required"}
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com")
            M.login(email, pw)
            M.logout()
        except Exception as e:
            msg = e.args[0] if e.args else e
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "replace")
            return {"error": "IMAP login failed: " + str(msg)}
        write_json(os.path.join(tokens_dir, email + ".json"), {"app_password": pw})
        if not any(a["email"] == email for a in accounts):
            accounts.append({"email": email, "provider": "imap", "added_at": time.time()})
        else:
            accounts = [dict(a, provider="imap") if a["email"] == email else a for a in accounts]
        write_json(accounts_path, accounts)
        return {"ok": True, "email": email}

    if op == "remove_account":
        email = account.strip()
        if not email or email == "demo":
            return {"error": "pick a real account to disconnect"}
        tok = os.path.join(tokens_dir, email + ".json")
        if os.path.exists(tok):
            os.remove(tok)
        write_json(accounts_path, [a for a in accounts if a["email"] != email])
        return {"ok": True, "removed": email}

    if op == "start_auth":
        if not os.path.exists(creds_path):
            return {"error": "credentials.json missing — see readme setup", "has_credentials": False}
        write_json(auth_status_path, {"status": "pending", "started_at": time.time()})
        helper = os.path.join(APP_DIR, "add_account.py")
        log = open(os.path.join(MAIL_DIR, "auth.log"), "ab")
        subprocess.Popen(
            [sys.executable, helper],
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=MAIL_DIR,
        )
        return {"ok": True, "started": True}

    if op == "auth_status":
        return read_json(auth_status_path, {"status": "idle"})

    if not account:
        return {"error": "account param required for op=" + op}

    provider = "demo" if account == "demo" else next(
        (a.get("provider", "gmail") for a in accounts if a["email"] == account), "gmail"
    )
    is_demo = provider == "demo"
    is_imap = provider == "imap"

    if op == "labels":
        if is_demo:
            return {"labels": DEMO_LABELS}
        if is_imap:
            return imap_labels(account)
        svc = get_service(account)
        res = svc.users().labels().list(userId="me").execute()
        labels = [
            {"id": l["id"], "name": l["name"], "type": l.get("type", "user")}
            for l in res.get("labels", [])
        ]
        order = {"INBOX": 0, "STARRED": 1, "SENT": 2, "DRAFT": 3, "SPAM": 4, "TRASH": 5}
        labels.sort(key=lambda l: (l["type"] != "system", order.get(l["id"], 99), l["name"].lower()))
        return {"labels": labels}

    if op == "list_threads":
        if is_demo:
            return demo_list()
        if is_imap:
            return imap_list(account)
        svc = get_service(account)
        kwargs = {"userId": "me", "maxResults": 25}
        if q:
            kwargs["q"] = q
        elif label and label != "ALL":
            kwargs["labelIds"] = [label]
        if page_token:
            kwargs["pageToken"] = page_token
        res = svc.users().threads().list(**kwargs).execute()
        ids = [t["id"] for t in res.get("threads", [])]
        rows = {}

        def cb(rid, resp, err):
            if err or not resp:
                return
            msgs = resp.get("messages", [])
            if not msgs:
                return
            last = msgs[-1]
            headers = last.get("payload", {}).get("headers", [])
            all_labels = set()
            for m in msgs:
                all_labels |= set(m.get("labelIds", []))
            rows[resp["id"]] = {
                "id": resp["id"],
                "subject": header(headers, "Subject") or "(no subject)",
                "from": header(headers, "From"),
                "date": int(last.get("internalDate", "0")),
                "snippet": last.get("snippet", ""),
                "unread": "UNREAD" in all_labels,
                "starred": "STARRED" in all_labels,
                "msg_count": len(msgs),
            }

        batch = svc.new_batch_http_request(callback=cb)
        for tid in ids:
            batch.add(
                svc.users()
                .threads()
                .get(userId="me", id=tid, format="metadata", metadataHeaders=["Subject", "From", "Date"])
            )
        if ids:
            batch.execute()
        threads = [rows[i] for i in ids if i in rows]
        return {"threads": threads, "next_page_token": res.get("nextPageToken", "")}

    if op == "get_thread":
        if is_demo:
            return demo_get(thread)
        if is_imap:
            return imap_get(account, thread)
        svc = get_service(account)
        res = svc.users().threads().get(userId="me", id=thread, format="full").execute()
        msgs = [message_to_dict(m) for m in res.get("messages", [])]
        labels = sorted({l for m in msgs for l in m["labels"]})
        if mark_read and "UNREAD" in labels:
            svc.users().threads().modify(userId="me", id=thread, body={"removeLabelIds": ["UNREAD"]}).execute()
            labels = [l for l in labels if l != "UNREAD"]
        return {
            "id": thread,
            "subject": next((m["subject"] for m in msgs if m["subject"]), "(no subject)"),
            "labels": labels,
            "messages": msgs,
        }

    if op in ("send", "reply"):
        if is_demo:
            return demo_send()
        if is_imap:
            return imap_send(account)
        svc = get_service(account)
        m = build_mime(account)
        payload = {}
        if op == "reply" and thread and reply_to_message:
            orig = svc.users().messages().get(userId="me", id=reply_to_message, format="metadata",
                                              metadataHeaders=["Message-ID", "References", "Subject"]).execute()
            oh = orig.get("payload", {}).get("headers", [])
            mid = header(oh, "Message-ID")
            refs = (header(oh, "References") + " " + mid).strip()
            osubj = header(oh, "Subject")
            m["Subject"] = osubj if osubj.lower().startswith("re:") else "Re: " + osubj
            if mid:
                m["In-Reply-To"] = mid
                m["References"] = refs
            payload["threadId"] = thread
        else:
            m["Subject"] = subject
        payload["raw"] = base64.urlsafe_b64encode(m.as_bytes()).decode()
        res = svc.users().messages().send(userId="me", body=payload).execute()
        return {"ok": True, "id": res.get("id", "")}

    if op == "modify":
        if is_demo:
            return demo_modify(thread, add_labels, remove_labels)
        if is_imap:
            return imap_modify(account, thread, add_labels, remove_labels)
        svc = get_service(account)
        svc.users().threads().modify(
            userId="me", id=thread,
            body={"addLabelIds": add_labels or [], "removeLabelIds": remove_labels or []},
        ).execute()
        return {"ok": True}

    if op == "trash":
        if is_demo:
            return demo_modify(thread, ["TRASH"], ["INBOX"])
        if is_imap:
            return imap_trash(account, thread)
        svc = get_service(account)
        svc.users().threads().trash(userId="me", id=thread).execute()
        return {"ok": True}

    if op == "attachment":
        if is_imap:
            return imap_attachment(account, message_id, attachment_id, filename)
        safe_name = os.path.basename(filename or "attachment")
        dest = os.path.join(downloads_dir, safe_name)
        if is_demo:
            with open(dest, "wb") as f:
                f.write(b"%PDF-1.4 demo attachment placeholder\n")
            return {"path": dest}
        svc = get_service(account)
        res = svc.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        data = base64.urlsafe_b64decode(res["data"] + "===")
        with open(dest, "wb") as f:
            f.write(data)
        return {"path": dest}

    return {"error": "unknown op: " + op}
