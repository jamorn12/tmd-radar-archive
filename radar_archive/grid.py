"""แปลงภาพที่ตัดพื้นหลังแล้ว -> กริดคาร์ทีเซียน 241x241 @ 2 กม. พร้อมป้อน pysteps

ทำไมต้องมีไฟล์นี้
  pysteps ไม่เคยอ่านไฟล์เรดาร์เอง มันรับ **อาร์เรย์ 2 มิติเป็นลำดับเวลา** กับ metadata dict
  เท่านั้น งานที่ Py-ART ทำในท่อของ CRI (อ่าน UF -> QC -> polar เป็น Cartesian)
  TMD ทำมาให้แล้วก่อนเรนเดอร์เป็นภาพ โมดูลนี้จึงเข้าท่อแทน `build_cappi_stack()`
  ได้ตรง ๆ โดยที่โค้ดหลังจากนี้ (Z-R, dB transform, motion, extrapolation, verification)
  ใช้ของเดิมได้ทั้งหมดไม่ต้องแก้

  build_cappi_stack(uf_files)      -> (T, 241, 241) dBZ, times, meta     <- ของเดิม
  stack_from_files(png_files, st)  -> (T, 241, 241) dBZ, times, meta     <- ตัวนี้

Projection — ตรวจแล้วด้วย `calibrate.py`
  ภาพ PPI ของ TMD เป็น **azimuthal equidistant** รอบจุดตั้งสถานี
  ยืนยันจากวงรัศมีทั้ง 4 วง: ฟิตเป็นวงกลมได้ rms 0.88 px และรัศมีแปรผันตรงกับระยะ
  โดยสเกลจากแต่ละวงต่างกันเพียง 0.64% -> การแปลง pixel เป็น กม. เป็นเชิงเส้นจริง

ข้อตกลงเรื่องค่าในอาร์เรย์ (สำคัญ — เอกสาร pysteps เตือนไว้ว่า NaN กับ 0 ห้ามปนกัน)
  NaN            = **ไม่มีข้อมูล** (นอกรัศมี 240 กม. / นอกภาพ / ใต้แถบสีที่บังขอบตะวันตก)
  NO_ECHO_DBZ    = **มีข้อมูลแต่ไม่มีฝน** (ต่ำกว่าขีดล่างของ colorbar คือ 10.5 dBZ
                   ซึ่งเท่ากับ 0.075 มม./ชม. ภายใต้ Rosenfeld tropical — ต่ำกว่าเกณฑ์
                   วิเคราะห์มาตรฐาน 0.1 มม./ชม. อยู่แล้ว จึงถือเป็นศูนย์ได้อย่างปลอดภัย)

การจัดวางแถว
  แถวที่ 0 = ใต้สุด (yorigin='lower') ให้ตรงกับกริดของ Py-ART และกับ metadata
  ที่ท่อ CRI ใช้อยู่ — ถ้าสลับ แกน v ของสนามลมจะกลับทิศโดยไม่มี error
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Station

# กริดเป้าหมาย — ชุดเดียวกับงาน CRI เพื่อให้เทียบสองสถานีได้ตรง ๆ
GRID_N = 241
GRID_HALF_KM = 240.0
KM_PER_PIXEL = 2.0 * GRID_HALF_KM / (GRID_N - 1)      # = 2.0 กม.

NO_ECHO_DBZ = 0.0        # dBZ ของ "ไม่มีฝน" -> 0.009 มม./ชม. ถือเป็นศูนย์ได้
MIN_COVER_FRAC = 0.35    # ต้องมี pixel ต้นทางอย่างน้อยเท่านี้ของที่ควรมี ไม่งั้นเป็น NaN


# ---------------------------------------------------------------- geometry

def center_in_crop(st: Station) -> tuple[float, float]:
    cx, cy = st.center_px
    left, top, _, _ = st.plot_box
    return cx - left, cy - top


def image_km(st: Station, shape: tuple[int, int]):
    """คืน (E, N) หน่วย กม. ของทุก pixel ในภาพที่ crop แล้ว (ตะวันออก+, เหนือ+)"""
    cx, cy = center_in_crop(st)
    yy, xx = np.indices(shape)
    return (xx - cx) * st.km_per_px, (cy - yy) * st.km_per_px


def grid_km():
    """พิกัด กม. ของจุดกึ่งกลางเซลล์กริด — คืน (E_1d, N_1d) โดย N เรียงจากใต้ไปเหนือ"""
    e = np.linspace(-GRID_HALF_KM, GRID_HALF_KM, GRID_N)
    return e, e.copy()


def grid_latlon(st: Station):
    """lat/lon ของทุกเซลล์กริด (สำหรับ GeoTIFF / แอป) — inverse azimuthal equidistant"""
    e, n = grid_km()
    E, N = np.meshgrid(e, n)                       # N เรียงจากใต้ไปเหนือ
    R = 6371.0088
    rho = np.hypot(E, N)
    c = rho / R
    p0, l0 = np.radians(st.lat), np.radians(st.lon)
    with np.errstate(invalid="ignore", divide="ignore"):
        lat = np.arcsin(np.where(rho == 0, np.sin(p0),
                                 np.cos(c) * np.sin(p0) + N * np.sin(c) * np.cos(p0) / rho))
        lon = l0 + np.arctan2(E * np.sin(c),
                              rho * np.cos(p0) * np.cos(c) - N * np.sin(p0) * np.sin(c))
    return np.degrees(lat), np.degrees(lon)


# ---------------------------------------------------------------- image -> dBZ

def dbz_field(res: dict, st: Station, pal_dbz: np.ndarray,
              no_echo: float = NO_ECHO_DBZ) -> np.ndarray:
    """สนาม dBZ ในพิกัดภาพ จากผลลัพธ์ของ strip/refine (+qc ถ้ามี)

    res ต้องมี key 'mask' และ 'band_index' ตามที่ strip.strip_background /
    refine.strip_background_refined คืนมา
    """
    mask = res["mask"]
    out = np.full(mask.shape, no_echo, np.float32)
    out[mask] = pal_dbz[res["band_index"][mask]]
    E, N = image_km(st, mask.shape)
    out[np.hypot(E, N) > st.range_km] = np.nan      # นอกรัศมีเรดาร์ = ไม่มีข้อมูล
    return out


# ---------------------------------------------------------------- regridding

def to_grid(dbz_img: np.ndarray, st: Station, agg: str = "mean") -> np.ndarray:
    """ย่อจากกริดภาพ (~0.66 กม./px) ลงกริด 241x241 @ 2 กม.

    agg='mean' : เฉลี่ยใน **ปริภูมิเชิงเส้น Z** ไม่ใช่ใน dBZ
                 (dBZ เป็นสเกลลอการิทึม การเฉลี่ย dBZ ตรง ๆ ให้ค่าต่ำกว่าความจริงเสมอ)
                 นี่คือสิ่งที่บินเรดาร์ที่หยาบกว่าจะวัดได้จริง — ใช้เป็นค่าเริ่มต้น
    agg='max'  : ค่าสูงสุดในเซลล์ เก็บแกนฝนไว้ได้ครบกว่า เหมาะถ้าสนใจค่าสุดขีด

    ผลพลอยได้: การเฉลี่ย ~9 pixel ต่อเซลล์ช่วยลด quantization noise จากแถบสี
    ซึ่งไปโผล่เป็นสัญญาณรบกวนที่ wavenumber สูงถ้าไม่ทำ
    """
    E, N = image_km(st, dbz_img.shape)
    col = np.rint((E + GRID_HALF_KM) / KM_PER_PIXEL).astype(np.int64)
    row = np.rint((N + GRID_HALF_KM) / KM_PER_PIXEL).astype(np.int64)   # แถว 0 = ใต้สุด

    ok = (col >= 0) & (col < GRID_N) & (row >= 0) & (row < GRID_N) & np.isfinite(dbz_img)
    flat = (row * GRID_N + col)[ok]
    vals = dbz_img[ok].astype(np.float64)
    n_cell = GRID_N * GRID_N

    cnt = np.bincount(flat, minlength=n_cell).astype(np.float64)
    if agg == "max":
        acc = np.full(n_cell, -np.inf)
        np.maximum.at(acc, flat, vals)
        out = acc
    else:
        z = 10.0 ** (vals / 10.0)                       # dBZ -> Z เชิงเส้น
        acc = np.bincount(flat, weights=z, minlength=n_cell)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = 10.0 * np.log10(acc / np.maximum(cnt, 1))
    out = out.reshape(GRID_N, GRID_N)
    cnt = cnt.reshape(GRID_N, GRID_N)

    expect = (KM_PER_PIXEL / st.km_per_px) ** 2
    out[cnt < MIN_COVER_FRAC * expect] = np.nan

    e, n = grid_km()
    EE, NN = np.meshgrid(e, n)
    out[np.hypot(EE, NN) > st.range_km] = np.nan
    return out.astype(np.float32)


def coverage_fraction(dbz_grid: np.ndarray) -> float:
    """สัดส่วนเซลล์ในรัศมีที่มีข้อมูล — ใช้เฝ้าดูว่าภาพต้นทางครบไหม"""
    e, n = grid_km()
    EE, NN = np.meshgrid(e, n)
    inside = np.hypot(EE, NN) <= GRID_HALF_KM
    return float(np.isfinite(dbz_grid[inside]).mean())


# ---------------------------------------------------------------- stack

def stack_from_files(paths, st: Station, pal_dbz: np.ndarray, processor,
                     agg: str = "mean"):
    """สร้าง (T, 241, 241) dBZ จากไฟล์ภาพเรียงตามเวลา (เก่า -> ใหม่)

    processor(img) -> res dict  (ส่ง refine.strip_background_refined + qc.apply_qc มา)
    คืน (stack, times, meta)
    """
    frames, times = [], []
    for p in paths:
        p = Path(p)
        img = Image.open(p).convert("RGB")
        res = processor(img)
        frames.append(to_grid(dbz_field(res, st, pal_dbz), st, agg=agg))
        times.append(time_from_name(p))
    return np.array(frames, np.float32), times, station_meta(st)


def time_from_name(path: Path) -> datetime:
    """PHS_20260902_1145Z.jpg -> datetime UTC"""
    stem = Path(path).stem.split("_")
    return datetime.strptime(stem[1] + stem[2], "%Y%m%d%H%MZ").replace(tzinfo=timezone.utc)


def check_regular(times, expected_min: float = 15.0, tol_min: float = 2.0):
    """pysteps สมมติว่าเฟรมห่างเท่ากัน ถ้าไม่จริงผลจะผิดโดยไม่มี error

    คืน (ok, รายการช่วงห่างเป็นนาที)
    """
    d = [(times[i + 1] - times[i]).total_seconds() / 60 for i in range(len(times) - 1)]
    return all(abs(x - expected_min) <= tol_min for x in d), d


def station_meta(st: Station) -> dict:
    """metadata dict สำหรับ pysteps — คีย์ครบตามที่ conversion/transformation ต้องใช้

    หน่วยตั้งต้นเป็น dBZ เพราะยังไม่ได้แปลง Z-R
    ถ้าจะเข้า pysteps ต่อ ให้ผ่าน dbz_to_rainrate() แล้ว dB_transform() ของท่อเดิม
    """
    half_m = GRID_HALF_KM * 1000.0
    return {
        "projection": f"+proj=aeqd +lat_0={st.lat} +lon_0={st.lon} +units=m +ellps=WGS84",
        "x1": -half_m, "y1": -half_m, "x2": half_m, "y2": half_m,
        "xpixelsize": KM_PER_PIXEL * 1000.0,
        "ypixelsize": KM_PER_PIXEL * 1000.0,
        "cartesian_unit": "m",
        "yorigin": "lower",
        "institution": f"Thai Meteorological Department ({st.code} radar, image-derived)",
        "product": "PPI 0.5 deg filtered ZH (single sweep)",
        "accutime": 15.0,
        "unit": "dBZ",
        "transform": None,
        "zerovalue": NO_ECHO_DBZ,
        "threshold": 11.98,        # 0.1 มม./ชม. ใน Rosenfeld tropical
        "zr_a": 250.0, "zr_b": 1.2,
        "kmperpixel": KM_PER_PIXEL,
        "timestep": 15.0,
    }


def save_stack(path, stack, times, meta):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path, dbz=stack.astype(np.float32),
        times=np.array([t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in times]),
        kmperpixel=KM_PER_PIXEL, grid_n=GRID_N,
        lat_0=float(meta["projection"].split("lat_0=")[1].split()[0]),
        lon_0=float(meta["projection"].split("lon_0=")[1].split()[0]),
    )
    return path


def load_stack(path):
    d = np.load(path, allow_pickle=False)
    times = [datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             for s in d["times"]]
    return d["dbz"], times
