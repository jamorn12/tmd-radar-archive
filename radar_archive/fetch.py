"""ดึงภาพเรดาร์ล่าสุดจาก TMD + อ่านเวลาจริงของภาพ + กันไฟล์ซ้ำ

หมายเหตุสำคัญ: TMD เขียนทับไฟล์ *_latest.jpg ทุกรอบ ไม่มี archive ย้อนหลัง
เพราะฉะนั้น archive ที่เราเก็บได้จะเริ่มนับจากวันที่ workflow เริ่มรันเท่านั้น
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from PIL import Image

from .config import Station

UA = "tmd-radar-archive/0.1 (research; contact via github issues)"
TIMEOUT = 30
TH = timezone(timedelta(hours=7))

# footer ตัวอย่าง: "PHI 2026-09-02 11:45:02 PPI Filtered Intensity(Horizontal) El:0.50° Sweep: 1 Polar"
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")


@dataclass
class Fetched:
    image: Image.Image
    raw_bytes: bytes
    sha256: str
    timestamp: datetime          # เวลาของภาพ (UTC)
    timestamp_source: str        # "ocr" | "last-modified" | "now"
    last_modified: datetime | None


def download(url: str) -> tuple[bytes, datetime | None]:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    lm = None
    if "Last-Modified" in r.headers:
        try:
            lm = parsedate_to_datetime(r.headers["Last-Modified"]).astimezone(timezone.utc)
        except Exception:
            lm = None
    return r.content, lm


# ---------------------------------------------------------------- OCR เวลาสแกน
#
# บทเรียนจากข้อมูลจริง (2026-09-03): เฟรมที่ footer เขียน 06:00:02 ถูกอ่านเป็น 08:00:02
# ต้นเหตุคือการ upscale ด้วย **LANCZOS** — ฟิลเตอร์นี้เกลี่ยขอบจนช่องเปิดของเลข 6
# ถูกปิดจนดูเหมือน 8   ทดสอบกับ 22 เฟรมจริง: LANCZOS ผิด 1 เฟรม / NEAREST ถูกทั้งหมด
#
# กันไว้สองชั้น
#   1. อ่านหลายแบบแล้วโหวต — ไม่ฝากชีวิตไว้กับ preprocessing ตัวเดียว
#   2. ตรวจความสมเหตุสมผลกับเวลาอ้างอิงจริง (Last-Modified / เวลาดาวน์โหลด)
#      เวลาสแกนต้องไม่ล้ำอนาคตและไม่เก่าเกินไป ถ้าหลุดกรอบ = ไม่เชื่อ OCR
#
# (scale, resample, psm)
_OCR_VARIANTS = (
    (3, Image.NEAREST, 7),
    (4, Image.NEAREST, 7),
    (4, Image.BICUBIC, 7),
    (6, Image.NEAREST, 6),
    (5, Image.NEAREST, 13),
)

OCR_MAX_AGE_MIN = 40.0    # เวลาสแกนเก่ากว่าเวลาอ้างอิงเกินนี้ = อ่านผิด (TMD อัปเดตทุก 15 นาที)
OCR_MAX_SKEW_MIN = 5.0    # ล้ำหน้าเวลาอ้างอิงได้ไม่เกินนี้ (เผื่อนาฬิกาคลาด)
SCAN_SLOT_MIN = 15        # TMD สแกนที่นาที :00 :15 :30 :45


def _ocr_candidates(img: Image.Image, st: Station) -> list:
    """อ่าน footer หลายแบบ คืนเวลาที่ parse ได้ทั้งหมด (ค่าที่ซ้ำ = คะแนนโหวต)"""
    try:
        import pytesseract
    except ImportError:
        return []
    try:
        crop = img.convert("L").crop(tuple(st.footer_box))
    except Exception:
        return []

    out = []
    for scale, resample, psm in _OCR_VARIANTS:
        try:
            big = crop.resize((crop.width * scale, crop.height * scale), resample)
            text = pytesseract.image_to_string(big, config="--psm {}".format(psm))
        except Exception:
            continue
        m = _TS_RE.search(text)
        if not m:
            continue
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            out.append(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc))
        except ValueError:
            continue
    return out


def plausible_scan_time(ts: datetime, ref: "datetime | None") -> bool:
    """เวลาสแกนสมเหตุสมผลไหมเมื่อเทียบกับเวลาอ้างอิง (ref=None คือข้ามการตรวจ)"""
    if ref is None:
        return True
    age_min = (ref - ts).total_seconds() / 60.0
    return -OCR_MAX_SKEW_MIN <= age_min <= OCR_MAX_AGE_MIN


def snap_to_slot(ts: datetime, minutes: int = SCAN_SLOT_MIN) -> datetime:
    """ปัดลงหาช่องเวลาสแกน — ใช้ตอน fallback เพื่อให้แกนเวลายังเป็นระเบียบ"""
    t = ts.astimezone(timezone.utc)
    return t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)


def read_timestamp_ocr(
    img: Image.Image,
    st: Station,
    ref: "datetime | None" = None,
) -> tuple:
    """อ่านเวลาจาก footer — แม่นกว่า Last-Modified เพราะเป็นเวลาสแกนจริง (UTC)

    ref : เวลาอ้างอิงไว้ตรวจความสมเหตุสมผล (Last-Modified หรือเวลาที่ดาวน์โหลด)
          ใส่ None เมื่ออ่านไฟล์เก่าที่ไม่รู้เวลาดาวน์โหลด

    คืน (เวลา, ที่มา) — เวลาเป็น None แปลว่าเชื่อ OCR ไม่ได้ ให้ผู้เรียก fallback เอง
    """
    cands = _ocr_candidates(img, st)
    if not cands:
        return None, "no-text"

    n = len(cands)
    for ts, votes in Counter(cands).most_common():
        if not plausible_scan_time(ts, ref):
            continue
        if votes == n:
            return ts, "ocr"
        if votes * 2 > n:
            return ts, "ocr-majority"
        return ts, "ocr-weak"

    return None, "ocr-implausible"


def fetch_latest(st: Station) -> Fetched:
    raw, lm = download(st.url)
    fetched_at = datetime.now(timezone.utc)
    img = Image.open(io.BytesIO(raw))
    img.load()

    # ใช้ Last-Modified เป็นตัวอ้างอิงหลัก ถ้าไม่มีก็ใช้เวลาที่เพิ่งดาวน์โหลด
    ts, src = read_timestamp_ocr(img, st, ref=lm or fetched_at)
    if ts is None:
        if lm:
            ts, src = lm, "last-modified ({})".format(src)
        else:
            ts, src = snap_to_slot(fetched_at), "now-slot ({})".format(src)
        print("[!] {}: เชื่อเวลาจาก OCR ไม่ได้ ({}) — ใช้ {:%Y-%m-%d %H:%M:%S}Z แทน"
              .format(st.code, src, ts))

    return Fetched(
        image=img.convert("RGB"),
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        timestamp=ts,
        timestamp_source=src,
        last_modified=lm,
    )


def fetch_from_file(st: Station, path: "Path | str", ref: "datetime | None" = None) -> Fetched:
    """อ่านภาพจากไฟล์ในเครื่องแทนการดาวน์โหลด — ใช้ทดสอบ / ingest เฟรมจาก loop GIF

    ไฟล์ในเครื่องไม่มีเวลาดาวน์โหลดให้เทียบ ref จึงเป็น None โดยปริยาย
    (การโหวตยังทำงาน แต่ข้ามการตรวจกรอบเวลา)
    """
    raw = Path(path).read_bytes()
    img = Image.open(io.BytesIO(raw))
    img.load()
    ts, src = read_timestamp_ocr(img, st, ref=ref)
    if ts is None:
        ts = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
        src = "file-mtime ({})".format(src)
    return Fetched(
        image=img.convert("RGB"),
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        timestamp=ts,
        timestamp_source=src,
        last_modified=None,
    )


def raw_path(root: Path, st: Station, ts: datetime) -> Path:
    """data/raw/PHS/2026/09/PHS_20260902_1145Z.jpg  (เวลาเป็น UTC)"""
    t = ts.astimezone(timezone.utc)
    return (
        Path(root) / "raw" / st.code / f"{t:%Y}" / f"{t:%m}"
        / f"{st.code}_{t:%Y%m%d_%H%M}Z.jpg"
    )


def processed_path(root: Path, st: Station, ts: datetime, kind: str, ext: str = "png") -> Path:
    t = ts.astimezone(timezone.utc)
    return (
        Path(root) / "processed" / st.code / kind / f"{t:%Y}" / f"{t:%m}"
        / f"{st.code}_{t:%Y%m%d_%H%M}Z_{kind}.{ext}"
    )


def already_have(root: Path, st: Station, ts: datetime) -> bool:
    return raw_path(root, st, ts).exists()


def save_raw(root: Path, st: Station, f: Fetched) -> Path:
    p = raw_path(root, st, f.timestamp)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(f.raw_bytes)
    return p


def th_time(ts: datetime) -> datetime:
    """แปลงเป็นเวลาไทย ไว้ใช้แสดงผล (ไฟล์ทั้งหมดเก็บเป็น UTC)"""
    return ts.astimezone(TH)
