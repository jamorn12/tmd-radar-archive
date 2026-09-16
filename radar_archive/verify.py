"""เก็บและตรวจสอบผลพยากรณ์ที่ระบบผลิตจริง (operational verification)

    python -m radar_archive.verify --station PHS archive              # เก็บรอบปัจจุบัน
    python -m radar_archive.verify --station PHS recover              # กู้ของเก่าจาก git
    python -m radar_archive.verify --station PHS score                # คำนวณ CSI/POD/FAR
    python -m radar_archive.verify --station PHS status               # ดูว่ามีอะไรในคลังบ้าง

ปัญหาที่โมดูลนี้แก้
    docs/nowcast/PHS/f/<epoch>.png ตั้งชื่อด้วย "เวลาที่ภาพมีผล" อย่างเดียว
    ไม่ได้บอกว่าทำนายจากรอบไหน  ทุก 15 นาทีรอบใหม่จึงเขียนทับไฟล์เดิม
    และสุดท้ายภาพสังเกตจริงจะทับภาพพยากรณ์ไปอีกที

    วัดจริงจากประวัติ git: ไฟล์ f/1789452000.png ถูกเขียนทับ 8 ครั้ง เนื้อหาต่างกันทุกครั้ง
    (T+114 -> T+99 -> ... -> T+9 -> ภาพสังเกต)

    การ verify ต้องรู้ทั้ง **origin** และ **lead** ไม่ใช่แค่ valid time
    เพราะคำถามคือ "ตอน 10:00 เราทำนายว่า 11:00 จะเป็นอย่างไร แล้วจริง ๆ เป็นอย่างไร"

โครงคลังที่โมดูลนี้สร้าง
    data/nowcast/<CODE>/
        <origin_epoch>/
            meta.json        motion, zr, levels, engine, confidence ของรอบนั้น
            obs.png          ภาพสังเกต ณ origin (offset 0)
            f+015.png        พยากรณ์ lead 15 นาที
            ...
            f+120.png

    ตั้งชื่อโฟลเดอร์ด้วย origin และชื่อไฟล์ด้วย lead -> ไม่มีทางทับกันเอง
    และ obs ของ origin หนึ่ง คือ "ความจริง" สำหรับ lead ของ origin ก่อนหน้า
    ทำให้คลังนี้ตรวจสอบตัวเองได้ครบ ไม่ต้องพึ่งไฟล์ภายนอก

ทำไมต้องเก็บภาพสังเกตซ้ำทั้งที่มีในคลังดิบแล้ว
    เพราะเราต้องการ "สิ่งที่ระบบเห็นจริง ณ เวลานั้น" ซึ่งผ่าน QC และ palette
    ชุดเดียวกับที่ใช้พยากรณ์  ถ้าไปดึงจากภาพดิบแล้วประมวลผลใหม่ อาจได้คนละค่า
    เพราะ palette สกัดรายรอบและ QC ขึ้นกับ clutter map ที่อัปเดตตามเวลา

operational verification ต่างจาก backtest อย่างไร
    backtest    รันย้อนหลังด้วย stack ที่ build ใหม่ทั้งชุด ข้อมูลครบเสมอ
                ตอบว่า "วิธีนี้ดีแค่ไหน"
    operational ใช้เฟรมที่ระบบมีจริง ณ ตอนนั้น รวมรอบที่ขาดเฟรมและ OCR เพี้ยน
                ตอบว่า "ระบบที่เดินอยู่จริงดีแค่ไหน"  <- คำถามที่ reviewer อยากรู้
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .config import CONFIG_PATH, get_station

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

MANIFEST_REL = "docs/nowcast/{code}/latest.json"
PAL_TOL2 = 8 * 8          # ระยะสีสูงสุดที่ยอมรับ — วัดจากเฟรมจริงต่างกันไม่เกิน 1.7 หน่วย


# ---------------------------------------------------------------- 1. ตัวช่วย

def store_dir(root: Path, code: str) -> Path:
    return Path(root) / "nowcast" / code


def origin_of(doc: dict) -> int | None:
    """หา epoch ของ origin จากเฟรม offset 0 — ตรงกว่าการอ่าน base_time_utc ที่เป็นสตริง"""
    for f in doc.get("frames", []):
        if f.get("offset_min") == 0:
            try:
                return int(Path(f["url"]).stem)
            except (ValueError, KeyError):
                return None
    return None


def decode_dbz(png_bytes: bytes, levels_rgb: list, levels_dbz: list) -> np.ndarray:
    """PNG -> สนาม dBZ  (NaN = ไม่มี echo)

    ภาพถูก render ด้วย palette ชุดนี้เอง การจับคู่กลับจึงเกือบตรงเป๊ะ
    ที่ต่างเล็กน้อย (<=1.7 หน่วย) มาจากการปัดเศษระหว่าง palette รายรอบกับที่เก็บใน meta
    สีที่ห่างเกิน PAL_TOL2 = ไม่ใช่สีใน palette -> ถือว่าไม่มีฝน
    """
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    a = np.asarray(im, dtype=np.uint8)
    rgb, alpha = a[..., :3].astype(np.int32), a[..., 3]

    out = np.full(rgb.shape[:2], np.nan, np.float32)
    opaque = alpha >= 128
    if not opaque.any():
        return out

    # เทียบเฉพาะ "สีที่ไม่ซ้ำ" ในภาพ แล้วค่อยกระจายผลกลับ
    # ภาพหนึ่งมีสีจริงราว 10-20 สี การไล่ทีละพิกเซล (241x241x22) จึงเสียเปล่ามาก
    packed = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    uniq, inv = np.unique(packed[opaque], return_inverse=True)
    ur = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], -1).astype(np.int32)

    pal = np.asarray(levels_rgb, dtype=np.int32)          # (P, 3)
    dbz = np.asarray(levels_dbz, dtype=np.float32)        # (P,)
    d2 = ((ur[:, None, :] - pal[None, :, :]) ** 2).sum(-1)   # (U, P) — U มักไม่เกิน 30
    best = d2.argmin(1)
    val = np.where(d2[np.arange(len(ur)), best] <= PAL_TOL2, dbz[best], np.nan)

    out[opaque] = val[inv]
    return out


def contingency(fc: np.ndarray, ob: np.ndarray, thr: float) -> tuple:
    """นับ hit / false alarm / miss ที่ threshold เดียวกันทั้งสองฝั่ง

    NaN ในภาพ = ไม่มี echo ไม่ใช่ไม่มีข้อมูล (นอกรัศมีถูก render เป็นโปร่งใสเหมือนกัน)
    จึงนับเป็น "ไม่มีฝน" ได้ตรง ๆ  ต่างจาก stack .npz ที่ NaN แปลว่านอกโดม
    """
    f = np.nan_to_num(fc, nan=-99.0) >= thr
    o = np.nan_to_num(ob, nan=-99.0) >= thr
    return int((f & o).sum()), int((f & ~o).sum()), int((~f & o).sum())


def scores(h: int, fa: int, m: int) -> dict:
    tot = h + fa + m
    return dict(
        CSI=round(h / tot, 4) if tot else None,
        POD=round(h / (h + m), 4) if (h + m) else None,
        FAR=round(fa / (h + fa), 4) if (h + fa) else None,
        BIAS=round((h + fa) / (h + m), 4) if (h + m) else None,
    )


# ---------------------------------------------------------------- 2. เก็บรอบปัจจุบัน

META_KEYS = ("station", "generated", "base_time_utc", "projection", "grid", "kmperpixel",
             "timestep_min", "yorigin", "levels_dbz", "levels_rgb", "zr",
             "motion", "wet_threshold_dbz", "qc", "source")


def archive_one(doc: dict, read_frame, out_root: Path, code: str,
                overwrite: bool = False) -> tuple:
    """เก็บหนึ่ง origin ลงคลัง  read_frame(url) -> bytes | None

    คืน (origin_epoch, จำนวนไฟล์ที่เขียน) หรือ (None, 0) ถ้าข้าม
    """
    origin = origin_of(doc)
    if origin is None:
        return None, 0

    d = store_dir(out_root, code) / str(origin)
    if d.exists() and not overwrite and (d / "meta.json").exists():
        return origin, 0                        # เก็บไว้แล้ว ข้าม

    written = 0
    tmp = {}
    for f in doc.get("frames", []):
        off = f.get("offset_min")
        if off is None or off < 0:
            continue                            # เฟรมอดีตไม่ต้องเก็บ — เป็น obs ของ origin ก่อนหน้าอยู่แล้ว
        b = read_frame(f["url"])
        if b is None:
            continue
        tmp["obs.png" if off == 0 else f"f+{off:03d}.png"] = b

    if "obs.png" not in tmp:                     # ไม่มีภาพสังเกต -> ตรวจสอบไม่ได้ ไม่ต้องเก็บ
        return origin, 0

    d.mkdir(parents=True, exist_ok=True)
    for name, b in tmp.items():
        (d / name).write_bytes(b)
        written += 1
    (d / "meta.json").write_text(
        json.dumps({k: doc[k] for k in META_KEYS if k in doc}, ensure_ascii=False),
        encoding="utf-8")
    return origin, written


def cmd_archive(a, st) -> int:
    """เก็บรอบล่าสุดจาก docs/ — เรียกทุกครั้งหลัง nowcast ใน workflow"""
    man = Path(a.docs) / "nowcast" / st.code / "latest.json"
    if not man.exists():
        print(f"[!] ไม่มี {man} — ยังไม่ได้รัน nowcast?", file=sys.stderr)
        return 1
    doc = json.loads(man.read_text(encoding="utf-8"))

    def read(url):
        p = man.parent / url
        return p.read_bytes() if p.exists() else None

    origin, n = archive_one(doc, read, Path(a.data), st.code, overwrite=a.overwrite)
    if origin is None:
        print("[!] manifest ไม่มีเฟรม offset 0", file=sys.stderr)
        return 1
    when = datetime.fromtimestamp(origin, timezone.utc)
    print(f"origin {when:%Y-%m-%d %H:%MZ} -> เขียน {n} ไฟล์" if n else
          f"origin {when:%Y-%m-%d %H:%MZ} -> มีอยู่แล้ว ข้าม")
    return 0


# ---------------------------------------------------------------- 3. กู้จาก git

def cmd_recover(a, st) -> int:
    """เดินประวัติ git ดึงภาพพยากรณ์ที่ถูกเขียนทับไปแล้วกลับมา

    ใช้ `git cat-file --batch` ตัวเดียวอ่านทุก blob แทนการเรียก `git show` ทีละไฟล์
    ที่ 1,000 commit x 9 ไฟล์ = 9,000 ครั้ง ถ้าเรียกทีละครั้งจะช้ามาก
    """
    rel = MANIFEST_REL.format(code=st.code)
    try:
        out = subprocess.run(["git", "log", "--format=%H", "--", rel],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[!] เรียก git ไม่ได้: {e}", file=sys.stderr)
        return 1
    commits = [c for c in out.split() if c]
    if a.limit:
        commits = commits[:a.limit]
    print(f"พบ {len(commits)} commit ที่แตะ {rel}")

    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def blob(spec: str):
        """อ่าน blob หนึ่งก้อนจาก git cat-file --batch"""
        proc.stdin.write((spec + "\n").encode()); proc.stdin.flush()
        head = proc.stdout.readline().decode().strip()
        if head.endswith("missing") or " " not in head:
            return None
        size = int(head.split()[-1])
        data = proc.stdout.read(size)
        proc.stdout.read(1)                     # newline ปิดท้าย
        return data

    base = f"docs/nowcast/{st.code}"
    n_origin = n_file = n_skip = 0
    for i, c in enumerate(commits, 1):
        raw = blob(f"{c}:{rel}")
        if raw is None:
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        origin, n = archive_one(doc, lambda u: blob(f"{c}:{base}/{u}"),
                                Path(a.data), st.code, overwrite=False)
        if n:
            n_origin += 1; n_file += n
        elif origin is not None:
            n_skip += 1
        if i % 100 == 0 or i == len(commits):
            print(f"  {i}/{len(commits)} commit · กู้ได้ {n_origin} origin "
                  f"({n_file} ไฟล์) · มีอยู่แล้ว {n_skip}")
    proc.stdin.close(); proc.wait()
    print(f"\nกู้สำเร็จ {n_origin} origin · {n_file} ไฟล์")
    return 0


# ---------------------------------------------------------------- 4. ให้คะแนน

def cmd_score(a, st) -> int:
    """จับคู่ (origin, lead) -> obs ที่ valid time แล้วนับ contingency"""
    root = store_dir(Path(a.data), st.code)
    if not root.exists():
        print(f"[!] ไม่มีคลังที่ {root} — รัน archive หรือ recover ก่อน", file=sys.stderr)
        return 1

    origins = sorted(int(d.name) for d in root.iterdir()
                     if d.is_dir() and d.name.isdigit())
    have = set(origins)
    print(f"คลังมี {len(origins)} origin")
    if not origins:
        return 1

    rows, cache = [], {}
    thr = a.threshold
    n_pair = n_noobs = 0

    for oi, o in enumerate(origins, 1):
        d = root / str(o)
        meta_p = d / "meta.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        lv_rgb, lv_dbz = meta.get("levels_rgb"), meta.get("levels_dbz")
        if not lv_rgb or not lv_dbz:
            continue
        thr_use = thr if thr is not None else float(meta.get("wet_threshold_dbz", 11.98))
        mot = meta.get("motion", {})

        for p in sorted(d.glob("f+*.png")):
            lead = int(p.stem[2:])
            valid = o + lead * 60
            if valid not in have:
                n_noobs += 1
                continue
            ob_p = root / str(valid) / "obs.png"
            if not ob_p.exists():
                n_noobs += 1
                continue

            if valid not in cache:
                cache[valid] = decode_dbz(ob_p.read_bytes(), lv_rgb, lv_dbz)
                if len(cache) > 400:
                    cache.pop(next(iter(cache)))
            ob = cache[valid]
            fc = decode_dbz(p.read_bytes(), lv_rgb, lv_dbz)
            if fc.shape != ob.shape:
                continue

            h, fa, m = contingency(fc, ob, thr_use)
            # persistence baseline: ใช้ภาพสังเกต ณ origin เป็นคำพยากรณ์
            oo = decode_dbz((d / "obs.png").read_bytes(), lv_rgb, lv_dbz)
            ph, pfa, pm = contingency(oo, ob, thr_use)

            rows.append(dict(
                origin=o, origin_utc=datetime.fromtimestamp(o, timezone.utc).isoformat(),
                lead_min=lead, threshold_dbz=thr_use,
                hit=h, fa=fa, miss=m,
                p_hit=ph, p_fa=pfa, p_miss=pm,
                engine=mot.get("engine"), confidence=mot.get("confidence"),
                motion_kmh=mot.get("kmh"), bearing=mot.get("bearing"),
            ))
            n_pair += 1
        if oi % 200 == 0:
            print(f"  ประมวลผล {oi}/{len(origins)} origin · จับคู่ได้ {n_pair}")

    if not rows:
        print("[!] จับคู่ไม่ได้เลย — คลังอาจมี origin ไม่ต่อเนื่องพอ", file=sys.stderr)
        return 1

    out = Path(a.data) / "nowcast" / f"{st.code}_verify.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    print(f"\nจับคู่ได้ {n_pair} คู่ · ไม่มี obs ให้เทียบ {n_noobs} คู่")
    print(f"-> {out}\n")

    leads = sorted({r["lead_min"] for r in rows})
    print(f"{'lead':>5} {'n':>5} {'CSI':>7} {'POD':>7} {'FAR':>7} {'BIAS':>7} "
          f"{'persist':>8} {'skill':>7}")
    for l in leads:
        sub = [r for r in rows if r["lead_min"] == l]
        s = scores(sum(r["hit"] for r in sub), sum(r["fa"] for r in sub),
                   sum(r["miss"] for r in sub))
        ps = scores(sum(r["p_hit"] for r in sub), sum(r["p_fa"] for r in sub),
                    sum(r["p_miss"] for r in sub))
        sk = (s["CSI"] - ps["CSI"]) if (s["CSI"] is not None and ps["CSI"] is not None) else None
        fmt = lambda v: f"{v:.4f}" if v is not None else "   -  "
        print(f"{l:5d} {len(sub):5d} {fmt(s['CSI']):>7} {fmt(s['POD']):>7} "
              f"{fmt(s['FAR']):>7} {fmt(s['BIAS']):>7} {fmt(ps['CSI']):>8} "
              f"{(f'{sk:+.4f}' if sk is not None else '   -  '):>7}")
    return 0


# ---------------------------------------------------------------- 4b. ตรวจ ETA

def _shift(a: np.ndarray, dr: float, dc: float) -> np.ndarray:
    """เลื่อนสนามแบบ nearest ไม่วน (นอกกรอบเป็น NaN)

    ใช้ nearest ไม่ใช่ bilinear เพราะเราถามแค่ "ตรงนั้นมี echo ไหม"
    ไม่ได้ต้องการค่า dBZ ที่แม่นระดับ sub-pixel
    """
    r, c = int(round(dr)), int(round(dc))
    out = np.full_like(a, np.nan)
    h, w = a.shape
    rs0, rs1 = max(0, r), min(h, h + r)          # ช่วงที่อ่านจากต้นทาง
    cs0, cs1 = max(0, c), min(w, w + c)
    rd0, rd1 = max(0, -r), min(h, h - r)         # ช่วงที่เขียนลงปลายทาง
    cd0, cd1 = max(0, -c), min(w, w - c)
    if rs1 > rs0 and cs1 > cs0:
        out[rd0:rd1, cd0:cd1] = a[rs0:rs1, cs0:cs1]
    return out


ETA_STEPS = tuple(range(5, 121, 5))        # วิธี B เดินทวนลมทีละ 5 นาที เหมือนในหน้าเว็บ
ETA_LEADS = tuple(range(15, 121, 15))      # lead ที่ระบบผลิตจริง


def cmd_eta(a, st) -> int:
    """ตรวจว่าการประมาณ "อีกกี่นาทีฝนจะมาถึง" แม่นแค่ไหน

    ทำซ้ำตรรกะสองวิธีที่หน้าเว็บใช้ แล้วเทียบกับสิ่งที่เกิดขึ้นจริง
      A  Lagrangian point sampling  — อ่านเฟรมพยากรณ์ตรงจุดนั้น
      B  upwind ray (frozen turbulence) — เดินทวนลมจากเฟรมปัจจุบัน

    ความจริง = ภาพสังเกตของ origin ถัด ๆ ไป ซึ่งอยู่ในคลังเดียวกันอยู่แล้ว

    สนใจเฉพาะจุดที่ "ตอนนี้ยังไม่มีฝน" เพราะ ETA มีความหมายเฉพาะกรณีนั้น
    """
    root = store_dir(Path(a.data), st.code)
    origins = sorted(int(d.name) for d in root.iterdir()
                     if d.is_dir() and d.name.isdigit()) if root.exists() else []
    if not origins:
        print(f"[!] ไม่มีคลังที่ {root}", file=sys.stderr)
        return 1
    have = set(origins)
    cache: dict = {}

    def obs(ep, lv_rgb, lv_dbz):
        if ep not in cache:
            p = root / str(ep) / "obs.png"
            if not p.exists():
                return None
            cache[ep] = decode_dbz(p.read_bytes(), lv_rgb, lv_dbz)
            if len(cache) > 300:
                cache.pop(next(iter(cache)))
        return cache[ep]

    rows = []
    stride = a.stride
    for oi, o in enumerate(origins, 1):
        d = root / str(o)
        mp = d / "meta.json"
        if not mp.exists():
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        lv_rgb, lv_dbz = meta.get("levels_rgb"), meta.get("levels_dbz")
        if not lv_rgb:
            continue
        thr = a.threshold if a.threshold is not None else float(meta.get("wet_threshold_dbz", 11.98))
        kpp = float(meta.get("kmperpixel", 2.0))
        mot = meta.get("motion", {})
        spd = mot.get("kmh")
        deg = mot.get("bearing")
        if spd is None or deg is None:
            continue

        f0 = obs(o, lv_rgb, lv_dbz)
        if f0 is None:
            continue
        n = f0.shape[0]

        # ต้องมีภาพสังเกตครบทุก lead ถึงจะรู้ "ความจริง" ได้
        truth = np.full(f0.shape, np.nan, np.float32)
        ok_all = True
        for L in reversed(ETA_LEADS):                 # ไล่จากหลังมาหน้า -> ค่าสุดท้ายคือ lead แรกสุดที่ฝนมา
            fl = obs(o + L * 60, lv_rgb, lv_dbz)
            if fl is None:
                ok_all = False; break
            truth[np.nan_to_num(fl, nan=-99.0) >= thr] = L
        if not ok_all:
            continue

        wet_now = np.nan_to_num(f0, nan=-99.0) >= thr

        # กรอบโดม — ตัดมุมภาพที่อยู่นอกรัศมีออก
        yy, xx = np.mgrid[0:n, 0:n]
        cen = (n - 1) / 2.0
        inside = np.hypot(yy - cen, xx - cen) * kpp <= (st.range_km - 10)

        # --- วิธี B: เดินทวนลม ---
        up = np.radians((deg + 180.0) % 360.0)
        eta_b = np.full(f0.shape, np.nan, np.float32)
        for m in reversed(ETA_STEPS):
            dist_px = (spd * (m / 60.0)) / kpp
            dc = dist_px * np.sin(up)
            dr = -dist_px * np.cos(up)                # แถวเพิ่มไปทางใต้
            src = _shift(f0, dr, dc)
            eta_b[np.nan_to_num(src, nan=-99.0) >= thr] = m

        # --- วิธี A: อ่านเฟรมพยากรณ์ ---
        eta_a = np.full(f0.shape, np.nan, np.float32)
        for L in reversed(ETA_LEADS):
            fp = d / f"f+{L:03d}.png"
            if not fp.exists():
                continue
            fc = decode_dbz(fp.read_bytes(), lv_rgb, lv_dbz)
            if fc.shape != f0.shape:
                continue
            eta_a[np.nan_to_num(fc, nan=-99.0) >= thr] = L

        sel = inside & ~wet_now
        sel[::stride, ::stride] &= True
        mask = np.zeros_like(sel); mask[::stride, ::stride] = True
        sel &= mask
        if not sel.any():
            continue

        # หน้าเว็บใช้ A ก่อน ถ้าไม่เจอถึงตกไป B -> พฤติกรรมจริงคือการรวมสองวิธี
        eta_c = np.where(np.isfinite(eta_a), eta_a, eta_b)

        for name, eta in (("A_frames", eta_a), ("B_upwind", eta_b), ("app_A_then_B", eta_c)):
            e, t = eta[sel], truth[sel]
            pred = np.isfinite(e)
            came = np.isfinite(t)
            hit = pred & came
            rows.append(dict(
                origin=o, method=name, n_points=int(sel.sum()),
                predicted=int(pred.sum()), actually_came=int(came.sum()),
                hit=int(hit.sum()), false_alarm=int((pred & ~came).sum()),
                missed=int((~pred & came).sum()),
                err_med=float(np.median(e[hit] - t[hit])) if hit.any() else None,
                err_mean=float(np.mean(e[hit] - t[hit])) if hit.any() else None,
                early=int(((e < t) & hit).sum()), late=int(((e > t) & hit).sum()),
                spd_kmh=spd, bearing=deg, confidence=mot.get("confidence"),
                engine=mot.get("engine"), threshold_dbz=thr,
            ))
        if oi % 200 == 0:
            print(f"  {oi}/{len(origins)} origin · เก็บได้ {len(rows)//3} รอบ")

    if not rows:
        print("[!] ไม่มีรอบที่มีภาพสังเกตครบทุก lead", file=sys.stderr)
        return 1

    out = Path(a.data) / "nowcast" / f"{st.code}_eta.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(f"\nรอบที่ตรวจได้ {len(rows)//3} · จุดตัวอย่าง stride {stride}")
    print(f"-> {out}\n")

    print(f"{'method':<13}{'จุด':>9}{'ทายว่ามา':>11}{'มาจริง':>9}{'FAR':>8}{'POD':>8}"
          f"{'คลาด(med)':>11}{'เร็วไป':>8}{'ช้าไป':>8}")
    for name in ("A_frames", "B_upwind", "app_A_then_B"):
        sub = [r for r in rows if r["method"] == name]
        P = sum(r["predicted"] for r in sub); H = sum(r["hit"] for r in sub)
        FA = sum(r["false_alarm"] for r in sub); MS = sum(r["missed"] for r in sub)
        errs = [r["err_med"] for r in sub if r["err_med"] is not None]
        print(f"{name:<13}{sum(r['n_points'] for r in sub):>9,}{P:>11,}"
              f"{H + MS:>9,}{(FA / P if P else 0):>8.3f}{(H / (H + MS) if H + MS else 0):>8.3f}"
              f"{(float(np.median(errs)) if errs else 0):>+10.1f}น."
              f"{sum(r['early'] for r in sub):>8,}{sum(r['late'] for r in sub):>8,}")

    print(f"\n{'conf':<8}{'method':<13}{'n รอบ':>7}{'FAR':>8}{'POD':>8}{'คลาด(med)':>11}")
    for c in ("high", "medium", "low"):
        for name in ("A_frames", "B_upwind", "app_A_then_B"):
            sub = [r for r in rows if r["method"] == name and r["confidence"] == c]
            if not sub:
                continue
            P = sum(r["predicted"] for r in sub); H = sum(r["hit"] for r in sub)
            FA = sum(r["false_alarm"] for r in sub); MS = sum(r["missed"] for r in sub)
            errs = [r["err_med"] for r in sub if r["err_med"] is not None]
            print(f"{c:<8}{name:<13}{len(sub):>7}{(FA / P if P else 0):>8.3f}"
                  f"{(H / (H + MS) if H + MS else 0):>8.3f}"
                  f"{(float(np.median(errs)) if errs else 0):>+10.1f}น.")
    return 0


# ---------------------------------------------------------------- 5. สถานะคลัง

def cmd_status(a, st) -> int:
    root = store_dir(Path(a.data), st.code)
    if not root.exists():
        print(f"ยังไม่มีคลังที่ {root}")
        return 0
    dirs = sorted(int(d.name) for d in root.iterdir() if d.is_dir() and d.name.isdigit())
    if not dirs:
        print("คลังว่าง")
        return 0
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    nfile = sum(1 for f in root.rglob("*.png"))
    a0, a1 = datetime.fromtimestamp(dirs[0], timezone.utc), datetime.fromtimestamp(dirs[-1], timezone.utc)
    print(f"origin   : {len(dirs)} รอบ")
    print(f"ช่วงเวลา : {a0:%d %b %H:%M}Z - {a1:%d %b %H:%M}Z")
    print(f"ไฟล์ PNG : {nfile} · ขนาดรวม {size/1024/1024:.1f} MB")
    gaps = sum(1 for x, y in zip(dirs, dirs[1:]) if y - x > 900)
    print(f"ช่วงขาด  : {gaps}")
    have = set(dirs)
    pairs = sum(1 for o in dirs for l in range(15, 121, 15) if o + l * 60 in have)
    print(f"คู่ที่ตรวจได้: ~{pairs} (forecast-observation)")
    return 0


# ---------------------------------------------------------------- 6. CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="radar_archive.verify",
        description="เก็บผลพยากรณ์ที่ระบบผลิตจริง และให้คะแนนเทียบกับสิ่งที่เกิดขึ้นจริง")
    p.add_argument("mode", choices=["archive", "recover", "score", "status", "eta"])
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--docs", default=str(DOCS))
    p.add_argument("--overwrite", action="store_true", help="เขียนทับ origin ที่มีอยู่แล้ว")
    p.add_argument("--limit", type=int, default=None, help="recover: จำกัดจำนวน commit")
    p.add_argument("--stride", type=int, default=6,
                   help="eta: สุ่มจุดทุก ๆ กี่พิกเซล (6 = ทุก 12 กม.)")
    p.add_argument("--threshold", type=float, default=None,
                   help="score: dBZ (ค่าเริ่มต้นใช้ wet_threshold_dbz ของแต่ละรอบ)")
    a = p.parse_args(argv)

    st = get_station(a.station, a.config)
    return {"archive": cmd_archive, "recover": cmd_recover, "score": cmd_score,
            "status": cmd_status, "eta": cmd_eta}[a.mode](a, st)


if __name__ == "__main__":
    raise SystemExit(main())
