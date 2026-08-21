"""Thorough test of vps_manager do_copy / do_rename against a real filesystem.

We extract the *shipped* helpers (_exists, _reject_if_exists, _claim, _rm_quiet)
and do_copy / do_rename verbatim from vps.py, then run them with faithful
stand-ins:

  * sh(c, cmd)   -> runs the real command through bash, so cp -a / mv /
                    rm -rf are genuine coreutils and a nonzero exit raises
                    Http(400) like the daemon's sh().
  * sftp.lstat   -> os.lstat (does NOT follow symlinks — a broken link is
                    seen as a name in use, matching the fix).
  * sftp.rename  -> OpenSSH SFTP semantics: refuse an existing destination.

Covers happy paths plus the reviewed findings: overwrite, false-success race,
orphaned temp, dangling-symlink dst, rename parity, the BusyBox guard (copy
must never shell out to mv), and rename's cross-device shell-move fallback.
"""
import contextlib
import glob
import os
import posixpath
import shlex
import subprocess
import tempfile
import textwrap
import threading

_default = os.path.join(os.path.dirname(__file__), "..", "vps.py")
VPS = os.path.normpath(os.environ.get("VPS_PATH", _default))
WANT = ("_exists", "_reject_if_exists", "_claim", "_rm_quiet", "do_rename", "do_copy")


def extract(path, names):
    lines = open(path, encoding="utf-8").read().splitlines()
    out = []
    for name in names:
        start = next(i for i, l in enumerate(lines)
                     if l.strip().startswith(f"def {name}("))
        indent = len(lines[start]) - len(lines[start].lstrip())
        body = [lines[start]]
        for l in lines[start + 1:]:
            if l.strip() and (len(l) - len(l.lstrip())) <= indent and l.lstrip().startswith("def "):
                break
            body.append(l)
        while body and not body[-1].strip():
            body.pop()
        out.append(textwrap.dedent("\n".join(body)))
    return "\n\n".join(out)


SRC = extract(VPS, WANT)


class Http(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Busy:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def real_sh(c, cmd, timeout=60):
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    if p.returncode != 0:
        raise Http(400, p.stderr.strip() or f"command failed (exit {p.returncode})")
    return p.stdout


class FakeSFTP:
    """OpenSSH SFTP: rename refuses an existing destination; lstat does not
    follow symlinks."""

    def lstat(self, path):
        return os.lstat(path)

    def rename(self, a, b):
        if os.path.lexists(b):
            raise OSError(f"{b} exists")
        os.rename(a, b)


def make_conn():
    return {"lock": threading.Lock(), "sftp": FakeSFTP()}


NS = {"os": os, "posixpath": posixpath, "shlex": shlex, "contextlib": contextlib,
      "Http": Http, "Busy": Busy}
exec(SRC, NS)
do_copy, do_rename = NS["do_copy"], NS["do_rename"]


def wire(conn, sh=real_sh, sftp_of=None):
    NS["get_conn"] = lambda mid: conn
    NS["sh"] = sh
    NS["sftp_of"] = sftp_of or (lambda c: c["sftp"])


def temps(d):
    return glob.glob(os.path.join(d, "*.fused-copy-*"))


def fresh():
    return tempfile.mkdtemp(prefix="vpscopy_").replace("\\", "/")


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


# --- S1: copy a file ---------------------------------------------------------
def s1():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("hello")
    wire(make_conn())
    r = do_copy("m", src, dst)
    check("S1 returns ok", r == {"ok": True})
    check("S1 dst written", os.path.isfile(dst) and open(dst).read() == "hello")
    check("S1 src preserved", os.path.isfile(src))
    check("S1 no temp left", temps(d) == [], f"leftover {temps(d)}")


# --- S2: copy a directory tree ----------------------------------------------
def s2():
    d = fresh()
    src, dst = f"{d}/srcdir", f"{d}/dstdir"
    os.makedirs(f"{src}/sub")
    open(f"{src}/f1", "w", encoding="utf-8").write("1")
    open(f"{src}/sub/f2", "w", encoding="utf-8").write("2")
    wire(make_conn())
    r = do_copy("m", src, dst)
    check("S2 returns ok", r == {"ok": True})
    check("S2 tree copied", os.path.isfile(f"{dst}/f1") and os.path.isfile(f"{dst}/sub/f2"))
    check("S2 no temp left", temps(d) == [], f"leftover {temps(d)}")


# --- S3: dst already exists (file) -> 409, no clobber ------------------------
def s3():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("new")
    open(dst, "w", encoding="utf-8").write("KEEP")
    wire(make_conn())
    try:
        do_copy("m", src, dst)
        check("S3 raised 409", False, "no exception")
    except Http as e:
        check("S3 raised 409", e.code == 409, f"code {e.code}")
    check("S3 dst untouched", open(dst).read() == "KEEP")
    check("S3 no temp left", temps(d) == [], f"leftover {temps(d)}")


# --- S4: dst is an existing directory -> 409, no nesting ---------------------
def s4():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/existing_dir"
    open(src, "w", encoding="utf-8").write("x")
    os.makedirs(dst)
    wire(make_conn())
    try:
        do_copy("m", src, dst)
        check("S4 raised 409", False, "no exception")
    except Http as e:
        check("S4 raised 409", e.code == 409, f"code {e.code}")
    check("S4 not nested into dir", not os.path.exists(f"{dst}/a.txt"))
    check("S4 no temp left", temps(d) == [], f"leftover {temps(d)}")


# --- S5: dst appears in the race window (now claimed by sftp.rename) ---------
def s5():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("mine")

    class RacySFTP(FakeSFTP):
        # dst is free at pre-check; a competitor creates it just before our
        # sftp.rename runs. The no-clobber refusal must then kick in.
        def rename(self, a, b):
            if b == dst and not os.path.lexists(dst):
                open(dst, "w", encoding="utf-8").write("SOMEONE ELSE")
            return super().rename(a, b)

    conn = {"lock": threading.Lock(), "sftp": RacySFTP()}
    wire(conn)
    try:
        do_copy("m", src, dst)
        check("S5 raised 409", False, "reported success on a skipped copy")
    except Http as e:
        check("S5 raised 409", e.code == 409, f"code {e.code}")
    check("S5 racing dst intact", os.path.isfile(dst) and open(dst).read() == "SOMEONE ELSE")
    check("S5 temp cleaned up", temps(d) == [], f"orphan {temps(d)}")


# --- S6: cp fails partway (orphaned-temp finding) ----------------------------
def s6():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("data")
    conn = make_conn()

    def faulty_sh(c, cmd, timeout=60):
        if cmd.lstrip().startswith("cp "):
            tmp = shlex.split(cmd)[-1]
            open(tmp, "w", encoding="utf-8").write("PARTIAL")   # partial temp, then die
            raise Http(500, "disk full")
        return real_sh(c, cmd, timeout)        # real rm -rf for cleanup

    wire(conn, sh=faulty_sh)
    try:
        do_copy("m", src, dst)
        check("S6 propagated error", False, "swallowed cp failure")
    except Http as e:
        check("S6 propagated error", e.code == 500, f"code {e.code}")
    check("S6 no dst created", not os.path.exists(dst))
    check("S6 partial temp cleaned", temps(d) == [], f"orphan {temps(d)}")


# --- S7: dangling-symlink dst is a name in use -> 409 ------------------------
def s7():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/link"
    open(src, "w", encoding="utf-8").write("data")
    try:
        os.symlink(f"{d}/missing_target", dst)  # dangling
    except (OSError, NotImplementedError, AttributeError) as e:
        check("S7 dangling-symlink dst -> 409", True, f"symlink unsupported, skipped: {e}")
        check("S7 no temp left", True)
        return
    wire(make_conn())
    try:
        do_copy("m", src, dst)
        check("S7 dangling-symlink dst -> 409", False, "treated broken link as free")
    except Http as e:
        check("S7 dangling-symlink dst -> 409", e.code == 409, f"code {e.code}")
    check("S7 no temp left", temps(d) == [], f"orphan {temps(d)}")


# --- S8: rename parity (same helpers) ----------------------------------------
def s8():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("mv me")
    wire(make_conn())
    r = do_rename("m", src, dst)
    check("S8 rename ok", r == {"ok": True} and os.path.isfile(dst) and not os.path.exists(src))
    # rename onto an existing dst -> 409
    d2 = fresh()
    s2p, d2p = f"{d2}/a.txt", f"{d2}/b.txt"
    open(s2p, "w", encoding="utf-8").write("x")
    open(d2p, "w", encoding="utf-8").write("KEEP")
    wire(make_conn())
    try:
        do_rename("m", s2p, d2p)
        check("S8 rename existing -> 409", False, "no exception")
    except Http as e:
        check("S8 rename existing -> 409", e.code == 409, f"code {e.code}")
    check("S8 rename dst untouched", open(d2p).read() == "KEEP")


# --- S9: BUSYBOX GUARD — copy must never shell out to mv ----------------------
def s9():
    def no_mv_sh(c, cmd, timeout=60):
        assert not cmd.lstrip().startswith("mv "), f"copy shelled out to mv: {cmd}"
        return real_sh(c, cmd, timeout)

    # normal file copy: no mv anywhere
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("no busybox worries")
    wire(make_conn(), sh=no_mv_sh)
    r = do_copy("m", src, dst)
    check("S9 copy ok without mv", r == {"ok": True} and open(dst).read() == "no busybox worries")
    # copy onto an existing dst: still no mv, clean 409
    d2 = fresh()
    s2p, d2p = f"{d2}/a.txt", f"{d2}/b.txt"
    open(s2p, "w", encoding="utf-8").write("x")
    open(d2p, "w", encoding="utf-8").write("KEEP")
    wire(make_conn(), sh=no_mv_sh)
    try:
        do_copy("m", s2p, d2p)
        check("S9 409 without mv", False, "no exception")
    except Http as e:
        check("S9 409 without mv", e.code == 409, f"code {e.code}")
    check("S9 dst untouched", open(d2p).read() == "KEEP")
    check("S9 no temp left", temps(d) == [] and temps(d2) == [],
          f"leftover {temps(d) + temps(d2)}")


# --- S10: rename cross-device -> shell mv fallback ---------------------------
def s10():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("exdev")

    class ExdevSFTP(FakeSFTP):
        # a server whose rename always fails (e.g. src and dst straddle
        # filesystems) — do_rename must fall back to the real shell move
        def rename(self, a, b):
            raise OSError("EXDEV: cross-device link")

    conn = {"lock": threading.Lock(), "sftp": ExdevSFTP()}
    wire(conn)
    r = do_rename("m", src, dst)
    check("S10 fallback rename ok", r == {"ok": True})
    check("S10 src moved to dst",
          os.path.isfile(dst) and open(dst).read() == "exdev" and not os.path.exists(src))


# --- S11: a DIRECTORY races into dst during the claim -> 409, no nesting ------
def s11():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b"
    open(src, "w", encoding="utf-8").write("mine")

    class RacyDirSFTP(FakeSFTP):
        # dst is free at pre-check; a competitor creates it as a DIRECTORY just
        # before the claim. sftp.rename must refuse (no overwrite, no nesting) —
        # the exact case a shell `mv` would silently nest the temp into.
        def rename(self, a, b):
            if b == dst and not os.path.lexists(dst):
                os.makedirs(dst)
            return super().rename(a, b)

    conn = {"lock": threading.Lock(), "sftp": RacyDirSFTP()}
    wire(conn)
    try:
        do_copy("m", src, dst)
        check("S11 racing dir -> 409", False, "nested into or clobbered the dir")
    except Http as e:
        check("S11 racing dir -> 409", e.code == 409, f"code {e.code}")
    check("S11 not nested into racing dir",
          os.path.isdir(dst) and os.listdir(dst) == [], f"contents {os.listdir(dst)}")
    check("S11 temp cleaned up", temps(d) == [], f"orphan {temps(d)}")


# --- S12: a non-conflict rename failure (dst free) must NOT become a 409 -----
def s12():
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b.txt"
    open(src, "w", encoding="utf-8").write("data")

    class DeniedSFTP(FakeSFTP):
        # rename fails though dst does not exist (e.g. permission denied / quota
        # / dropped channel). Must surface as the real error, not "already
        # exists".
        def rename(self, a, b):
            raise OSError(13, "Permission denied")

    conn = {"lock": threading.Lock(), "sftp": DeniedSFTP()}
    wire(conn)
    try:
        do_copy("m", src, dst)
        check("S12 non-conflict error surfaced", False, "no exception")
    except Http as e:
        check("S12 non-conflict error surfaced", False, f"mislabeled as Http {e.code}")
    except OSError as e:
        check("S12 non-conflict error surfaced", "Permission denied" in str(e), str(e))
    check("S12 temp cleaned up", temps(d) == [], f"orphan {temps(d)}")


# --- S13: do_rename, dst races in while the shell fallback would run ---------
def s13():
    # dst is free at the pre-check, then a directory races in AND sftp.rename
    # fails (as a cross-device move would). _claim must classify this as the
    # conflict it is (dst now present -> 409) and NOT shell out to mv, which
    # would nest src inside the directory and report success.
    d = fresh()
    src, dst = f"{d}/a.txt", f"{d}/b"
    open(src, "w", encoding="utf-8").write("mine")

    class RacyDirExdevSFTP(FakeSFTP):
        def rename(self, a, b):
            if b == dst and not os.path.lexists(dst):
                os.makedirs(dst)
            raise OSError("EXDEV: cross-device link")

    conn = {"lock": threading.Lock(), "sftp": RacyDirExdevSFTP()}
    wire(conn)
    try:
        do_rename("m", src, dst)
        check("S13 rename racing dir -> 409", False, "nested into or clobbered the dir")
    except Http as e:
        check("S13 rename racing dir -> 409", e.code == 409, f"code {e.code}")
    check("S13 src not nested into racing dir",
          os.path.isfile(src) and os.path.isdir(dst) and os.listdir(dst) == [],
          f"src={os.path.isfile(src)} contents={os.listdir(dst) if os.path.isdir(dst) else 'n/a'}")


for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13):
    fn()

print("=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("ALL GREEN")
