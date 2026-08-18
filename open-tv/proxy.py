"""Local HLS proxy for the sports.m3u viewer. Stdlib only.

Run:  python3 proxy.py   (listens on http://127.0.0.1:8787)

Fetches stream URLs server-side (no browser CORS / mixed-content limits),
adds Access-Control-Allow-Origin: *, and rewrites playlist URIs so
segments/keys also go through the proxy.
"""
import re
import ssl
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


def proxied(url: str) -> str:
    return "/proxy?url=" + urllib.parse.quote(url, safe="")


def rewrite_playlist(text: str, base_url: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#") or stripped.startswith("EXT"):
            # some servers emit malformed tags missing the leading '#'
            out.append(URI_ATTR_RE.sub(
                lambda m: 'URI="%s"' % proxied(urllib.parse.urljoin(base_url, m.group(1))),
                line))
        else:
            out.append(proxied(urllib.parse.urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/proxy":
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        url = (qs.get("url") or [""])[0]
        if not url.startswith(("http://", "https://")):
            self.send_error(400, "bad url")
            return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                final_url = resp.geturl()
                body = resp.read()
                # content-type lies on some servers (e.g. AES keys served as
                # mpegurl) — only rewrite if it actually looks like a playlist
                if body.lstrip()[:7] == b"#EXTM3U":
                    body = rewrite_playlist(
                        body.decode("utf-8", "replace"), final_url).encode()
                    ctype = "application/vnd.apple.mpegurl"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            try:
                msg = str(e).encode()
                self.send_response(502)
                self._cors()
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except Exception:
                pass


if __name__ == "__main__":
    print(f"HLS proxy on http://127.0.0.1:{PORT}/proxy?url=...")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
