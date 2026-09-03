"""รวมทุกขั้นตอนเข้าด้วยกัน: ดึง -> ตัด -> เซฟ -> บันทึก log"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import fetch, palette, qc as qcmod, refine, strip
from .config import Station

LOG_FIELDS = [
    "station", "timestamp_utc", "timestamp_th", "timestamp_source",
    "coverage_pct", "max_dbz", "echo_px", "echo_px_ge35",
    "qc_spike_px", "qc_spike_az", "qc_spike_type",
    "qc_clutter_px", "qc_removed_px", "qc_removed_pct",
    "sha256", "raw_file",
]


def clutter_path(root: Path, st: Station) -> Path:
    return Path(root) / "masks" / f"{st.code}_clutter_freq.npy"


def load_clutter(root: Path, st: Station) -> "np.ndarray | None":
    p = clutter_path(root, st)
    if not p.exists():
        return None
    return np.load(p) >= st.clutter_thresh


def palette_path(root: Path, st: Station) -> Path:
    return Path(root) / "masks" / f"{st.code}_palette.json"


def static_mask_path(root: Path, st: Station) -> Path:
    return Path(root) / "masks" / f"{st.code}_static_mask.png"


def log_path(root: Path, st: Station) -> Path:
    return Path(root) / "log" / f"{st.code}_index.csv"


def get_palette(root: Path, st: Station, img: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """ใช้ palette ที่เคยสกัดไว้ ถ้ายังไม่มีก็สกัดจากภาพนี้แล้วเก็บไว้"""
    p = palette_path(root, st)
    if p.exists():
        return palette.load_palette(p)
    rgb, dbz = palette.extract_palette(img, st)
    palette.save_palette(p, rgb, dbz)
    return rgb, dbz


def append_log(root: Path, st: Station, row: dict) -> None:
    p = log_path(root, st)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def run_once(
    st: Station,
    root: Path,
    outputs: tuple[str, ...] = ("alpha", "solid"),
    solid_background: tuple[int, int, int] = (0, 0, 0),
    force: bool = False,
    local_file: Path | None = None,
) -> dict | None:
    """ดึงภาพล่าสุด 1 เฟรม แล้วประมวลผล คืน dict สรุป หรือ None ถ้าเป็นเฟรมซ้ำ

    local_file: ถ้าระบุ จะอ่านจากไฟล์ในเครื่องแทนการดาวน์โหลด
                (ใช้ตอนทดสอบ หรือตอน ingest เฟรมที่แกะจาก loop GIF)
    """
    root = Path(root)
    f = fetch.fetch_latest(st) if local_file is None else fetch.fetch_from_file(st, local_file)

    if not force and fetch.already_have(root, st, f.timestamp):
        return None

    raw_file = fetch.save_raw(root, st, f)
    pal_rgb, pal_dbz = get_palette(root, st, f.image)

    if palette.palette_changed(pal_rgb, palette.extract_palette(f.image, st)[0]):
        print(f"[!] {st.code}: colorbar ของ TMD ดูเหมือนเปลี่ยนไป — ตรวจสอบ {palette_path(root, st)}")

    smask = strip.load_static_mask(static_mask_path(root, st))
    if st.refine:
        res = refine.strip_background_refined(f.image, st, pal_rgb, static_mask=smask,
                                              **st.refine_params)
        render_alpha = lambda: refine.render_alpha(res, pal_rgb)
        render_solid = lambda: refine.render_solid(res, pal_rgb, solid_background)
    else:
        res = strip.strip_background(f.image, st, pal_rgb, static_mask=smask)
        render_alpha = lambda: strip.render_alpha(res)
        render_solid = lambda: strip.render_solid(res, solid_background)

    qc_row = {}
    if st.qc:
        res, rep = qcmod.apply_qc(res, st, clutter=load_clutter(root, st),
                                  when=f.timestamp, **st.qc_params)
        qc_row = rep.as_row()
        if rep.spike_px:
            print(f"[qc]   {st.code}: ตัด radial spike {rep.spike_px} px "
                  f"({rep.spike_type}, az {', '.join(f'{a:.1f}' for a in rep.spike_azimuths_deg)})")

    if "alpha" in outputs:
        p = fetch.processed_path(root, st, f.timestamp, "alpha")
        p.parent.mkdir(parents=True, exist_ok=True)
        render_alpha().save(p, optimize=True)
    if "solid" in outputs:
        p = fetch.processed_path(root, st, f.timestamp, "solid")
        p.parent.mkdir(parents=True, exist_ok=True)
        render_solid().save(p, optimize=True)

    dbz = strip.to_dbz(res, pal_dbz)
    if "dbz" in outputs:
        p = fetch.processed_path(root, st, f.timestamp, "dbz", ext="npz")
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(p, dbz=dbz.astype(np.float32))

    summary = {
        "station": st.code,
        "timestamp_utc": f.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_th": fetch.th_time(f.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_source": f.timestamp_source,
        "coverage_pct": round(res["coverage_pct"], 4),
        "max_dbz": round(float(np.nanmax(dbz)), 1) if res["mask"].any() else "",
        "echo_px": int(res["mask"].sum()),
        "echo_px_ge35": int(np.nansum(dbz >= 35)),
        "sha256": f.sha256[:16],
        "raw_file": str(raw_file.relative_to(root)),
        **qc_row,
    }
    append_log(root, st, summary)
    return summary


def prune_old(root: Path, st: Station, keep_days: int) -> int:
    """ลบไฟล์ที่เก่ากว่า keep_days ออกจาก working copy (หลัง sync ขึ้น Drive แล้ว)"""
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    removed = 0
    for sub in ("raw", "processed"):
        base = Path(root) / sub / st.code
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed
