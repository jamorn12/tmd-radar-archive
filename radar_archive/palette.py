"""สกัด palette สี -> dBZ จาก colorbar ที่อยู่ในภาพเอง

ไม่ hardcode ค่าสี เพราะถ้า TMD เปลี่ยน colorbar เมื่อไหร่ โค้ดจะปรับตามอัตโนมัติ
(แต่ควรตรวจ palette เทียบของเก่าเป็นระยะ — ดู `palette_changed()`)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import lab
from .config import Station


def extract_palette(img: Image.Image, st: Station) -> tuple[np.ndarray, np.ndarray]:
    """คืน (rgb, dbz) ของแต่ละแถบสีใน colorbar เรียงจากบน(สูงสุด)ลงล่าง(ต่ำสุด)

    rgb : (N,3) float 0-255
    dbz : (N,)  ค่ากลางของแต่ละแถบ (โดยประมาณ — colorbar ของ TMD ไม่ linear เป๊ะ)
    """
    left, top, right, bottom = st.colorbar_box
    a = np.asarray(img.convert("RGB"), dtype=float)
    strip = a[top:bottom, left:right, :].mean(axis=1)  # (H,3) เฉลี่ยตามแนวนอน

    n = st.n_colorbar_bands
    h = strip.shape[0] / n
    pad = max(2, int(h * 0.2))

    rgb, dbz = [], []
    for i in range(n):
        s, e = int(i * h) + pad, int((i + 1) * h) - pad
        rgb.append(np.median(strip[s:e], axis=0))
        # ค่ากลางของแถบ mapped เชิงเส้นจาก dbz_top (บน) ไป dbz_bottom (ล่าง)
        frac = (i + 0.5) / n
        dbz.append(st.dbz_top - frac * (st.dbz_top - st.dbz_bottom))

    rgb = np.asarray(rgb)
    dbz = np.asarray(dbz)

    if st.drop_top_band:
        # แถบบนสุดเป็นสีขาว ซึ่งชนกับเส้นขอบจังหวัด/ตัวอักษรบนแผนที่
        rgb, dbz = rgb[1:], dbz[1:]

    return rgb, dbz


def to_lab(rgb: np.ndarray) -> np.ndarray:
    return lab.rgb2lab(rgb.reshape(-1, 1, 3) / 255.0).reshape(-1, 3)


def save_palette(path: Path, rgb: np.ndarray, dbz: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rgb": rgb.round(1).tolist(), "dbz": dbz.round(2).tolist()}, indent=1),
        encoding="utf-8",
    )


def load_palette(path: Path) -> tuple[np.ndarray, np.ndarray]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return np.asarray(d["rgb"], dtype=float), np.asarray(d["dbz"], dtype=float)


def palette_changed(ref_rgb: np.ndarray, new_rgb: np.ndarray, thresh: float = 5.0) -> bool:
    """เตือนเมื่อ TMD เปลี่ยน colorbar (ระยะสีเฉลี่ยใน Lab เกิน thresh)"""
    if ref_rgb.shape != new_rgb.shape:
        return True
    d = np.linalg.norm(to_lab(ref_rgb) - to_lab(new_rgb), axis=1)
    return bool(d.mean() > thresh)
