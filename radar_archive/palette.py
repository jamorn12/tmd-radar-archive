"""สกัด palette สี -> dBZ จาก colorbar ที่อยู่ในภาพเอง

ไม่ hardcode ค่าสี เพราะถ้า TMD เปลี่ยน colorbar เมื่อไหร่ โค้ดจะปรับตามอัตโนมัติ
(แต่ควรตรวจ palette เทียบของเก่าเป็นระยะ — ดู `palette_changed()`)

ค่า dBZ ของแต่ละแถบ — แก้แล้ว 2026-09-03
  เดิมเรา map ค่า dBZ แบบเชิงเส้นจาก dbz_top ลง dbz_bottom เท่า ๆ กันทุกแถบ
  แต่ ONWR (สทนช.) เปิด endpoint /api/frames ที่ใช้ข้อมูล TMD ชุดเดียวกัน และส่ง
  ตาราง `levels_dbz` มาด้วย ปรากฏว่า **สองแถบล่างสุดไม่เป็นเชิงเส้น**

      ของจริง : 10.4, 11.3, 16.5, 19, 21.5, 24, ... 66.5     (เว้น 2.5 ตั้งแต่แถบที่ 3)
      ที่เราเดา: 11.7, 14.2, 16.6, 19.0, 21.5, 23.9, ... 62.9

  ค่าที่เพี้ยนอยู่ที่แถบจางสุดซึ่งเป็นฝนเบา — กระทบทั้งการนับ coverage และค่าสะสม
  ตอนนี้จึงจับคู่แถบสีที่สุ่มได้กับ **สีอ้างอิง** แล้วหยิบค่า dBZ ที่ถูกต้องมาใช้
  ถ้าจับคู่ไม่ได้ (TMD เปลี่ยน colorbar) จะถอยกลับไปใช้การ interpolate แบบเดิม
  พร้อมพิมพ์เตือน

หมายเหตุเรื่องสีซ้ำ
  ในตารางอ้างอิงมีสีที่ซ้ำกัน 4 คู่ ([0,202,0], [232,154,0], [208,0,83], [242,229,242])
  แปลว่าการอ่านค่า dBZ กลับจากสีมี ambiguity จริงในช่วงราว 44-51 dBZ
  ไม่ใช่ข้อจำกัดของวิธีเรา — หน่วยงานที่ใช้ข้อมูลชุดเดียวกันก็เจอเหมือนกัน
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import lab
from .config import Station

# ---------------------------------------------------------------- ตารางอ้างอิง
# ที่มา: ONWR WAM /api/frames (levels_rgb / levels_dbz) เรียงจาก dBZ ต่ำ -> สูง
TMD_REF_RGB = np.array([
    [0, 169, 0], [0, 188, 0], [0, 202, 0], [0, 202, 0], [0, 208, 69],
    [0, 228, 0], [0, 243, 0], [246, 246, 0], [225, 225, 0], [216, 216, 0],
    [254, 199, 0], [232, 154, 0], [232, 154, 0], [254, 84, 0], [241, 0, 0],
    [229, 0, 92], [208, 0, 83], [208, 0, 83], [254, 0, 254], [255, 128, 255],
    [254, 199, 254], [242, 229, 242], [242, 229, 242],
], dtype=float)

TMD_REF_DBZ = np.array([
    10.4, 11.3, 16.5, 19.0, 21.5, 24.0, 26.5, 29.0, 31.5, 34.0, 36.5, 39.0,
    41.5, 44.0, 46.5, 49.0, 51.5, 54.0, 56.5, 59.0, 61.5, 64.0, 66.5,
], dtype=float)

# ระยะสีใน Lab ที่ยังถือว่า "ใช่แถบเดียวกัน" — JPEG ทำให้เพี้ยนได้ราว 3-6 หน่วย
REF_MATCH_TOL = 12.0


def _match_reference(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    """จับคู่แถบสีที่สุ่มจาก colorbar กับตารางอ้างอิง คืน (dbz เรียงตาม rgb, ระยะสีเฉลี่ย)

    rgb เรียงจากบน(dBZ สูง) ลงล่าง(dBZ ต่ำ) เหมือนที่เห็นในภาพ

    **ต้องจับคู่ตามลำดับ ไม่ใช่หาสีที่ใกล้ที่สุดทีละแถบ**
    เพราะในตารางมีสีซ้ำกัน 4 คู่ ถ้าหาสีใกล้สุดแบบอิสระ ทั้งคู่จะไปลงที่ตัวแรก
    ทำให้แถบบนของแต่ละคู่ได้ค่าต่ำไป 2.5 dBZ ทุกครั้ง (เจอตอนทดสอบจริง)
    colorbar เรียงตามค่าอยู่แล้ว การจับคู่ตามตำแหน่งจึงถูกต้องและแก้ ambiguity ไปในตัว

    การจับคู่ตามตำแหน่งไม่ได้เชื่อแบบตาบอด — คืนระยะสีเฉลี่ยออกไปให้ผู้เรียกตรวจ
    ถ้า TMD สลับสีหรือเพิ่มแถบ ระยะจะพุ่งขึ้นแล้วผู้เรียกจะถอยไปใช้วิธีอื่นเอง
    """
    n = len(rgb)
    if n != len(TMD_REF_RGB):
        return None, float("inf")          # จำนวนแถบไม่ตรง ใช้ตารางนี้ไม่ได้
    lab_got = to_lab(rgb)[::-1]            # กลับเป็นเรียงจาก dBZ ต่ำ -> สูง ให้ตรงกับตาราง
    lab_ref = to_lab(TMD_REF_RGB)
    err = float(np.linalg.norm(lab_got - lab_ref, axis=1).mean())
    return TMD_REF_DBZ[::-1].copy(), err    # กลับกลับให้เรียงตาม rgb ที่รับเข้ามา


def _linear_dbz(st: Station, n: int) -> np.ndarray:
    """ค่า dBZ แบบ interpolate เชิงเส้น — ใช้เป็นทางถอยเมื่อจับคู่สีไม่ได้"""
    frac = (np.arange(n) + 0.5) / n
    return st.dbz_top - frac * (st.dbz_top - st.dbz_bottom)


# ---------------------------------------------------------------- สกัด palette

def extract_palette(img: Image.Image, st: Station,
                    use_reference: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """คืน (rgb, dbz) ของแต่ละแถบสีใน colorbar เรียงจากบน(สูงสุด)ลงล่าง(ต่ำสุด)

    rgb : (N,3) float 0-255 — สีที่สุ่มจากภาพจริง (ใช้จับคู่ pixel ได้ตรงกว่าค่าอ้างอิง
          เพราะมันผ่าน JPEG มาด้วยกัน)
    dbz : (N,)  ค่า dBZ ของแต่ละแถบ — เอามาจากตารางอ้างอิงถ้าจับคู่ได้
    """
    left, top, right, bottom = st.colorbar_box
    a = np.asarray(img.convert("RGB"), dtype=float)
    strip = a[top:bottom, left:right, :].mean(axis=1)  # (H,3) เฉลี่ยตามแนวนอน

    n = st.n_colorbar_bands
    h = strip.shape[0] / n
    pad = max(2, int(h * 0.2))

    rgb = []
    for i in range(n):
        s, e = int(i * h) + pad, int((i + 1) * h) - pad
        rgb.append(np.median(strip[s:e], axis=0))
    rgb = np.asarray(rgb)

    if use_reference:
        dbz, err = _match_reference(rgb)
        if dbz is None or err > REF_MATCH_TOL:
            why = ("จำนวนแถบไม่ตรงกับตาราง" if dbz is None
                   else f"ระยะสีเฉลี่ย {err:.1f} > {REF_MATCH_TOL}")
            print(f"[!] {st.code}: colorbar ไม่ตรงกับตารางอ้างอิง ({why}) "
                  f"— ถอยไปใช้การ interpolate เชิงเส้น ค่า dBZ อาจคลาดเคลื่อน "
                  f"ควรตรวจ colorbar ว่า TMD เปลี่ยนอะไรไป")
            dbz = _linear_dbz(st, n)
    else:
        dbz = _linear_dbz(st, n)

    if st.drop_top_band:
        # แถบบนสุดเป็นสีขาว ซึ่งชนกับเส้นขอบจังหวัด/ตัวอักษรบนแผนที่
        rgb, dbz = rgb[1:], dbz[1:]

    return rgb, np.asarray(dbz, dtype=float)


def to_lab(rgb: np.ndarray) -> np.ndarray:
    return lab.rgb2lab(np.asarray(rgb, float).reshape(-1, 1, 3) / 255.0).reshape(-1, 3)


def save_palette(path: Path, rgb: np.ndarray, dbz: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rgb": rgb.round(1).tolist(), "dbz": dbz.round(2).tolist(),
                    "source": "tmd-colorbar + onwr-levels"}, indent=1),
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


def needs_upgrade(path: Path) -> bool:
    """palette ที่เก็บไว้เป็นเวอร์ชันเก่า (ยังใช้ค่า interpolate) หรือเปล่า"""
    p = Path(path)
    if not p.exists():
        return False
    return json.loads(p.read_text(encoding="utf-8")).get("source") != "tmd-colorbar + onwr-levels"
