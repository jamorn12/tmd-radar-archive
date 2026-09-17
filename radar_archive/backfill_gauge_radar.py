#!/usr/bin/env python3
"""เติมคอลัมน์ radar_* ย้อนหลังให้ไฟล์ประวัติสถานีวัดน้ำฝนที่เก็บไว้แล้ว

    python backfill_gauge_radar.py --dry-run          # ดูผลก่อน ไม่แตะไฟล์
    python backfill_gauge_radar.py                    # เขียนจริง
    python backfill_gauge_radar.py --month 202609     # เฉพาะเดือนเดียว
    python backfill_gauge_radar.py --report out.csv   # เขียนคู่ที่จับได้ออกมาวิเคราะห์ต่อ

ทำไมต้องมีสคริปต์นี้
    gauges.py เวอร์ชันใหม่จะเติม radar_1h ให้ทุกรอบที่รันตั้งแต่นี้ไป แต่ประวัติที่
    เก็บมาแล้วหลายพันแถวจะยังว่าง ทั้งที่ภาพ obs.png ของช่วงเวลานั้นยังอยู่ในคลัง
    สคริปต์นี้ย้อนไปคำนวณให้ ทำให้ได้ชุดข้อมูลจับคู่เต็มความยาวที่คลังมี
    แทนที่จะต้องรอสะสมใหม่อีกหลายสัปดาห์

ขั้นตอน (แยกเป็นฟังก์ชันให้หยิบไปใช้ทีละขั้นได้)
    1. find_history_files   หาไฟล์ CSV ประวัติที่จะเติม
    2. load_rows            อ่านแถวเข้ามาเป็น dict
    3. fill_radar           คำนวณ radar_1h ต่อแถว (ใช้ gauges.attach_radar)
    4. write_rows           เขียนกลับ พร้อมสำรองไฟล์เดิมไว้เป็น .bak
    5. summarize            สรุปผลการจับคู่ให้ดูว่าคุ้มไหมและตัวเลขหน้าตาอย่างไร

ข้อควรรู้
    แถวที่ obs_utc อยู่นอกช่วงที่คลัง nowcast ครอบคลุม จะเว้นว่างไว้ตามเดิม
    ไม่ใช่เติม 0 — "ไม่มีภาพให้เทียบ" ไม่ใช่ "เรดาร์เห็นว่าฝนไม่ตก"
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

import numpy as np

def _find_repo_root(start: Path) -> Path:
    """หา root ของ repo โดยเดินขึ้นไปจนเจอโฟลเดอร์แพ็กเกจ radar_archive

    ทำไมไม่ใช้ parent ของไฟล์ตรง ๆ
        สคริปต์นี้ถูกวางได้หลายที่ (root ของ repo หรือใน radar_archive/ ก็ได้)
        ถ้าใช้ parent ตรง ๆ แล้วไฟล์ไปอยู่ใน radar_archive/ จะได้ sys.path ผิด
        (import radar_archive ไม่เจอ) และ --data จะชี้ไป radar_archive/data ซึ่งไม่มีจริง
        เดินขึ้นหาแพ็กเกจแบบนี้ ทำงานถูกไม่ว่าจะวางไว้ตรงไหนในโครงสร้าง repo
    """
    for d in [start, *start.parents]:
        if (d / "radar_archive" / "__init__.py").exists():
            return d
    return start


ROOT = _find_repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))

from radar_archive import gauges as G          # noqa: E402
from radar_archive.config import CONFIG_PATH, get_station   # noqa: E402
from radar_archive.verify import store_dir     # noqa: E402


def has_value(v) -> bool:
    """ช่องนี้มีค่าไหม — ต้องรับได้ทั้ง str จาก CSV และ float ที่เพิ่งเติมเข้าไป

    เคยพลาดตรงนี้มาแล้ว: fill_radar เขียน float ลงแถว แล้วโค้ดถัดไปเรียก .strip()
    ซึ่งใช้กับ float ไม่ได้ ตรวจผ่าน str() ทีเดียวจบ
    """
    return v is not None and str(v).strip() != ""


# ---------------------------------------------------------------- 1. หาไฟล์

def find_history_files(data_root: Path, code: str, month: str | None) -> list[Path]:
    d = Path(data_root) / "gauges"
    if not d.exists():
        return []
    pat = f"{code}_{month}.csv" if month else f"{code}_*.csv"
    return sorted(p for p in d.glob(pat) if not p.name.endswith(".bak"))


# ---------------------------------------------------------------- 2. อ่าน

def load_rows(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        cols = list(rd.fieldnames or [])
        return list(rd), cols


# ---------------------------------------------------------------- 3. เติมค่า

def fill_radar(rows: list[dict], store: Path, st, overwrite: bool = False) -> int:
    """คำนวณ radar_1h ให้แถวที่ยังว่าง — คืนจำนวนแถวที่เติมได้

    แปลงแถว CSV เป็นรูปแบบที่ attach_radar รับ แล้วเขียนผลกลับเข้าแถวเดิม
    ใช้ attach_radar ตัวเดียวกับ pipeline เพื่อให้ค่าที่ backfill กับค่าที่เก็บสด
    มาจากโค้ดบรรทัดเดียวกันเป๊ะ ไม่งั้นชุดข้อมูลจะมีสองนิยามปนกันโดยไม่รู้ตัว
    """
    todo, shim = [], []
    for r in rows:
        if not overwrite and has_value(r.get("radar_1h")):
            continue
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, ValueError):
            continue
        if not r.get("obs_utc"):
            continue
        g = dict(id=r.get("station_id", ""), name=r.get("name", ""),
                 lat=lat, lon=lon, obs_utc=r["obs_utc"])
        shim.append(g)
        todo.append(r)

    if not shim:
        return 0

    G.attach_radar(shim, store, st, verbose=False)
    n = 0
    for r, g in zip(todo, shim):
        if g.get("radar_1h") is None:
            continue
        r["radar_1h"] = g["radar_1h"]
        r["radar_1h_max3"] = g["radar_1h_max3"]
        r["radar_1h_n"] = g["radar_1h_n"]
        n += 1
    return n


# ---------------------------------------------------------------- 4. เขียนกลับ

def write_rows(path: Path, rows: list[dict], backup: bool = True) -> None:
    if backup:
        shutil.copy2(path, path.with_suffix(".csv.bak"))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=G.HIST_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in G.HIST_COLS})


# ---------------------------------------------------------------- 5. สรุปผล

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict]) -> None:
    """สรุปคู่ที่จับได้ — ตัวเลขชุดนี้คือวัตถุดิบของหัวข้อเทียบเรดาร์กับสถานีในเปเปอร์"""
    pairs = []
    for r in rows:
        g, rd, n = _f(r.get("rain_1h")), _f(r.get("radar_1h")), _f(r.get("radar_1h_n"))
        if g is None or rd is None:
            continue
        pairs.append((g, rd, n or 0, _f(r.get("dist_km")) or 0.0))

    if not pairs:
        print("\nยังไม่มีคู่ที่จับได้เลย")
        return

    full = [p for p in pairs if p[2] >= 4]
    print(f"\n=== สรุปการจับคู่ ===")
    print(f"คู่ทั้งหมด {len(pairs):,} · เฟรมครบ 4 {len(full):,} "
          f"({len(full) / len(pairs) * 100:.0f}%)")
    print("ใช้เฉพาะคู่ที่เฟรมครบเท่านั้นในการสรุป — คู่ที่ภาพขาดจะต่ำกว่าความจริงเสมอ")
    if not full:
        return

    wet = [p for p in full if p[0] >= 0.1 or p[1] >= 0.1]
    dry = len(full) - len(wet)
    print(f"\nคู่ที่แห้งทั้งสองฝั่ง {dry:,} (ตัดออกจากการสรุป ไม่งั้นจะลากค่าเฉลี่ยเข้าหา 0)")
    if not wet:
        return

    ga = np.array([p[0] for p in wet])
    ra = np.array([p[1] for p in wet])
    print(f"คู่ที่มีฝนอย่างน้อยฝั่งหนึ่ง {len(wet):,}")
    print(f"  รวม  สถานี {ga.sum():9.1f} มม. · เรดาร์ {ra.sum():9.1f} มม. "
          f"· อัตราส่วน {ra.sum() / ga.sum():.3f}")
    print(f"  มัธยฐานผลต่าง (เรดาร์ − สถานี) {np.median(ra - ga):+.2f} มม.")
    if len(wet) > 2:
        print(f"  สหสัมพันธ์ r = {np.corrcoef(ga, ra)[0, 1]:.3f}")

    both = int(((ga >= 0.1) & (ra >= 0.1)).sum())
    only_g = int(((ga >= 0.1) & (ra < 0.1)).sum())
    only_r = int(((ra >= 0.1) & (ga < 0.1)).sum())
    print(f"  เห็นตรงกัน {both:,} · สถานีเห็นฝ่ายเดียว {only_g:,} "
          f"· เรดาร์เห็นฝ่ายเดียว {only_r:,}")

    print("\nแยกตามความแรงของฝนที่สถานีวัดได้")
    for lo, hi, lab in [(0.1, 1.0, "0.1–1"), (1.0, 5.0, "1–5"),
                        (5.0, 10.0, "5–10"), (10.0, 1e9, ">10")]:
        m = (ga >= lo) & (ga < hi)
        if not m.any():
            continue
        print(f"  {lab:<7} มม./ชม.  n={int(m.sum()):5d} · "
              f"อัตราส่วน {ra[m].sum() / ga[m].sum():.3f} · "
              f"มัธยฐานเรดาร์ {np.median(ra[m]):5.2f} มม.")

    print("\nแยกตามระยะห่างจากเรดาร์ (ทดสอบผลของ beam overshoot)")
    da = np.array([p[3] for p in wet])
    for lo, hi in [(0, 60), (60, 120), (120, 180), (180, 999)]:
        m = (da >= lo) & (da < hi) & (ga >= 0.1)
        if m.sum() < 5:
            continue
        print(f"  {lo:3d}–{hi:3d} กม.  n={int(m.sum()):5d} · "
              f"อัตราส่วน {ra[m].sum() / ga[m].sum():.3f}")


def write_report(path: Path, rows: list[dict]) -> int:
    cols = ["obs_utc", "station_id", "name", "agency", "province", "lat", "lon",
            "dist_km", "rain_1h", "radar_1h", "radar_1h_max3", "radar_1h_n"]
    out = [r for r in rows
           if has_value(r.get("rain_1h")) and has_value(r.get("radar_1h"))]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in out:
            w.writerow({k: r.get(k, "") for k in cols})
    return len(out)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="backfill_gauge_radar.py",
        description="เติม radar_1h ย้อนหลังให้ประวัติสถานีวัดน้ำฝน")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--month", default=None, help="เช่น 202609 (ไม่ใส่ = ทุกเดือน)")
    p.add_argument("--overwrite", action="store_true",
                   help="คำนวณใหม่ทับค่าที่มีอยู่แล้ว")
    p.add_argument("--no-backup", action="store_true", help="ไม่ต้องสำรอง .bak")
    p.add_argument("--report", default=None, help="เขียนคู่ที่จับได้เป็น CSV แยก")
    p.add_argument("--dry-run", action="store_true", help="ไม่เขียนไฟล์ประวัติ")
    a = p.parse_args(argv)

    st = get_station(a.station, a.config)
    store = store_dir(Path(a.data), st.code)
    if not store.exists():
        print(f"[!] ไม่พบคลัง nowcast ที่ {store}", file=sys.stderr)
        return 2

    files = find_history_files(Path(a.data), st.code, a.month)
    if not files:
        print(f"[!] ไม่พบไฟล์ประวัติของ {st.code}"
              + (f" เดือน {a.month}" if a.month else ""), file=sys.stderr)
        return 2

    print(f"=== backfill {st.code} · {len(files)} ไฟล์ · คลัง {store} ===")
    all_rows = []
    for path in files:
        rows, cols = load_rows(path)
        missing = [c for c in G.HIST_COLS if c not in cols]
        n = fill_radar(rows, store, st, overwrite=a.overwrite)
        blank = sum(1 for r in rows if not has_value(r.get("radar_1h")))
        print(f"  {path.name:<24} {len(rows):6,d} แถว · เติมได้ {n:6,d} "
              f"· ยังว่าง {blank:6,d}"
              + (f" · เพิ่มคอลัมน์ {','.join(missing)}" if missing else ""))
        if n and not a.dry_run:
            write_rows(path, rows, backup=not a.no_backup)
        all_rows.extend(rows)

    summarize(all_rows)

    if a.report and not a.dry_run:
        n = write_report(Path(a.report), all_rows)
        print(f"\n-> {a.report}  ({n:,} คู่)")
    if a.dry_run:
        print("\n--dry-run: ไม่ได้เขียนไฟล์ใด ๆ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
