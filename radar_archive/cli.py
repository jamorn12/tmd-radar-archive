"""จุดเรียกใช้จาก command line / GitHub Actions

ตัวอย่าง
    python -m radar_archive.cli fetch --repeat 3 --interval 240
    python -m radar_archive.cli fetch --station PHS --force
    python -m radar_archive.cli reprocess --station PHS --since 2026-09-01
    python -m radar_archive.cli mask --station PHS --n 40
    python -m radar_archive.cli prune --keep-days 7
    python -m radar_archive.cli --station PHS repair            # ดูก่อน
    python -m radar_archive.cli --station PHS repair --apply    # แก้จริง
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


def _stamp_from_name(p: Path) -> datetime:
    """PHS_20260903_0600Z.jpg -> datetime(2026, 9, 3, 6, 0, tzinfo=utc)"""
    return datetime.strptime(p.stem.split("_", 1)[1], "%Y%m%d_%H%MZ").replace(tzinfo=timezone.utc)


def _strip_frame(img, st, pal_rgb, smask):
    """ตัด background ตามโหมดของสถานี คืน res

    หมายเหตุ: **อย่า** คืน lambda ที่ปิดทับ res จากในนี้ — apply_qc สร้าง res ก้อนใหม่
    ถ้าปิดทับไว้ก่อน จะได้ภาพก่อน QC (พลาดมาแล้ว: spike ที่ QC ตัดทิ้งโผล่กลับมาในภาพ)
    """
    if st.refine:
        return refine.strip_background_refined(img, st, pal_rgb, static_mask=smask,
                                               **st.refine_params)
    return strip.strip_background(img, st, pal_rgb, static_mask=smask)


def _render(res, st, pal_rgb, bg, kind: str):
    """วาดภาพจาก res ที่ผ่าน QC แล้ว"""
    if st.refine:
        return (refine.render_alpha(res, pal_rgb) if kind == "alpha"
                else refine.render_solid(res, pal_rgb, bg))
    return strip.render_alpha(res) if kind == "alpha" else strip.render_solid(res, bg)


def _save(im, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, optimize=True)


def cmd_repair(args) -> int:
    """ซ่อม archive ที่เก็บมาแล้ว: แก้เวลาที่ OCR อ่านผิด + ลบไฟล์ซ้ำ + สร้าง index ใหม่

    ทำไมต้องมี: เวอร์ชันแรกอ่าน footer โดย upscale ด้วย LANCZOS ซึ่งทำให้เลข 6 กลายเป็น 8
    (เกิดจริง 1 ใน 21 เฟรม) ไฟล์ที่ตั้งชื่อผิดไปแล้วต้องมาแก้ย้อนหลัง และตอนที่ OCR
    ใช้ไม่ได้ ภาพสแกนเดียวกันถูกเซฟซ้ำหลายชื่อ

    เป็น dry-run โดยปริยาย — ต้องใส่ --apply ถึงจะแตะไฟล์จริง
    """
    import hashlib

    import numpy as np

    data = Path(args.data)
    outputs = tuple(args.outputs.split(","))
    bg = tuple(int(x) for x in args.background.split(","))
    tag = "" if args.apply else "   [dry-run]"

    for st in _stations(args):
        base = data / "raw" / st.code
        files = sorted(base.rglob("*.jpg"))
        if not files:
            print("[skip] {}: ไม่มีไฟล์ raw".format(st.code))
            continue
        print("\n=== {}: ตรวจ {} เฟรม ==={}".format(st.code, len(files), tag))

        # ---- รอบที่ 1: อ่านเวลาใหม่ + หาไฟล์ที่ไบต์ซ้ำกัน ----
        plan = []          # (path, stamp_old, stamp_new, source, action)
        by_sha = {}
        full_stamp = {}    # stamp ระดับนาที -> stamp ที่มีวินาทีจริงจาก footer
        for p in files:
            try:
                old = _stamp_from_name(p)
            except ValueError:
                print("[warn] {}: ชื่อไฟล์ผิดรูปแบบ ข้าม".format(p.name))
                continue
            img = Image.open(p)
            img.load()
            new, src = fetch.read_timestamp_ocr(img, st, ref=None)

            # ชื่อไฟล์เก็บถึงระดับนาที จึงเทียบกันที่ระดับนาที
            new_min = new.replace(second=0, microsecond=0) if new is not None else None
            if new_min is None or src == "ocr-weak":
                if new_min is not None and new_min != old:
                    print("[warn] {}: OCR ว่า {:%H:%M} แต่คะแนนโหวตไม่พอ ({}) — ไม่แก้"
                          .format(p.name, new, src))
                new_min, src = old, "kept ({})".format(src)
            else:
                full_stamp[new_min] = new
            new = new_min

            sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            first = by_sha.get(sha)
            if first is not None:
                plan.append((p, old, new, src, "drop-dup"))
                print("[dup]  {}  ไบต์ซ้ำกับ {} — ลบ".format(p.name, first.name))
                continue
            by_sha[sha] = p

            if new != old:
                plan.append((p, old, new, src, "rename"))
                print("[fix]  {}  {:%H:%M}Z -> {:%H:%M}Z   ({})".format(p.name, old, new, src))
            else:
                plan.append((p, old, new, src, "keep"))

        n_fix = sum(1 for x in plan if x[4] == "rename")
        n_dup = sum(1 for x in plan if x[4] == "drop-dup")
        print("--- ต้องแก้ชื่อ {} เฟรม / ลบซ้ำ {} ไฟล์ ---".format(n_fix, n_dup))
        if not args.apply:
            print("(ยังไม่แก้อะไร — ใส่ --apply เพื่อลงมือจริง)")
            continue

        # ---- รอบที่ 2: ลบซ้ำ + เปลี่ยนชื่อ raw และ processed ทุกชนิด ----
        kinds = (("alpha", "png"), ("solid", "png"), ("dbz", "npz"))
        for p, old, new, _src, action in plan:
            if action == "drop-dup":
                p.unlink()
                for k, ext in kinds:
                    q = fetch.processed_path(data, st, old, k, ext)
                    if q.exists():
                        q.unlink()
            elif action == "rename":
                target = fetch.raw_path(data, st, new)
                target.parent.mkdir(parents=True, exist_ok=True)
                p.replace(target)
                for k, ext in kinds:
                    q = fetch.processed_path(data, st, old, k, ext)
                    if q.exists():
                        d = fetch.processed_path(data, st, new, k, ext)
                        d.parent.mkdir(parents=True, exist_ok=True)
                        q.replace(d)

        # ---- รอบที่ 3: สร้าง index CSV ใหม่จากไฟล์ที่เหลือจริง ----
        # แถวซ้ำ / แถวที่หายไปจาก merge conflict จะหายไปในตัว
        log = pipeline.log_path(data, st)
        if log.exists():
            log.unlink()
        smask = strip.load_static_mask(pipeline.static_mask_path(data, st))
        clutter = pipeline.load_clutter(data, st)
        rows = 0
        for p in sorted((data / "raw" / st.code).rglob("*.jpg")):
            stamp = _stamp_from_name(p)
            full = full_stamp.get(stamp, stamp)   # เวลาที่มีวินาทีจริงจาก footer
            img = Image.open(p).convert("RGB")
            pal_rgb, pal_dbz = pipeline.get_palette(data, st, img)
            res = _strip_frame(img, st, pal_rgb, smask)
            qc_row = {}
            if st.qc:
                res, rep = qcmod.apply_qc(res, st, clutter=clutter, when=full, **st.qc_params)
                qc_row = rep.as_row()
            if "alpha" in outputs:
                _save(_render(res, st, pal_rgb, bg, "alpha"),
                      fetch.processed_path(data, st, stamp, "alpha"))
            if "solid" in outputs:
                _save(_render(res, st, pal_rgb, bg, "solid"),
                      fetch.processed_path(data, st, stamp, "solid"))
            dbz = strip.to_dbz(res, pal_dbz)
            pipeline.append_log(data, st, {
                "station": st.code,
                "timestamp_utc": full.strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_th": fetch.th_time(full).strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_source": "ocr-repaired",
                "coverage_pct": round(res["coverage_pct"], 4),
                "max_dbz": round(float(np.nanmax(dbz)), 1) if res["mask"].any() else "",
                "echo_px": int(res["mask"].sum()),
                "echo_px_ge35": int(np.nansum(dbz >= 35)),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16],
                "raw_file": str(p.relative_to(data)),
                **qc_row,
            })
            rows += 1
        print("[ok]   {}: เขียน index ใหม่ {} แถว -> {}".format(st.code, rows, log))
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


def cmd_basemap(args) -> int:
    """สร้างแผนที่ฐานสองแบบสำหรับหน้าเว็บ จากภาพ raw ที่เก็บไว้

    ทำไม่บ่อย — รันใหม่เมื่อ TMD เปลี่ยนแผนที่ในภาพ หรือเมื่อคลังโตพอจนภาพ
    มัธยฐานสะอาดขึ้น (ยิ่งเฟรมกระจายหลายวัน ก้อนฝนที่ค้างอยู่ที่เดิมยิ่งหลุดออกหมด)
    """
    from . import basemap as bm

    docs = Path(args.docs)
    docs.mkdir(parents=True, exist_ok=True)
    for st in _stations(args):
        paths = sorted((Path(args.data) / "raw" / st.code).rglob("*.jpg"))
        if not paths:
            print(f"[!] {st.code}: ไม่มีภาพ raw")
            continue
        if len(paths) < 20:
            print(f"[!] {st.code}: มีแค่ {len(paths)} เฟรม — น้อยไปที่จะแยกแผนที่ออกจากฝนได้สะอาด "
                  f"(ควรมี >= 20 เฟรมกระจายหลายชั่วโมง) จะสร้างให้แต่ควรทำใหม่ทีหลัง")
        bm.build(paths, st, docs, size=args.size, max_frames=args.n)
        print(f"[ok] {st.code}: แผนที่ฐาน (สว่าง+มืด) จาก {min(len(paths), args.n)} เฟรม -> {docs}")
    return 0


def cmd_site(args) -> int:
    """ประกอบ docs/index.html — เปิด GitHub Pages ที่ main /docs แล้วใช้ได้เลย"""
    from . import webapp

    docs = Path(args.docs)
    dark, terr = docs / "base_dark.png", docs / "base_light.png"
    if not (dark.exists() and terr.exists()):
        print("[!] ยังไม่มีแผนที่ฐาน — รัน `basemap` ก่อน")
        return 1
    print(f"[ok] {webapp.build_pages(docs, dark, terr, args.manifest)}")
    if args.demo:
        mf = docs / args.manifest
        if not mf.exists():
            print(f"[!] ไม่มี {mf} — ข้ามไฟล์สาธิตแบบฝังข้อมูล")
        else:
            print(f"[ok] {webapp.build_artifact(Path(args.demo), mf, dark, terr)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar_archive")
    p.add_argument("--docs", default=str(Path(__file__).resolve().parent.parent / "docs"),
                   help="โฟลเดอร์ที่ GitHub Pages เสิร์ฟ")
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

    rp = sub.add_parser("repair", help="แก้เวลาที่ OCR อ่านผิด + ลบไฟล์ซ้ำ + สร้าง index ใหม่")
    rp.add_argument("--apply", action="store_true", help="ลงมือแก้จริง (ไม่ใส่ = dry-run)")
    rp.add_argument("--outputs", default="alpha,solid")
    rp.add_argument("--background", default="0,0,0")
    rp.set_defaults(func=cmd_repair)

    bmp = sub.add_parser("basemap", help="สร้างแผนที่ฐานของหน้าเว็บ จากภาพ raw")
    bmp.add_argument("--n", type=int, default=60, help="ใช้กี่เฟรม (กระจายทั่วช่วงเวลา)")
    bmp.add_argument("--size", type=int, default=1446, help="ความละเอียดด้านละกี่พิกเซล")
    bmp.set_defaults(func=cmd_basemap)

    si = sub.add_parser("site", help="ประกอบ docs/index.html")
    si.add_argument("--manifest", default="nowcast/PHS/latest.json",
                    help="ที่อยู่ latest.json เทียบกับ docs/")
    si.add_argument("--demo", default=None,
                    help="เขียนไฟล์สาธิตแบบฝังข้อมูลไว้ที่นี่ด้วย (เปิดจากไฟล์ตรง ๆ ได้)")
    si.set_defaults(func=cmd_site)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
