"""ประกอบหน้าเว็บ "ทันฝน" จาก template + ผลลัพธ์ของ nowcast.py

มีสองรูปแบบจาก template เดียวกัน เพื่อไม่ให้โค้ดสองชุดแยกกันเดิน
  pages    : เอกสาร HTML เต็ม อ้างไฟล์จริงข้าง ๆ -> วางใน docs/ แล้วเปิด GitHub Pages
  artifact : เนื้อหาอย่างเดียว (ไม่มี doctype/html/head/body) และ **ฝังข้อมูลมาด้วย**
             เพราะที่นั่น fetch ข้ามโดเมนถูกบล็อก จึงต้องพกข้อมูลติดตัวไป

โครงไฟล์ที่หน้าเว็บคาดหวังใน docs/
    index.html
    base_dark.png  base_light.png
    nowcast/PHS/latest.json
    nowcast/PHS/f/<epoch>.png
"""
from __future__ import annotations

import base64
import json
import mimetypes
import shutil
from pathlib import Path

TEMPLATE = Path(__file__).with_name("web") / "app.html"

HEAD = """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0C1524">
<meta name="description" content="เรดาร์พิษณุโลกของกรมอุตุนิยมวิทยา ตัดพื้นหลังแล้ว พร้อม nowcast แบบ extrapolation ถึง 60 นาที และตัวนับถอยหลังว่าฝนจะถึงตำแหน่งคุณในกี่นาที">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="icon-192.png">
</head>
<body>
"""
FOOT = "\n</body>\n</html>\n"

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#0C1524"/>
<circle cx="16" cy="14" r="9" fill="none" stroke="#2A4A66" stroke-width=".9"/>
<circle cx="16" cy="14" r="4.8" fill="none" stroke="#2A4A66" stroke-width=".9"/>
<path d="M16 14 L23.6 9.2" stroke="#3ED8E0" stroke-width="1.4" stroke-linecap="round"/>
<circle cx="16" cy="14" r="1.6" fill="#3ED8E0"/>
<path d="M9.5 24 l1.8 3.6 M15 23.4 l1.8 3.6 M20.5 24 l1.8 3.6" stroke="#3ED8E0"
      stroke-width="1.3" stroke-linecap="round" opacity=".75"/>
</svg>
"""

WEBMANIFEST = {
    "name": "ทันฝน — เรดาร์และ nowcast",
    "short_name": "ทันฝน",
    "description": "ฝนจะถึงคุณในกี่นาที จากเรดาร์พิษณุโลกของกรมอุตุนิยมวิทยา",
    "start_url": ".",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#040911",
    "theme_color": "#0C1524",
    "lang": "th",
    "icons": [
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _icons(out_dir: Path) -> None:
    """ไอคอน PNG สองขนาดสำหรับ Add to Home Screen — วาดเองไม่ต้องพึ่งไฟล์ภายนอก"""
    from PIL import Image, ImageDraw

    (out_dir / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    for size in (192, 512):
        s = size / 32.0
        im = Image.new("RGBA", (size, size), (12, 21, 36, 255))
        d = ImageDraw.Draw(im)
        cx, cy = 16 * s, 14 * s
        for r, col, w in ((9 * s, (42, 74, 102), max(1, int(0.9 * s))),
                          (4.8 * s, (42, 74, 102), max(1, int(0.9 * s)))):
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=w)
        d.line([cx, cy, 23.6 * s, 9.2 * s], fill=(62, 216, 224), width=max(1, int(1.5 * s)))
        d.ellipse([cx - 1.8 * s, cy - 1.8 * s, cx + 1.8 * s, cy + 1.8 * s], fill=(62, 216, 224))
        for x0, y0 in ((9.5, 24), (15, 23.4), (20.5, 24)):
            d.line([x0 * s, y0 * s, (x0 + 1.8) * s, (y0 + 3.6) * s],
                   fill=(62, 216, 224), width=max(1, int(1.4 * s)))
        im.save(out_dir / f"icon-{size}.png", optimize=True)


def _fill(template: str, manifest_url: str, dark: str, light: str, embedded: str) -> str:
    return (template
            .replace("__MANIFEST__", manifest_url)
            .replace("__BASE_DARK__", dark)
            .replace("__BASE_LIGHT__", light)
            .replace("__EMBEDDED__", embedded))


def build_pages(out_dir: Path, base_dark: Path, base_light: Path,
                manifest_url: str = "nowcast/PHS/latest.json") -> Path:
    """เขียน docs/ ให้พร้อมเปิด GitHub Pages (Settings -> Pages -> main, /docs)"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for src, name in ((base_dark, "base_dark.png"), (base_light, "base_light.png")):
        dst = out_dir / name
        if Path(src).resolve() != dst.resolve():       # `basemap` เขียนลง docs/ อยู่แล้ว
            shutil.copyfile(src, dst)
    _icons(out_dir)
    (out_dir / "manifest.webmanifest").write_text(
        json.dumps(WEBMANIFEST, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # กัน Jekyll กินโฟลเดอร์ที่ขึ้นต้นด้วย _

    body = _fill(TEMPLATE.read_text(encoding="utf-8"),
                 manifest_url, "base_dark.png", "base_light.png", "null")
    page = out_dir / "index.html"
    page.write_text(HEAD + body + FOOT, encoding="utf-8")
    return page


def build_artifact(out_file: Path, manifest_path: Path, base_dark: Path,
                   base_light: Path) -> Path:
    """เวอร์ชันสาธิต: ฝัง manifest + PNG ทุกเฟรม + แผนที่ฐาน ไว้ในไฟล์เดียว"""
    manifest_path = Path(manifest_path)
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    imgs = {f["url"]: data_uri(manifest_path.parent / f["url"]) for f in doc["frames"]}
    embedded = json.dumps({"manifest": doc, "img": imgs}, ensure_ascii=False)

    body = _fill(TEMPLATE.read_text(encoding="utf-8"),
                 "nowcast/PHS/latest.json",
                 data_uri(base_dark), data_uri(base_light), embedded)
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(body, encoding="utf-8")
    return out_file
