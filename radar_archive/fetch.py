"""ดึงภาพเรดาร์ล่าสุดจาก TMD + อ่านเวลาจริงของภาพ + กันไฟล์ซ้ำ

หมายเหตุสำคัญ: TMD เขียนทับไฟล์ *_latest.jpg ทุกรอบ ไม่มี archive ย้อนหลัง
เพราะฉะนั้น archive ที่เราเก็บได้จะเริ่มนับจากวันที่ workflow เริ่มรันเท่านั้น
"""
from __future__ import annotations

import hashlib
import io
import re
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


def read_timestamp_ocr(img: Image.Image, st: Station) -> datetime | None:
    """อ่านเวลาจากแถบข้อความล่างภาพ — แม่นกว่า Last-Modified เพราะเป็นเวลาสแกนจริง

    เวลาใน footer ของ TMD เป็น UTC
    """
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        left, top, right, bottom = st.footer_box
        crop = img.convert("L").crop((left, top, right, bottom))
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
        text = pytesseract.image_to_string(crop, config="--psm 7")
    except Exception:
        return None
    m = _TS_RE.search(text)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_latest(st: Station) -> Fetched:
    raw, lm = download(st.url)
    img = Image.open(io.BytesIO(raw))
    img.load()

    ts = read_timestamp_ocr(img, st)
    src = "ocr"
    if ts is None:
        ts, src = (lm, "last-modified") if lm else (datetime.now(timezone.utc), "now")

    return Fetched(
        image=img.convert("RGB"),
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        timestamp=ts,
        timestamp_source=src,
        last_modified=lm,
    )


def fetch_from_file(st: Station, path: Path | str) -> Fetched:
    """อ่านภาพจากไฟล์ในเครื่องแทนการดาวน์โหลด — ใช้ทดสอบ และ ingest เฟรมจาก loop GIF"""
    raw = Path(path).read_bytes()
    img = Image.open(io.BytesIO(raw))
    img.load()
    ts = read_timestamp_ocr(img, st)
    src = "ocr"
    if ts is None:
        ts, src = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc), "file-mtime"
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
