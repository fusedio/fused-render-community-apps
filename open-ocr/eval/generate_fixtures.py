from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), "white")
    return image, ImageDraw.Draw(image)


def make_paragraph() -> str:
    lines = [
        "Quarterly Infrastructure Review",
        "",
        "Uptime across the fleet held at 99.94 percent for the third",
        "quarter, with two incidents traced to a misconfigured load",
        "balancer in the eu-west region. Both were resolved within",
        "eleven minutes of detection. The on-call rotation logged",
        "forty-two pages, down from sixty-one the previous quarter.",
    ]
    image, draw = _canvas(900, 420)
    title_font = _font(28)
    body_font = _font(20)
    y = 40
    draw.text((50, y), lines[0], font=title_font, fill="black")
    y += 55
    for line in lines[2:]:
        draw.text((50, y), line, font=body_font, fill="black")
        y += 32
    path = FIXTURES_DIR / "paragraph.png"
    image.save(path)
    (FIXTURES_DIR / "paragraph.gt.txt").write_text("\n".join([lines[0]] + lines[2:]), encoding="utf-8")
    return str(path)


def make_table() -> str:
    headers = ["Item", "Qty", "Unit Price", "Total"]
    rows = [
        ["USB-C Cable", "12", "$8.00", "$96.00"],
        ["Wireless Mouse", "5", "$22.50", "$112.50"],
        ["Monitor Stand", "3", "$41.00", "$123.00"],
    ]
    col_x = [50, 340, 480, 650]
    image, draw = _canvas(900, 320)
    header_font = _font(22)
    body_font = _font(20)
    draw.text((50, 30), "Purchase Order 4471", font=_font(26), fill="black")
    y = 90
    for x, header in zip(col_x, headers):
        draw.text((x, y), header, font=header_font, fill="black")
    draw.line((50, y + 32, 820, y + 32), fill="black", width=2)
    y += 50
    for row in rows:
        for x, cell in zip(col_x, row):
            draw.text((x, y), cell, font=body_font, fill="black")
        y += 38
    path = FIXTURES_DIR / "table.png"
    image.save(path)
    ground_truth = ["Purchase Order 4471", " | ".join(headers)] + [" | ".join(row) for row in rows]
    (FIXTURES_DIR / "table.gt.txt").write_text("\n".join(ground_truth), encoding="utf-8")
    return str(path)


def make_receipt() -> str:
    image, draw = _canvas(560, 620)
    title_font = _font(24)
    body_font = _font(18)
    lines = [
        ("Corner Bakery Cafe", title_font),
        ("482 Market Street, San Francisco", body_font),
        ("2026-09-14  7:42 AM", body_font),
        ("", body_font),
        ("2x Cold Brew Coffee        $9.00", body_font),
        ("1x Almond Croissant        $4.50", body_font),
        ("1x Avocado Toast           $8.75", body_font),
        ("", body_font),
        ("Subtotal                  $22.25", body_font),
        ("Tax                        $1.95", body_font),
        ("Total                     $24.20", body_font),
    ]
    y = 40
    for text, font in lines:
        draw.text((40, y), text, font=font, fill="black")
        y += 34 if font is body_font else 42
    path = FIXTURES_DIR / "receipt.png"
    image.save(path)
    ground_truth = [text for text, _ in lines if text]
    (FIXTURES_DIR / "receipt.gt.txt").write_text("\n".join(ground_truth), encoding="utf-8")
    return str(path)


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for builder in (make_paragraph, make_table, make_receipt):
        path = builder()
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
