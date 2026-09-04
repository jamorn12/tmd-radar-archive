"""เฟส 1 — จากคลังภาพ เป็น array ที่ pysteps กินได้

    python -m radar_archive.build_stack --station PHS
    python -m radar_archive.build_stack --station PHS --since 2026-09-03 --agg max
    python -m radar_archive.build_stack --station PHS --list        # ดูช่วงเวลาเฉย ๆ

ทำอะไร (แยกเป็นขั้น ๆ ให้หยิบไปเรียกทีละอันได้)

    1. find_frames()      หาไฟล์ raw ทั้งหมดของสถานี เรียงตามเวลา
    2. split_runs()       ตัดเป็น "ช่วงที่ต่อเนื่องจริง" ทุก 15 นาที
                          -- ขั้นนี้สำคัญที่สุด ถ้าข้ามไป pysteps จะคิดว่าเฟรมที่ห่างกัน
                             2 ชั่วโมงคือ 15 นาที แล้วให้ความเร็วลมผิดเกือบ 10 เท่า
                             โดยไม่มี error อะไรเลย
    3. process_frame()    ตัดพื้นหลัง + QC (ตรรกะเดียวกับ pipeline.run_once เป๊ะ)
    4. build_run()        แปลงเป็น (T, 241, 241) dBZ + ตรวจคุณภาพ
    5. save               เขียน .npz หนึ่งไฟล์ต่อหนึ่งช่วง + manifest.json รวม

ทำไมประมวลผลจาก raw ใหม่ ไม่อ่านจาก alpha PNG ที่มีอยู่แล้ว
    ภาพ alpha เก็บ "สี" ไว้ แต่ไม่ได้เก็บ band index ที่ใช้ระบุแถบสี การอ่านค่ากลับ
    จากสีอีกรอบจะเจอ ambiguity ของสีซ้ำ (ราว 44-51 dBZ) ซ้อนเข้าไปอีกชั้น
    ประมวลผลจาก raw ใช้เวลาเพิ่มราว 1.5 วินาที/เฟรม แลกกับค่าที่ตรงกว่า คุ้ม
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import fetch, grid, pipeline, qc as qcmod, refine, strip
from .config import CONFIG_PATH, enabled_stations, get_station

DATA = Path(__file__).resolve().parent.parent / "data"

STEP_MIN = 15.0        # ระยะห่างที่ TMD สแกน
GAP_TOL_MIN = 2.0      # ยอมให้เพี้ยนได้เท่านี้ (เวลาสแกนจริงคือ :02 วินาที)
MIN_RUN = 3            # nowcast ต้องการอย่างน้อย 3 เฟรมต่อเนื่อง


# ---------------------------------------------------------------- 1. หาไฟล์

def find_frames(root: Path, code: str, since=None, until=None) -> list:
    """คืน [(datetime, path)] เรียงจากเก่าไปใหม่"""
    base = Path(root) / "raw" / code
    out = []
    for p in sorted(base.rglob(f"{code}_*.jpg")):
        try:
            t = grid.time_from_name(p)
        except ValueError:
            print(f"[warn] ชื่อไฟล์ผิดรูปแบบ ข้าม: {p.name}", file=sys.stderr)
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        out.append((t, p))
    return sorted(out)


# ---------------------------------------------------------------- 2. ตัดเป็นช่วง

def split_runs(frames: list, step_min: float = STEP_MIN,
               tol_min: float = GAP_TOL_MIN) -> list:
    """ตัด [(t, path)] เป็นช่วงย่อยที่ห่างกัน step_min เป๊ะ

    คืน list ของ list — แต่ละอันคือช่วงที่เอาไปทำ nowcast ได้โดยไม่ต้องกังวลเรื่องรู
    """
    if not frames:
        return []
    runs, cur = [], [frames[0]]
    for prev, nxt in zip(frames, frames[1:]):
        gap = (nxt[0] - prev[0]).total_seconds() / 60.0
        if abs(gap - step_min) <= tol_min:
            cur.append(nxt)
        else:
            runs.append(cur)
            cur = [nxt]
    runs.append(cur)
    return runs


# ---------------------------------------------------------------- 3. ประมวลผลเฟรม

def make_processor(root: Path, st, smask=None, clutter=None):
    """คืนฟังก์ชัน img -> res dict ที่ผ่าน strip/refine + QC แล้ว

    ตรรกะตรงกับ pipeline.run_once ทุกประการ เพื่อให้ค่าที่ได้ตรงกับที่ index CSV บันทึกไว้
    """
    if smask is None:
        smask = strip.load_static_mask(pipeline.static_mask_path(root, st))
    if clutter is None:
        clutter = pipeline.load_clutter(root, st)

    def process(img: Image.Image, when: datetime, pal_rgb):
        if st.refine:
            res = refine.strip_background_refined(img, st, pal_rgb, static_mask=smask,
                                                  **st.refine_params)
        else:
            res = strip.strip_background(img, st, pal_rgb, static_mask=smask)
        rep = None
        if st.qc:
            res, rep = qcmod.apply_qc(res, st, clutter=clutter, when=when, **st.qc_params)
        return res, rep

    return process


# ---------------------------------------------------------------- 4. สร้าง stack

def build_run(run: list, st, root: Path, agg: str = "mean", verbose: bool = True):
    """แปลงหนึ่งช่วงเป็น (stack, times, meta, report)"""
    process = make_processor(root, st)
    pal_rgb = pal_dbz = None
    frames, times, rows = [], [], []

    for t, p in run:
        img = Image.open(p).convert("RGB")
        if pal_rgb is None:
            pal_rgb, pal_dbz = pipeline.get_palette(root, st, img)
        res, rep = process(img, t, pal_rgb)
        g = grid.to_grid(grid.dbz_field(res, st, pal_dbz), st, agg=agg)
        frames.append(g)
        times.append(t)

        inside = np.isfinite(g)
        rows.append(dict(
            time=t.strftime("%Y-%m-%d %H:%M"),
            cover=round(grid.coverage_fraction(g) * 100, 2),
            wet=round(float((g[inside] > grid.NO_ECHO_DBZ).mean() * 100), 3),
            max_dbz=round(float(np.nanmax(g)), 1) if inside.any() else None,
            qc_removed=int(rep.removed_px) if rep is not None else 0,
        ))
        if verbose:
            r = rows[-1]
            print(f"    {r['time']}  cover {r['cover']:5.2f}%  wet {r['wet']:6.3f}%  "
                  f"max {str(r['max_dbz']):>5} dBZ  qc -{r['qc_removed']}")

    stack = np.array(frames, np.float32)
    meta = grid.station_meta(st)
    meta["agg"] = agg
    return stack, times, meta, rows


# ---------------------------------------------------------------- ตรวจคุณภาพ

def check_stack(stack: np.ndarray, times: list) -> dict:
    """ตรวจสิ่งที่ถ้าผิดแล้ว pysteps จะไม่ฟ้อง แต่ผลจะพัง"""
    e, n = grid.grid_km()
    EE, NN = np.meshgrid(e, n)
    inside = np.hypot(EE, NN) <= grid.GRID_HALF_KM

    ok_regular, gaps = grid.check_regular(times, STEP_MIN, GAP_TOL_MIN)
    cover = np.array([np.isfinite(f[inside]).mean() for f in stack])
    nan_inside = ~np.isfinite(stack[:, inside])

    return dict(
        n_frames=len(times),
        regular=bool(ok_regular),
        gaps_min=[round(g, 1) for g in gaps],
        cover_min=round(float(cover.min()) * 100, 2),
        cover_mean=round(float(cover.mean()) * 100, 2),
        nan_inside_pct=round(float(nan_inside.mean()) * 100, 3),
        finite_outside=bool(np.isfinite(stack[:, ~inside]).any()),
        dbz_min=round(float(np.nanmin(stack)), 1),
        dbz_max=round(float(np.nanmax(stack)), 1),
        wet_pct=[round(float((f[inside] > grid.NO_ECHO_DBZ).mean() * 100), 3) for f in stack],
        frame_max=[round(float(np.nanmax(f)), 1) for f in stack],
        n_ge50=[int(np.nansum(f > 50.0)) for f in stack],
    )


def report_check(chk: dict) -> list:
    """คืนรายการปัญหาที่เจอ (ว่าง = ผ่านหมด)"""
    bad = []
    if not chk["regular"]:
        bad.append(f"เฟรมห่างไม่เท่ากัน: {chk['gaps_min']} นาที")
    if chk["cover_min"] < 95:
        bad.append(f"บางเฟรมมีข้อมูลไม่ครบวง (ต่ำสุด {chk['cover_min']}%)")
    if chk["finite_outside"]:
        bad.append("มีค่าหลุดออกนอกรัศมี 240 กม. — ควรเป็น NaN ทั้งหมด")
    if chk["dbz_max"] > 70:
        bad.append(f"ค่า dBZ สูงผิดปกติ {chk['dbz_max']}")

    # เซลล์แรงจัดที่โผล่มาไม่กี่เซลล์ในเฟรมเดียว มักไม่ใช่ฝน
    # ตัวหนังสือสีขาวบนแผนที่ (ชื่อเมือง) จะถูกจับเป็นแถบสีจางสุด = dBZ สูงสุด
    # ถ้ามันบังเอิญติดกับก้อนฝนจริง drop_pale_blobs จะไม่ตัดให้ เพราะดูค่ากลางทั้งก้อน
    med = float(np.median(chk["frame_max"]))
    for i, (mx, n50) in enumerate(zip(chk["frame_max"], chk["n_ge50"])):
        if n50 and mx > med + 10 and n50 <= 10:
            bad.append(f"เฟรมที่ {i} มี {n50} เซลล์ > 50 dBZ (max {mx}) "
                       f"ขณะที่เฟรมอื่นสูงสุดราว {med:.0f} — น่าจะเป็นตัวหนังสือบนแผนที่ "
                       f"ไม่ใช่ฝน ตรวจก่อนเอาไปหา motion")
    return bad


# ---------------------------------------------------------------- 5. main

def run_name(run: list) -> str:
    return f"{run[0][0]:%Y%m%d_%H%M}-{run[-1][0]:%H%M}Z"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar_archive.build_stack",
                                description="สร้าง dBZ stack 241x241 สำหรับ pysteps")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--station", default=None, help="รหัสสถานี เช่น PHS")
    p.add_argument("--since", default=None, help="YYYY-MM-DD หรือ YYYY-MM-DDTHH:MM")
    p.add_argument("--until", default=None)
    p.add_argument("--agg", default="mean", choices=["mean", "max"],
                   help="mean = เฉลี่ยใน linear Z (ค่าเริ่มต้น) · max = เก็บแกนฝน")
    p.add_argument("--min-run", type=int, default=MIN_RUN,
                   help=f"ช่วงที่สั้นกว่านี้ข้ามไป (ค่าเริ่มต้น {MIN_RUN})")
    p.add_argument("--out", default=None, help="โฟลเดอร์ผลลัพธ์ (ค่าเริ่มต้น <data>/stack)")
    p.add_argument("--list", action="store_true", help="แสดงช่วงเวลาเฉย ๆ ไม่สร้างไฟล์")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    root = Path(a.data)
    out_dir = Path(a.out) if a.out else root / "stack"
    stations = [get_station(a.station, a.config)] if a.station else enabled_stations(a.config)

    def parse(s):
        if not s:
            return None
        fmt = "%Y-%m-%dT%H:%M" if "T" in s else "%Y-%m-%d"
        return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)

    since, until = parse(a.since), parse(a.until)
    manifest = []

    for st in stations:
        if not st.is_calibrated:
            print(f"[skip] {st.code}: ยังไม่ได้ calibrate")
            continue

        frames = find_frames(root, st.code, since, until)
        runs = split_runs(frames)
        keep = [r for r in runs if len(r) >= a.min_run]

        print(f"\n=== {st.code} · ไฟล์ {len(frames)} เฟรม · ช่วงต่อเนื่อง {len(runs)} ช่วง "
              f"· ใช้ได้ {len(keep)} ช่วง (>= {a.min_run} เฟรม) ===")
        for r in runs:
            mark = "  <<" if len(r) >= a.min_run else "  (สั้นเกินไป ข้าม)"
            print(f"  {r[0][0]:%d %H:%M}-{r[-1][0]:%H:%M}Z  {len(r):3d} เฟรม{mark}")

        if a.list or not keep:
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        for r in keep:
            name = f"{st.code}_{run_name(r)}"
            print(f"\n  --- {name} ({len(r)} เฟรม) ---")
            stack, times, meta, rows = build_run(r, st, root, agg=a.agg,
                                                 verbose=not a.quiet)
            chk = check_stack(stack, times)
            bad = report_check(chk)

            path = out_dir / f"{name}_{a.agg}.npz"
            grid.save_stack(path, stack, times, meta)

            print(f"    -> {path.name}  shape {stack.shape}  "
                  f"cover {chk['cover_mean']}%  dBZ {chk['dbz_min']}-{chk['dbz_max']}")
            if bad:
                for b in bad:
                    print(f"    [!] {b}")
            else:
                print("    [ok] ตรวจผ่านทุกข้อ")

            manifest.append(dict(station=st.code, name=name, file=path.name,
                                 agg=a.agg, shape=list(stack.shape),
                                 t0=times[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 t1=times[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 check=chk, issues=bad, frames=rows))

    if manifest and not a.list:
        mp = out_dir / "manifest.json"
        mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        total = sum(m["shape"][0] for m in manifest)
        print(f"\nเขียน {len(manifest)} stack รวม {total} เฟรม -> {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
