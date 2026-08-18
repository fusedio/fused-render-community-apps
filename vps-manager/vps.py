"""SSH connection daemon for the VPS Manager — stdlib only, no FastAPI, no uvicorn.

ThreadingHTTPServer plus a hand-rolled RFC 6455 WebSocket for the terminal, with
paramiko as the single third-party import.

Each fused-render runPython call is a fresh subprocess, so SSH sessions can't
live there and a resident daemon is unavoidable: a cold connect measures ~8.5s
(interpreter + paramiko import + SSH handshake) against ~0.9s on a warm one.
This module is therefore both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost daemon and returns its port and token; and
  2. the daemon (run as `python vps.py --serve`) — holds live paramiko
     connections per machine and serves HTTP + a terminal WebSocket.

Endpoints (CORS *; every route but /ping requires `?t=<token>`):
  GET  /ping                       -> {"ok", "version"}
  GET  /quit
  GET  /machines                   -> {"machines": [{id, name, host, port,
                                       username, key_path, source, connected}],
                                       config_path, config_error, hidden}
  POST /machines/add               -> body {name, host, port, username,
                                       key_path, password} -> {machine}
  POST /machines/{id}/update       -> same body -> {machine}
  POST /machines/{id}/remove       -> {"ok"}
  POST /machines/{id}/hide         -> {"ok"}   (ssh-config hosts only)
  POST /machines/{id}/unhide       -> {"ok"}
  GET  /known?limit=               -> {hosts: [machine], total, hashed, path}
  POST /connect?id=                -> {"ok", "home", "sftp"}  (400 with reason)
  POST /probe?id=                  -> {"ok", "ms", "shell", "sftp", "detail"}
  POST /reach?id=                  -> {"ok", "ms", "banner"}  (banner only, no
                                      login — except on a ProxyJump host's hop)
  POST /disconnect?id=             -> {"ok"}
  GET  /ls?id=&path=               -> {path, parent, home, entries: [{name,
                                       is_dir, is_link, size, mtime}]}
  GET  /search?id=&path=&q=        -> {entries: [...], truncated}
  POST /mkdir?id=&path=            -> {"ok"}
  POST /rename?id=&src=&dst=       -> {"ok"}   (also move)
  POST /copy?id=&src=&dst=         -> {"ok"}
  POST /delete?id=&path=           -> {"ok"}
  GET  /download?id=&path=         -> file bytes
  GET  /localize?id=&path=         -> {local_path, name, size}  (cache for preview)
  POST /upload?id=&dir=&name=      -> raw request body is the file -> {"ok"}
  WS   /term?id=&cols=&rows=       -> in: {"data"} | {"resize": [c, r]},
                                       out: raw shell output text

The machine list is ~/.ssh/config — plus /etc/ssh/ssh_config, or %ProgramData%/ssh
on Windows — read live, ids `cfg:<alias>`, Include directives expanded, first
setting wins as in ssh; plus anything added by hand in machines.json beside this
file (passwords included — prefer key_path). A hand-added machine on the same
user@host:port as a config alias supersedes it. /known adds the hosts that only
~/.ssh/known_hosts remembers (ids `kh:<host>:<port>`, username guessed) so a box
rented for an afternoon can be picked up again. Idle shutdown after 30 min with
no requests and no open terminals. The state file embeds a hash of this module,
so editing it respawns a fresh daemon on the next ensure().
"""
import contextlib
import glob
import hashlib
import json
import os
import posixpath
import sys
import threading
import time

IDLE_EXIT_S = 30 * 60
STATE = os.path.expanduser("~/.cache/fused-render-vps-stdlib-v1/daemon.json")
PREVIEW_DIR = os.path.expanduser("~/.cache/fused-render-vps-stdlib-v1/preview")
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.path.abspath(sys.path[0])
MACHINES = os.path.join(HERE, "machines.json")


def _lock_down(path, is_dir=False):
    """Best-effort on POSIX: keep this owner-only. The default umask (022)
    would otherwise leave machines.json (plaintext passwords) and daemon.json
    (the bearer token that authorizes every route) world-readable, and the
    daemon listens on 127.0.0.1 — reachable by every local account, not just
    this one. A no-op on Windows: NTFS already scopes the user profile."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            pass


# OpenSSH keeps per-user files in ~/.ssh on macOS, Linux and Windows alike
# (expanduser falls back to %USERPROFILE% there). Only the system-wide directory
# moves: /etc/ssh on unix, %ProgramData%/ssh for Windows' OpenSSH port.
SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")
SSH_CONFIG = os.path.join(SSH_DIR, "config")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
SYSTEM_SSH_DIR = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ssh")
                  if os.name == "nt" else "/etc/ssh")
SYSTEM_CONFIG = os.path.join(SYSTEM_SSH_DIR, "ssh_config")
SYSTEM_KNOWN_HOSTS = os.path.join(SYSTEM_SSH_DIR, "ssh_known_hosts")


class Http(Exception):
    """Mirrors FastAPI's HTTPException shape so the page's error handling is
    unchanged: the body is {"detail": ...} at the given status."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _me():
    return os.path.join(HERE, "vps.py")


def _version():
    """Hash the code, not its mtime plus the interpreter path — a version that
    moves with either churns the daemon on every checkout or interpreter switch."""
    try:
        with open(_me(), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "0"


def _daemon_executable():
    """Prefer the windowless pythonw.exe so no console can flash on Windows."""
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return sys.executable


# ================================================================ ssh config
def _config_text(path, depth=0):
    """ssh_config text with Include directives inlined — paramiko doesn't do them.
    A relative Include resolves against the directory of the file naming it, so
    ~/.ssh/config picks up ~/.ssh/conf.d/* and /etc/ssh/ssh_config /etc/ssh/*."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    if depth >= 5:
        return "".join(lines)
    out = []
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "include":
            for pat in parts[1].split():
                pat = os.path.expanduser(pat.strip('"'))
                if not os.path.isabs(pat):
                    pat = os.path.join(os.path.dirname(path), pat)
                for inc in sorted(glob.glob(pat)):
                    out.append(_config_text(inc, depth + 1))
        else:
            out.append(line)
    return "".join(out)


def _parse_config():
    """(paramiko.SSHConfig, [alias]) — aliases in file order, wildcards dropped.

    The user's config comes first and the system one after, because both ssh and
    paramiko keep the first value they find for a setting."""
    import io

    import paramiko
    text = _config_text(SSH_CONFIG) + "\n" + _config_text(SYSTEM_CONFIG)
    cfg = paramiko.SSHConfig()
    cfg.parse(io.StringIO(text))
    aliases, seen = [], set()
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "host":
            for a in parts[1].replace('"', "").split():
                if any(c in a for c in "*?!") or a in seen:
                    continue
                seen.add(a)
                aliases.append(a)
    return cfg, aliases


def config_machines():
    """Machines described by ~/.ssh/config, in the order they appear there."""
    import getpass
    cfg, aliases = _parse_config()
    out = []
    for alias in aliases:
        h = cfg.lookup(alias)
        keys = [os.path.expanduser(k) for k in h.get("identityfile") or []]
        out.append({"id": "cfg:" + alias,
                    "name": alias,
                    "host": h.get("hostname") or alias,
                    "port": int(h.get("port") or 22),
                    "username": h.get("user") or getpass.getuser(),
                    "key_path": keys[0] if keys else "",
                    "key_paths": keys,
                    "proxy_jump": h.get("proxyjump") or "",
                    "proxy_command": h.get("proxycommand") or "",
                    "source": "ssh_config"})
    return out


# ================================================================ known_hosts
def ssh_private_keys(limit=6):
    """Private keys in ~/.ssh, newest first — a box rented last week was almost
    certainly reached with a recent key. Capped because a guessed host tries them
    one connection at a time, and that time adds up."""
    out = []
    for name in sorted(os.listdir(SSH_DIR)) if os.path.isdir(SSH_DIR) else []:
        p = os.path.join(SSH_DIR, name)
        if name.endswith(".pub") or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(64)
        except OSError:
            continue                   # unix: a key can be there but not ours to read
        if b"PRIVATE KEY" in head:
            out.append(p)
    out.sort(key=os.path.getmtime, reverse=True)
    return out[:limit]


def _host_port(token):
    """`host`, `[host]:port` -> (host, port). The known_hosts grammar, where a
    port is always bracketed."""
    if token.startswith("["):
        host, _, port = token[1:].partition("]:")
        return host, int(port) if port.isdigit() else 22
    return token, 22


def _jump_host_port(token):
    """ssh's ProxyJump host form -> (host, port or None).

    A different grammar from known_hosts above, so that one can't just be reused:
    ProxyJump writes the port unbracketed as `host:port`, and needs the brackets
    only to tell a port apart from the colons in an IPv6 literal. Hence the
    colon count — `[v6]:port`, `host:port`, or a bare host, which covers both
    `example.com` and an unbracketed `2001:db8::1`."""
    if token.startswith("["):
        host, _, port = token[1:].partition("]:")
        return host.rstrip("]"), int(port) if port.isdigit() else None
    if token.count(":") == 1:
        host, _, port = token.partition(":")
        return host, int(port) if port.isdigit() else None
    return token, None


def _known_machine(cfg, keys, host, port):
    h = cfg.lookup(host)      # the file can key an entry by alias as well as by IP
    return {"id": f"kh:{host}:{port}",
            "name": host,
            "host": h.get("hostname") or host,
            "port": port,
            "username": h.get("user") or "root",
            "key_path": "",
            "key_paths": [os.path.expanduser(k) for k in h.get("identityfile") or []] or keys,
            "proxy_jump": h.get("proxyjump") or "",
            "proxy_command": h.get("proxycommand") or "",
            "source": "known_hosts",
            "guessed": True}


def known_machine(host, port):
    return _known_machine(_parse_config()[0], ssh_private_keys(), host, port)


def known_hosts_files(cfg):
    """The host-key files ssh would read, system ones first so the user's file owns
    the recent end. GlobalKnownHostsFile/UserKnownHostsFile override the defaults;
    entries using ssh's % tokens are left to ssh."""
    out = []
    for setting, default in (("globalknownhostsfile", SYSTEM_KNOWN_HOSTS),
                             ("userknownhostsfile", KNOWN_HOSTS)):
        named = (cfg.lookup("*").get(setting) or "").split()
        out += [os.path.expanduser(p) for p in named if "%" not in p] or [default]
    return out


def known_hosts_machines(limit=25, taken=()):
    """(machines, total, hashed) from the known_hosts files, most recent first.

    known_hosts stores no username, no key and no timestamp, so the username here
    is a guess and "most recent" means "nearest the end of the file" — the order
    ssh appends in. Hashed (|1|…) entries can't be reversed, so they're only counted.
    """
    cfg, _ = _parse_config()
    seen, total, hashed = {}, 0, 0
    for path in known_hosts_files(cfg):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if parts and parts[0].startswith("@"):
                parts = parts[1:]
            if not parts or parts[0].startswith("#"):
                continue
            total += 1
            if parts[0].startswith("|"):
                hashed += 1
                continue
            for token in parts[0].split(","):
                if not token or any(c in token for c in "*?"):
                    continue
                host, port = _host_port(token)
                seen.pop((host.lower(), port), None)      # re-seen == more recent
                seen[(host.lower(), port)] = (host, port)
    keys = ssh_private_keys()
    out = []
    for host, port in reversed(list(seen.values())):
        if (host.lower(), port) in taken:
            continue
        out.append(_known_machine(cfg, keys, host, port))
        if len(out) >= limit:
            break
    return out, total, hashed


# ================================================================ ensure()
def _alive(port, token, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version and token
    except Exception:
        return False


def main(action: str = "ensure"):
    """runPython entrypoint: make sure the daemon is running, return {port, token}."""
    import importlib.util
    import subprocess
    # find_spec, not `import paramiko` — this runs on every page load, and actually
    # importing it costs ~1.5s for a check the daemon's own log would report anyway.
    if importlib.util.find_spec("paramiko") is None:
        return {"error": "paramiko is not installed — run: uv pip install paramiko "
                         f"--python {sys.executable}"}
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), st.get("token"), version):
            return {"port": st["port"], "token": st["token"], "reused": True,
                    "version": version}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    _lock_down(os.path.dirname(STATE), is_dir=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    # DETACHED_PROCESS and CREATE_NO_WINDOW are conflicting console flags — the
    # proven templates (docs, latex, usd) detach with the process-group pair only,
    # and CREATE_NO_WINDOW belongs on blocking console children instead.
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_executable(), _me(), "--serve"],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         cwd=HERE, close_fds=True, **detach)
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), st.get("token"), version):
                return {"port": st["port"], "token": st["token"], "reused": False,
                        "version": version}
        except (OSError, ValueError):
            continue
    return {"error": f"daemon did not start — see {log}"}


# ================================================================ websocket
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"      # RFC 6455 §1.3


def _ws_accept(key):
    return __import__("base64").b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _ws_frame(payload, opcode=0x1):
    """Server->client frame: never masked, FIN always set (we don't fragment)."""
    import struct
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def _ws_read(rfile):
    """Read one client frame -> (opcode, payload); (None, b"") at EOF.

    Continuation frames are reassembled, because a browser is free to fragment
    even the small JSON messages the terminal sends."""
    import struct
    data, first = b"", None
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return None, b""
        b0, b1 = head[0], head[1]
        fin, opcode, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", rfile.read(8))[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(n) if n else b""
        if len(chunk) < n:
            return None, b""
        if masked:
            chunk = bytes(c ^ mask[i % 4] for i, c in enumerate(chunk))
        if first is None:
            first = opcode
        if opcode in (0x8, 0x9, 0xA):      # control frames are never fragmented
            return opcode, chunk
        data += chunk
        if fin:
            return first, data


def _safe_name(s):
    """`s` reduced to one path component this machine will actually accept.

    Used for the preview cache, whose path is built from a machine id and a
    remote filename — neither of which is safe as-is. Windows rejects a colon
    outright, so every `cfg:host` and `kh:host:port` id failed there; and
    os.path.join reads a backslash as a separator, so a remote file named
    `..\\..\\x` would land outside the cache. Unicode and spaces are kept."""
    s = posixpath.basename(s.replace("\\", "/"))
    s = "".join("_" if (ch in '<>:"|?*' or ord(ch) < 32) else ch for ch in s)
    return s if s.strip(". ") else "file"


# ================================================================ daemon
def _serve():
    import getpass
    import secrets
    import shlex
    import socket
    import stat as statmod
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, quote, unquote, urlparse

    import paramiko

    VERSION = _version()
    TOKEN = secrets.token_urlsafe(32)
    last_hit = [time.time()]
    open_terms = [0]
    # += / -= on a shared int is a read-modify-write, and two terminals can
    # close on different threads at the same instant — without a lock that
    # can lose a decrement and leave this stuck above 0, which means idle
    # shutdown (below) never fires again for the life of the daemon.
    open_terms_lock = threading.Lock()
    # last_hit is stamped once, when a request STARTS — a download, upload, or
    # localize that runs longer than the idle window looks identical to actual
    # idleness to a check that only reads last_hit, so idle_watch (below) would
    # shut the daemon down mid-transfer. This is the same "count what's still
    # busy" fix as open_terms, for the transfers instead of the terminals.
    long_ops = [0]
    long_ops_lock = threading.Lock()

    class Busy:
        def __enter__(self):
            with long_ops_lock:
                long_ops[0] += 1

        def __exit__(self, *exc):
            with long_ops_lock:
                long_ops[0] -= 1
            # so the idle clock restarts from when the transfer actually ended,
            # not from last_hit's stamp at its START — otherwise a transfer that
            # ran most of the idle window could trip the very next check
            last_hit[0] = time.time()

    # ---- machine registry ----
    reg_lock = threading.Lock()

    def load_registry():
        try:
            with open(MACHINES, encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            reg = {}
        reg.setdefault("machines", [])
        reg.setdefault("hidden", [])
        return reg

    def load_machines():
        return load_registry()["machines"]

    def save_registry(reg):
        tmp = MACHINES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
        _lock_down(tmp)      # before the rename — machines.json holds plaintext passwords
        os.replace(tmp, MACHINES)

    def save_machines(machines):
        reg = load_registry()
        reg["machines"] = machines
        save_registry(reg)

    def endpoint(m):
        return (m["host"].lower(), int(m.get("port") or 22), m["username"])

    def all_machines():
        """ssh_config hosts + hand-added machines. A hand-added machine on the same
        user@host:port supersedes the config alias, which it then links back to."""
        reg = load_registry()
        added = reg["machines"]
        try:
            cfg = config_machines()
            err = ""
        except Exception as e:
            cfg, err = [], str(e) or type(e).__name__
        by_endpoint = {endpoint(m): m for m in added}
        out, hidden = [], []
        for m in cfg:
            twin = by_endpoint.get(endpoint(m))
            if twin is not None:
                twin["from_config"] = m["name"]
                # This entry hides the alias, so it has to carry the alias's jump
                # host or the box goes from reachable to unreachable. A missing key
                # means it was saved before we stored one; an empty string means
                # the user cleared it on purpose, and that is left alone.
                if "proxy_jump" not in twin and m.get("proxy_jump"):
                    twin["proxy_jump"] = m["proxy_jump"]
                continue
            (hidden if m["name"] in reg["hidden"] else out).append(m)
        return out + added, hidden, err

    def get_machine(mid):
        if mid.startswith("kh:"):
            host, _, port = mid[3:].rpartition(":")
            if host and port.isdigit():
                return known_machine(host, int(port))
        pool, hidden, _ = all_machines()
        for m in pool + hidden:
            if m["id"] == mid:
                return m
        raise Http(404, f"unknown machine {mid}")

    def public(m):
        return {k: v for k, v in m.items() if k != "password"}

    # ---- live connections ----
    conns = {}            # id -> {"client", "jumps", "sftp", "home", "key_used", "lock"}
    conns_lock = threading.Lock()
    dials = {}            # id -> Lock serialising the dial for that one machine
    generations = {}      # id -> int, bumped whenever its host/credentials change

    def bump_gen(mid):
        """A dial already in flight for `mid` is dialling a definition that no
        longer exists — get_conn() checks this against the value it started
        with and throws its result away rather than registering a connection
        to the wrong box under the right name."""
        with conns_lock:
            generations[mid] = generations.get(mid, 0) + 1

    def close_jumps(jumps):
        """Hang up a jump chain, innermost hop first — the outer session carries
        the inner one, so closing it the other way round pulls the floor out."""
        for j in reversed(jumps):
            try:
                j.close()
            except Exception:
                pass

    def close_conn(c):
        """Hang up a connection and everything it was reached through."""
        try:
            c["client"].close()
        except Exception:
            pass
        close_jumps(c["jumps"])

    def drop_conn(mid, only=None):
        """Hang up `mid`'s connection, if it has one.

        `only` pins it to one particular connection object: a caller that found a
        dead session and wants it gone must not close the live one that another
        thread dialled for the same machine in the meantime."""
        with conns_lock:
            c = conns.get(mid)
            if c is None or (only is not None and c is not only):
                return
            del conns[mid]
        close_conn(c)

    def live_conn(mid):
        """The connection for `mid` if its transport is up, else None.

        One dict lookup, so a drop_conn() in another thread — an edit, a remove,
        a hide — can't land between a liveness check and the fetch and turn an
        in-flight request into a KeyError.

        A dead one is hung up here and now. Its own session is already gone, but
        the hops it was reached through are still live sessions on the bastion,
        and nothing else would notice them until this machine is dialled again or
        the daemon idles out half an hour later."""
        c = conns.get(mid)
        if not c:
            return None
        t = c["client"].get_transport()
        if t and t.is_active():
            return c
        drop_conn(mid, only=c)
        return None

    def is_connected(mid):
        return live_conn(mid) is not None

    def keys_of(m):
        """The machine's key files, loaded, as (path, key) — unreadable ones dropped.

        Loading them here rather than handing paramiko a key_filename is what
        makes a refusal legible. Given a filename it guesses the type by trying
        RSA, ECDSA and Ed25519 in turn and keeps only the LAST error, so a key
        the server simply refused comes back as "encountered RSA key, expected
        OPENSSH key" — an SSHException rather than an auth failure, which reads
        as a broken connection and stops us trying the rest of the keys.
        from_path() reads the type off the file instead."""
        paths = m.get("key_paths") or ([m["key_path"]] if m.get("key_path") else [])
        out = []
        for path in (os.path.expanduser(k) for k in paths):
            if not os.path.isfile(path):
                continue
            try:
                out.append((path, paramiko.PKey.from_path(path)))
            except Exception:
                continue      # passphrase-protected, or a format paramiko can't read
        return out

    def saved_hop(alias, host, port, user):
        """A hand-added machine standing in for a hop, by the name the ProxyJump
        used or by the endpoint it resolves to.

        The config is only half of what we know. A bastion added by hand keeps its
        password and its key in machines.json, and ssh's own resolution has never
        heard of that file — so a hop that connects perfectly well from the
        sidebar would be dialled here with no credentials at all. Matching the
        name too covers a bastion that exists ONLY as a hand-added machine, where
        the alias resolves to nothing and its host is not a real hostname.

        `user` is the account the ProxyJump asked for, if it named one. A saved
        machine for a different account is not this hop: its password belongs to
        somebody else, and offering it would be worse than having none."""
        mine = [m for m in load_machines() if not user or m["username"] == user]
        for m in mine:                        # the name is the plainer signal
            if m["name"] == alias:
                return m
        for m in mine:
            if m["host"].lower() == host.lower() and int(m.get("port") or 22) == port:
                return m
        return None

    def jump_machine(spec):
        """Resolve a ProxyJump value into the machine to dial for it.

        `spec` is ssh's [user@]host[:port], or a comma-separated chain. In
        "a,b" the target is reached through b and b through a, so the LAST hop
        is the one nearest the target: that becomes this machine, and everything
        before it becomes its own ProxyJump, which open_client() then resolves
        the same way (bounded by its depth guard). A hop that is itself a config
        alias with a ProxyJump of its own is followed too, as ssh would."""
        cfg, _ = _parse_config()
        hops = [s.strip() for s in spec.split(",") if s.strip()]
        if not hops:
            raise Http(400, "empty ProxyJump")
        earlier, last = hops[:-1], hops[-1]
        user, _, hostport = last.rpartition("@")
        alias, port = _jump_host_port(hostport)
        h = cfg.lookup(alias)
        hop = {"name": last, "host": h.get("hostname") or alias,
               "port": int(port or h.get("port") or 22),
               "username": user or h.get("user") or getpass.getuser(),
               "key_paths": [os.path.expanduser(k) for k in h.get("identityfile") or []],
               "proxy_jump": ",".join(earlier) or h.get("proxyjump") or "",
               "proxy_command": h.get("proxycommand") or ""}
        saved = saved_hop(alias, hop["host"], hop["port"], user)
        if saved:
            # Someone added this box by hand because the config alone didn't get
            # in, so its credentials win — the same way a hand-added machine
            # supersedes the alias it shares an endpoint with.
            hop["host"], hop["port"] = saved["host"], int(saved.get("port") or 22)
            hop["username"] = saved["username"]
            hop["password"] = saved.get("password") or ""
            hop["key_paths"] = ([os.path.expanduser(saved["key_path"])]
                                if saved.get("key_path") else hop["key_paths"])
            hop["proxy_jump"] = ",".join(earlier) or saved.get("proxy_jump") or ""
            hop["proxy_command"] = ""
        return hop

    def open_client(m, depth=0):
        """Connect to one machine, hopping through its ProxyJump if it has one.
        Returns (client, jump_clients, key_that_worked).

        `jump_clients` is every hop dialled for this connection, ordered outward
        from here, so the last one is nearest the target. The whole chain comes
        back rather than just the last hop: for "a,b" the session to b rides
        inside the session to a, and handing back only b leaves nobody holding a
        — disconnecting cannot hang it up, so every connect strands one more live
        session on the bastion.

        A known_hosts guess has no recorded identity, so its keys are offered one
        at a time — slower, but then we know which one to save."""
        if m.get("proxy_command"):
            raise Http(400, "ProxyCommand hosts aren't supported — "
                            "add the machine by hand instead")
        port = int(m.get("port") or 22)
        keys = keys_of(m)
        pw = m.get("password") or None
        # (path, key, may the agent answer instead). A guess has no recorded
        # identity, so its keys go one per connection — with the agent held back,
        # because an agent key answering for the key we offered would have us
        # record a path that signed nothing in. The agent gets its turn last, and
        # the empty path it reports honestly says "no key of yours worked, the
        # agent did".
        attempts = [(p, k, not m.get("guessed")) for p, k in keys]
        if not keys or m.get("guessed"):
            attempts.append(("", None, True))
        jumps, err = [], None
        # One handler for the whole dial, because everything in here can fail
        # after the hops are up — the channel to the target, a hop of the hop, the
        # depth guard, the last attempt running out of keys. Closing the chain at
        # each of those sites is how one of them ends up forgotten.
        try:
            for path, pkey, agent in attempts:
                sock = None
                if m.get("proxy_jump"):
                    if depth >= 3:
                        raise Http(400, "ProxyJump chain is too long")
                    if not jumps:
                        hop, earlier, _ = open_client(jump_machine(m["proxy_jump"]),
                                                      depth + 1)
                        jumps = earlier + [hop]
                    sock = jumps[-1].get_transport().open_channel(
                        "direct-tcpip", (m["host"], port), ("127.0.0.1", 0))
                client = paramiko.SSHClient()
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    client.connect(m["host"], port=port, username=m["username"],
                                   pkey=pkey, password=pw, sock=sock,
                                   look_for_keys=not (pkey or pw),
                                   allow_agent=agent and not pw,
                                   timeout=8, auth_timeout=12, banner_timeout=12)
                except paramiko.AuthenticationException as e:
                    err = e
                    client.close()
                    continue
                except BaseException:
                    # A refusal isn't the only way this fails after the socket is
                    # up — a bad banner, a protocol error, the handshake dropped
                    # mid-way — and every one of those leaves a half-open
                    # Transport and its background thread that only client.close()
                    # reaps. This one never made it into `conns`, so drop_conn()
                    # never gets a chance either.
                    client.close()
                    raise
                client.get_transport().set_keepalive(20)
                return client, jumps, path
            raise err
        except BaseException:
            close_jumps(jumps)
            raise

    def dial_lock(mid):
        with conns_lock:
            lk = dials.get(mid)
            if lk is None:
                # RLock, not Lock: get_conn() re-enters itself, still holding this,
                # when a dial it just finished turns out to be stale (the
                # generation check below) — a plain Lock would deadlock a thread
                # against itself on that retry.
                lk = dials[mid] = threading.RLock()
            return lk

    def get_conn(mid):
        """The live connection for one machine, dialling it if there isn't one.

        The dial is serialised per machine: the page fires /probe for every row
        and /connect for the one you clicked at the same time, and without this
        both would open their own SSH session, the second overwriting — and so
        leaking — the first."""
        c = live_conn(mid)
        if c:
            return c
        with dial_lock(mid):
            c = live_conn(mid)         # another thread dialled while we waited
            if c:
                return c
            # A dial takes seconds; an edit or a remove can land in that window
            # (dial_lock only keeps two DIALS for this id from overlapping, it
            # doesn't stop do_update/do_remove writing machines.json mid-dial).
            # Snapshotting the generation here and checking it after connecting
            # is what stops that connection from being registered under a name
            # whose host or password has since changed — or that no longer
            # exists at all.
            gen = generations.get(mid, 0)
            m = get_machine(mid)
            drop_conn(mid)
            client, jumps, key_used = open_client(m)
            try:
                sftp = client.open_sftp()
                home = sftp.normalize(".")
            except Exception:
                sftp, home = None, "/"   # git-only hosts authenticate but refuse SFTP
            c = {"client": client, "jumps": jumps, "sftp": sftp, "home": home,
                 "key_used": key_used, "username": m["username"], "lock": threading.Lock(),
                 "tsftp": None, "tlock": threading.Lock(), "gnu_find": None}
            with conns_lock:
                stale = generations.get(mid, 0) != gen
                if not stale:
                    conns[mid] = c
            if stale:
                close_conn(c)
                return get_conn(mid)   # dial again against whatever is current now
            return c

    def require_sftp(c):
        """Whether the host does SFTP at all is settled once, when we connect."""
        if c["sftp"] is None:
            raise Http(400, "this host allows SSH but not SFTP, so there are "
                            "no files to browse — try the terminal")

    def open_sftp_channel(c):
        """A fresh SFTP channel on this connection.

        The connection can be torn down under a request that is already running
        — the machine gets hidden, edited, disconnected — and paramiko reports
        that as an AttributeError on a None transport, so check first and say
        what actually happened."""
        t = c["client"].get_transport()
        if not (t and t.is_active()):
            raise Http(400, "the connection to this machine dropped — reconnect and retry")
        return c["client"].open_sftp()

    def sftp_of(c):
        """The connection's SFTP channel, reopened if it has dropped. Call under
        c["lock"].

        An SFTP channel can die on its own while the SSH transport stays up, and
        a live transport is all get_conn() looks at — so a dropped channel used
        to leave the file browser failing for good, until the daemon restarted
        or the machine entry was touched."""
        require_sftp(c)
        if c["sftp"].sock.closed:
            c["sftp"] = open_sftp_channel(c)
        return c["sftp"]

    def tsftp(c):
        """A second SFTP channel for bulk transfers, so streaming a big download or
        upload never blocks directory listings on c["lock"]. Call under c["tlock"]."""
        require_sftp(c)
        t = c.get("tsftp")
        if t is None or t.sock.closed:
            t = c["tsftp"] = open_sftp_channel(c)
        return t

    def _drain_both(out, err):
        """Read stdout and stderr at the same time (see sh()'s docstring for
        why one-after-the-other deadlocks), and hand back whichever of the two
        actually raised instead of leaving a bare KeyError for the caller to
        puzzle over — the paramiko `timeout` is a per-read idle timeout, not a
        ceiling on the command's total runtime, so a long but perfectly healthy
        command with a quiet stretch longer than that timeout raises here too,
        not just a truly dead connection."""
        got, errs = {}, {}

        def drain(f, key):
            try:
                got[key] = f.read()
            except Exception as e:
                errs[key] = e

        readers = [threading.Thread(target=drain, args=(out, "out"), daemon=True),
                  threading.Thread(target=drain, args=(err, "err"), daemon=True)]
        for t in readers:
            t.start()
        for t in readers:
            t.join()
        if errs:
            e = next(iter(errs.values()))
            raise Http(504, f"lost contact with the remote command: {e}")
        return got["out"], got["err"]

    def sh(c, cmd, timeout=60):
        """This used to wait on recv_exit_status() before reading anything —
        fine for a quiet command, but paramiko's own docs warn that order hangs
        forever once the remote fills the channel window (2 MB by default) and
        blocks on writing more. `rm -rf` printing one "Permission denied" per
        file is exactly that command.

        `timeout` is per read, not a ceiling on the command — pass None for
        one that's allowed to run long (paired with Busy() at the call site,
        since idle shutdown can't tell a slow command from an idle daemon
        either)."""
        _, out, err = c["client"].exec_command(cmd, timeout=timeout)
        out_b, err_b = _drain_both(out, err)
        rc = out.channel.recv_exit_status()
        data = out_b.decode("utf-8", "replace")
        msg = err_b.decode("utf-8", "replace").strip()
        if rc != 0:
            raise Http(400, msg or f"command failed (exit {rc})")
        return data

    def sh_soft(c, cmd, timeout=60):
        """Never reading stderr at all is the same latent hang as sh() had — it
        just needs the remote to write enough of it, which a well-behaved host
        won't, but this runs on first contact with whatever was typed into the
        Add Machine form. Draining both concurrently costs nothing when there's
        nothing to drain."""
        _, out, err = c["client"].exec_command(cmd, timeout=timeout)
        out_b, _ = _drain_both(out, err)
        out.channel.recv_exit_status()
        return out_b.decode("utf-8", "replace")

    def sh_rc(c, cmd):
        """Just the exit status — for asking a remote what its tools can do."""
        _, out, _ = c["client"].exec_command(cmd, timeout=60)
        out.read()
        return out.channel.recv_exit_status()

    def gnu_find(c):
        """Does this host's `find` support GNU -printf? BSD (macOS) and older
        BusyBox (Alpine) don't, and would otherwise fail the whole search.
        -maxdepth 0 walks nothing, so the probe is instant; cached per
        connection."""
        if c["gnu_find"] is None:
            c["gnu_find"] = sh_rc(c, "find . -maxdepth 0 -printf '' 2>/dev/null") == 0
        return c["gnu_find"]

    # ---- endpoints ----
    def do_machines():
        pool, hidden, err = all_machines()
        return {"machines": [dict(public(m), connected=is_connected(m["id"]))
                             for m in pool],
                "hidden": [public(m) for m in hidden],
                "config_path": SSH_CONFIG if os.path.isfile(SSH_CONFIG) else "",
                "config_error": err}

    def do_known(limit):
        pool, hidden, _ = all_machines()
        taken = set()
        for m in pool + hidden:      # known_hosts keys entries by alias or hostname
            taken.add((m["host"].lower(), int(m.get("port") or 22)))
            taken.add((m["name"].lower(), int(m.get("port") or 22)))
        hosts, total, hashed = known_hosts_machines(limit, taken)
        paths = [p for p in known_hosts_files(_parse_config()[0]) if os.path.isfile(p)]
        return {"hosts": hosts, "total": total, "hashed": hashed,
                "path": "\n".join(paths)}

    def do_add(body):
        m = {"id": uuid.uuid4().hex[:8],
             "name": (body.get("name") or body.get("host") or "").strip(),
             "host": (body.get("host") or "").strip(),
             "port": int(body.get("port") or 22),
             "username": (body.get("username") or "").strip(),
             "key_path": (body.get("key_path") or "").strip(),
             "proxy_jump": (body.get("proxy_jump") or "").strip(),
             "password": body.get("password") or ""}
        if not m["host"] or not m["username"]:
            raise Http(400, "host and username are required")
        with reg_lock:
            ms = load_machines()
            ms.append(m)
            save_machines(ms)
        return {"machine": public(m)}

    def cfg_alias(mid):
        if not mid.startswith("cfg:"):
            raise Http(400, "not an ssh-config host")
        return mid[4:]

    def reject_derived(mid, verb):
        if mid.startswith("cfg:"):
            raise Http(400, f"{mid[4:]} comes from {SSH_CONFIG} — edit that "
                            f"file to {verb} it, or hide it from this list")
        if mid.startswith("kh:"):
            raise Http(400, "this host is only remembered in known_hosts — "
                            "save it here first")

    def do_update(mid, body):
        reject_derived(mid, "change")
        with reg_lock:
            ms = load_machines()
            for m in ms:
                if m["id"] == mid:
                    for k in ("name", "host", "username", "key_path", "proxy_jump"):
                        if k in body:
                            m[k] = (body.get(k) or "").strip()
                    if "port" in body:
                        m["port"] = int(body.get("port") or 22)
                    # a blank password field means "leave it as-is" (so a save
                    # that only touches the host doesn't accidentally drop it),
                    # which left no way to actually CLEAR a saved password
                    # short of deleting and recreating the machine — an
                    # explicit flag is what a blank field can't distinguish
                    # from "unchanged".
                    if body.get("clear_password"):
                        m["password"] = ""
                    elif body.get("password"):
                        m["password"] = body["password"]
                    save_machines(ms)
                    bump_gen(mid)
                    drop_conn(mid)
                    return {"machine": public(m)}
        raise Http(404, f"unknown machine {mid}")

    def do_remove(mid):
        reject_derived(mid, "remove")
        bump_gen(mid)
        drop_conn(mid)
        with reg_lock:
            save_machines([m for m in load_machines() if m["id"] != mid])
        return {"ok": True}

    def do_hide(mid):
        alias = cfg_alias(mid)
        # same reasoning as do_update/do_remove: a dial for this id that was
        # already in flight when the hide landed can still finish and
        # register afterward, and without bumping the generation it looks
        # exactly like any other live connection — held open for a machine
        # the user just asked to stop seeing, until idle shutdown eventually
        # notices nobody's touched it.
        bump_gen(mid)
        drop_conn(mid)
        with reg_lock:
            reg = load_registry()
            if alias not in reg["hidden"]:
                reg["hidden"].append(alias)
                save_registry(reg)
        return {"ok": True}

    def do_unhide(mid):
        alias = cfg_alias(mid)
        with reg_lock:
            reg = load_registry()
            reg["hidden"] = [a for a in reg["hidden"] if a != alias]
            save_registry(reg)
        return {"ok": True}

    def do_connect(mid):
        try:
            c = get_conn(mid)
        except Http:
            raise
        except Exception as e:
            # get_conn() failing here never registered anything for mid — it
            # drops whatever was there itself before it dials. Doing it again
            # is not just redundant: this dial's own lock is already released,
            # so another thread's dial for the same id can have succeeded and
            # registered a live connection in the time it takes us to get here,
            # and an unpinned drop_conn would tear that one down instead.
            raise Http(400, str(e) or type(e).__name__)
        return {"ok": True, "home": c["home"], "sftp": c["sftp"] is not None}

    def do_probe(mid):
        """Actually log in and report what answered — never raises for a dead host."""
        t0 = time.time()
        try:
            c = get_conn(mid)
        except Http as e:
            return {"ok": False, "error": e.detail}
        except Exception as e:
            # see do_connect: get_conn() already cleans up after its own
            # failure, so a second, unpinned drop_conn here would risk closing
            # a connection a concurrent dial for this id just registered
            return {"ok": False, "error": str(e) or type(e).__name__}
        ms = int((time.time() - t0) * 1000)
        detail, shell = "", False
        try:
            out = sh_soft(c, "echo SSHOK; (. /etc/os-release 2>/dev/null; echo "
                             "${PRETTY_NAME:-}); uname -sr; uptime -p 2>/dev/null")
        except Exception:
            out = ""
        lines = [x.strip() for x in out.splitlines()]
        if lines[:1] == ["SSHOK"]:
            shell = True
            os_name = (lines[1] if len(lines) > 1 else "") or (lines[2] if len(lines) > 2 else "")
            uptime = (lines[3] if len(lines) > 3 else "").split(",")[0]
            detail = " · ".join(x for x in (os_name, uptime) if x)
        return {"ok": True, "ms": ms, "shell": shell, "detail": detail,
                "sftp": c["sftp"] is not None,
                "username": c["username"], "key_path": c["key_used"]}

    def do_reach(mid):
        """Is anything listening? TCP connect and read the SSH banner, no login.

        A machine behind a bastion has an address that means something there and
        nothing here, so connecting straight to it would file a live box under
        "not answering". Those are reached the way ssh reaches them, through the
        hop — which does cost a login on the hop, but still none on the machine
        being asked about, which is the whole point of this route."""
        m = get_machine(mid)
        port = int(m.get("port") or 22)
        t0 = time.time()
        jumps = []
        try:
            if m.get("proxy_jump") and not m.get("proxy_command"):
                hop, jumps, _ = open_client(jump_machine(m["proxy_jump"]))
                jumps = jumps + [hop]
                s = jumps[-1].get_transport().open_channel(
                    "direct-tcpip", (m["host"], port), ("127.0.0.1", 0))
            else:
                s = socket.create_connection((m["host"], port), timeout=4)
        except Exception as e:
            close_jumps(jumps)
            reason = getattr(e, "strerror", None) or str(e)
            if m.get("proxy_jump"):
                reason = f"via {m['proxy_jump']}: {reason}"   # which leg gave up
            return {"ok": False, "error": reason}
        try:
            s.settimeout(4)
            raw = s.recv(255).decode("utf-8", "replace")
        except OSError:
            raw = ""
        finally:
            s.close()
            close_jumps(jumps)
        # RFC 4253: the server speaks first, and what it says begins with "SSH-".
        # An open port that then says nothing, or says something else, is not a
        # machine anyone can sign in to — calling that "answering" just sends
        # someone to a box that will turn them away.
        line = next((x for x in raw.splitlines() if x.strip()), "")
        if not line.startswith("SSH-"):
            return {"ok": False,
                    "error": "the port is open, but nothing there speaks SSH"
                    if line else "the port is open, but nothing answered"}
        # SSH-protoversion-softwareversion SP comments; the software version is
        # what people recognise, and it is allowed to be absent.
        rest = line.split("-", 2)[2].split() if line.count("-") >= 2 else []
        return {"ok": True, "ms": int((time.time() - t0) * 1000),
                "banner": rest[0] if rest else ""}

    def do_ls(mid, path):
        c = get_conn(mid)
        path = path or c["home"]
        with c["lock"]:
            sftp = sftp_of(c)
            # normalize() is a server round-trip — only pay it for relative paths
            path = posixpath.normpath(path) if path.startswith("/") else sftp.normalize(path)
            entries = []
            for a in sftp.listdir_attr(path):
                is_link = statmod.S_ISLNK(a.st_mode or 0)
                is_dir = statmod.S_ISDIR(a.st_mode or 0)
                if is_link:
                    try:
                        is_dir = statmod.S_ISDIR(sftp.stat(posixpath.join(path, a.filename)).st_mode)
                    except OSError:
                        is_dir = False
                entries.append({"name": a.filename, "is_dir": is_dir,
                                "is_link": is_link, "size": a.st_size or 0,
                                "mtime": a.st_mtime or 0})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        return {"path": path, "parent": parent if path != "/" else None,
                "home": c["home"], "entries": entries}

    def do_search(mid, path, q, limit):
        """Recursive name search under one folder, like the Ubuntu file manager.

        GNU find reports type, size and mtime in the same pass via -printf. Where
        that isn't supported, a second -print pass marks the directories: paths
        and folder-ness are POSIX, size and mtime aren't, so those come back blank
        rather than the whole search reporting nothing found.

        Either way a symlink is classified by what it points AT, because /ls
        resolves link targets with stat() — otherwise a link to a folder would
        open as a folder while browsing and as a file from search."""
        q = q.strip()
        if not q:
            return {"entries": [], "truncated": False}
        c = get_conn(mid)
        walk = (f"{shlex.quote(path)} -maxdepth 8 "
                f"-iname {shlex.quote('*' + q + '*')}")
        cap = f"2>/dev/null | head -n {int(limit)}"
        found = []            # (path, is_dir, is_link, size, mtime)
        with Busy():        # a walk over a big enough tree is a long_ops case too
            if gnu_find(c):
                # %y is the entry itself, %Y what it resolves to (L/N/? if broken)
                out = sh_soft(c, f"find {walk} -printf '%y\\t%Y\\t%s\\t%T@\\t%p\\n' {cap}",
                              timeout=None)
                for line in out.splitlines():
                    parts = line.split("\t", 4)
                    if len(parts) != 5:
                        continue
                    typ, target, size, mtime, p = parts
                    found.append((p, target == "d", typ == "l",
                                  int(size) if size.isdigit() else None,
                                  int(float(mtime)) if mtime else None))
            else:
                # A directories-only pass over the SAME tree, capped the same way,
                # sounds equivalent to filtering the full listing — it isn't. -L
                # makes it follow a matching symlink-to-a-directory and walk
                # everything inside, which the non-L full listing never descends
                # into; that can push a real directory past ITS OWN head -n limit
                # while the full listing (shorter, since it didn't recurse there)
                # still shows that directory within the visible results — filed as
                # a file, because it never made it into `dirs`.
                #
                # Testing exactly the paths already matched can't have this
                # problem: nothing is re-walked, so there's no second cap to
                # disagree with the first.
                matches = sh_soft(c, f"find {walk} -print {cap}", timeout=None).splitlines()
                if matches:
                    paths = " ".join(shlex.quote(p) for p in matches)
                    dirs = set(sh_soft(c, f"find -L {paths} -maxdepth 0 -type d "
                                          f"-print 2>/dev/null", timeout=None).splitlines())
                else:
                    dirs = set()
                # size/mtime are None, not 0 — 0 would read as "confirmed empty" and
                # the page uses that to skip asking before downloading a huge file
                # for preview. None means "not measured", which is the truth here.
                found = [(p, p in dirs, False, None, None) for p in matches]
        # truncated has to be judged on the RAW hit count, before the search
        # root itself is filtered out below — if the root happens to match the
        # query and the pipeline was already at the cap, dropping that one
        # entry would make a truncated result look complete.
        was_capped = len(found) >= limit
        entries = [{"name": posixpath.basename(p), "path": p, "is_dir": is_dir,
                    "is_link": is_link, "size": size, "mtime": mtime}
                   for p, is_dir, is_link, size, mtime in found if p != path]
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"entries": entries, "truncated": was_capped}

    def do_mkdir(mid, path):
        c = get_conn(mid)
        with c["lock"]:
            sftp_of(c).mkdir(path)
        return {"ok": True}

    def _reject_if_exists(c, dst):
        # The no-overwrite / no-move-into-directory contract both rename and
        # copy honor: refuse a destination that is already a name in use.
        # lstat, not stat — a broken symlink is still a name in use, and stat
        # would follow it to its missing target, raise, and wrongly report the
        # name as free.
        with c["lock"]:
            try:
                sftp_of(c).lstat(dst)
            except OSError:
                return
        raise Http(409, f"{posixpath.basename(dst)} already exists")

    def _claim(c, src, dst, shell_fallback):
        # Move src onto dst without ever overwriting it. The caller has already
        # rejected a dst that existed at check time; this closes the check→move
        # race. sftp.rename refuses an existing dst outright — no overwrite, no
        # nesting into a directory — atomically, which is exactly the claim we
        # want, and it needs no shell (BusyBox mv has no -n flag). The one
        # thing it cannot do is move across filesystems, so rename (whose src
        # and dst may straddle them) allows a shell fallback for that case; a
        # copy stages a temp sibling of dst, always same-filesystem, and never
        # does.
        with c["lock"]:
            try:
                sftp_of(c).rename(src, dst)
                return
            except OSError:
                if not shell_fallback:
                    raise Http(409, f"{posixpath.basename(dst)} already exists")
        # mv -n exits 0 whether it moved anything or not, so the lstat afterward
        # is what tells a real move from a skipped one: src gone means it landed.
        with Busy():
            sh(c, f"mv -n -- {shlex.quote(src)} {shlex.quote(dst)}", timeout=None)
        with c["lock"]:
            try:
                sftp_of(c).lstat(src)
            except OSError:
                return
        raise Http(409, f"{posixpath.basename(dst)} already exists")

    def _rm_quiet(c, path):
        # Best-effort remove — never raises. Used to tidy a staged temp (which
        # may be a tree, hence rm -rf) so a partial copy can't linger on the
        # remote; the error that got us here is the one worth surfacing.
        try:
            with Busy():
                sh(c, f"rm -rf -- {shlex.quote(path)}", timeout=None)
        except Exception:
            pass

    def do_rename(mid, src, dst):
        c = get_conn(mid)
        _reject_if_exists(c, dst)
        _claim(c, src, dst, shell_fallback=True)
        return {"ok": True}

    def do_copy(mid, src, dst):
        c = get_conn(mid)
        # cp has no atomic "fail if dst exists" (cp -n skips silently, exit 0),
        # so copy to a temp sibling and claim dst from there with the same SFTP
        # rename do_rename trusts. The ExitStack drops the temp if anything
        # before the claim raises; pop_all() cancels that once dst is claimed.
        _reject_if_exists(c, dst)
        tmp = f"{dst}.fused-copy-{os.urandom(6).hex()}"
        with contextlib.ExitStack() as stack:
            stack.callback(_rm_quiet, c, tmp)
            with Busy():
                sh(c, f"cp -a -- {shlex.quote(src)} {shlex.quote(tmp)}", timeout=None)
            _claim(c, tmp, dst, shell_fallback=False)
            stack.pop_all()
        return {"ok": True}

    def do_delete(mid, path):
        # normpath first: the raw string "/home/.." isn't "/", but it's where
        # rm -rf would land — comparing the un-normalized path only catches
        # someone who typed "/" itself, not ".." dressed up to reach the same
        # place.
        if not posixpath.normpath(path or "/").rstrip("/"):
            raise Http(400, "refusing to delete /")
        c = get_conn(mid)
        with Busy():
            sh(c, f"rm -rf -- {shlex.quote(path)}", timeout=None)
        return {"ok": True}

    def do_localize(mid, path):
        c = get_conn(mid)
        with c["tlock"]:
            st = tsftp(c).stat(path)
        size = st.st_size or 0
        mtime = int(st.st_mtime or 0)
        # the sha1 pins which remote file this is, so the id and name in the path
        # only have to be legible — and legal (see _safe_name)
        base = _safe_name(posixpath.basename(path) or "file")
        key = hashlib.sha1(f"{mid}:{path}:{mtime}:{size}".encode()).hexdigest()[:12]
        dest_dir = os.path.join(PREVIEW_DIR, _safe_name(mid), key)
        dest = os.path.join(dest_dir, base)
        if not (os.path.isfile(dest) and os.path.getsize(dest) == size):
            os.makedirs(dest_dir, exist_ok=True)
            with Busy(), c["tlock"]:
                f = tsftp(c).open(path, "rb")
                try:
                    f.prefetch()
                except BaseException:
                    f.close()   # open() succeeded; nothing else closes this handle
                    raise
                with f, open(dest, "wb") as out:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
        return {"local_path": dest.replace("\\", "/"), "name": base, "size": size}

    def do_upload(mid, directory, name, limited, length):
        """`limited` is already a _Limited over the request body — do_POST keeps
        its own reference to it, so it can drain whatever this doesn't read if
        we raise before reaching the end of the upload."""
        c = get_conn(mid)
        dst = posixpath.join(directory, os.path.basename(name or "upload"))
        with Busy(), c["tlock"]:
            tsftp(c).putfo(limited, dst, file_size=length)
        return {"ok": True, "path": dst}

    # ---- terminal ----
    def run_term(handler, mid, cols, rows):
        """Bridge an interactive shell to the browser over a hand-rolled WebSocket."""
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler.send_error(400, "expected a websocket upgrade")
            return
        handler.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + _ws_accept(key).encode() + b"\r\n\r\n")
        handler.wfile.flush()
        handler.close_connection = True
        wlock = threading.Lock()

        def send(payload, opcode=0x2):
            """Shell output goes out as BINARY frames. A text frame has to be
            valid UTF-8 on its own, but chan.recv() cuts at an arbitrary byte and
            can split a multibyte character in half — the browser drops the whole
            connection over that (1007 per the RFC; Chrome just aborts it), and
            the terminal dies mid-session. xterm.js decodes a Uint8Array
            incrementally, so it stitches those halves back together."""
            with wlock:
                handler.wfile.write(_ws_frame(payload, opcode))
                handler.wfile.flush()

        try:
            c = get_conn(mid)
            chan = c["client"].invoke_shell(term="xterm-256color", width=cols, height=rows)
        except Exception as e:
            try:
                send(f"\r\n\x1b[31m{e}\x1b[0m\r\n".encode())
                send(b"", 0x8)
            except OSError:
                pass
            return
        handler.connection.settimeout(None)      # a terminal may idle indefinitely
        with open_terms_lock:
            open_terms[0] += 1
        last_hit[0] = time.time()
        alive = [True]

        def pump():
            while alive[0]:
                try:
                    data = chan.recv(65536)
                except Exception:
                    data = b""
                if not data:
                    break
                last_hit[0] = time.time()
                try:
                    send(data)
                except OSError:
                    break
            alive[0] = False
            try:
                send(b"", 0x8)
            except OSError:
                pass
            try:
                handler.connection.shutdown(socket.SHUT_RD)   # unblock the reader
            except OSError:
                pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while alive[0]:
                opcode, payload = _ws_read(handler.rfile)
                if opcode is None or opcode == 0x8:
                    break
                last_hit[0] = time.time()
                if opcode == 0x9:
                    send(payload, 0xA)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                msg = json.loads(payload.decode("utf-8", "replace"))
                if "data" in msg:
                    # sendall, not send: send() writes what fits the remote's
                    # window and reports how much that was, so a paste larger
                    # than the window would arrive at the shell with the tail
                    # silently missing.
                    chan.sendall(msg["data"].encode("utf-8"))
                elif "resize" in msg:
                    chan.resize_pty(width=int(msg["resize"][0]), height=int(msg["resize"][1]))
        except (OSError, ValueError):
            pass
        finally:
            alive[0] = False
            with open_terms_lock:
                open_terms[0] -= 1
            try:
                chan.close()
            except Exception:
                pass

    # ---- http ----
    def one(q, key, default=""):
        return q.get(key, [default])[0]

    def content_disposition(name):
        """RFC 6266 attachment header for a remote filename.

        http.server encodes header values as latin-1, so a name with any
        character outside it raised there and killed the response before a byte
        of the file was sent — and a name is free to contain a quote or a CRLF,
        which went straight into the header. The plain filename is the ASCII
        fallback; filename* carries the real one."""
        ascii_name = "".join(ch for ch in name
                             if ch.isascii() and ch.isprintable() and ch not in '"\\')
        return (f'attachment; filename="{ascii_name or "download"}"; '
                f"filename*=UTF-8''{quote(name, safe='')}")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"      # keep-alive: no TCP setup per listing
        timeout = 300

        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _guard(self, u, q):
            if u.path == "/ping":
                return True
            if one(q, "t") != TOKEN:
                self._send(403, {"detail": "forbidden"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _dispatch(self, fn):
            try:
                self._send(200, fn())
            except Http as e:
                self._send(e.code, {"detail": e.detail})
            except Exception as e:
                self._send(500, {"detail": str(e) or type(e).__name__})

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            if u.path == "/term":
                run_term(self, one(q, "id"), int(one(q, "cols", "120")),
                         int(one(q, "rows", "32")))
                return
            if u.path == "/download":
                self._download(one(q, "id"), one(q, "path"))
                return
            if u.path == "/ping":
                self._send(200, {"ok": True, "version": VERSION})
            elif u.path == "/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=srv.shutdown, daemon=True).start()
            elif u.path == "/machines":
                self._dispatch(do_machines)
            elif u.path == "/known":
                self._dispatch(lambda: do_known(int(one(q, "limit", "25"))))
            elif u.path == "/ls":
                self._dispatch(lambda: do_ls(one(q, "id"), one(q, "path")))
            elif u.path == "/search":
                self._dispatch(lambda: do_search(one(q, "id"), one(q, "path"),
                                                 one(q, "q"), int(one(q, "limit", "400"))))
            elif u.path == "/localize":
                self._dispatch(lambda: do_localize(one(q, "id"), one(q, "path")))
            else:
                self._send(404, {"detail": "not found"})

        def do_POST(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._guard(u, q):
                return
            if u.path == "/upload":
                n = int(self.headers.get("Content-Length") or 0)
                limited = _Limited(self.rfile, n)
                try:
                    self._dispatch(lambda: do_upload(one(q, "id"), one(q, "dir"),
                                                     one(q, "name"), limited, n))
                finally:
                    # do_upload can fail before reading the whole body — a bad
                    # machine id, a full disk, the SFTP channel dropping mid
                    # upload. What's left of this request is still sitting on a
                    # keep-alive socket in front of the next one; left there, it
                    # gets parsed as the start of that next request.
                    while limited.left > 0 and limited.read(65536):
                        pass
                return
            parts = [p for p in u.path.split("/") if p]
            if parts[:1] == ["machines"] and len(parts) == 3:
                # urlparse doesn't decode, and the page percent-encodes the id
                # into the path — so "cfg:host" arrives as "cfg%3Ahost" and every
                # cfg_alias() check would reject it
                mid, verb = unquote(parts[1]), parts[2]
                if verb == "update":
                    self._dispatch(lambda: do_update(mid, self._body()))
                elif verb == "remove":
                    self._dispatch(lambda: do_remove(mid))
                elif verb == "hide":
                    self._dispatch(lambda: do_hide(mid))
                elif verb == "unhide":
                    self._dispatch(lambda: do_unhide(mid))
                else:
                    self._send(404, {"detail": "not found"})
                return
            if u.path == "/machines/add":
                self._dispatch(lambda: do_add(self._body()))
            elif u.path == "/connect":
                self._dispatch(lambda: do_connect(one(q, "id")))
            elif u.path == "/probe":
                self._dispatch(lambda: do_probe(one(q, "id")))
            elif u.path == "/reach":
                self._dispatch(lambda: do_reach(one(q, "id")))
            elif u.path == "/disconnect":
                self._dispatch(lambda: (drop_conn(one(q, "id")), {"ok": True})[1])
            elif u.path == "/mkdir":
                self._dispatch(lambda: do_mkdir(one(q, "id"), one(q, "path")))
            elif u.path == "/rename":
                self._dispatch(lambda: do_rename(one(q, "id"), one(q, "src"), one(q, "dst")))
            elif u.path == "/copy":
                self._dispatch(lambda: do_copy(one(q, "id"), one(q, "src"), one(q, "dst")))
            elif u.path == "/delete":
                self._dispatch(lambda: do_delete(one(q, "id"), one(q, "path")))
            else:
                self._send(404, {"detail": "not found"})

        def _download(self, mid, path):
            try:
                c = get_conn(mid)
            except Http as e:
                self._send(e.code, {"detail": e.detail})
                return
            except Exception as e:
                self._send(500, {"detail": str(e) or type(e).__name__})
                return
            # One SFTP channel can't carry two transfers, and prefetch() keeps
            # reading ahead on it in the background — so tlock covers the open
            # AND the streaming, never just the open. Listings are unaffected:
            # they run on the other channel, under c["lock"].
            with Busy(), c["tlock"]:
                f = None
                try:
                    t = tsftp(c)
                    size = t.stat(path).st_size or 0
                    f = t.open(path, "rb")
                    f.prefetch()
                except Http as e:
                    if f:
                        f.close()   # open() made it through; only prefetch() failed
                    self._send(e.code, {"detail": e.detail})
                    return
                except Exception as e:
                    if f:
                        f.close()
                    self._send(500, {"detail": str(e) or type(e).__name__})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 content_disposition(posixpath.basename(path)))
                self._cors()
                self.end_headers()
                try:
                    with f:
                        while True:
                            chunk = f.read(256 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    port = srv.server_address[1]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    _lock_down(os.path.dirname(STATE), is_dir=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": TOKEN, "version": VERSION,
                   "pid": os.getpid()}, f)
    _lock_down(STATE)

    def idle_watch():
        while True:
            time.sleep(60)
            if open_terms[0] == 0 and long_ops[0] == 0 and \
                    time.time() - last_hit[0] > IDLE_EXIT_S:
                srv.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True).start()
    print(f"vps stdlib daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


class _Limited:
    """Exactly `length` bytes off a socket stream — paramiko's putfo would otherwise
    read past the request body into the next keep-alive request."""

    def __init__(self, stream, length):
        self.stream = stream
        self.left = length

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        want = self.left if n is None or n < 0 else min(n, self.left)
        data = self.stream.read(want)
        self.left -= len(data)
        return data


if __name__ == "__main__":
    if "--serve" in sys.argv:
        _serve()
    else:
        print(json.dumps(main(), indent=2))
