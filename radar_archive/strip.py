"""ตัด background ออกจากภาพเรดาร์ ให้เหลือเฉพาะ pixel ที่เป็นค่า reflectivity

หลักการ
  1. crop เอาเฉพาะพื้นที่แผนที่ (ตัด colorbar ซ้าย + footer ล่าง)
  2. แปลงทุก pixel เป็น CIELAB แล้วหาสีที่ใกล้ที่สุดใน palette
  3. pixel ที่ห่างจากทุกสีใน palette เกิน tolerance = background -> ทิ้ง
     (ใช้ nearest-color ไม่ใช่ exact match เพราะภาพต้นทางเป็น JPEG มี compression artifact)
  4. ลบ blob เล็ก ๆ ที่เกิดจาก noise
  5. ลบ static overlay (เส้น range ring / เส้นขอบ / ตัวอักษร) ด้วย mask ที่สร้างไว้ล่วงหน้า
     ถ้ามี — ดู build_static_mask()
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from . import lab
from .config import Station
from .palette import to_lab


def crop_plot(img: Image.Image, st: Station) -> Image.Image:
    return img.convert("RGB").crop(st.plot_box)


def classify(
    plot_rgb: np.ndarray,
    pal_rgb: np.ndarray,
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """คืน (mask, band_index, distance)

    mask        : bool (H,W) True = เป็น echo
    band_index  : int  (H,W) index ของสีใน palette ที่ใกล้ที่สุด
    distance    : float(H,W) ระยะใน Lab ไปยังสีที่ใกล้ที่สุด (ใช้ debug/จูน tolerance)
    """
    lab_img = lab.rgb2lab(plot_rgb / 255.0)
    lab_pal = to_lab(pal_rgb)
    d = np.linalg.norm(lab_img[:, :, None, :] - lab_pal[None, None, :, :], axis=-1)
    dist = d.min(axis=-1)
    idx = d.argmin(axis=-1)
    return dist < tolerance, idx, dist


def clean_mask(
    mask: np.ndarray,
    min_blob_px: int = 6,
    static_mask: np.ndarray | None = None,
) -> np.ndarray:
    out = mask.copy()
    if static_mask is not None:
        out &= ~static_mask
    if min_blob_px > 1:
        out = lab.remove_small(out, min_blob_px)
    return out


def strip_background(
    img: Image.Image,
    st: Station,
    pal_rgb: np.ndarray,
    static_mask: np.ndarray | None = None,
    tolerance: float | None = None,
) -> dict:
    """คืน dict ของผลลัพธ์ทั้งหมด

    keys: plot(RGB array), mask, band_index, distance, coverage_pct
    """
    plot = np.asarray(crop_plot(img, st), dtype=float)
    tol = st.lab_tolerance if tolerance is None else tolerance
    mask, idx, dist = classify(plot, pal_rgb, tol)
    mask = clean_mask(mask, st.min_blob_px, static_mask)
    return {
        "plot": plot.astype(np.uint8),
        "mask": mask,
        "band_index": idx,
        "distance": dist,
        "coverage_pct": float(mask.mean() * 100.0),
    }


def render_alpha(res: dict) -> Image.Image:
    """PNG พื้นหลังโปร่งใส"""
    h, w = res["mask"].shape
    out = np.zeros((h, w, 4), np.uint8)
    out[..., :3] = res["plot"]
    out[..., 3] = np.where(res["mask"], 255, 0)
    return Image.fromarray(out, "RGBA")


def render_solid(res: dict, background=(0, 0, 0)) -> Image.Image:
    """PNG พื้นหลังทึบ (ค่า default = ดำ) — สีของ echo เหมือนต้นฉบับเป๊ะ"""
    out = np.zeros((*res["mask"].shape, 3), np.uint8)
    out[:] = np.asarray(background, np.uint8)
    out[res["mask"]] = res["plot"][res["mask"]]
    return Image.fromarray(out, "RGB")


def to_dbz(res: dict, pal_dbz: np.ndarray, fill: float = np.nan) -> np.ndarray:
    """array ค่า dBZ (float32) — ยังไม่ได้เปิดใช้ใน pipeline หลัก แต่เรียกได้ทันที

    หมายเหตุ: colorbar ของ TMD มีบางช่วงที่สีซ้ำกัน ค่าที่ได้จึงเป็น quantized
    และมี ambiguity ในช่วง 44-51 dBZ — ใช้ทำ nowcasting/tracking ได้ แต่ไม่เหมาะ QPE เชิงปริมาณ
    """
    out = np.full(res["mask"].shape, fill, np.float32)
    out[res["mask"]] = pal_dbz[res["band_index"][res["mask"]]]
    return out


def build_static_mask(
    images: list[Image.Image],
    st: Station,
    pal_rgb: np.ndarray,
    keep_ratio: float = 0.9,
) -> np.ndarray:
    """สร้าง mask ของ overlay ที่อยู่ตำแหน่งเดิมตลอด (range ring / เส้นขอบ / ตัวอักษร)

    วิธี: เอาหลายเฟรม (ยิ่งเป็นเฟรมที่ฝนน้อยยิ่งดี) มาดูว่า pixel ไหนถูกจัดเป็น echo
    ซ้ำ ๆ เกิน keep_ratio ของจำนวนเฟรม -> แทบเป็นไปไม่ได้ที่ฝนจะตกที่เดิมทุกเฟรม
    ดังนั้นถือว่าเป็น overlay ที่ค้างอยู่

    ต้องใช้อย่างน้อย ~20 เฟรมกระจายหลายวันจึงจะน่าเชื่อถือ
    """
    acc = None
    for im in images:
        plot = np.asarray(crop_plot(im, st), dtype=float)
        m, _, _ = classify(plot, pal_rgb, st.lab_tolerance)
        acc = m.astype(np.int32) if acc is None else acc + m
    if acc is None:
        raise ValueError("ต้องมีอย่างน้อย 1 เฟรม")
    return acc >= int(np.ceil(keep_ratio * len(images)))


def save_static_mask(path: Path, mask: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((mask * 255).astype(np.uint8), "L").save(path)


def load_static_mask(path: Path) -> np.ndarray | None:
    p = Path(path)
    if not p.exists():
        return None
    return np.asarray(Image.open(p).convert("L")) > 127
