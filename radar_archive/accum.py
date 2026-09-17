"""ฝนสะสมจากภาพสังเกตย้อนหลัง — Σ R(t_i) × Δt

    python -m radar_archive.accum --station PHS --hours 1
    python -m radar_archive.accum --station PHS --hours 1 --at 1789524000
    python -m radar_archive.accum --station PHS --hours 3 --at-latlon 17.87,100.09
    python -m radar_archive.accum --station PHS --hours 1 --npz out/accum.npz

ทำไมต้องมีโมดูลนี้
    ก่อนหน้านี้ทั้งระบบมี "ความเข้มฝน ณ ขณะหนึ่ง" (rain rate, มม./ชม.) จากเฟรมเดียว
    แต่ไม่เคยมี "ฝนสะสม" (มม.) เลย — ซึ่งเป็นคนละปริมาณกัน

    ผลคือหน้าเว็บเอา rain rate ของเฟรมล่าสุด (มม./ชม.) ไปลบกับ rain_1h ของสถานี
    วัดน้ำฝน (มม. สะสมหนึ่งชั่วโมง) แล้วเรียกผลลัพธ์ว่า "Bias" ซึ่งไม่ได้มีความหมาย
    อะไรเลย ตัวอย่างจริง 16 ก.ย. 2569: สถานีบ้านน้ำหินรายงาน 39.0 มม./ชม.
    เรดาร์ ณ วินาทีนั้นเห็น 0.2 มม./ชม. -> แสดง Bias -38.8
    ความจริงคือฝนตกหนักแล้ว "ผ่านไปแล้ว" ไม่ใช่เรดาร์ประเมินต่ำไป 38.8 มม.
    สังเกตแถวที่ฝนกำลังตกอยู่จริงในภาพเดียวกัน (อบต.ร้องกวาง 5.6 vs 6.8) Bias +1.2
    ถ้าเป็นความคลาดของการสอบเทียบจริง สัดส่วนต้องใกล้เคียงกันทุกแถว ไม่ใช่แบบนี้

    โมดูลนี้ให้ปริมาณที่เทียบกับ gauge ได้ตรงนิยาม และใช้ซ้ำได้อีกสองงาน
      1. gauges.py   -> radar_1h ต่อสถานี เอาไปเทียบกับ rain_1h ตรง ๆ
      2. ฝนสะสมเชิงพื้นที่รายตำบล/อำเภอ (ยังไม่ได้ทำ) -> ใช้ accumulate() ตัวเดียวกันนี้

นิยามที่ใช้ — เขียนไว้ให้อ้างในเปเปอร์ได้
    R_i    = rain rate จากเฟรมสังเกตที่เวลา t_i ผ่านสมการ Z-R ของ meta รอบนั้น
    Δt     = timestep_min / 60 ชั่วโมง (ปกติ 15 นาที = 0.25 ชม.)
    A(t)   = Σ R_i × Δt  สำหรับ t_i ที่อยู่ในช่วง (t − H, t]

    นี่คือ **left Riemann sum** บนเฟรมที่มีจริง สมมติว่าความเข้มคงที่ตลอด 15 นาที
    ที่เฟรมนั้นเป็นตัวแทน ซึ่งประเมินฝนพุ่งแรงช่วงสั้นต่ำกว่าความจริง
    (convective burst 5 นาทีจะถูกเฉลี่ยจนจาง) — ข้อจำกัดนี้มาจากคาบภาพต้นทาง
    15 นาที ไม่ใช่จากวิธี ถ้าวันหนึ่งได้ภาพถี่ขึ้น ตัวเลขจะดีขึ้นเองโดยไม่ต้องแก้โค้ด

ทิศทางแกน — จุดที่พลาดง่ายที่สุดในโปรเจกต์นี้
    กริดใน nowcast.py  แถว 0 = ใต้   (yorigin = lower)
    ภาพ PNG ที่เขียนออก แถว 0 = เหนือ (nowcast.py ทำ [::-1] ตอน render)
    โมดูลนี้อ่านจาก PNG จึงทำงานบน "แถว 0 = เหนือ" ตลอด เหมือน verify.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import CONFIG_PATH, get_station
from .verify import decode_dbz, store_dir

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

R_EARTH_KM = 6371.0088

# ค่าสำรองเมื่อ meta ของรอบนั้นไม่มีฟิลด์ — Marshall & Palmer (1948) J. Meteor. 5(4), 165-166
ZR_FALLBACK = (200.0, 1.6)
THR_FALLBACK = 16.5


# ---------------------------------------------------------------- 1. Z-R

def rain_rate(dbz: np.ndarray, zr_a: float, zr_b: float, thr: float) -> np.ndarray:
    """dBZ -> มม./ชม.  โดย Z = a R^b  =>  R = (Z/a)^(1/b)

    NaN (ไม่มี echo / นอกรัศมี) และค่าที่ต่ำกว่า thr คืนเป็น 0.0
    ไม่ใช่ NaN เพราะ "ไม่มีฝน" กับ "ไม่มีข้อมูล" ต้องรวมได้ทั้งคู่ในผลรวมสะสม
    การกรองว่าจุดไหนอยู่นอกรัศมีทำที่ขั้นสุ่มค่า (sample) ไม่ใช่ตรงนี้
    """
    d = np.where(np.isfinite(dbz), dbz, -np.inf)
    z = np.power(10.0, d / 10.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.power(z / zr_a, 1.0 / zr_b)
    return np.where(d >= thr, r, 0.0).astype(np.float32)


def zr_params(meta: dict) -> tuple[float, float, float]:
    """ดึง (a, b, threshold) จาก meta ของรอบนั้น — ไม่ hardcode

    ใช้ wet_threshold_effective_dbz ก่อนเสมอ เพราะภาพถูก quantise ลงแถบ palette
    แล้ว threshold ที่มีผลจริงคือ "ขอบแถบ" ไม่ใช่เลขที่ตั้งไว้
    (ตั้ง 11.98 กับตั้ง 16.5 ให้ผลเหมือนกันเป๊ะ เพราะตกในแถบ [11.3, 16.5) เดียวกัน)
    """
    zr = meta.get("zr")
    if isinstance(zr, (list, tuple)) and len(zr) == 2:
        a, b = float(zr[0]), float(zr[1])
    else:
        a, b = ZR_FALLBACK
    thr = meta.get("wet_threshold_effective_dbz")
    if thr is None:
        thr = meta.get("wet_threshold_dbz", THR_FALLBACK)
    return a, b, float(thr)


# ---------------------------------------------------------------- 2. อ่านคลัง

def list_origins(store: Path) -> list[int]:
    """epoch ของทุก origin ที่มี obs.png จริง เรียงจากเก่าไปใหม่"""
    out = []
    for d in Path(store).iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        if (d / "obs.png").exists():
            out.append(int(d.name))
    out.sort()
    return out


def load_meta(store: Path, epoch: int) -> dict | None:
    p = Path(store) / str(epoch) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_rate(store: Path, epoch: int) -> tuple[np.ndarray, dict] | None:
    """obs.png ของ origin นั้น -> (สนาม rain rate มม./ชม., meta)  แถว 0 = เหนือ

    คืน None เมื่ออ่านไม่ได้ ให้ตัวเรียกข้ามไป ไม่ใช่ล้มทั้งรอบ
    เฟรมเดียวหายไม่ควรทำให้ฝนสะสมทั้งชั่วโมงใช้ไม่ได้ — แต่ต้องนับว่าหายกี่เฟรม
    """
    meta = load_meta(store, epoch)
    if meta is None:
        return None
    png = Path(store) / str(epoch) / "obs.png"
    try:
        dbz = decode_dbz(png.read_bytes(), meta["levels_rgb"], meta["levels_dbz"])
    except Exception as e:
        print(f"    [!] อ่าน {png.name} ของ {epoch} ไม่ได้: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None
    a, b, thr = zr_params(meta)
    return rain_rate(dbz, a, b, thr), meta


# ---------------------------------------------------------------- 3. สะสม

def window_origins(origins: list[int], end_epoch: int, hours: float) -> list[int]:
    """origin ที่อยู่ในช่วง (end − H, end]  — ปลายเปิดข้างซ้าย ปิดข้างขวา

    ปลายเปิดข้างซ้ายสำคัญ: ถ้าใช้ [end−H, end] เฟรมที่ขอบจะถูกนับซ้ำเมื่อเอา
    หน้าต่างติดกันมาต่อกัน แล้วฝนสะสมรายวันจะเกินความจริงเป็นระบบ
    """
    lo = end_epoch - int(round(hours * 3600))
    return [t for t in origins if lo < t <= end_epoch]


def accumulate(store: Path, end_epoch: int, hours: float = 1.0,
               origins: list[int] | None = None,
               cache: dict | None = None) -> dict | None:
    """ฝนสะสม (มม.) ในช่วง (end − hours, end]  คืน dict หรือ None ถ้าไม่มีเฟรมเลย

    cache  dict epoch -> (rate_array, meta) ใช้ซ้ำข้ามการเรียกหลายครั้ง
           สำคัญมากตอนทำทีละสถานี: สถานีรายงานเป็นรายชั่วโมงและเวลาซ้ำกันเยอะ
           ถ้าไม่ cache จะ decode PNG เดิมซ้ำหลายร้อยรอบ

    คืน
        mm        ndarray (n, n) ฝนสะสม หน่วย มม.  แถว 0 = เหนือ
        frames    epoch ที่ใช้จริง เรียงจากเก่าไปใหม่
        n_used    จำนวนเฟรมที่ใช้
        n_expect  จำนวนเฟรมที่ "ควรจะมี" ตาม timestep
        step_min  คาบเวลาที่ใช้คิด Δt
        meta      meta ของเฟรมล่าสุดในหน้าต่าง (ใช้หาพิกัด/กริด)
        window    (lo, end) epoch
    """
    store = Path(store)
    if origins is None:
        origins = list_origins(store)
    picked = window_origins(origins, end_epoch, hours)
    if not picked:
        return None

    cache = cache if cache is not None else {}
    total = None
    used, last_meta, step_min = [], None, None

    for t in picked:
        if t not in cache:
            cache[t] = load_rate(store, t)
        got = cache[t]
        if got is None:
            continue
        rate, meta = got
        if total is None:
            total = np.zeros_like(rate, dtype=np.float32)
        elif rate.shape != total.shape:
            print(f"    [!] ข้าม {t}: ขนาดภาพ {rate.shape} ไม่ตรงกับ {total.shape}",
                  file=sys.stderr)
            continue
        step = float(meta.get("timestep_min") or 15.0)
        total += rate * np.float32(step / 60.0)
        used.append(t)
        last_meta, step_min = meta, step

    if total is None or not used:
        return None

    step_min = step_min or 15.0
    return dict(
        mm=total, frames=used, n_used=len(used),
        n_expect=int(round(hours * 60.0 / step_min)),
        step_min=step_min, meta=last_meta,
        window=(end_epoch - int(round(hours * 3600)), end_epoch),
    )


# ---------------------------------------------------------------- 4. พิกัด

def project_en(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """azimuthal equidistant ไปข้างหน้า — คืน (ตะวันออก, เหนือ) หน่วย กม.

    ตรงกับ +proj=aeqd ที่ระบุใน meta และตรงกับ project() ในหน้าเว็บทุกพจน์
    ถ้าแก้ตรงนี้ต้องแก้ที่หน้าเว็บด้วย ไม่งั้นหมุดบนแผนที่กับตัวเลขในตารางจะคนละจุด
    """
    p, l = math.radians(lat), math.radians(lon)
    p0, l0 = math.radians(lat0), math.radians(lon0)
    cos_c = (math.sin(p0) * math.sin(p)
             + math.cos(p0) * math.cos(p) * math.cos(l - l0))
    c = math.acos(max(-1.0, min(1.0, cos_c)))
    if c < 1e-9:
        return 0.0, 0.0
    k = c / math.sin(c)
    e = R_EARTH_KM * k * math.cos(p) * math.sin(l - l0)
    n = R_EARTH_KM * k * (math.cos(p0) * math.sin(p)
                          - math.sin(p0) * math.cos(p) * math.cos(l - l0))
    return e, n


def grid_geometry(meta: dict) -> tuple[int, float, float]:
    """(ขนาดกริด n, กม./pixel, ครึ่งความกว้าง กม.) — อ่านจาก meta ไม่ hardcode 241/2.0"""
    n = int(meta.get("grid", [241, 241])[0])
    kpp = float(meta.get("kmperpixel", 2.0))
    return n, kpp, (n - 1) * kpp / 2.0


def latlon_to_px(lat: float, lon: float, meta: dict,
                 lat0: float, lon0: float) -> tuple[int, int] | None:
    """lat/lon -> (col, row) ของภาพ PNG  แถว 0 = เหนือ  คืน None ถ้าตกนอกภาพ"""
    n, kpp, half = grid_geometry(meta)
    e, nn = project_en(lat, lon, lat0, lon0)
    col = int(round((e + half) / kpp))
    row = int(round((half - nn) / kpp))          # กลับแกนเหนือ-ใต้ให้ตรงกับ PNG
    if not (0 <= col < n and 0 <= row < n):
        return None
    return col, row


def sample(arr: np.ndarray, lat: float, lon: float, meta: dict,
           lat0: float, lon0: float, mode: str = "nearest") -> float | None:
    """สุ่มค่าจากสนาม ณ พิกัดสถานี — คืน None ถ้าอยู่นอกภาพ

    mode
      nearest  เซลล์เดียวที่ใกล้ที่สุด (~2x2 กม.)  <- ค่าเริ่มต้น ใช้ในเปเปอร์
               เป็นนิยามที่อธิบายได้ตรงที่สุด ไม่มีการเลือกที่ทำให้ตัวเลขดูดีขึ้น
      mean3    เฉลี่ย 3x3 (~6x6 กม.) ลดผลของความคลาดตำแหน่งสถานี
      max3     ค่าสูงสุด 3x3 — เอนเข้าหาค่าสูงอย่างเป็นระบบ **อย่าใช้ทำ validation**
               มีไว้เพื่อให้ตรงกับที่หน้าเว็บใช้ตอนแสดงผลเท่านั้น
    """
    px = latlon_to_px(lat, lon, meta, lat0, lon0)
    if px is None:
        return None
    col, row = px
    if mode == "nearest":
        return float(arr[row, col])

    n = arr.shape[0]
    r0, r1 = max(0, row - 1), min(n, row + 2)
    c0, c1 = max(0, col - 1), min(n, col + 2)
    block = arr[r0:r1, c0:c1]
    if block.size == 0:
        return None
    return float(block.max() if mode == "max3" else block.mean())


# ---------------------------------------------------------------- 5. เกณฑ์ฝน

def tmd_category_24h(mm: float | None) -> str:
    """เกณฑ์ปริมาณฝนของกรมอุตุนิยมวิทยา

    ⚠️ เกณฑ์ชุดนี้นิยามไว้สำหรับ **ฝนสะสม 24 ชั่วโมง** เท่านั้น
       อย่าเอาไปติดป้ายฝนสะสม 1 หรือ 3 ชั่วโมง — ฝน 35 มม. ใน 1 ชม. ไม่ใช่
       "ฝนหนัก" ตามเกณฑ์นี้ มันหนักกว่านั้นมาก และการติดป้ายผิดจะทำให้คนอ่าน
       เปเปอร์เอาไปใช้ผิดตาม ชื่อฟังก์ชันจึงมี _24h ติดไว้ให้สะดุดตา
    """
    if mm is None or not math.isfinite(mm) or mm < 0.1:
        return "ไม่มีฝน"
    if mm <= 10.0:
        return "ฝนเล็กน้อย"
    if mm <= 35.0:
        return "ฝนปานกลาง"
    if mm <= 90.0:
        return "ฝนหนัก"
    return "ฝนหนักมาก"


# ---------------------------------------------------------------- 6. CLI

def _utc(ep: int) -> str:
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="radar_archive.accum",
        description="ฝนสะสมจากภาพสังเกตย้อนหลัง  Σ R(t) × Δt")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--hours", type=float, default=1.0, help="ความยาวหน้าต่าง (ชม.)")
    p.add_argument("--at", type=int, default=None,
                   help="epoch ปลายหน้าต่าง (ค่าเริ่มต้น = origin ล่าสุดที่มี)")
    p.add_argument("--at-latlon", default=None,
                   help="สุ่มค่าที่พิกัดเดียว รูปแบบ lat,lon")
    p.add_argument("--sample", choices=("nearest", "mean3", "max3"), default="nearest")
    p.add_argument("--npz", default=None, help="เขียนผลเป็น .npz (คีย์ mm, frames)")
    a = p.parse_args(argv)

    st = get_station(a.station, a.config)
    store = store_dir(Path(a.data), st.code)
    if not store.exists():
        print(f"[!] ไม่พบคลัง {store}", file=sys.stderr)
        return 2

    origins = list_origins(store)
    if not origins:
        print(f"[!] คลัง {store} ยังไม่มี obs.png เลย", file=sys.stderr)
        return 2
    end = a.at if a.at else origins[-1]

    res = accumulate(store, end, a.hours, origins=origins)
    if res is None:
        print(f"[!] ไม่มีเฟรมในช่วง {a.hours} ชม. ก่อน {_utc(end)}", file=sys.stderr)
        return 1

    lo, hi = res["window"]
    print(f"=== {st.code} · ฝนสะสม {a.hours:g} ชม. ===")
    print(f"หน้าต่าง  ({_utc(lo)}, {_utc(hi)}]")
    print(f"เฟรม      ใช้ {res['n_used']} จาก {res['n_expect']} ที่ควรมี "
          f"· Δt = {res['step_min']:.0f} นาที")
    if res["n_used"] < res["n_expect"]:
        miss = res["n_expect"] - res["n_used"]
        print(f"          [!] ขาด {miss} เฟรม -> ค่าสะสมต่ำกว่าความจริง "
              f"ประมาณ {miss / res['n_expect'] * 100:.0f}%")

    a_zr, b_zr, thr = zr_params(res["meta"])
    print(f"Z-R       Z = {a_zr:g} R^{b_zr:g} · threshold {thr:g} dBZ")

    mm = res["mm"]
    wet = mm[mm >= 0.1]
    print(f"\nทั้งภาพ   สูงสุด {float(mm.max()):.1f} มม. "
          f"· เซลล์ที่มีฝน {wet.size:,} จาก {mm.size:,} "
          f"({wet.size / mm.size * 100:.1f}%)")
    if wet.size:
        print(f"          เฉพาะเซลล์ที่มีฝน: เฉลี่ย {float(wet.mean()):.1f} มม. "
              f"· มัธยฐาน {float(np.median(wet)):.1f} มม.")

    if a.at_latlon:
        try:
            lat, lon = (float(x) for x in a.at_latlon.split(","))
        except ValueError:
            print("[!] --at-latlon ต้องเป็น lat,lon", file=sys.stderr)
            return 2
        v = sample(mm, lat, lon, res["meta"], st.lat, st.lon, a.sample)
        if v is None:
            print(f"\nที่ ({lat}, {lon}) อยู่นอกภาพเรดาร์")
        else:
            print(f"\nที่ ({lat}, {lon}) [{a.sample}] = {v:.1f} มม.")
            if a.hours >= 24.0:
                print(f"          เกณฑ์กรมอุตุ: {tmd_category_24h(v)}")

    if a.npz:
        out = Path(a.npz)
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out, mm=mm, frames=np.array(res["frames"]),
                            window=np.array(res["window"]), hours=a.hours)
        print(f"\n-> {out}  ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
