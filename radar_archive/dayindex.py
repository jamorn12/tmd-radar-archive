"""ดัชนีรายวันสำหรับหน้า "คลังภาพ" (docs/archive.html)

    python -m radar_archive.dayindex --station PHS              # วันนี้ + เมื่อวาน (ใช้ใน archive.yml ทุกรอบ)
    python -m radar_archive.dayindex --station PHS --all        # สร้างใหม่ทุกวันที่มีในคลัง (ครั้งแรก)
    python -m radar_archive.dayindex --station PHS --day 2026-09-20

ทำไมต้องมีไฟล์นี้
    หน้าคลังภาพเปิดภาพจาก data/ ผ่าน raw.githubusercontent.com โดยตรง (ไม่คัดลอกไฟล์ซ้ำ)
    แต่หน้าเว็บไม่รู้ว่าวันหนึ่งมีรอบไหนบ้าง และไม่ควรต้องถอดภาพเองเพื่อหาพื้นที่ฝน
    โมดูลนี้สรุปทุกอย่างที่หน้าเว็บต้องใช้ลงไฟล์ JSON เล็ก ๆ ใน docs/archive/<CODE>/

ไฟล์ที่เขียน
    docs/archive/<CODE>/<YYYY-MM-DD>.json   หนึ่งไฟล์ต่อวัน (เวลาไทย) — รายการรอบ 96 ช่อง + สรุปของวัน
    docs/archive/<CODE>/calendar.json       รายการวันที่มีข้อมูล + ข้อมูลสถานี (palette, กรอบภาพต้นทาง)

ที่มาของแต่ละค่า (ไม่มีค่าที่คำนวณใหม่แบบต่างจากระบบ)
    obs / fc          มีไฟล์ data/nowcast/<CODE>/<origin>/obs.png, f+NNN.png หรือไม่
    wet_km2, max_dbz  ถอด obs.png ด้วย verify.decode_dbz · เกณฑ์ wet_threshold_effective_dbz ของรอบนั้น · ช่องละ 4 กม²
    motion            meta.json ของรอบนั้น (kmh, bearing = ทิศที่เคลื่อนไป, confidence, engine)
    raw, ocr, rfi     data/log/<CODE>_index.csv (บันทึกการรับภาพ) — raw_file, timestamp_source, qc_spike_px, qc_removed_pct
    score30           f+030 ของรอบ t เทียบ obs ของรอบ t+30 นาที ทั้งโดม · persistence = obs ของรอบ t
                      (ตรรกะเดียวกับ verify.py: เกณฑ์ effective, นับเฉพาะช่องที่มีข้อมูลทั้งสองฝั่ง = ทั้งกริด)
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .config import CONFIG_PATH, get_station
from .verify import decode_dbz

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
TZ = timezone(timedelta(hours=7))
SLOT_S = 900
LEADS = (15, 30, 45, 60, 75, 90, 105, 120)
CELL_KM2 = 4.0


# ---------------------------------------------------------------- อ่านคลัง
def store(data: Path, code: str) -> Path:
    return Path(data) / "nowcast" / code


def list_origins(data: Path, code: str) -> list[int]:
    s = store(data, code)
    if not s.exists():
        return []
    return sorted(int(p.name) for p in s.iterdir() if p.name.isdigit() and (p / "obs.png").exists())


def load_meta(data: Path, code: str, epoch: int) -> dict | None:
    p = store(data, code) / str(epoch) / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _thr(meta: dict) -> float:
    return float(meta.get("wet_threshold_effective_dbz", meta.get("wet_threshold_dbz", 16.5)))


def _wet(png: Path, meta: dict) -> np.ndarray | None:
    if not png.exists():
        return None
    a = decode_dbz(png.read_bytes(), meta["levels_rgb"], meta["levels_dbz"])
    return a


def ingest_log(data: Path, code: str) -> dict[int, dict]:
    """{slot epoch (UTC): แถวบันทึกการรับภาพ}  — slot = เวลาสแกนปัดลงทีละ 15 นาที"""
    p = Path(data) / "log" / f"{code}_index.csv"
    out = {}
    if not p.exists():
        return out
    with p.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                t = datetime.strptime(r["timestamp_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            e = int(t.timestamp()) // SLOT_S * SLOT_S
            out[e] = r
    return out


# ---------------------------------------------------------------- คำนวณ
def _score(f: np.ndarray, o: np.ndarray, p: np.ndarray, thr: float) -> dict:
    F, O, P = (np.nan_to_num(x, nan=-99.0) >= thr for x in (f, o, p))
    H, M, Fa = int((F & O).sum()), int((~F & O).sum()), int((F & ~O).sum())
    Hp, Mp, Fp = int((P & O).sum()), int((~P & O).sum()), int((P & ~O).sum())
    return dict(csi=round(H / max(H + M + Fa, 1), 3), csip=round(Hp / max(Hp + Mp + Fp, 1), 3))


def build_day(day: str, code: str, data: Path = DATA, origins: list[int] | None = None,
              log: dict | None = None) -> dict | None:
    """สรุปหนึ่งวัน (เวลาไทย 00:00–23:45) — คืน None ถ้าวันนั้นไม่มีทั้งรอบพยากรณ์และภาพต้นทาง"""
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    lo = int(d0.timestamp())
    slots = [lo + k * SLOT_S for k in range(96)]
    origins = list_origins(data, code) if origins is None else origins
    have = set(origins)
    log = ingest_log(data, code) if log is None else log
    if not any(t in have or t in log for t in slots):
        return None
    sdir = store(data, code)
    cycles, cache = [], {}

    def obs(t):
        if t not in cache:
            m = load_meta(data, code, t) if t in have else None
            cache[t] = (_wet(sdir / str(t) / "obs.png", m), m) if m else (None, None)
        return cache[t]

    for t in slots:
        r = log.get(t)
        c = dict(t=t, hhmm=datetime.fromtimestamp(t, TZ).strftime("%H:%M"), obs=t in have, fc=[], raw=None)
        if r:
            c.update(raw=r.get("raw_file") or None, ocr=r.get("timestamp_source") or None,
                     rfi_px=int(float(r.get("qc_spike_px") or 0)),
                     qc_removed_pct=round(float(r.get("qc_removed_pct") or 0), 2))
        if t in have:
            a, m = obs(t)
            thr = _thr(m)
            w = np.nan_to_num(a, nan=-99.0) >= thr
            mo = m.get("motion") or {}
            c.update(fc=[L for L in LEADS if (sdir / str(t) / f"f+{L:03d}.png").exists()],
                     wet_km2=int(w.sum() * CELL_KM2),
                     max_dbz=float(np.nanmax(a)) if np.isfinite(a).any() else None,
                     motion=dict(kmh=mo.get("kmh"), bearing=mo.get("bearing"),
                                 confidence=mo.get("confidence"), engine=mo.get("engine")))
            v = t + 30 * 60
            if 30 in c["fc"] and v in have:
                f = _wet(sdir / str(t) / "f+030.png", m)
                o, _ = obs(v)
                if f is not None and o is not None:
                    c["score30"] = _score(f, o, a, thr)
        cycles.append(c)

    present = [c for c in cycles if c["obs"]]
    gaps, s = [], None
    for c in cycles:
        if not c["obs"]:
            s = c["t"] if s is None else s
        elif s is not None:
            gaps.append([s, c["t"]]); s = None
    if s is not None:
        gaps.append([s, slots[-1] + SLOT_S])
    peak = max(present, key=lambda c: c["wet_km2"], default=None)
    sc = [c["score30"] for c in present if "score30" in c]
    summary = dict(
        n_cycles=len(present), n_slots=96, n_raw=sum(1 for c in cycles if c["raw"]),
        gaps=gaps, rfi_images=sum(1 for c in cycles if c.get("rfi_px", 0) > 0),
        peak=dict(t=peak["t"], km2=peak["wet_km2"]) if peak else None,
        max_dbz=max((c["max_dbz"] for c in present if c.get("max_dbz") is not None), default=None),
        n_scored30=len(sc), wins30=sum(1 for x in sc if x["csi"] > x["csip"]),
    )
    return dict(station=code, date=day, generated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                cycles=cycles, summary=summary)


def station_block(code: str, data: Path, origins: list[int]) -> dict:
    st = get_station(code, CONFIG_PATH)
    L, T, R, B = st.plot_box
    cx, cy = st.center_px
    s = st.km_per_px
    m = load_meta(data, code, origins[-1]) if origins else {}
    return dict(code=code, name_th=getattr(st, "name_th", code), lat=st.lat, lon=st.lon, range_km=st.range_km,
                # กรอบของภาพต้นทาง TMD (หลังตัดด้วย plot_box) เป็น กม. จากเรดาร์: [ตะวันตก, ตะวันออก, ใต้, เหนือ]
                raw_extent_km=[round((L - cx) * s, 2), round((R - cx) * s, 2),
                               round(-(B - cy) * s, 2), round(-(T - cy) * s, 2)],
                plot_box=list(st.plot_box), grid=m.get("grid", [241, 241]), kmperpixel=m.get("kmperpixel", 2.0),
                levels_dbz=m.get("levels_dbz"), levels_rgb=m.get("levels_rgb"),
                wet_threshold_dbz=_thr(m) if m else 16.5)


def write(code: str, days: list[str] | None, data: Path = DATA, docs: Path = DOCS, quiet: bool = False) -> int:
    out = Path(docs) / "archive" / code
    out.mkdir(parents=True, exist_ok=True)
    origins = list_origins(data, code)
    log = ingest_log(data, code)
    all_days = sorted({datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d") for t in set(origins) | set(log)})
    todo = all_days if days is None else [d for d in days if d in all_days]
    n = 0
    for d in todo:
        doc = build_day(d, code, data, origins, log)
        if doc is None:
            continue
        (out / f"{d}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
        if not quiet:
            s = doc["summary"]
            print(f"{d}: {s['n_cycles']}/96 รอบ · ภาพต้นทาง {s['n_raw']} · RFI {s['rfi_images']} · "
                  f"+30 ชนะ persistence {s['wins30']}/{s['n_scored30']}")
    # ปฏิทิน: อ่านจากไฟล์รายวันที่มีอยู่ทั้งหมด (วันที่ไม่ได้สร้างใหม่รอบนี้ใช้ไฟล์เดิม)
    cal = []
    for p in sorted(out.glob("????-??-??.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))["summary"]
        except Exception:
            continue
        cal.append(dict(date=p.stem, n=s["n_cycles"], n_raw=s["n_raw"],
                        max_km2=s["peak"]["km2"] if s.get("peak") else 0, rfi=s["rfi_images"]))
    (out / "calendar.json").write_text(json.dumps(
        dict(station=station_block(code, data, origins), days=cal,
             generated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if not quiet:
        print(f"เขียน {n} วัน · ปฏิทิน {len(cal)} วัน -> {out}")
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar_archive.dayindex", description="ดัชนีรายวันสำหรับหน้าคลังภาพ")
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--docs", default=str(DOCS))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="สร้างใหม่ทุกวันที่มีในคลัง")
    g.add_argument("--day", action="append", help="YYYY-MM-DD (เวลาไทย) ใส่ซ้ำได้")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)
    if a.all:
        days = None
    elif a.day:
        days = a.day
    else:   # วันนี้ + เมื่อวาน: คะแนน +30 ของรอบท้ายวันต้องรอภาพของวันถัดไป
        now = datetime.now(TZ)
        days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    write(a.station, days, Path(a.data), Path(a.docs), a.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
