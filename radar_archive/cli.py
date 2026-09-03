"""จุดเรียกใช้จาก command line / GitHub Actions

ตัวอย่าง
    python -m radar_archive.cli fetch --repeat 3 --interval 240
    python -m radar_archive.cli fetch --station PHS --force
    python -m radar_archive.cli reprocess --station PHS --since 2026-09-01
    python -m radar_archive.cli mask --station PHS --n 40
    python -m radar_archive.cli prune --keep-days 7
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from . import fetch, pipeline, qc as qcmod, refine, strip
from .config import CONFIG_PATH, enabled_stations, get_station

DATA = Path(__file__).resolve().parent.parent / "data"


def _stations(args):
    if args.station:
        return [get_station(args.station, args.config)]
    return enabled_stations(args.config)


def cmd_fetch(args) -> int:
    outputs = tuple(args.outputs.split(","))
    bg = tuple(int(x) for x in args.background.split(","))
    got = 0
    for attempt in range(args.repeat):
        for st in _stations(args):
            if not st.is_calibrated:
                print(f"[skip] {st.code}: ยังไม่ได้ calibrate (center_px/km_per_px เป็น null)")
                continue
            try:
                s = pipeline.run_once(st, args.data, outputs, bg, force=args.force,
                                      local_file=Path(args.from_file) if args.from_file else None)
            except Exception as e:  # อย่าให้ job ล้มทั้งรอบเพราะสถานีเดียว
                print(f"[error] {st.code}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if s is None:
                print(f"[dup]  {st.code}: เฟรมเดิม ข้าม")
            else:
                got += 1
                print(f"[ok]   {st.code} {s['timestamp_th']} TH  "
                      f"coverage={s['coverage_pct']}%  max={s['max_dbz']} dBZ  ({s['timestamp_source']})")
        if attempt < args.repeat - 1:
            time.sleep(args.interval)
    print(f"เฟรมใหม่ทั้งหมด: {got}")
    return 0


def cmd_reprocess(args) -> int:
    """ประมวลผลภาพ raw ที่เก็บไว้แล้วใหม่ทั้งหมด (ใช้เมื่อจูน tolerance หรืออัปเดต mask)"""
    outputs = tuple(args.outputs.split(","))
    bg = tuple(int(x) for x in args.background.split(","))
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.since else None
    n = 0
    for st in _stations(args):
        base = Path(args.data) / "raw" / st.code
        smask = strip.load_static_mask(pipeline.static_mask_path(Path(args.data), st))
        files = sorted(base.rglob("*.jpg"))
        for p in files:
            stamp = datetime.strptime(p.stem.split("_", 1)[1], "%Y%m%d_%H%M" + "Z").replace(tzinfo=timezone.utc)
            if since and stamp < since:
                continue
            img = Image.open(p).convert("RGB")
            pal_rgb, pal_dbz = pipeline.get_palette(Path(args.data), st, img)
            if st.refine:
                res = refine.strip_background_refined(img, st, pal_rgb, static_mask=smask,
                                                      **st.refine_params)
                mk_alpha, mk_solid = (lambda: refine.render_alpha(res, pal_rgb),
                                      lambda: refine.render_solid(res, pal_rgb, bg))
            else:
                res = strip.strip_background(img, st, pal_rgb, static_mask=smask)
                mk_alpha, mk_solid = lambda: strip.render_alpha(res), lambda: strip.render_solid(res, bg)
            if st.qc:
                res, _ = qcmod.apply_qc(res, st, clutter=pipeline.load_clutter(Path(args.data), st),
                                        when=stamp, **st.qc_params)
            if "alpha" in outputs:
                q = fetch.processed_path(Path(args.data), st, stamp, "alpha")
                q.parent.mkdir(parents=True, exist_ok=True)
                mk_alpha().save(q, optimize=True)
            if "solid" in outputs:
                q = fetch.processed_path(Path(args.data), st, stamp, "solid")
                q.parent.mkdir(parents=True, exist_ok=True)
                mk_solid().save(q, optimize=True)
            n += 1
    print(f"reprocess {n} เฟรม")
    return 0


def cmd_mask(args) -> int:
    """สร้าง static overlay mask จากภาพ raw ที่เก็บไว้ (ยิ่งหลายเฟรม/หลายวัน ยิ่งดี)"""
    for st in _stations(args):
        base = Path(args.data) / "raw" / st.code
        files = sorted(base.rglob("*.jpg"))
        if len(files) < args.n:
            print(f"[skip] {st.code}: มีแค่ {len(files)} เฟรม ต้องการอย่างน้อย {args.n}")
            continue
        step = max(1, len(files) // args.n)
        picked = files[::step][: args.n]
        imgs = [Image.open(p).convert("RGB") for p in picked]
        pal_rgb, _ = pipeline.get_palette(Path(args.data), st, imgs[0])
        m = strip.build_static_mask(imgs, st, pal_rgb, keep_ratio=args.keep_ratio)
        out = pipeline.static_mask_path(Path(args.data), st)
        strip.save_static_mask(out, m)
        print(f"[ok] {st.code}: static mask {int(m.sum())} px -> {out}")
    return 0


def cmd_clutter(args) -> int:
    """สร้างแผนที่ความถี่ของ echo จาก archive ที่เก็บไว้ -> ใช้ตรวจ ground clutter / RFI ประจำที่

    ฝนเคลื่อนที่เสมอ ถ้า pixel ไหนถูกจัดว่ามี echo เกิน clutter_thresh ของเฟรมทั้งหมด
    แปลว่าไม่ใช่ฝน ยิ่งใช้เฟรมครอบคลุมหลายวัน/หลายฤดู ยิ่งเชื่อถือได้
    """
    import numpy as np
    for st in _stations(args):
        base = Path(args.data) / "raw" / st.code
        files = sorted(base.rglob("*.jpg"))
        # กันพลาด: clutter map ที่สร้างจากเฟรมน้อยเกินไปจะกลายเป็น "ลบฝนทิ้งทั้งภาพ"
        MIN_FRAMES = 30
        if len(files) < max(MIN_FRAMES, args.n):
            print(f"[skip] {st.code}: มีแค่ {len(files)} เฟรม ต้องการอย่างน้อย "
                  f"{max(MIN_FRAMES, args.n)} (และควรกระจายหลายวัน) — ยังไม่สร้าง clutter map")
            continue
        step = max(1, len(files) // args.n)
        picked = files[::step][: args.n]
        masks = []
        for fp in picked:
            im = Image.open(fp).convert("RGB")
            pal_rgb, _ = pipeline.get_palette(Path(args.data), st, im)
            r = (refine.strip_background_refined(im, st, pal_rgb, **st.refine_params)
                 if st.refine else strip.strip_background(im, st, pal_rgb))
            masks.append(r["mask"])
        freq = qcmod.clutter_frequency(masks)
        out = pipeline.clutter_path(Path(args.data), st)
        out.parent.mkdir(parents=True, exist_ok=True)
        np.save(out, freq.astype(np.float32))
        n_hit = int((freq >= st.clutter_thresh).sum())
        print(f"[ok] {st.code}: ใช้ {len(masks)} เฟรม -> clutter {n_hit} px "
              f"(threshold {st.clutter_thresh}) -> {out}")
    return 0


def cmd_prune(args) -> int:
    for st in _stations(args):
        n = pipeline.prune_old(Path(args.data), st, args.keep_days)
        print(f"[ok] {st.code}: ลบ {n} ไฟล์ที่เก่ากว่า {args.keep_days} วัน")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar_archive")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--station", default=None, help="รหัสสถานี เช่น PHS (ไม่ใส่ = ทุกสถานีที่ enabled)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="ดึงภาพล่าสุด + ประมวลผล")
    f.add_argument("--repeat", type=int, default=1, help="เช็กกี่รอบใน 1 job")
    f.add_argument("--interval", type=int, default=240, help="เว้นกี่วินาทีระหว่างรอบ")
    f.add_argument("--outputs", default="alpha,solid", help="alpha,solid,dbz")
    f.add_argument("--background", default="0,0,0", help="สีพื้นของ solid PNG เช่น 0,0,0 หรือ 255,255,255")
    f.add_argument("--force", action="store_true", help="ประมวลผลใหม่แม้เป็นเฟรมซ้ำ")
    f.add_argument("--from-file", default=None,
                   help="อ่านจากไฟล์ในเครื่องแทนการดาวน์โหลด (ใช้ทดสอบ / ingest เฟรมจาก GIF)")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("reprocess", help="ประมวลผล raw ที่เก็บไว้ใหม่")
    r.add_argument("--since", default=None, help="YYYY-MM-DD")
    r.add_argument("--outputs", default="alpha,solid")
    r.add_argument("--background", default="0,0,0")
    r.set_defaults(func=cmd_reprocess)

    m = sub.add_parser("mask", help="สร้าง static overlay mask")
    m.add_argument("--n", type=int, default=40)
    m.add_argument("--keep-ratio", type=float, default=0.9)
    m.set_defaults(func=cmd_mask)

    c = sub.add_parser("clutter", help="สร้างแผนที่ความถี่ echo เพื่อตรวจ ground clutter")
    c.add_argument("--n", type=int, default=200, help="ใช้กี่เฟรม (ยิ่งมากยิ่งดี)")
    c.set_defaults(func=cmd_clutter)

    pr = sub.add_parser("prune", help="ลบไฟล์เก่าออกจาก repo")
    pr.add_argument("--keep-days", type=int, default=7)
    pr.set_defaults(func=cmd_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
