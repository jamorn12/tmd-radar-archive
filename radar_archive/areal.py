"""ฝนเชิงพื้นที่รายเขตปกครอง — รวมค่าจากกริดเรดาร์ลงอำเภอ/ตำบล

    python -m radar_archive.areal --station PHS --level amphoe
    python -m radar_archive.areal --station PHS --level tambon --kind observed --hours 3
    python -m radar_archive.areal --station PHS --level all --csv
    python -m radar_archive.areal --station PHS --rebuild-masks    # บังคับสร้าง mask ใหม่

ทำไมต้องมีโมดูลนี้
    accum.py ให้ฝนสะสมเป็นกริด 241x241 ซึ่งตอบว่า "ตรงนี้ฝนเท่าไร" ได้
    แต่คนที่ต้องตัดสินใจจริงถามเป็นชื่อพื้นที่ — "อำเภอลาดยาวจะโดนไหม กี่โมง"
    โมดูลนี้แปลงจากกริดเป็นคำตอบระดับเขตปกครอง

สามตัวชี้วัด — ทำทั้งสามโดยตั้งใจ ไม่ใช่เลือกอันเดียว
    1. mean_mm     ฝนสะสมเฉลี่ยทั้งเขต (มม.)
                   คุ้นตาที่สุด แต่**อิงค่า มม. สัมบูรณ์** ซึ่งเรารู้แล้วว่ามีปัญหา
                   (เทียบกับสถานีวัดน้ำฝนได้อัตราส่วนเพียง 0.084 ดู gauge_radar_comparison)
                   ใช้เพื่อเปรียบเทียบระหว่างพื้นที่ ไม่ควรอ้างเป็นปริมาณจริง
    2. coverage    สัดส่วนพื้นที่ในเขตที่จะมีฝน (0-1)
                   **ไม่ขึ้นกับสมการ Z-R เลย** เพราะเป็นการนับเซลล์ที่ผ่าน threshold
                   ไม่ได้แปลง dBZ เป็น มม. จึงเป็นตัวที่แข็งแรงที่สุดในสามตัว
    3. eta_area    อีกกี่นาทีฝนจะครอบคลุมพื้นที่ถึงเกณฑ์ (ค่าเริ่มต้น 10%)
                   ใช้สนามลมรายจุดจาก motion grid

⚠️ เรื่องนิยามของ ETA — จุดที่พลาดง่ายและพลาดแล้วอันตราย
    นิยามที่ง่ายที่สุดคือ "เซลล์แรกในเขตที่มีฝน" แต่เขตหนึ่งมีเซลล์เฉลี่ย 139 เซลล์
    เซลล์เดียวเข้าเกณฑ์ก็ทำให้ทั้งอำเภอถูกประกาศว่า "ฝนมาใน 15 นาที" ได้
    ซึ่งจะกลายเป็น false alarm จำนวนมากเมื่อเอาไปใช้เตือนภัยจริง
    ค่าหลักจึงเป็น eta_area (ต้องครอบคลุมถึงเกณฑ์) ส่วน eta_first เก็บไว้เป็นค่ารอง
    ให้เปรียบเทียบได้ว่านิยามต่างกันให้ผลต่างกันแค่ไหน — เป็นผลที่รายงานได้ในเปเปอร์

⚠️ ขนาดของเขตมีผลต่อความน่าเชื่อถือ
    อำเภอ มัธยฐาน 139 เซลล์  -> ค่าเฉลี่ยนิ่ง
    ตำบล  มัธยฐาน  16 เซลล์  -> แกว่งกว่ามาก และ 13.2% มีน้อยกว่า 5 เซลล์
    ทุกแถวจึงมี n_cells ติดไปด้วยเสมอ ห้ามตัดคอลัมน์นี้ทิ้งตอนทำตาราง
    เพราะค่าจากเขตที่มี 2 เซลล์กับ 200 เซลล์ไม่ควรถูกอ่านเท่ากัน
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import accum
from .config import CONFIG_PATH, get_station
from .grid import grid_latlon
from .verify import decode_dbz, store_dir

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

# ⚠️ กับดัก: มีไฟล์ชื่อ provinces_240km.geojson อยู่สองที่ และ **เนื้อหาคนละอย่าง**
#     ./provinces_240km.geojson       244 feature = อำเภอ (ชื่อไฟล์ผิด)
#     docs/provinces_240km.geojson     29 feature = จังหวัดจริง
#     docs/districts_240km.geojson    244 feature = อำเภอ (ชุดเดียวกับ root เป๊ะ 244/244)
#
# โมดูลนี้อ่านจาก docs/ ทั้งหมดโดยตั้งใจ เพราะเป็นไฟล์ที่หน้าเว็บ fetch ไปวาดจริง
# ถ้าอ่านคนละไฟล์กับหน้าเว็บ ขอบเขตที่คำนวณกับขอบเขตที่ผู้ใช้เห็นจะไม่ตรงกัน
# ซึ่งเป็นความผิดพลาดที่มองไม่เห็นจนกว่าจะมีคนเอาไปเทียบกัน
LEVELS = {
    "province": ("provinces_240km.geojson", "จังหวัด"),
    "amphoe": ("districts_240km.geojson", "อำเภอ"),
    "tambon": ("subdistricts_240km.geojson", "ตำบล"),
}
# ชื่อจังหวัดในไฟล์เป็นภาษาอังกฤษ ปล่อยไว้อย่างนั้น
# หน้าเว็บมี PROV_THAI_MAP แปลอยู่แล้ว ทำที่เดียวพอ อย่าทำสองที่ให้ไม่ตรงกัน

WET_MM_PER_FRAME = 0.02      # มม. ต่อเฟรมที่ถือว่า "เซลล์นี้มีฝน" ตอนนับ coverage
DEFAULT_ETA_COVER = 0.10     # ต้องครอบคลุมพื้นที่เท่านี้ถึงนับว่า "ฝนมาถึงเขตนี้"

# จำนวนเซลล์ขั้นต่ำที่ถือว่า coverage เป็น "การประมาณ" ไม่ใช่ "ตัวอย่างเดียว"
#
# เจอจากการทดสอบจริง: เรียงตำบลตาม coverage แล้ว 10 อันดับแรกเป็นตำบลที่มี 1-4 เซลล์
# ทั้งหมด เพราะเซลล์เดียวที่มีฝน = coverage 100% โดยอัตโนมัติ
# การเรียงแบบนั้นจึงดันเขตที่เชื่อถือได้น้อยที่สุดขึ้นบนสุดพอดี ซึ่งตรงข้ามกับที่ควรเป็น
# ค่ายังถูกคำนวณและเก็บลง CSV ครบ แต่ถูกทำเครื่องหมาย reliable=False
MIN_CELLS_RELIABLE = 5


# ---------------------------------------------------------------- 1. mask

def _points_in_ring(px: np.ndarray, py: np.ndarray,
                    rx: np.ndarray, ry: np.ndarray) -> np.ndarray:
    """จุดไหนอยู่ในรูปปิด — ray casting เวกเตอร์บนจุดทั้งชุดพร้อมกัน

    ทำไมไม่ใช้ matplotlib.path.Path.contains_points
        matplotlib **ไม่ได้อยู่ใน requirements.txt** มันติดมากับ pysteps เฉย ๆ
        การพึ่งของที่มาโดยบังเอิญแบบนั้นคือจุดเปราะ วันไหน pysteps เปลี่ยน
        dependency ขั้นตอนนี้จะล้มเงียบ ๆ เพราะตั้ง continue-on-error ไว้
        โปรเจกต์นี้เคยถอด scikit-image ออกด้วยเหตุผลเดียวกันมาแล้ว (ดู requirements.txt)

    ตรวจแล้วว่าให้ผลเท่ากับ matplotlib ทุกเซลล์
        อำเภอ 44,973 เซลล์ · ตำบล 43,022 · จังหวัด 48,808 -> ต่างกัน 0 เซลล์ทั้งหมด
        ความเร็วพอกัน (ตำบลทั้งชุด 0.26 วิ เทียบกับ 0.31 วิ)
    """
    inside = np.zeros(px.shape, dtype=bool)
    n = len(rx)
    j = n - 1
    for i in range(n):
        yi, yj = ry[i], ry[j]
        cond = (yi > py) != (yj > py)
        if cond.any():
            # cond เป็นจริงได้ก็ต่อเมื่อ yi != yj อยู่แล้ว จึงไม่มีทางหารด้วยศูนย์
            xint = rx[i] + (py - yi) * (rx[j] - rx[i]) / (yj - yi)
            inside ^= cond & (px < xint)
        j = i
    return inside


def _rings(geom: dict) -> list:
    """คืน list ของวงรอบนอก รองรับทั้ง Polygon และ MultiPolygon"""
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def build_masks(geojson_path: Path, meta: dict, st) -> dict:
    """หาว่าเซลล์กริดไหนตกอยู่ในเขตไหน — คืน dict พร้อมชื่อและ index แบน

    งานนี้หนัก (ตำบล 1,790 รูป) แต่ผลไม่เปลี่ยนตราบใดที่กริดกับไฟล์ขอบเขตเท่าเดิม
    จึงคำนวณครั้งเดียวแล้ว cache ลงดิสก์ ไม่ใช่ทำใหม่ทุกรอบ pipeline ทุก 15 นาที

    index ที่เก็บเป็นตำแหน่งในอาร์เรย์ที่ **แถว 0 = เหนือ** ให้ตรงกับภาพ PNG
    ที่ accum.py คืนมา ไม่ใช่กริดดิบของ nowcast ที่แถว 0 = ใต้
    """
    lat, lon = grid_latlon(st)              # แถว 0 = ใต้
    lat, lon = lat[::-1], lon[::-1]         # พลิกให้ตรงกับ PNG
    n = lat.shape[0]
    pts = np.column_stack([lon.ravel(), lat.ravel()])

    doc = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
    names, idx_list = [], []
    for f in doc["features"]:
        mask = np.zeros(n * n, dtype=bool)
        for ring in _rings(f["geometry"]):
            arr = np.asarray(ring, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[0] < 3:
                continue
            lo, hi = arr.min(0), arr.max(0)
            # กรองด้วยกรอบสี่เหลี่ยมก่อน — contains_points แพงกว่ามาก
            pre = ((pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0])
                   & (pts[:, 1] >= lo[1]) & (pts[:, 1] <= hi[1]))
            if not pre.any():
                continue
            mask[pre] |= _points_in_ring(pts[pre, 0], pts[pre, 1], arr[:, 0], arr[:, 1])
        names.append(f["properties"].get("name") or f["properties"].get("id") or "?")
        idx_list.append(np.flatnonzero(mask).astype(np.int32))
    return {"names": names, "idx": idx_list, "grid_n": n}


def _cache_path(data_root: Path, code: str, level: str, n: int) -> Path:
    return Path(data_root) / "areal" / f"{code}_{level}_mask_{n}.npz"


def load_masks(data_root: Path, code: str, level: str, meta: dict, st,
               rebuild: bool = False, verbose: bool = True) -> dict:
    n = int(meta.get("grid", [241, 241])[0])
    p = _cache_path(data_root, code, level, n)
    if p.exists() and not rebuild:
        z = np.load(p, allow_pickle=True)
        return {"names": list(z["names"]), "grid_n": int(z["grid_n"]),
                "idx": [z[f"i{k}"] for k in range(len(z["names"]))]}

    gj = DOCS / LEVELS[level][0]      # docs/ ไม่ใช่ root — ดูหมายเหตุที่ LEVELS
    if not gj.exists():
        raise SystemExit(f"[!] ไม่พบไฟล์ขอบเขต {gj}")
    if verbose:
        print(f"    สร้าง mask ใหม่จาก {gj.name} (ทำครั้งเดียวแล้ว cache ไว้)...", flush=True)
    t0 = time.time()
    m = build_masks(gj, meta, st)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, names=np.array(m["names"], dtype=object),
                        grid_n=m["grid_n"],
                        **{f"i{k}": v for k, v in enumerate(m["idx"])})
    if verbose:
        print(f"    -> {p.name}  ({time.time()-t0:.1f} วินาที · {len(m['names'])} เขต)")
    return m


# ---------------------------------------------------------------- 2. สนามฝน

def forecast_stack(store: Path, epoch: int, max_lead: int = 120) -> tuple | None:
    """อ่านเฟรมพยากรณ์ของ origin นั้น คืน ([(lead, rate)], meta)"""
    meta = accum.load_meta(store, epoch)
    if meta is None:
        return None
    a, b, thr = accum.zr_params(meta)
    step = int(round(float(meta.get("timestep_min") or 15.0)))
    out = []
    for lead in range(step, max_lead + 1, step):
        p = Path(store) / str(epoch) / f"f+{lead:03d}.png"
        if not p.exists():
            continue
        try:
            dbz = decode_dbz(p.read_bytes(), meta["levels_rgb"], meta["levels_dbz"])
        except Exception:
            continue
        out.append((lead, accum.rain_rate(dbz, a, b, thr)))
    return (out, meta) if out else None


def observed_stack(store: Path, epoch: int, hours: float,
                   origins: list | None = None) -> tuple | None:
    """อ่านเฟรมสังเกตย้อนหลัง คืน ([(นาทีย้อนหลัง, rate)], meta) — เรียงเก่าไปใหม่"""
    if origins is None:
        origins = accum.list_origins(store)
    picked = accum.window_origins(origins, epoch, hours)
    out, meta = [], None
    for t in picked:
        got = accum.load_rate(store, t)
        if got is None:
            continue
        rate, meta = got
        out.append((int((t - epoch) / 60), rate))
    return (out, meta) if out else None


# ---------------------------------------------------------------- 3. รวมค่า

def aggregate(stack: list, meta: dict, masks: dict,
              eta_cover: float = DEFAULT_ETA_COVER) -> list[dict]:
    """รวมค่าลงเขตปกครอง — คืน list ของ dict หนึ่งตัวต่อหนึ่งเขต

    stack  [(lead_min, rate_2d)] เรียงตามเวลา
    """
    step_h = float(meta.get("timestep_min") or 15.0) / 60.0
    n = int(meta.get("grid", [241, 241])[0])

    leads = [L for L, _ in stack]
    flat = np.stack([r.ravel() for _, r in stack])          # (T, n*n)
    acc = (flat * step_h).sum(axis=0)                        # มม. สะสมรวม
    wet = flat * step_h >= WET_MM_PER_FRAME                  # (T, n*n) มีฝนไหมในเฟรมนั้น
    wet_any = wet.any(axis=0)

    rows = []
    for name, idx in zip(masks["names"], masks["idx"]):
        nc = int(idx.size)
        if nc == 0:
            rows.append(dict(name=name, n_cells=0, mean_mm=None, max_mm=None,
                             coverage=None, eta_area=None, eta_first=None,
                             reliable=False))
            continue
        a = acc[idx]
        cov = float(wet_any[idx].mean())

        # ETA สองนิยาม — ดูหมายเหตุหัวไฟล์ว่าทำไมค่าหลักไม่ใช่ eta_first
        eta_area = eta_first = None
        seen = np.zeros(nc, dtype=bool)
        for k, L in enumerate(leads):
            seen |= wet[k][idx]
            frac = seen.mean()
            if eta_first is None and frac > 0:
                eta_first = int(L)
            if eta_area is None and frac >= eta_cover:
                eta_area = int(L)
            if eta_area is not None and eta_first is not None:
                break

        rows.append(dict(
            name=name, n_cells=nc,
            mean_mm=round(float(a.mean()), 3),
            max_mm=round(float(a.max()), 3),
            coverage=round(cov, 4),
            eta_area=eta_area, eta_first=eta_first,
            reliable=nc >= MIN_CELLS_RELIABLE,
        ))
    return rows


# ---------------------------------------------------------------- 4. เขียนผล

def write_web(docs_root: Path, code: str, level: str, rows: list,
              meta: dict, epoch: int, kind: str, eta_cover: float) -> Path:
    """เขียน docs/areal/<CODE>_<level>.json — เก็บเฉพาะเขตที่มีฝน ลดขนาดไฟล์

    เขตที่แห้งไม่ต้องส่งไปหน้าเว็บ ที่ตำบล 1,790 เขต วันที่ฝนตกไม่กี่ที่
    การส่งครบทุกเขตคือการส่ง null เปล่า ๆ เป็นพันแถว
    """
    hit = [r for r in rows if r["n_cells"] > 0 and (r["coverage"] or 0) > 0]
    hit.sort(key=lambda r: (not r["reliable"], -(r["coverage"] or 0)))
    out = Path(docs_root) / "areal"
    out.mkdir(parents=True, exist_ok=True)
    fields = ["name", "n_cells", "mean_mm", "max_mm", "coverage",
              "eta_area", "eta_first", "reliable"]
    doc = {
        "generated": int(datetime.now(timezone.utc).timestamp()),
        "station": code, "level": level, "level_th": LEVELS[level][1],
        "kind": kind, "origin": epoch,
        "base_time_utc": meta.get("base_time_utc"),
        "zr": meta.get("zr"),
        "wet_threshold_dbz": meta.get("wet_threshold_effective_dbz",
                                      meta.get("wet_threshold_dbz")),
        "eta_cover": eta_cover,
        "n_units_total": len(rows),
        "n_units_wet": len(hit),
        "note_th": ("coverage = สัดส่วนพื้นที่ที่จะมีฝน ไม่ขึ้นกับสมการ Z-R · "
                    "mean_mm อิงค่าสัมบูรณ์ซึ่งประเมินต่ำกว่าจริง ใช้เทียบระหว่างพื้นที่เท่านั้น · "
                    f"eta_area = นาทีที่ฝนครอบคลุมถึง {eta_cover*100:.0f}% ของเขต "
                    "(eta_first = เซลล์แรกที่มีฝน ซึ่งไวเกินไปสำหรับการเตือนภัย) · "
                    f"reliable=false คือเขตที่มีน้อยกว่า {MIN_CELLS_RELIABLE} เซลล์ "
                    "coverage ของเขตพวกนี้มาจากตัวอย่างน้อยเกินไป ควรแสดงแบบจางหรือซ่อน"),
        "fields": fields,
        "units": [[r[k] for k in fields] for r in hit],
    }
    p = out / f"{code}_{level}.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                 encoding="utf-8")
    return p


def write_csv(data_root: Path, code: str, level: str, rows: list,
              epoch: int, kind: str) -> Path:
    """เขียนทุกเขตลง CSV รวมเขตที่แห้ง — ตารางในเปเปอร์ต้องนับเขตที่ไม่มีฝนด้วย
    ไม่งั้น base rate จะผิด และตัวเลข skill จะเทียบกันไม่ได้"""
    out = Path(data_root) / "areal"
    out.mkdir(parents=True, exist_ok=True)
    # แยกไฟล์รายวัน (วันตาม UTC ของ origin) — GitHub ปฏิเสธการ push ไฟล์ที่ใหญ่เกิน 100 MB
    # ไฟล์ตำบลแบบรวมไฟล์เดียวโตวันละ ~13 MB และชนเพดานเมื่อ 25 ก.ย. 2569 06:00 น.
    # ทำให้ทุกรอบหลังจากนั้น push ไม่ผ่าน · ไฟล์เดิม <code>_<level>_<kind>.csv
    # เก็บไว้ตามเดิมเป็นข้อมูลช่วง 17–25 ก.ย. และไม่ถูกเขียนต่ออีก
    day = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d")
    p = out / f"{code}_{level}_{kind}_{day}.csv"
    cols = ["origin", "utc", "name", "n_cells", "mean_mm", "max_mm",
            "coverage", "eta_area", "eta_first", "reliable"]
    utc = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    exists = p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({"origin": epoch, "utc": utc,
                        **{k: ("" if r.get(k) is None else r[k]) for k in cols[2:]}})
    return p


# ---------------------------------------------------------------- 5. CLI

def run_level(level: str, a, st, store: Path, epoch: int) -> None:
    if a.kind == "forecast":
        got = forecast_stack(store, epoch, a.max_lead)
        what = f"พยากรณ์ถึง {a.max_lead} นาที"
    else:
        got = observed_stack(store, epoch, a.hours)
        what = f"สังเกตย้อนหลัง {a.hours:g} ชม."
    if got is None:
        print(f"[!] ไม่มีเฟรมให้รวมสำหรับ {level}", file=sys.stderr)
        return
    stack, meta = got

    masks = load_masks(Path(a.data), st.code, level, meta, st,
                       rebuild=a.rebuild_masks, verbose=not a.quiet)
    rows = aggregate(stack, meta, masks, a.eta_cover)

    nz = [r for r in rows if r["n_cells"] > 0]
    hit = [r for r in nz if (r["coverage"] or 0) > 0]
    med = int(np.median([r["n_cells"] for r in nz])) if nz else 0
    small = sum(1 for r in nz if r["n_cells"] < 5)
    print(f"\n=== {LEVELS[level][1]} · {what} · {len(stack)} เฟรม ===")
    print(f"เขตในโดม {len(nz)}/{len(rows)} · เซลล์ต่อเขต มัธยฐาน {med} "
          f"· เขตที่มีน้อยกว่า 5 เซลล์ {small} ({small/max(1,len(nz))*100:.1f}%)")
    print(f"เขตที่จะมีฝน {len(hit)}")

    if hit:
        print(f"\n{'เขต':<20}{'สะสม':>8}{'สูงสุด':>9}{'พื้นที่':>9}{'ETA':>7}{'เซลล์แรก':>10}{'เซลล์':>7}")
        show = [r for r in hit if r["reliable"]]
        weak = len(hit) - len(show)
        if weak:
            print(f"  (ซ่อน {weak} เขตที่มีน้อยกว่า {MIN_CELLS_RELIABLE} เซลล์ "
                  f"— coverage จากเซลล์เดียวไม่ใช่การประมาณ · ยังอยู่ครบใน CSV)")
        for r in sorted(show, key=lambda x: -x["coverage"])[:10]:
            ea = "—" if r["eta_area"] is None else f"{r['eta_area']}น."
            ef = "—" if r["eta_first"] is None else f"{r['eta_first']}น."
            print(f"{r['name'][:19]:<20}{r['mean_mm']:8.2f}{r['max_mm']:9.2f}"
                  f"{r['coverage']*100:8.0f}%{ea:>7}{ef:>10}{r['n_cells']:7d}")
        # นิยาม ETA ต่างกันแค่ไหน — ตัวเลขนี้ควรอยู่ในเปเปอร์
        both = [r for r in show if r["eta_area"] is not None and r["eta_first"] is not None]
        if both:
            d = [r["eta_area"] - r["eta_first"] for r in both]
            early = sum(1 for x in d if x > 0)
            print(f"\nนิยาม ETA: eta_first เตือนเร็วกว่า eta_area ใน {early}/{len(both)} เขต "
                  f"· เฉลี่ยเร็วกว่า {np.mean(d):.0f} นาที")
            print("  -> eta_first ไวกว่าจริง แต่ไวเพราะเซลล์เดียวก็ทำให้ทั้งเขตติดสัญญาณ")

    if a.dry_run:
        print("\n--dry-run: ไม่เขียนไฟล์")
        return
    web = write_web(Path(a.docs), st.code, level, rows, meta, epoch, a.kind, a.eta_cover)
    print(f"\n-> {web}  ({web.stat().st_size/1024:.1f} KB · {len([r for r in rows if (r['coverage'] or 0)>0])} เขต)")
    if a.csv:
        c = write_csv(Path(a.data), st.code, level, rows, epoch, a.kind)
        print(f"-> {c}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="radar_archive.areal",
        description="รวมฝนจากกริดเรดาร์ลงเขตปกครอง (อำเภอ/ตำบล)")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--docs", default=str(DOCS))
    p.add_argument("--level", choices=("province", "amphoe", "tambon", "all"),
                   default="all")
    p.add_argument("--kind", choices=("forecast", "observed"), default="forecast")
    p.add_argument("--max-lead", type=int, default=120, help="forecast: ถึงกี่นาที")
    p.add_argument("--hours", type=float, default=3.0, help="observed: ย้อนหลังกี่ชั่วโมง")
    p.add_argument("--at", type=int, default=None, help="epoch ของ origin (ว่าง = ล่าสุด)")
    p.add_argument("--eta-cover", type=float, default=DEFAULT_ETA_COVER,
                   help="สัดส่วนพื้นที่ที่ถือว่า 'ฝนมาถึงเขตนี้' (ค่าเริ่มต้น 0.10)")
    p.add_argument("--csv", action="store_true", help="เขียนต่อท้าย CSV สำหรับเปเปอร์")
    p.add_argument("--rebuild-masks", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    st = get_station(a.station, a.config)
    store = store_dir(Path(a.data), st.code)
    if not store.exists():
        print(f"[!] ไม่พบคลัง {store}", file=sys.stderr)
        return 2
    origins = accum.list_origins(store)
    if not origins:
        print(f"[!] คลัง {store} ยังไม่มีข้อมูล", file=sys.stderr)
        return 2
    epoch = a.at or origins[-1]
    print(f"=== {st.code} · origin {epoch} "
          f"({datetime.fromtimestamp(epoch, timezone.utc):%Y-%m-%d %H:%M UTC}) ===")

    for lv in (("province", "amphoe", "tambon") if a.level == "all" else (a.level,)):
        run_level(lv, a, st, store, epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
