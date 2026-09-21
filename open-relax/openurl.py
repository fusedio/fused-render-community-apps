"""Hand a link to the OS's default browser.

The page lives in an iframe, so a plain target="_blank" is at the mercy of the
sandbox's popup policy. This is the fallback that always works — and it opens
in the user's real browser rather than inside the app.
"""

import platform
import subprocess
from urllib.parse import urlparse

ALLOWED = {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"}


def main(url: str = ""):
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED:
        return {"ok": False, "error": f"refused: {url[:80]}"}

    system = platform.system()
    cmd = {"Darwin": ["open", url],
           "Windows": ["cmd", "/c", "start", "", url],
           "Linux": ["xdg-open", url]}.get(system)
    if not cmd:
        return {"ok": False, "error": f"no opener for {system}"}

    r = subprocess.run(cmd, check=False, capture_output=True, timeout=20)
    return {"ok": r.returncode == 0, "error": r.stderr.decode("utf-8", "replace")[:200]}
