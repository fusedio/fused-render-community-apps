"""Flatten a document snapshot to pixels with Pillow.

The page renders with Konva; this renders the same snapshot without a browser,
so the agent tools can show Claude what it drew (`render_document`), write a
thumbnail per step, and save a PNG/JPEG/PDF while the editor is closed.

It follows Konva's geometry exactly -- node origin, offset, scale, rotation in
degrees clockwise, centred strokes that do not scale -- so layer positions and
shapes match the page. Text is the approximation: it uses the macOS system copy
of each font (Source Sans 3 falls back to Helvetica Neue unless it is installed
locally), so glyph widths can differ by a few percent from the browser.
"""

from __future__ import annotations

import math
import os
import re
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont

SS = 2  # supersampling for vector shapes and text

_FONT_DIRS = [os.path.expanduser("~/Library/Fonts"), "/Library/Fonts",
              "/System/Library/Fonts", "/System/Library/Fonts/Supplemental"]
_FAMILY_FILES = {
    "source sans 3": ["SourceSans3", "Source Sans 3", "SourceSansPro"],
    "source serif 4": ["SourceSerif4", "Source Serif 4", "SourceSerifPro"],
    "helvetica neue": ["HelveticaNeue"],
    "helvetica": ["Helvetica"],
    "arial": ["Arial"],
    "georgia": ["Georgia"],
    "times new roman": ["Times New Roman"],
    "futura": ["Futura"],
    "avenir next": ["Avenir Next"],
    "avenir": ["Avenir"],
    "gill sans": ["GillSans"],
    "impact": ["Impact"],
    "courier new": ["Courier New"],
    "menlo": ["Menlo"],
}
_FALLBACK = {"source sans 3": "helvetica neue", "source serif 4": "georgia"}


# ---------------------------------------------------------------- colours


def parse_color(value, default=None):
    """CSS-ish colour -> RGBA tuple, or None for 'no paint'."""
    if value is None or value == "" or value == "transparent":
        return default
    if isinstance(value, (list, tuple)):
        return tuple(int(v) for v in value) + ((255,) if len(value) == 3 else ())
    text = str(value).strip()
    match = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if match:
        parts = [p.strip() for p in match.group(1).split(",")]
        rgb = [int(float(p)) for p in parts[:3]]
        alpha = int(round(float(parts[3]) * 255)) if len(parts) > 3 else 255
        return (*rgb, alpha)
    try:
        from PIL import ImageColor

        rgba = ImageColor.getcolor(text, "RGBA")
        return rgba
    except ValueError:
        return default


# ---------------------------------------------------------------- fonts


@lru_cache(maxsize=None)
def _font_faces(family: str):
    """[(path, index, style name)] for every face of `family` found on disk."""
    stems = _FAMILY_FILES.get(family.lower(), [family])
    faces = []
    for directory in _FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            if not any(entry.lower().startswith(s.lower()) for s in stems):
                continue
            path = os.path.join(directory, entry)
            for index in range(32):
                try:
                    font = ImageFont.truetype(path, 12, index=index)
                except OSError:
                    break
                name, style = font.getname()
                if any(name.lower().replace(" ", "").startswith(s.lower().replace(" ", "")) for s in stems):
                    faces.append((path, index, style or "Regular"))
                if not entry.lower().endswith(".ttc"):
                    break
    return faces


def _style_score(style: str, weight: int, italic: bool) -> float:
    s = style.lower()
    weights = [("thin", 100), ("ultralight", 200), ("extralight", 200), ("light", 300), ("medium", 500),
               ("semibold", 600), ("demibold", 600), ("demi", 600), ("extrabold", 800), ("heavy", 800),
               ("black", 900), ("bold", 700)]
    face_weight = next((w for key, w in weights if key in s), 400)
    face_italic = "italic" in s or "oblique" in s
    return abs(face_weight - weight) + (0 if face_italic == italic else 1000) + (50 if "condensed" in s else 0)


@lru_cache(maxsize=256)
def load_font(family: str, font_style: str, size: int):
    style = (font_style or "normal").lower()
    italic = "italic" in style
    numbers = re.findall(r"\d{3}", style)
    weight = int(numbers[0]) if numbers else (700 if "bold" in style else 400)
    for name in (family, _FALLBACK.get((family or "").lower(), ""), "helvetica neue", "arial"):
        if not name:
            continue
        faces = _font_faces(name)
        if faces:
            path, index, _ = min(faces, key=lambda f: _style_score(f[2], weight, italic))
            return ImageFont.truetype(path, max(1, size), index=index)
    return ImageFont.load_default(size=max(1, size))


# ---------------------------------------------------------------- geometry


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _translate(x, y):
    return [[1, 0, x], [0, 1, y], [0, 0, 1]]


def _scale(sx, sy):
    return [[sx, 0, 0], [0, sy, 0], [0, 0, 1]]


def _rotate(degrees):
    r = math.radians(degrees or 0)
    c, s = math.cos(r), math.sin(r)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _invert(m):
    a, b, c = m[0]
    d, e, f = m[1]
    det = a * e - b * d
    if abs(det) < 1e-12:
        return None
    ia, ib, id_, ie = e / det, -b / det, -d / det, a / det
    return [[ia, ib, -(ia * c + ib * f)], [id_, ie, -(id_ * c + ie * f)], [0, 0, 1]]


def _apply(m, x, y):
    return m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]


def _composite(canvas: Image.Image, source: Image.Image, matrix, opacity: float, blend: str) -> None:
    """Draw `source` (RGBA) onto `canvas` through the affine `matrix`
    (source pixel -> canvas pixel)."""
    if opacity <= 0 or source.width == 0 or source.height == 0:
        return
    corners = [_apply(matrix, x, y) for x, y in ((0, 0), (source.width, 0), (0, source.height), (source.width, source.height))]
    x0 = max(0, int(math.floor(min(p[0] for p in corners))))
    y0 = max(0, int(math.floor(min(p[1] for p in corners))))
    x1 = min(canvas.width, int(math.ceil(max(p[0] for p in corners))))
    y1 = min(canvas.height, int(math.ceil(max(p[1] for p in corners))))
    if x1 <= x0 or y1 <= y0:
        return
    inverse = _invert(_mat_mul(_translate(-x0, -y0), matrix))
    if inverse is None:
        return
    shrink = max(math.hypot(matrix[0][0], matrix[1][0]), math.hypot(matrix[0][1], matrix[1][1]))
    if shrink < 0.5:
        # Big downscale: pre-reduce so the affine resample does not alias.
        factor = max(1, int(1 / shrink))
        reduced = source.reduce(factor)
        inverse = _mat_mul(_scale(reduced.width / source.width, reduced.height / source.height), inverse)
        source = reduced
    data = (inverse[0][0], inverse[0][1], inverse[0][2], inverse[1][0], inverse[1][1], inverse[1][2])
    placed = source.transform((x1 - x0, y1 - y0), Image.AFFINE, data, resample=Image.BICUBIC)
    if opacity < 1:
        alpha = placed.getchannel("A").point(lambda v: int(v * opacity))
        placed.putalpha(alpha)
    region = canvas.crop((x0, y0, x1, y1))
    canvas.paste(_blend(region, placed, blend), (x0, y0))


def _blend(base: Image.Image, top: Image.Image, mode: str) -> Image.Image:
    mode = (mode or "normal").lower()
    ops = {"multiply": ImageChops.multiply, "screen": ImageChops.screen, "overlay": ImageChops.overlay,
           "darken": ImageChops.darker, "lighten": ImageChops.lighter, "difference": ImageChops.difference,
           "hard-light": ImageChops.hard_light, "soft-light": ImageChops.soft_light}
    if mode not in ops:
        return Image.alpha_composite(base, top)
    mixed = ops[mode](base.convert("RGB"), top.convert("RGB")).convert("RGBA")
    mixed.putalpha(top.getchannel("A"))
    return Image.alpha_composite(base, mixed)


# ---------------------------------------------------------------- layers


def _node_matrix(attrs, offset=(0.0, 0.0), with_scale=True):
    m = _mat_mul(_translate(attrs.get("x", 0) or 0, attrs.get("y", 0) or 0), _rotate(attrs.get("rotation", 0)))
    if with_scale:
        m = _mat_mul(m, _scale(attrs.get("scaleX", 1) or 1, attrs.get("scaleY", 1) or 1))
    return _mat_mul(m, _translate(-offset[0], -offset[1]))


def _image_source(layer: dict) -> Image.Image | None:
    path = layer.get("workingPath") or ""
    if not path or not os.path.exists(path):
        return None
    with Image.open(path) as handle:
        image = handle.convert("RGBA")
    adjust = layer.get("adjust") or {}
    if any(float(v or 0) for v in adjust.values()) or (layer.get("filter") or "none") != "none":
        # Slider values the user is still previewing (not yet applied) --
        # bake them the same way Apply would, into a throwaway copy.
        import tempfile

        import imaging

        with tempfile.TemporaryDirectory() as scratch:
            src = os.path.join(scratch, "in.png")
            image.save(src)
            if any(float(v or 0) for v in adjust.values()):
                imaging.apply_adjustments(src, os.path.join(scratch, "a.png"), adjust)
                src = os.path.join(scratch, "a.png")
            if (layer.get("filter") or "none") != "none":
                imaging.apply_filter_preset(src, os.path.join(scratch, "f.png"), layer["filter"],
                                            float(layer.get("filterStrength", 100)))
                src = os.path.join(scratch, "f.png")
            with Image.open(src) as handle:
                image = handle.convert("RGBA")
    return image


def _shape_canvas(width: float, height: float, pad: float):
    w = max(1, int(math.ceil((width + 2 * pad) * SS)))
    h = max(1, int(math.ceil((height + 2 * pad) * SS)))
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _draw_rect(a):
    sx, sy = abs(a.get("scaleX", 1) or 1), abs(a.get("scaleY", 1) or 1)
    w, h = (a.get("width", 0) or 0) * sx, (a.get("height", 0) or 0) * sy
    sw = float(a.get("strokeWidth", 0) or 0)
    stroke = parse_color(a.get("stroke")) if sw > 0 else None
    fill = parse_color(a.get("fill"))
    radius = a.get("cornerRadius", 0) or 0
    radius = float(radius[0] if isinstance(radius, list) else radius)
    radius = max(0.0, min(radius, w / 2, h / 2))
    pad = sw / 2 + 2
    image = _shape_canvas(w, h, pad)
    draw = ImageDraw.Draw(image)
    box = [pad * SS, pad * SS, (pad + w) * SS, (pad + h) * SS]
    if fill:
        draw.rounded_rectangle(box, radius=radius * SS, fill=fill)
    if stroke:
        grow = sw / 2 * SS
        outer = [box[0] - grow, box[1] - grow, box[2] + grow, box[3] + grow]
        draw.rounded_rectangle(outer, radius=(radius + sw / 2) * SS if radius else 0, outline=stroke, width=max(1, int(round(sw * SS))))
    return image, (-pad, -pad)


def _draw_ellipse(a):
    sx, sy = abs(a.get("scaleX", 1) or 1), abs(a.get("scaleY", 1) or 1)
    rx, ry = (a.get("radiusX", 0) or 0) * sx, (a.get("radiusY", 0) or 0) * sy
    sw = float(a.get("strokeWidth", 0) or 0)
    stroke = parse_color(a.get("stroke")) if sw > 0 else None
    fill = parse_color(a.get("fill"))
    pad = sw / 2 + 2
    image = _shape_canvas(2 * rx, 2 * ry, pad)
    draw = ImageDraw.Draw(image)
    box = [pad * SS, pad * SS, (pad + 2 * rx) * SS, (pad + 2 * ry) * SS]
    if fill:
        draw.ellipse(box, fill=fill)
    if stroke:
        grow = sw / 2 * SS
        draw.ellipse([box[0] - grow, box[1] - grow, box[2] + grow, box[3] + grow], outline=stroke, width=max(1, int(round(sw * SS))))
    return image, (-rx - pad, -ry - pad)


def _dashed(points, dash):
    """Split a polyline into dash segments."""
    if not dash:
        return [points]
    out, on, left, index = [], True, dash[0], 0
    current = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while seg - pos > left:
            pos += left
            t = pos / seg
            p = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            current.append(p)
            if on:
                out.append(current)
            current = [p]
            on = not on
            index += 1
            left = dash[index % len(dash)]
        left -= seg - pos
        current.append((x1, y1))
    if on and len(current) > 1:
        out.append(current)
    return out


def _draw_line(a):
    sx, sy = a.get("scaleX", 1) or 1, a.get("scaleY", 1) or 1
    flat = a.get("points") or [0, 0, 0, 0]
    pts = [(flat[i] * sx, flat[i + 1] * sy) for i in range(0, len(flat) - 1, 2)]
    sw = float(a.get("strokeWidth", 4) or 0)
    color = parse_color(a.get("stroke"), (255, 255, 255, 255))
    pl, pw = float(a.get("pointerLength", 18) or 0), float(a.get("pointerWidth", 16) or 0)
    pad = max(sw, pw) + 4
    minx, miny = min(p[0] for p in pts), min(p[1] for p in pts)
    maxx, maxy = max(p[0] for p in pts), max(p[1] for p in pts)
    image = _shape_canvas(maxx - minx, maxy - miny, pad)
    draw = ImageDraw.Draw(image)
    local = [((x - minx + pad) * SS, (y - miny + pad) * SS) for x, y in pts]
    width = max(1, int(round(sw * SS)))
    for run in _dashed(local, [d * SS for d in (a.get("dash") or [])]):
        draw.line(run, fill=color, width=width, joint="curve")
        for x, y in (run[0], run[-1]):
            r = width / 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def head(tip, tail):
        dx, dy = tip[0] - tail[0], tip[1] - tail[1]
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        bx, by = tip[0] - ux * pl * SS, tip[1] - uy * pl * SS
        half = pw / 2 * SS
        draw.polygon([tip, (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)], fill=color)

    if len(local) >= 2:
        if a.get("pointerAtEnding"):
            head(local[-1], local[-2])
        if a.get("pointerAtBeginning"):
            head(local[0], local[1])
    return image, (minx - pad, miny - pad)


def text_metrics(a) -> dict:
    """Konva.Text layout: lines, their widths and the box size."""
    size = float(a.get("fontSize", 64) or 64)
    font = load_font(a.get("fontFamily") or "Source Sans 3", a.get("fontStyle") or "normal", int(round(size)))
    spacing = float(a.get("letterSpacing", 0) or 0)
    padding = float(a.get("padding", 4) if a.get("padding") is not None else 4)
    line_h = size * float(a.get("lineHeight", 1.15) or 1.15)

    def measure(text):
        return font.getlength(text) + spacing * max(0, len(text))

    fixed = a.get("width")
    raw_lines = str(a.get("text", "")).split("\n")
    lines = []
    if fixed:
        limit = max(1.0, float(fixed) - 2 * padding)
        for raw in raw_lines:
            words, line = raw.split(" "), ""
            for word in words:
                trial = word if not line else line + " " + word
                if line and measure(trial) > limit:
                    lines.append(line)
                    line = word
                else:
                    line = trial
            lines.append(line)
    else:
        lines = raw_lines
    widths = [measure(line) for line in lines]
    width = float(fixed) if fixed else (max(widths or [0]) + 2 * padding)
    return {"font": font, "lines": lines, "widths": widths, "width": width,
            "height": line_h * len(lines) + 2 * padding, "line_height": line_h,
            "padding": padding, "spacing": spacing, "size": size}


def _draw_text(a):
    m = text_metrics(a)
    sx, sy = abs(a.get("scaleX", 1) or 1), abs(a.get("scaleY", 1) or 1)
    k = SS * max(sx, sy)
    font = load_font(a.get("fontFamily") or "Source Sans 3", a.get("fontStyle") or "normal", int(round(m["size"] * k)))
    fill = parse_color(a.get("fill"), (255, 255, 255, 255))
    sw = float(a.get("strokeWidth", 0) or 0)
    stroke = parse_color(a.get("stroke")) if sw > 0 else None
    pad = sw + 2
    image = Image.new("RGBA", (max(1, int(math.ceil((m["width"] + 2 * pad) * k))),
                               max(1, int(math.ceil((m["height"] + 2 * pad) * k)))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    align = a.get("align") or "left"
    inner = m["width"] - 2 * m["padding"]
    for i, (line, lw) in enumerate(zip(m["lines"], m["widths"])):
        x = m["padding"] + (inner - lw) / 2 if align == "center" else m["padding"] + inner - lw if align == "right" else m["padding"]
        y = m["padding"] + i * m["line_height"] + m["line_height"] / 2
        px, py = (x + pad) * k, (y + pad) * k
        options = dict(fill=fill, anchor="lm")
        if stroke:
            options.update(stroke_width=max(1, int(round(sw * k / 2))), stroke_fill=stroke)
        if m["spacing"]:
            for ch in line:
                draw.text((px, py), ch, font=font, **options)
                px += font.getlength(ch) + m["spacing"] * k
        else:
            draw.text((px, py), line, font=font, **options)
        if "underline" in (a.get("textDecoration") or ""):
            uy = py + m["size"] * k * 0.4
            draw.line([((x + pad) * k, uy), ((x + pad + lw) * k, uy)], fill=fill, width=max(1, int(m["size"] * k / 18)))
    return image, (-pad, -pad), k


def render(snap: dict, scale: float = 1.0, background: bool = True) -> Image.Image:
    """The whole page as RGBA at `scale` x its pixel size."""
    width = max(1, int(round((snap.get("width") or 1) * scale)))
    height = max(1, int(round((snap.get("height") or 1) * scale)))
    fill = (0, 0, 0, 0)
    if background and not snap.get("transparent"):
        fill = parse_color(snap.get("background") or "#ffffff", (255, 255, 255, 255))
    canvas = Image.new("RGBA", (width, height), fill)
    view = _scale(scale, scale)
    for layer in snap.get("layers") or []:
        if layer.get("visible") is False:
            continue
        a = dict(layer.get("attrs") or {})
        kind = layer.get("type")
        opacity = float(layer.get("opacity", 1) if layer.get("opacity") is not None else 1)
        blend = layer.get("blend") or "normal"
        if kind == "image":
            source = _image_source(layer)
            if source is None:
                continue
            nw = layer.get("naturalWidth") or source.width
            nh = layer.get("naturalHeight") or source.height
            m = _mat_mul(view, _node_matrix(a, (nw / 2, nh / 2)))
            m = _mat_mul(m, _scale(nw / source.width, nh / source.height))
            _composite(canvas, source, m, opacity, blend)
            continue
        if kind == "rect":
            image, origin = _draw_rect(a)
            k = SS
        elif kind == "ellipse":
            image, origin = _draw_ellipse(a)
            k = SS
        elif kind == "line":
            image, origin = _draw_line(a)
            k = SS
        elif kind == "text":
            image, origin, k = _draw_text(a)
            sx, sy = abs(a.get("scaleX", 1) or 1), abs(a.get("scaleY", 1) or 1)
            m = _mat_mul(view, _node_matrix(a, with_scale=False))
            m = _mat_mul(m, _scale(sx, sy))
            m = _mat_mul(m, _translate(origin[0], origin[1]))
            m = _mat_mul(m, _scale(1 / k, 1 / k))
            _composite(canvas, image, m, opacity, blend)
            continue
        else:
            continue
        # Shapes are drawn with their scale already applied (so strokes keep
        # their width, like Konva's strokeScaleEnabled=false); only flips and
        # rotation remain for the matrix.
        flip = _scale(-1 if (a.get("scaleX", 1) or 1) < 0 else 1, -1 if (a.get("scaleY", 1) or 1) < 0 else 1)
        if kind == "line":
            flip = _scale(1, 1)
        m = _mat_mul(view, _node_matrix(a, with_scale=False))
        m = _mat_mul(m, flip)
        m = _mat_mul(m, _translate(origin[0], origin[1]))
        m = _mat_mul(m, _scale(1 / k, 1 / k))
        _composite(canvas, image, m, opacity, blend)
    return canvas


def thumbnail_png(snap: dict, max_side: int = 320) -> bytes:
    import io

    longest = max(snap.get("width") or 1, snap.get("height") or 1)
    image = render(snap, scale=min(1.0, max_side / longest))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def layer_bounds(layer: dict) -> dict:
    """Axis-aligned box of a layer in page pixels (what the agent sees)."""
    a = layer.get("attrs") or {}
    kind = layer.get("type")
    sx, sy = a.get("scaleX", 1) or 1, a.get("scaleY", 1) or 1
    if kind == "image":
        w, h = layer.get("naturalWidth", 0) or 0, layer.get("naturalHeight", 0) or 0
        pts = [(0, 0), (w, 0), (0, h), (w, h)]
        m = _node_matrix(a, (w / 2, h / 2))
    elif kind == "rect":
        w, h = a.get("width", 0) or 0, a.get("height", 0) or 0
        pts, m = [(0, 0), (w, 0), (0, h), (w, h)], _node_matrix(a)
    elif kind == "ellipse":
        rx, ry = a.get("radiusX", 0) or 0, a.get("radiusY", 0) or 0
        pts, m = [(-rx, -ry), (rx, -ry), (-rx, ry), (rx, ry)], _node_matrix(a)
    elif kind == "line":
        flat = a.get("points") or [0, 0]
        pts, m = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)], _node_matrix(a)
    elif kind == "text":
        t = text_metrics(a)
        pts, m = [(0, 0), (t["width"], 0), (0, t["height"]), (t["width"], t["height"])], _node_matrix(a)
    else:
        return {}
    del sx, sy
    placed = [_apply(m, x, y) for x, y in pts]
    x0, y0 = min(p[0] for p in placed), min(p[1] for p in placed)
    x1, y1 = max(p[0] for p in placed), max(p[1] for p in placed)
    return {"x": round(x0, 1), "y": round(y0, 1), "width": round(x1 - x0, 1), "height": round(y1 - y0, 1)}
