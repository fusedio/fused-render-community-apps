# /// script
# dependencies = ["py360convert>=1.0.4"]
# ///
"""Backend for Ranov-UR (index.html): panoramic image library with
validation, normalization (any Pillow-readable format -> browser-displayable
JPEG/PNG), and on-the-fly projection conversions (py360convert e2c/e2p/c2e
plus custom little-planet / fisheye resamplers). Assets and an event log
persist in pano.db so the library is restored across sessions.

Layout on disk (all next to this file):
  library/            original imported bytes, verbatim
  display/<id>/       display.jpg|png (capped 8192px), thumb.jpg, derived/<hash>.jpg
  .cache/uploads/     chunk staging for browser uploads
"""

import base64
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import os, sys
    __file__ = os.path.join(sys.path[0], "pano.py")

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY = os.path.join(HERE, "library")
DISPLAY = os.path.join(HERE, "display")
UPLOADS = os.path.join(HERE, ".cache", "uploads")
DB = os.path.join(HERE, "pano.db")

DISPLAY_MAX_W = 8192   # keep under common WebGL texture limits
CONVERT_MAX_W = 4096   # resample source cap so conversions stay interactive
THUMB_W = 320
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp",
              ".gif", ".avif", ".jxl"}


# ---------------------------------------------------------------- db / log

def _db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        """CREATE TABLE IF NOT EXISTS assets (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,
             name TEXT NOT NULL,
             orig_name TEXT NOT NULL,
             orig_path TEXT NOT NULL,
             orig_format TEXT NOT NULL,
             orig_bytes INTEGER NOT NULL,
             width INTEGER NOT NULL,
             height INTEGER NOT NULL,
             kind TEXT NOT NULL,
             valid INTEGER NOT NULL,
             reasons TEXT NOT NULL DEFAULT '[]',
             display_path TEXT NOT NULL,
             thumb_path TEXT NOT NULL,
             display_w INTEGER NOT NULL,
             display_h INTEGER NOT NULL,
             source_id INTEGER NOT NULL DEFAULT 0
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,
             kind TEXT NOT NULL,
             detail TEXT NOT NULL DEFAULT ''
           )"""
    )
    return con


def _log(kind, detail=""):
    con = _db()
    with con:
        con.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), str(kind),
             detail if isinstance(detail, str) else json.dumps(detail)),
        )
    con.close()


def _row_to_asset(r):
    keys = ["id", "ts", "name", "orig_name", "orig_path", "orig_format",
            "orig_bytes", "width", "height", "kind", "valid", "reasons",
            "display_path", "thumb_path", "display_w", "display_h", "source_id"]
    a = dict(zip(keys, r))
    a["reasons"] = json.loads(a["reasons"])
    a["valid"] = bool(a["valid"])
    return a


def _get_asset(asset_id):
    con = _db()
    r = con.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    con.close()
    if not r:
        raise ValueError(f"no asset with id {asset_id}")
    return _row_to_asset(r)


# ---------------------------------------------------------------- validation

def _dice_corners_blank(img):
    """A cube-cross layout has uniform (blank) corner blocks; a regular 4:3
    photo almost never does. Checks the top-left and bottom-right face cells."""
    import numpy as np

    g = np.asarray(img.convert("L"))
    ch, cw = g.shape[0] // 3, g.shape[1] // 4
    qh, qw = ch // 4, cw // 4  # central half of each corner cell
    cells = []
    for row in (0, 2):
        for col in (0, 2, 3):
            cell = g[row * ch:(row + 1) * ch, col * cw:(col + 1) * cw]
            cells.append(cell[qh:-qh or None, qw:-qw or None])
    return all(float(c.std()) < 6.0 for c in cells)


def _classify(w, h, raw_head, img):
    """Decide what kind of panorama the pixel dimensions suggest.

    Returns (kind, valid, reasons). kind is one of:
    equirect, equirect_180, cube_dice, cube_horizon, flat.
    """
    reasons = []
    ratio = w / h
    has_gpano = b"GPano" in raw_head or b"equirectangular" in raw_head
    if has_gpano:
        reasons.append("GPano/photo-sphere XMP metadata found")

    def close(a, b, tol=0.02):
        return abs(a - b) <= b * tol

    if close(ratio, 2.0):
        reasons.append(f"aspect ratio {ratio:.3f} ≈ 2:1 (full equirectangular)")
        return "equirect", True, reasons
    if close(ratio, 6.0):
        reasons.append("aspect ratio 6:1 (horizontal cube strip)")
        return "cube_horizon", True, reasons
    if close(ratio, 4.0 / 3.0):
        if _dice_corners_blank(img):
            reasons.append("aspect ratio 4:3 with blank corners (cube cross / dice layout)")
            return "cube_dice", True, reasons
        reasons.append("aspect ratio 4:3 but corners contain image data — regular photo, not a cube cross")
        return "flat", False, reasons
    if close(ratio, 1.0):
        if has_gpano:
            reasons.append("1:1 with photo-sphere metadata (VR180 half pano)")
            return "equirect_180", True, reasons
        reasons.append("aspect ratio 1:1 — could be VR180, treating as half pano")
        return "equirect_180", False, reasons
    if has_gpano:
        reasons.append(f"unusual aspect {ratio:.3f} but metadata says panorama (cropped?)")
        return "equirect", True, reasons
    reasons.append(f"aspect ratio {ratio:.3f} matches no panoramic layout")
    return "flat", False, reasons


# ---------------------------------------------------------------- import

def _safe_name(name):
    name = os.path.basename(str(name)).strip()
    name = re.sub(r"[^\w.\- ()]+", "_", name)
    if not name or name.startswith("."):
        raise ValueError(f"invalid file name {name!r}")
    return name


def _import_bytes(raw, orig_name):
    import io

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 400_000_000
    orig_name = _safe_name(orig_name)
    img = Image.open(io.BytesIO(raw))
    fmt = (img.format or "?").upper()
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    kind, valid, reasons = _classify(w, h, raw[: 256 * 1024], img)

    con = _db()
    with con:
        cur = con.execute(
            "INSERT INTO assets (ts,name,orig_name,orig_path,orig_format,orig_bytes,"
            "width,height,kind,valid,reasons,display_path,thumb_path,display_w,display_h)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), orig_name, orig_name, "", fmt, len(raw), w, h,
             kind, int(valid), json.dumps(reasons), "", "", 0, 0),
        )
        asset_id = cur.lastrowid

    os.makedirs(LIBRARY, exist_ok=True)
    ext = os.path.splitext(orig_name)[1] or "." + fmt.lower()
    orig_rel = f"library/{asset_id}_{os.path.splitext(orig_name)[0]}{ext}"
    with open(os.path.join(HERE, orig_rel), "wb") as f:
        f.write(raw)

    ddir = os.path.join(DISPLAY, str(asset_id))
    os.makedirs(ddir, exist_ok=True)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    disp = img.convert("RGBA" if has_alpha else "RGB")
    if disp.width > DISPLAY_MAX_W:
        disp = disp.resize(
            (DISPLAY_MAX_W, max(1, round(disp.height * DISPLAY_MAX_W / disp.width))),
            Image.LANCZOS,
        )
    if has_alpha:
        disp_rel = f"display/{asset_id}/display.png"
        disp.save(os.path.join(HERE, disp_rel))
    else:
        disp_rel = f"display/{asset_id}/display.jpg"
        disp.save(os.path.join(HERE, disp_rel), quality=92)

    thumb = disp.convert("RGB")
    thumb = thumb.resize(
        (THUMB_W, max(1, round(thumb.height * THUMB_W / thumb.width))), Image.LANCZOS
    )
    thumb_rel = f"display/{asset_id}/thumb.jpg"
    thumb.save(os.path.join(HERE, thumb_rel), quality=85)

    with con:
        con.execute(
            "UPDATE assets SET orig_path=?, display_path=?, thumb_path=?,"
            " display_w=?, display_h=? WHERE id=?",
            (orig_rel, disp_rel, thumb_rel, disp.width, disp.height, asset_id),
        )
    con.close()
    _log("import", {"id": asset_id, "name": orig_name, "format": fmt,
                    "size": f"{w}x{h}", "kind": kind, "valid": valid})
    return _get_asset(asset_id)


def op_upload_begin():
    os.makedirs(UPLOADS, exist_ok=True)
    token = base64.urlsafe_b64encode(os.urandom(9)).decode()
    os.makedirs(os.path.join(UPLOADS, token), exist_ok=True)
    return {"token": token}


def op_upload_chunk(token, seq, data_b64):
    d = os.path.join(UPLOADS, _safe_name(token))
    if not os.path.isdir(d):
        raise ValueError("unknown upload token")
    with open(os.path.join(d, f"{int(seq):06d}.part"), "wb") as f:
        f.write(base64.b64decode(data_b64))
    return {"ok": True}


def op_upload_end(token, name):
    d = os.path.join(UPLOADS, _safe_name(token))
    if not os.path.isdir(d):
        raise ValueError("unknown upload token")
    raw = b""
    for part in sorted(os.listdir(d)):
        with open(os.path.join(d, part), "rb") as f:
            raw += f.read()
    shutil.rmtree(d, ignore_errors=True)
    if not raw:
        raise ValueError("upload was empty")
    return {"asset": _import_bytes(raw, name)}


def op_import_path(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ValueError(f"not a file: {path}")
    with open(path, "rb") as f:
        raw = f.read()
    return {"asset": _import_bytes(raw, os.path.basename(path))}


def op_import_url(url):
    import urllib.request
    from urllib.parse import unquote, urlsplit

    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("URL must start with http:// or https://")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Pano Viewer)"})
    cap = 128 * 1024 * 1024
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("download larger than 128 MB")
    name = os.path.basename(unquote(urlsplit(url).path)) or "download"
    return {"asset": _import_bytes(raw, name)}


def op_browse(path):
    """List a directory for the import file-explorer: subdirs + image files."""
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise ValueError(f"not a directory: {path}")
    dirs, files = [], []
    try:
        names = os.listdir(path)
    except PermissionError:
        raise ValueError(f"permission denied: {path}")
    for n in names:
        if n.startswith("."):
            continue
        full = os.path.join(path, n)
        try:
            if os.path.isdir(full):
                dirs.append({"name": n})
            elif os.path.splitext(n)[1].lower() in IMAGE_EXTS:
                st = os.stat(full)
                files.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return {"path": path,
            "parent": os.path.dirname(path) if path != os.path.sep else "",
            "dirs": dirs, "files": files}


def op_delete(asset_id):
    a = _get_asset(asset_id)
    con = _db()
    with con:
        con.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    con.close()
    if a["orig_path"]:
        try:
            os.remove(os.path.join(HERE, a["orig_path"]))
        except OSError:
            pass
    shutil.rmtree(os.path.join(DISPLAY, str(asset_id)), ignore_errors=True)
    _log("delete", {"id": asset_id, "name": a["name"]})
    return {"ok": True}


def op_list():
    con = _db()
    rows = con.execute("SELECT * FROM assets ORDER BY id").fetchall()
    con.close()
    return {"assets": [_row_to_asset(r) for r in rows]}


# ---------------------------------------------------------------- conversion

def _load_equirect(asset):
    """Load the asset as an equirectangular numpy array (converting cube
    layouts via c2e), capped at CONVERT_MAX_W for interactive speed."""
    import numpy as np
    import py360convert
    from PIL import Image

    img = Image.open(os.path.join(HERE, asset["display_path"])).convert("RGB")
    if img.width > CONVERT_MAX_W:
        img = img.resize(
            (CONVERT_MAX_W, max(1, round(img.height * CONVERT_MAX_W / img.width))),
            Image.LANCZOS,
        )
    arr = np.asarray(img)
    if asset["kind"] == "cube_dice":
        face = arr.shape[1] // 4
        arr = py360convert.c2e(arr, face * 2, face * 4, cube_format="dice")
    elif asset["kind"] == "cube_horizon":
        face = arr.shape[1] // 6
        arr = py360convert.c2e(arr, face * 2, face * 4, cube_format="horizon")
    return arr.astype("uint8")


def _sample_equirect(equi, lon, lat):
    """Bilinear-sample equirect image at lon/lat arrays (radians)."""
    import numpy as np

    h, w, _ = equi.shape
    x = (lon / (2 * math.pi) + 0.5) * w - 0.5
    y = (0.5 - lat / math.pi) * h - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    x0w, x1w = x0 % w, (x0 + 1) % w
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    top = equi[y0c, x0w] * (1 - fx) + equi[y0c, x1w] * fx
    bot = equi[y1c, x0w] * (1 - fx) + equi[y1c, x1w] * fx
    return (top * (1 - fy) + bot * fy).astype("uint8")


def _little_planet(equi, size, zoom, roll):
    """Stereographic 'little planet': sphere projected from the zenith."""
    import numpy as np

    s = np.linspace(-1, 1, size)
    x, y = np.meshgrid(s, -s)
    r = np.sqrt(x * x + y * y) + 1e-9
    theta = np.arctan2(y, x) + math.radians(roll)
    lat = math.pi / 2 - 2 * np.arctan(r / max(zoom, 0.05))
    lon = (theta + math.pi) % (2 * math.pi) - math.pi
    return _sample_equirect(equi, lon, lat)


def _fisheye180(equi, size, yaw, pitch):
    """Equidistant 180-degree fisheye looking at (yaw, pitch)."""
    import numpy as np

    s = np.linspace(-1, 1, size)
    x, y = np.meshgrid(s, -s)
    r = np.sqrt(x * x + y * y)
    theta = r * (math.pi / 2)          # angle from view axis, max 90 deg
    phi = np.arctan2(y, x)
    # direction in camera space (z forward)
    dx = np.sin(theta) * np.cos(phi)
    dy = np.sin(theta) * np.sin(phi)
    dz = np.cos(theta)
    # rotate by pitch (about x) then yaw (about y)
    p, yw = math.radians(pitch), math.radians(yaw)
    dy2 = dy * math.cos(p) + dz * math.sin(p)
    dz2 = -dy * math.sin(p) + dz * math.cos(p)
    dx2 = dx * math.cos(yw) + dz2 * math.sin(yw)
    dz3 = -dx * math.sin(yw) + dz2 * math.cos(yw)
    lon = np.arctan2(dx2, dz3)
    lat = np.arcsin(np.clip(dy2, -1, 1))
    out = _sample_equirect(equi, lon, lat)
    out[r > 1] = 16                    # outside the image circle
    return out


def op_convert(asset_id, mode, fov, yaw, pitch, roll, zoom, out_w, out_h, face_w):
    import numpy as np
    import py360convert
    from PIL import Image

    t0 = time.time()
    a = _get_asset(asset_id)
    key = f"{asset_id}:{mode}:{fov}:{yaw}:{pitch}:{roll}:{zoom}:{out_w}:{out_h}:{face_w}"
    hid = hashlib.sha1(key.encode()).hexdigest()[:16]
    ddir = os.path.join(DISPLAY, str(asset_id), "derived")
    os.makedirs(ddir, exist_ok=True)

    def finish(rel_or_faces, w, h, cached):
        res = {"w": w, "h": h, "cached": cached,
               "ms": round((time.time() - t0) * 1000)}
        if isinstance(rel_or_faces, list):
            res["faces"] = rel_or_faces
        else:
            res["path"] = rel_or_faces
        if not cached:
            _log("convert", {"id": asset_id, "mode": mode, "key": key,
                             "ms": res["ms"]})
        return res

    if mode == "cube_faces":
        names = ["F", "R", "B", "L", "U", "D"]
        rels = [f"display/{asset_id}/derived/{hid}_{n}.jpg" for n in names]
        if all(os.path.isfile(os.path.join(HERE, r)) for r in rels):
            fw = face_w or 1024
            return finish([{"face": n, "path": r} for n, r in zip(names, rels)],
                          fw, fw, True)
        equi = _load_equirect(a)
        fw = face_w or min(1024, equi.shape[1] // 4)
        faces = py360convert.e2c(equi, face_w=fw, cube_format="dict")
        for n, r in zip(names, rels):
            Image.fromarray(faces[n]).save(os.path.join(HERE, r), quality=90)
        return finish([{"face": n, "path": r} for n, r in zip(names, rels)],
                      fw, fw, False)

    rel = f"display/{asset_id}/derived/{hid}.jpg"
    full = os.path.join(HERE, rel)
    if os.path.isfile(full):
        with Image.open(full) as im:
            return finish(rel, im.width, im.height, True)

    equi = _load_equirect(a)
    if mode in ("cube_dice", "cube_horizon"):
        fw = face_w or min(1024, equi.shape[1] // 4)
        out = py360convert.e2c(equi, face_w=fw, cube_format=mode.split("_")[1])
    elif mode == "perspective":
        ow, oh = out_w or 1280, out_h or 720
        h_fov = max(1.0, min(fov or 90.0, 175.0))
        v_fov = math.degrees(
            2 * math.atan(math.tan(math.radians(h_fov) / 2) * oh / ow))
        out = py360convert.e2p(equi, (h_fov, v_fov), yaw, pitch, (oh, ow),
                               in_rot_deg=roll)
    elif mode == "little_planet":
        out = _little_planet(equi, out_w or 1024, zoom or 1.0, roll)
    elif mode == "fisheye180":
        out = _fisheye180(equi, out_w or 1024, yaw, pitch)
    elif mode == "equirect":
        out = equi
    else:
        raise ValueError(f"unknown conversion mode {mode!r}")

    out = np.ascontiguousarray(out)
    Image.fromarray(out).save(full, quality=90)
    return finish(rel, out.shape[1], out.shape[0], False)


def op_to_equirect(asset_id):
    """Materialize a cube-layout asset as a new equirect asset (c2e)."""
    import io

    from PIL import Image

    a = _get_asset(asset_id)
    if a["kind"] not in ("cube_dice", "cube_horizon"):
        raise ValueError("asset is not a cube layout")
    equi = _load_equirect(a)
    buf = io.BytesIO()
    Image.fromarray(equi).save(buf, format="JPEG", quality=92)
    base = os.path.splitext(a["name"])[0]
    asset = _import_bytes(buf.getvalue(), f"{base}_equirect.jpg")
    con = _db()
    with con:
        con.execute("UPDATE assets SET source_id=? WHERE id=?",
                    (asset_id, asset["id"]))
    con.close()
    asset["source_id"] = asset_id
    _log("to_equirect", {"from": asset_id, "to": asset["id"]})
    return {"asset": _get_asset(asset["id"])}


# ---------------------------------------------------------------- samples

def _make_samples():
    """Synthetic demo panos so the app works out of the box: a JPEG scene,
    a WEBP color-band pano, a TIFF pano, a cube-cross layout, and one
    deliberately non-panoramic image."""
    import io

    import numpy as np
    import py360convert
    from PIL import Image, ImageDraw, ImageFont

    W, H = 4096, 2048
    lon = np.linspace(-180, 180, W, endpoint=False)[None, :].repeat(H, 0)
    lat = np.linspace(90, -90, H)[:, None].repeat(W, 1)

    # --- scene: sky gradient, sun, ground, cardinal markers, meridian grid
    img = np.zeros((H, W, 3), dtype=np.float64)
    t = np.clip((lat + 90) / 180, 0, 1)
    img[..., 0] = 30 + 90 * t
    img[..., 1] = 40 + 130 * t
    img[..., 2] = 70 + 170 * t
    ground = lat < 0
    img[ground] = np.stack([
        50 + 25 * np.sin(np.radians(lon[ground] * 6)),
        90 + 30 * np.cos(np.radians(lat[ground] * 8)),
        55 + 10 * np.sin(np.radians(lon[ground] * 3)),
    ], axis=-1)
    sun = (lon - 45) ** 2 + (lat - 45) ** 2 < 64
    img[sun] = [255, 240, 180]
    grid = (np.abs((lon % 30)) < 0.25) | (np.abs((lat % 30)) < 0.25)
    img[grid] = img[grid] * 0.5 + 110
    pil = Image.fromarray(img.astype("uint8"))
    draw = ImageDraw.Draw(pil)
    try:
        font = ImageFont.load_default(size=140)
    except TypeError:
        font = ImageFont.load_default()
    for name, deg in [("N", 0), ("E", 90), ("S", 180), ("W", -90)]:
        x = (deg + 180) / 360 * W
        draw.text((x, H * 0.48), name, fill=(255, 255, 255), anchor="mm",
                  font=font)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=90)
    _import_bytes(buf.getvalue(), "sample_scene_equirect.jpg")

    # --- webp: hue wheel by longitude
    hue = ((lon + 180) / 360 * 255).astype("uint8")
    sat = np.full_like(hue, 200)
    val = (255 * np.clip((lat + 90) / 180 * 1.2, 0.15, 1)).astype("uint8")
    hsv = Image.fromarray(np.stack([hue, sat, val], -1), "HSV").convert("RGB")
    hsv = hsv.resize((2048, 1024))
    buf = io.BytesIO()
    hsv.save(buf, format="WEBP", quality=88)
    _import_bytes(buf.getvalue(), "sample_hue_wheel.webp")

    # --- tiff: checker + latitude bands
    checker = (((lon // 15) + (lat // 15)) % 2) * 120 + 60
    tif = np.stack([checker, 255 - checker, np.abs(lat) * 2.5 + 30], -1)
    tif_img = Image.fromarray(tif.astype("uint8")).resize((2048, 1024))
    buf = io.BytesIO()
    tif_img.save(buf, format="TIFF")
    _import_bytes(buf.getvalue(), "sample_checker.tiff")

    # --- cube cross derived from the scene (exercises c2e on import)
    dice = py360convert.e2c(np.asarray(pil.resize((2048, 1024))), face_w=512,
                            cube_format="dice")
    buf = io.BytesIO()
    Image.fromarray(dice).save(buf, format="PNG")
    _import_bytes(buf.getvalue(), "sample_cube_cross.png")

    # --- a non-panoramic image to demo validation failure
    flat = Image.fromarray(
        (np.random.default_rng(7).random((600, 800, 3)) * 80 + 100).astype("uint8"))
    ImageDraw.Draw(flat).text((400, 300), "not a pano", fill=(255, 80, 80),
                              anchor="mm", font=font)
    buf = io.BytesIO()
    flat.save(buf, format="PNG")
    _import_bytes(buf.getvalue(), "sample_not_a_pano.png")


def op_setup():
    for d in (LIBRARY, DISPLAY, UPLOADS):
        os.makedirs(d, exist_ok=True)
    if not op_list()["assets"]:
        try:
            _make_samples()
        except Exception as e:
            print(f"sample generation failed: {e}")
    return op_list()


def op_history(limit):
    con = _db()
    rows = con.execute(
        "SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    con.close()
    return {"events": [{"ts": r[0], "kind": r[1], "detail": r[2]} for r in rows]}


# ---------------------------------------------------------------- dispatcher

# Bare main() (no @fused.udf): the builtin executor calls main() directly —
# a udf wrapper hides the signature and hangs on hosted auth. The fused
# engine's compat bridge accepts a bare main() too, so this runs under both.
def main(
    action: str,
    asset_id: int = 0,
    name: str = "",
    path: str = "",
    token: str = "",
    seq: int = 0,
    data_b64: str = "",
    mode: str = "",
    fov: float = 90.0,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    zoom: float = 1.0,
    out_w: int = 0,
    out_h: int = 0,
    face_w: int = 0,
    kind: str = "",
    detail: str = "",
    limit: int = 100,
    url: str = "",
):
    # The fused engine passes @fused.udf params as raw JSON (the browser sends
    # numbers as strings) and does not coerce by annotation like the built-in
    # executor does. Coerce here so every op sees real ints/floats — and so an
    # empty/blank value falls back to the declared default instead of a truthy
    # "0" string (which is what broke cube conversions).
    def _int(v, d):
        try:
            return int(float(v)) if str(v).strip() != "" else d
        except (TypeError, ValueError):
            return d

    def _float(v, d):
        try:
            return float(v) if str(v).strip() != "" else d
        except (TypeError, ValueError):
            return d

    asset_id = _int(asset_id, 0)
    seq = _int(seq, 0)
    out_w = _int(out_w, 0)
    out_h = _int(out_h, 0)
    face_w = _int(face_w, 0)
    limit = _int(limit, 100)
    fov = _float(fov, 90.0)
    yaw = _float(yaw, 0.0)
    pitch = _float(pitch, 0.0)
    roll = _float(roll, 0.0)
    zoom = _float(zoom, 1.0)

    if action == "setup":
        return op_setup()
    if action == "list":
        return op_list()
    if action == "upload_begin":
        return op_upload_begin()
    if action == "upload_chunk":
        return op_upload_chunk(token, seq, data_b64)
    if action == "upload_end":
        return op_upload_end(token, name)
    if action == "import_path":
        return op_import_path(path)
    if action == "import_url":
        return op_import_url(url)
    if action == "browse":
        return op_browse(path)
    if action == "delete":
        return op_delete(asset_id)
    if action == "convert":
        return op_convert(asset_id, mode, fov, yaw, pitch, roll, zoom,
                          out_w, out_h, face_w)
    if action == "to_equirect":
        return op_to_equirect(asset_id)
    if action == "log":
        _log(kind or "event", detail)
        return {"ok": True}
    if action == "history":
        return op_history(limit)
    raise ValueError(f"unknown action {action!r}")
