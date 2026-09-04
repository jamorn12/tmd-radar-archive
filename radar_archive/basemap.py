"""สร้างแผนที่ฐานสำหรับหน้าเว็บ จากภาพ TMD เอง — ไม่ต้องพึ่งข้อมูลภายนอก

ทำไมไม่โหลดขอบเขตจังหวัดจากที่อื่น
  ถ้าเอา shapefile จากแหล่งอื่นมาวาด จะต้องจัด projection ให้ตรงกับภาพเรดาร์เองอีกที
  ซึ่งเป็นจุดที่พลาดง่ายและตรวจยาก แต่ภาพ TMD **มีแผนที่พิมพ์อยู่ในภาพอยู่แล้ว**
  และมันอยู่ใน projection เดียวกับ echo เป๊ะ ๆ โดยนิยาม
  จึงดึงมันออกมาแล้ววาร์ปด้วยสมการเดียวกับที่ใช้วาร์ป echo — ลงทะเบียนตรงกันโดยอัตโนมัติ

วิธีแยกแผนที่ออกจากฝน
  ฝนเคลื่อนที่ แผนที่ไม่เคลื่อน -> ค่ามัธยฐานรายพิกเซลข้ามหลายเฟรมคือแผนที่
  ต้องใช้เฟรมที่กระจายตัวพอสมควร (>= 20 เฟรม กินเวลาหลายชั่วโมง) ไม่งั้นก้อนฝน
  ที่อยู่นิ่งจะติดมาด้วย

จากนั้น **ไม่ใช้ภาพนั้นตรง ๆ** แต่แยกเป็นชั้น ๆ ก่อน
  ภาพดิบของ TMD มีของที่เราไม่ต้องการปนอยู่: วงรัศมีสีน้ำเงิน เส้นรัศมีสีแดง
  ชื่อเมืองภาษาอังกฤษ ป้ายระยะ และพื้นสีเขียวมะกอกแบบแผนที่ทหาร
  ถ้าเอามาย้อมสีทั้งแผ่น ของพวกนี้ติดมาหมดและทับกับสิ่งที่เราวาดเอง (วงรัศมี ป้ายไทย)

  จึงแยกออกเป็นสามชั้นที่เอาไปย้อมสีใหม่ได้อิสระ
      terrain : ภาพที่ผ่าน median filter -> เหลือแต่เงาภูมิประเทศ เส้นบางและตัวอักษรหายหมด
      border  : เส้น **มืด** กว่าพื้นรอบข้าง = ขอบเขตการปกครอง
      water   : เส้น **น้ำเงิน** กว่าพื้นรอบข้าง = แม่น้ำและอ่างเก็บน้ำ
  ส่วนที่ **สว่าง** กว่าพื้นรอบข้าง (ตัวอักษร ป้าย จุดเมือง) ทิ้งทั้งหมด เพราะเราวาดเองเป็นภาษาไทย
  ส่วนวงรัศมีกับเส้นรัศมีลบด้วยหน้ากากเชิงเรขาคณิต เพราะรู้ตำแหน่งเป๊ะอยู่แล้ว

  ได้ผลลัพธ์เป็นภาพเวกเตอร์เทียม 3 ชั้น ย้อมเป็นธีมสว่างหรือมืดก็ได้จากชุดเดียวกัน
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import grid
from .config import Station

# วงรัศมีและเส้นรัศมีที่ TMD พิมพ์มาในภาพ — ลบทิ้งเพราะเราวาดเองให้คมกว่า
TMD_RING_KM = (30.0, 48.0, 120.0, 240.0)
TMD_RADIAL_DEG = tuple(range(0, 360, 45))
FURNITURE_PX = 3.0          # ความหนาที่เผื่อไว้รอบเส้นของ TMD
LABEL_BAND_PX = 16.0        # ความสูงของแถบที่ป้ายระยะกินคร่อมวง
LABEL_HALF_W_PX = 36.0      # ครึ่งความกว้างของป้ายระยะ วัดจากแกนตั้ง

THEMES = {
    # ground, relief, water, border  (RGB) — relief คือสีที่ภูมิประเทศไล่ไปหาเมื่อสูง
    "dark":  dict(ground=(11, 18, 32), relief=(30, 44, 66), water=(34, 78, 112),
                  border=(92, 116, 148), void=(6, 10, 18),
                  relief_amt=0.85, water_amt=0.95, border_amt=0.9),
    # โหมดสว่าง: เงาภูมิประเทศต้องเบากว่ามาก ไม่งั้นลายเทา ๆ จะแย่งสายตากับ echo
    "light": dict(ground=(238, 242, 247), relief=(199, 210, 224), water=(138, 180, 214),
                  border=(104, 124, 150), void=(224, 229, 236),
                  relief_amt=0.55, water_amt=0.95, border_amt=0.9),
}


def _crop(path: Path, st: Station) -> np.ndarray:
    left, top, right, bottom = st.plot_box
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)[top:bottom, left:right]


def static_layer(paths, st: Station, max_frames: int = 60) -> np.ndarray:
    """ค่ามัธยฐานรายพิกเซลข้ามเฟรม = ส่วนที่ไม่ขยับ (แผนที่ + ตัวอักษร + พื้นหลัง)"""
    paths = list(paths)
    if len(paths) > max_frames:                    # กระจายให้ทั่วช่วงเวลา ไม่ใช่เอาติดกัน
        idx = np.linspace(0, len(paths) - 1, max_frames).round().astype(int)
        paths = [paths[i] for i in idx]
    if not paths:
        raise ValueError("ไม่มีภาพให้ใช้")
    cube = np.stack([_crop(p, st) for p in paths])
    return np.median(cube, axis=0).astype(np.uint8)


# ---------------------------------------------------------------- แยกชั้น

def _furniture_mask(shape, st: Station) -> np.ndarray:
    """True ตรงที่ TMD พิมพ์วงรัศมี/เส้นรัศมีไว้ — รู้ตำแหน่งเป๊ะจึงลบแบบเรขาคณิตได้"""
    cx, cy = grid.center_in_crop(st)
    yy, xx = np.indices(shape[:2]).astype(np.float32)
    dx, dy = xx - cx, cy - yy
    r = np.hypot(dx, dy)
    m = np.zeros(shape[:2], bool)
    for km in TMD_RING_KM:
        dr = np.abs(r - km / st.km_per_px)
        m |= dr <= FURNITURE_PX
        # ป้ายระยะ ("120.0 km") พิมพ์คร่อมวงอยู่ริมแกนตั้ง — กินพื้นที่กว้างกว่าตัวเส้นมาก
        m |= (dr <= LABEL_BAND_PX) & (np.abs(dx) <= LABEL_HALF_W_PX)
    for deg in TMD_RADIAL_DEG:                      # ระยะตั้งฉากกับรังสีแต่ละเส้น
        a = np.radians(deg)
        ux, uy = np.sin(a), np.cos(a)               # 0 องศา = เหนือ
        along = dx * ux + dy * uy
        # แกนตั้งเผื่อกว้างกว่าเส้นอื่น เพราะมีเส้นรบกวน RFI ที่ az 180 องศาซ้อนอยู่
        w = FURNITURE_PX + (2.0 if deg in (0, 180) else 0.0)
        m |= (np.abs(dx * uy - dy * ux) <= w) & (along >= -w)
    return m


def _ramp(a: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((a - lo) / max(hi - lo, 1e-6), 0.0, 1.0)


def separate(med: np.ndarray, st: Station) -> dict[str, np.ndarray]:
    """แยกภาพมัธยฐานเป็นชั้น terrain / border / water (float 0-1 ทั้งหมด)

    ใช้ส่วนต่างกับ **พื้นหลังเฉพาะที่** (median filter) ไม่ใช่ค่าสัมบูรณ์
    เพราะพื้นแผนที่ TMD เป็นสีเขียวมะกอกที่สว่างไม่เท่ากันทั่วภาพ
    เกณฑ์ตายตัวจึงใช้ไม่ได้ แต่ "มืดกว่าที่รอบตัว" กับ "น้ำเงินกว่าที่รอบตัว" ใช้ได้ทุกที่
    """
    a = med.astype(np.float32)
    lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
    blueness = a[..., 2] - 0.5 * (a[..., 0] + a[..., 1])
    junk = _furniture_mask(a.shape, st)

    # อุดรอยของเส้นรัศมี/วงรัศมีก่อน แล้วค่อยหาพื้นหลัง ไม่งั้นเงาภูมิประเทศ
    # จะมีรอยตะเข็บเป็นกากบาทพาดกลางภาพ (เส้นแดงดันค่า median ขึ้นเฉพาะแนวนั้น)
    def patch(x):
        y = x.copy()
        y[junk] = ndimage.median_filter(x, size=21, mode="nearest")[junk]
        return y

    bg_lum = ndimage.median_filter(patch(lum), size=11, mode="nearest")
    bg_blue = ndimage.median_filter(patch(blueness), size=11, mode="nearest")

    border = _ramp(bg_lum - lum, 35.0, 105.0)
    water = _ramp(blueness - bg_blue, 18.0, 68.0)
    border[junk] = 0.0
    water[junk] = 0.0
    # เส้นแดง (รังสี) ที่หลุดหน้ากากมา ยังทิ้งได้ด้วยสีของมันเอง
    redness = a[..., 0] - 0.5 * (a[..., 1] + a[..., 2])
    red = _ramp(redness - ndimage.median_filter(redness, size=11, mode="nearest"), 20.0, 60.0)
    border *= 1.0 - red
    water *= 1.0 - red

    lo, hi = np.percentile(bg_lum, [3, 97])
    # เงาภูมิประเทศจาก JPEG มี noise ความถี่สูงเยอะ เกลี่ยก่อนเพื่อให้พื้นหลังนิ่ง
    terrain = ndimage.gaussian_filter(_ramp(bg_lum, lo, hi), 1.4)
    return {"terrain": terrain, "border": border, "water": water}


# ---------------------------------------------------------------- วาร์ป + ย้อมสี

def warp(layer: np.ndarray, st: Station, size: int, order: int = 1) -> np.ndarray:
    """สุ่มค่าจากพิกัดภาพลงตารางจัตุรัส ±240 กม. — แถวบนสุด = เหนือสุด

    order=1 (bilinear) เพราะตอนนี้ชั้นข้อมูลเป็นค่าต่อเนื่อง 0-1 แล้ว
    ไม่ใช่ภาพสีที่มีเส้นคม การ interpolate จึงให้ขอบเรียบแทนที่จะให้เส้นจาง
    """
    cx, cy = grid.center_in_crop(st)
    half = grid.GRID_HALF_KM
    e = np.linspace(-half, half, size, dtype=np.float32)
    n = np.linspace(half, -half, size, dtype=np.float32)
    E, N = np.meshgrid(e, n)
    col = cx + E / st.km_per_px
    row = cy - N / st.km_per_px
    out = ndimage.map_coordinates(layer.astype(np.float32), [row, col],
                                  order=order, mode="nearest")
    out[np.hypot(E, N) > st.range_km] = 0.0
    return out


def compose(layers: dict[str, np.ndarray], theme: str = "dark") -> np.ndarray:
    """ย้อมสามชั้นเป็นภาพ RGB ชั้นเดียวตามธีม"""
    t = THEMES[theme]
    ground = np.asarray(t["ground"], np.float32)
    out = np.broadcast_to(ground, layers["terrain"].shape + (3,)).copy()

    def over(dst, mask, color, amt):
        m = (np.clip(mask, 0, 1) * amt)[..., None]
        return dst * (1 - m) + np.asarray(color, np.float32) * m

    out = over(out, layers["terrain"], t["relief"], t["relief_amt"])
    out = over(out, layers["water"], t["water"], t["water_amt"])
    out = over(out, layers["border"], t["border"], t["border_amt"])
    return np.clip(out, 0, 255).astype(np.uint8)


def build(paths, st: Station, out_dir: Path, size: int = 1446,
          max_frames: int = 60, colors: int = 128) -> dict[str, Path]:
    """สร้าง base_dark.png + base_light.png จากคลังภาพ raw

    เขียนเป็น PNG แบบ palette เพราะภาพที่ประกอบเสร็จมีสีไม่กี่เฉด
    ได้ไฟล์เล็กกว่า JPEG มากและไม่มี ringing รอบเส้นขอบ
    """
    med = static_layer(paths, st, max_frames)
    lay = separate(med, st)
    warped = {k: warp(v, st, size) for k, v in lay.items()}

    e = np.linspace(-grid.GRID_HALF_KM, grid.GRID_HALF_KM, size, dtype=np.float32)
    E, N = np.meshgrid(e, e[::-1])
    outside = np.hypot(E, N) > st.range_km

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = {}
    for theme in THEMES:
        rgb = compose(warped, theme)
        # นอกรัศมีคือ "ไม่มีข้อมูล" ไม่ใช่ "ไม่มีฝน" — ให้เป็นสีว่างที่ต่างจากพื้นในวง
        rgb[outside] = np.asarray(THEMES[theme]["void"], np.uint8)
        im = Image.fromarray(rgb, "RGB").quantize(colors=colors, method=Image.MEDIANCUT)
        p = out_dir / f"base_{theme}.png"
        im.save(p, optimize=True)
        made[theme] = p
    return made
