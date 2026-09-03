"""QC — ตรวจจับและกำจัดสัญญาณผิดปกติที่ไม่ใช่ฝน

สิ่งที่โมดูลนี้จับได้ (จากภาพอย่างเดียว ไม่ต้องใช้ข้อมูลดิบ)

1. **radial spike / RFI**  เส้นตรงยาวพุ่งออกจากสถานีตามแนวรัศมี
   เกิดจากคลื่นรบกวน (Wi-Fi 5 GHz, microwave link) หรือ sun spike ตอนเช้า/เย็น
   ลายเซ็นที่ใช้แยก: **ยาวมากในแนวรัศมี แต่แคบมากในแนวมุมกวาด**
   ฝนจริงกว้างอย่างน้อยหลาย km ในทุกทิศ ไม่มีทางเป็นเส้นกว้าง 1-2 km ยาว 200 km

2. **ground clutter / AP / overlay ที่เหลือค้าง**  สิ่งที่นิ่งอยู่กับที่ข้ามหลายเฟรม
   ใช้ความถี่การปรากฏจาก archive ที่เราเก็บเอง (`clutter_frequency`)
   ฝนเคลื่อนที่เสมอ ถ้า pixel ไหน "มีฝน" เกิน 60-70% ของเฟรมทั้งหมด แปลว่าไม่ใช่ฝน

3. **speckle**  จุดกระจัดกระจายเล็ก ๆ — จัดการไปแล้วใน refine.py (`min_blob_px`)

ข้อจำกัดที่ต้องยอมรับ: วิธี QC ที่ดีที่สุดต้องใช้ข้อมูลดิบ (ρhv จาก dual-pol,
texture ของ Doppler velocity, spectrum width, CMD) ซึ่งภาพจากเว็บไม่มีให้
สิ่งที่ทำได้จากภาพจึงเป็น QC เชิงเรขาคณิตกับเชิงสถิติเวลาเท่านั้น

หลักการทำงาน: **QC ไม่ลบข้อมูลทิ้งถาวร** — คืน mask ที่ทำความสะอาดแล้วพร้อม
รายงานว่าตัดอะไรไปเท่าไร ส่วนภาพ raw ยังเก็บครบทุกเฟรม ย้อนกลับมาทำใหม่ได้เสมอ
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .config import Station

DEFAULTS = dict(
    n_az=720,              # ความละเอียดมุมกวาดตอนแปลงเป็น polar (0.5° ต่อ bin)
    r_min_km=10.0,         # ไม่ประเมินในรัศมีนี้ (แถวสถานีมี clutter หนาแน่นเป็นปกติ)
    support_deg=2.5,       # หน้าต่างมุมที่ใช้ถามว่า "เพื่อนบ้านข้าง ๆ มี echo ไหม"
    support_gap_deg=0.5,   # เว้นรอบตัวเองเท่าไรก่อนเริ่มนับเพื่อนบ้าน
    support_thresh=0.35,   # ถ้าเพื่อนบ้านมี echo น้อยกว่านี้ = ไม่มีใครหนุน
    min_spike_km=40.0,     # ต้องต่อกันยาวในแนวรัศมีเกินนี้จึงนับเป็น spike
    bridge_gap_px=3,       # ยอมให้เส้นขาดได้กี่ bin แล้วยังนับว่าต่อเนื่อง
    grow_deg=1.0,          # ขยายไปยัง azimuth ข้างเคียงเพื่อเก็บขอบเส้นที่ยังเหลือ
    grow_support=0.60,     # ขยายได้เฉพาะ pixel ที่ยังมีเพื่อนบ้านหนุนน้อยกว่านี้
    min_blob_px=6,         # ทำความสะอาดเศษที่เหลือหลังตัด spike
)


@dataclass
class QCReport:
    spike_px: int = 0
    clutter_px: int = 0
    removed_px: int = 0
    removed_pct_of_echo: float = 0.0
    spike_azimuths_deg: list[float] = field(default_factory=list)
    spike_type: str = ""          # "" | "rfi" | "sun" | "rfi+sun"

    def as_row(self) -> dict:
        return {
            "qc_spike_px": self.spike_px,
            "qc_spike_az": ";".join(f"{a:.1f}" for a in self.spike_azimuths_deg),
            "qc_spike_type": self.spike_type,
            "qc_clutter_px": self.clutter_px,
            "qc_removed_px": self.removed_px,
            "qc_removed_pct": round(self.removed_pct_of_echo, 2),
        }


def classify_spikes(azimuths: list[float], when, st: Station) -> str:
    """แยกว่า spike ที่เจอเป็น sun spike หรือ RFI

    sun spike เกิดเฉพาะตอนดวงอาทิตย์อยู่ต่ำใกล้ขอบฟ้าและอยู่ในแนวลำคลื่นพอดี
    (วันละ 2 ช่วงสั้น ๆ) ส่วน RFI ประจำอยู่มุมเดิมได้ตลอดเวลา
    การแยกไว้ตั้งแต่ต้นทำให้รายงาน QC ในเปเปอร์อธิบายได้ว่าตัดอะไรไปด้วยเหตุผลอะไร
    """
    if not azimuths or when is None:
        return ""
    from .solar import is_sun_spike
    sun = any(is_sun_spike(when, st.lat, st.lon, a) for a in azimuths)
    other = any(not is_sun_spike(when, st.lat, st.lon, a) for a in azimuths)
    return "rfi+sun" if (sun and other) else ("sun" if sun else "rfi")


# ---------------------------------------------------------------- polar helpers

def center_in_crop(st: Station) -> tuple[float, float]:
    """พิกัดสถานีในระบบพิกัดของภาพที่ crop แล้ว"""
    cx, cy = st.center_px
    left, top, _, _ = st.plot_box
    return cx - left, cy - top


def _polar_coords(shape, center, n_az, n_r):
    cy_max, cx_max = shape
    cx, cy = center
    az = np.linspace(0.0, 2 * np.pi, n_az, endpoint=False)
    r = np.arange(n_r, dtype=float)
    R, A = np.meshgrid(r, az, indexing="ij")          # (n_r, n_az)
    X = cx + R * np.sin(A)                            # มุมวัดตามเข็มจากทิศเหนือ
    Y = cy - R * np.cos(A)
    return X, Y


def max_range_bins(shape, st: Station) -> int:
    """จำนวน range bin ที่มีความหมาย — ไม่เกินรัศมีสูงสุดของเรดาร์ และไม่เกินขอบภาพ"""
    diag = int(np.hypot(*shape))
    return int(min(diag, round(st.range_km / st.km_per_px * 1.02)))


def to_polar(arr: np.ndarray, center, n_az: int, n_r: int, order: int = 0) -> np.ndarray:
    X, Y = _polar_coords(arr.shape, center, n_az, n_r)
    return ndimage.map_coordinates(arr.astype(float), [Y, X], order=order,
                                   mode="constant", cval=0.0)


def polar_to_cart(polar: np.ndarray, shape, center, order: int = 0) -> np.ndarray:
    """แปลงกลับ — ใช้ nearest neighbour เพื่อไม่ให้ mask เบลอ"""
    n_r, n_az = polar.shape
    cx, cy = center
    yy, xx = np.indices(shape, dtype=float)
    dx, dy = xx - cx, cy - yy
    r = np.hypot(dx, dy)
    a = np.mod(np.arctan2(dx, dy), 2 * np.pi) / (2 * np.pi) * n_az
    return ndimage.map_coordinates(polar, [r, a], order=order, mode="grid-wrap", cval=0.0)


# ---------------------------------------------------------------- spike detection

def _azimuthal_support(pm: np.ndarray, half_win: int, gap: int) -> np.ndarray:
    """สัดส่วนของ azimuth ข้างเคียง (ที่ระยะเดียวกัน) ที่มี echo — ไม่นับตัวเองและเพื่อนติดกัน

    ทำด้วย cumulative sum ตามแกน azimuth แบบวนรอบ จึงเร็วพอสำหรับทุกเฟรม
    """
    n_r, n_az = pm.shape
    ext = np.concatenate([pm, pm, pm], axis=1)                    # วนรอบ
    cs = np.concatenate([np.zeros((n_r, 1)), np.cumsum(ext, axis=1)], axis=1)
    i = np.arange(n_az) + n_az

    def win(w):
        return cs[:, i + w + 1] - cs[:, i - w]

    wide, inner = win(half_win), win(gap)
    n_wide, n_inner = 2 * half_win + 1, 2 * gap + 1
    denom = max(n_wide - n_inner, 1)
    return (wide - inner) / denom


def _long_radial_runs(flag: np.ndarray, min_len: int, bridge: int) -> np.ndarray:
    """เก็บเฉพาะ flag ที่ต่อกันยาวในแนวรัศมี (แกน 0) เกิน min_len bin"""
    out = np.zeros_like(flag, bool)
    closed = ndimage.binary_closing(flag, structure=np.ones((2 * bridge + 1, 1)))
    lbl, n = ndimage.label(closed, structure=np.array([[0, 1, 0]] * 3))  # ต่อกันเฉพาะแนวรัศมี
    if n == 0:
        return out
    objs = ndimage.find_objects(lbl)
    for k, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        if sl[0].stop - sl[0].start >= min_len:
            out |= lbl == k
    return out & flag


def detect_radial_spikes(mask: np.ndarray, st: Station, **kw) -> tuple[np.ndarray, list[float]]:
    """คืน (mask ของ pixel ที่เป็น spike ในพิกัดภาพ, รายการมุมกวาดที่พบ)"""
    p = {**DEFAULTS, **kw}
    center = center_in_crop(st)
    n_az = int(p["n_az"])
    max_r = max_range_bins(mask.shape, st)
    pm = to_polar(mask.astype(float), center, n_az, max_r) > 0.5

    deg_per_bin = 360.0 / n_az
    half_win = max(1, int(round(p["support_deg"] / deg_per_bin)))
    gap = max(0, int(round(p["support_gap_deg"] / deg_per_bin)))
    r_min = int(p["r_min_km"] / st.km_per_px)
    min_len = int(p["min_spike_km"] / st.km_per_px)

    support = _azimuthal_support(pm.astype(float), half_win, gap)
    unsupported = pm & (support < p["support_thresh"])
    unsupported[:r_min] = False

    spike_polar = _long_radial_runs(unsupported, min_len, int(p["bridge_gap_px"]))

    # ขอบของเส้นมีเพื่อนบ้าน (ก็คือตัวเส้นเอง) หนุนอยู่ จึงรอดจากการทดสอบข้างบน
    # -> ขยายออกด้านข้างเล็กน้อย แต่เข้าไปได้เฉพาะที่ยังหนุนน้อย จึงไม่กินก้อนฝนจริง
    grow = max(0, int(round(p["grow_deg"] / deg_per_bin)))
    if grow and spike_polar.any():
        wide = ndimage.binary_dilation(
            spike_polar, structure=np.ones((1, 2 * grow + 1)), border_value=0)
        # ขยายแบบวนรอบแกน azimuth
        wide |= np.roll(ndimage.binary_dilation(
            np.roll(spike_polar, n_az // 2, axis=1),
            structure=np.ones((1, 2 * grow + 1))), -n_az // 2, axis=1)
        spike_polar = spike_polar | (wide & pm & (support < p["grow_support"]))
        spike_polar[:r_min] = False

    az_hits = np.flatnonzero(spike_polar.any(axis=0))
    azimuths = [float(a * deg_per_bin) for a in az_hits]

    spike_cart = polar_to_cart(spike_polar.astype(float), mask.shape, center) > 0.5
    return spike_cart & mask, azimuths


# ---------------------------------------------------------------- clutter map

def clutter_frequency(masks: list[np.ndarray]) -> np.ndarray:
    """สัดส่วนของเฟรมที่แต่ละ pixel ถูกจัดว่าเป็น echo (0-1)

    ใช้กับ archive ที่เก็บมาแล้ว ยิ่งครอบคลุมหลายวัน/หลายฤดู ยิ่งแม่น
    pixel ที่ค่าสูงผิดปกติ = ground clutter, AP, RFI ประจำที่, หรือ overlay ที่หลุดมา
    """
    if not masks:
        raise ValueError("ต้องมีอย่างน้อย 1 เฟรม")
    acc = np.zeros(masks[0].shape, np.float32)
    for m in masks:
        acc += m
    return acc / len(masks)


def clutter_mask(freq: np.ndarray, thresh: float = 0.6) -> np.ndarray:
    return freq >= thresh


# ---------------------------------------------------------------- entry point

def apply_qc(
    res: dict,
    st: Station,
    clutter: np.ndarray | None = None,
    remove_spikes: bool = True,
    when=None,
    **kw,
) -> tuple[dict, QCReport]:
    """ทำ QC กับผลลัพธ์จาก strip/refine — คืน (res ใหม่, รายงาน)

    res เดิมไม่ถูกแก้ไข
    """
    p = {**DEFAULTS, **kw}
    mask = res["mask"].copy()
    n0 = int(mask.sum())
    rep = QCReport()

    if remove_spikes and st.is_calibrated:
        spike, az = detect_radial_spikes(mask, st, **kw)
        rep.spike_px = int(spike.sum())
        rep.spike_azimuths_deg = az
        rep.spike_type = classify_spikes(az, when, st)
        mask &= ~spike

    if clutter is not None:
        hit = mask & clutter
        rep.clutter_px = int(hit.sum())
        mask &= ~clutter

    if p["min_blob_px"] > 1:
        lbl, n = ndimage.label(mask, structure=np.ones((3, 3)))
        if n:
            sizes = np.bincount(lbl.ravel())
            mask &= ~np.isin(lbl, np.flatnonzero(sizes < p["min_blob_px"]))

    rep.removed_px = n0 - int(mask.sum())
    rep.removed_pct_of_echo = 100.0 * rep.removed_px / max(n0, 1)

    out = dict(res)
    out["mask"] = mask
    out["coverage_pct"] = float(mask.mean() * 100.0)
    out["qc"] = rep
    return out, rep
