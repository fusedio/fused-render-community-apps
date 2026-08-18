# /// script
# dependencies = [
#     "google-api-python-client",
#     "google-auth-oauthlib",
# ]
# ///
"""Detached OAuth consent worker.

Spawned by mail.py op=start_auth (never through the fused-render engine —
the interactive consent takes longer than the engine's runPython budget).

Manual flow (not run_local_server): a detached process can't reliably open a
browser, so this writes the consent URL into auth_status.json for the app (or
a shell `open`) to launch, then runs a one-shot localhost callback server to
capture the authorization code. Writes:
  ~/.fused-mail/auth_status.json      pending {url} -> ok {email} | error
  ~/.fused-mail/tokens/<email>.json   refresh token
  ~/.fused-mail/accounts.json         account registry entry
"""
import json
import os
import time
from wsgiref.simple_server import WSGIServer, make_server

MAIL_DIR = os.path.expanduser("~/.fused-mail")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PORT = 8873


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main():
    status_path = os.path.join(MAIL_DIR, "auth_status.json")
    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build

        creds_file = os.path.join(MAIL_DIR, "credentials.json")
        if not os.path.exists(creds_file):
            # client shipped alongside this script (zip distribution)
            creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
        flow = Flow.from_client_secrets_file(creds_file, SCOPES)
        flow.redirect_uri = f"http://localhost:{PORT}/"
        auth_url, _state = flow.authorization_url(
            access_type="offline", prompt="consent select_account"
        )

        captured = {}

        def app(environ, start_response):
            from urllib.parse import parse_qs

            qs = parse_qs(environ.get("QUERY_STRING", ""))
            if "code" in qs:
                captured["code"] = qs["code"][0]
                body = b"<h2>Account connected. You can close this tab and return to Fused Mail.</h2>"
            else:
                captured["error"] = qs.get("error", ["unknown"])[0]
                body = b"<h2>Authorization failed. You can close this tab.</h2>"
            start_response("200 OK", [("Content-Type", "text/html")])
            return [body]

        class Quiet(WSGIServer):
            def handle_error(self, *a):  # keep auth.log clean
                pass

        httpd = make_server("localhost", PORT, app, server_class=Quiet)
        httpd.timeout = 300

        write_json(status_path, {"status": "pending", "url": auth_url, "started_at": time.time()})

        # one request = the redirect back from Google (ignore favicon by looping until code/error)
        deadline = time.time() + 300
        while time.time() < deadline and "code" not in captured and "error" not in captured:
            httpd.handle_request()
        httpd.server_close()

        if "code" not in captured:
            raise RuntimeError(captured.get("error", "consent timed out (5 min)"))

        flow.fetch_token(code=captured["code"])
        creds = flow.credentials

        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        email = svc.users().getProfile(userId="me").execute()["emailAddress"]

        tokens_dir = os.path.join(MAIL_DIR, "tokens")
        os.makedirs(tokens_dir, exist_ok=True)
        with open(os.path.join(tokens_dir, email + ".json"), "w") as f:
            f.write(creds.to_json())

        accounts_path = os.path.join(MAIL_DIR, "accounts.json")
        accounts = []
        if os.path.exists(accounts_path):
            with open(accounts_path) as f:
                accounts = json.load(f)
        if not any(a["email"] == email for a in accounts):
            accounts.append({"email": email, "provider": "gmail", "added_at": time.time()})
        else:
            accounts = [dict(a, provider="gmail") if a["email"] == email else a for a in accounts]
        write_json(accounts_path, accounts)
        write_json(status_path, {"status": "ok", "email": email, "finished_at": time.time()})
    except Exception as e:
        write_json(status_path, {"status": "error", "error": str(e), "finished_at": time.time()})
        raise


if __name__ == "__main__":
    main()
