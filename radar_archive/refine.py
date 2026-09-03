"""ปรับคุณภาพผลลัพธ์ให้ "เนียน" เท่าภาพต้นฉบับ

ปัญหาของการ threshold สีแบบตรงไปตรงมา (strip.classify เดี่ยว ๆ)
  1. ขอบ echo ถูกกัดหาย  — pixel ขอบเป็นสีผสมระหว่าง echo กับแผนที่ (anti-alias + JPEG)
     ระยะสีจึงเกิน tolerance แล้วโดนทิ้ง ก้อนฝนเลยดูแหว่งกว่าต้นฉบับ
  2. เป็นรูพรุนข้างใน — เส้น range ring / ชื่อเมือง ที่วาดทับ echo เจาะรูทะลุก้อนฝน
  3. สีข้างในด่าง      — เก็บ RGB ดิบจาก JPEG มาเลย ซึ่งเพี้ยนไปจากสี palette จริง

วิธีแก้ (ทำตามลำดับ)
  A. hysteresis: ใช้ threshold เข้ม (tol_core) หา "แกน" ของ echo แล้วขยายออกไปได้ไม่เกิน
     grow_px pixel เฉพาะในบริเวณที่ยังผ่าน threshold หลวม (tol_edge)
     -> ได้ขอบคืนมาโดยที่เส้นแม่น้ำ/เส้นขอบไม่วิ่งทะลุเข้ามา เพราะขยายได้จำกัดระยะ
  B. closing + อุดรูเล็ก -> ปะรอยที่ overlay เจาะไว้
  C. mode filter บน band index -> ลบ salt-and-pepper ที่เกิดจาก JPEG
  D. snap สีให้ตรงกับ palette เป๊ะ -> สีเรียบเป็นแถบ ๆ เหมือนต้นฉบับตั้งใจให้เป็น
  E. ขอบนุ่ม (soft alpha) -> ไม่เห็นรอยหยักแบบขั้นบันได
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from scipy import ndimage

from . import lab
from .config import Station
from .palette import to_lab
from .strip import crop_plot

# ค่าที่จูนแล้วกับ PHS — เปลี่ยนได้ผ่าน strip_background_refined(...)
DEFAULTS = dict(
    tol_core=8.0,        # threshold เข้ม ใช้หาแกน echo
    tol_edge=16.0,       # threshold หลวม ใช้ได้เฉพาะบริเวณติดกับแกน
    grow_px=2,           # ขยายจากแกนได้ไม่เกินกี่ pixel
    min_blob_px=6,       # ลบก้อนที่เล็กกว่านี้
    close_px=1,          # รัศมี closing
    fill_hole_px=256,    # อุดรูที่เล็กกว่านี้
    drop_pale_blobs=3,   # ทิ้งก้อนที่ค่ากลางอยู่ในแถบจางสุด n แถบแรก (= ตัวหนังสือสีขาว)
    mode_filter=True,    # ลบ salt-and-pepper บน band index
    snap_palette=True,   # บังคับสีให้ตรง palette
    soft_edge=0.6,       # sigma ของขอบนุ่ม (0 = ขอบคม)
)


def _distance_stack(plot_rgb: np.ndarray, pal_rgb: np.ndarray):
    lab_img = lab.rgb2lab(plot_rgb / 255.0)
    lab_pal = to_lab(pal_rgb)
    d = np.linalg.norm(lab_img[:, :, None, :] - lab_pal[None, None, :, :], axis=-1)
    return d.min(axis=-1), d.argmin(axis=-1)


def _hysteresis(dist: np.ndarray, tol_core: float, tol_edge: float, grow_px: int) -> np.ndarray:
    """ขยายจากแกนออกไปได้ไม่เกิน grow_px เฉพาะในบริเวณที่ผ่าน threshold หลวม

    ใช้การขยายทีละ pixel แทน morphological reconstruction เต็มรูปแบบ เพราะถ้าปล่อยให้
    ขยายไม่จำกัด เส้นขอบจังหวัดสีขาว (ซึ่งห่างจากแถบสีจาง ๆ แค่ ~20 ใน Lab) จะไหล
    ไปตามเส้นทั้งเส้นทันทีที่เส้นนั้นพาดผ่าน echo สักจุดเดียว
    """
    mask = dist < tol_core
    cand = dist < tol_edge
    for _ in range(max(0, grow_px)):
        mask = ndimage.binary_dilation(mask) & cand
    return mask


def _mode_filter(idx: np.ndarray, mask: np.ndarray, n_bands: int) -> np.ndarray:
    """3x3 majority vote บน band index นับเฉพาะ pixel ที่อยู่ใน mask"""
    k = np.ones((3, 3), float)
    best = np.zeros(idx.shape, np.int32)
    best_n = np.zeros(idx.shape, float)
    for b in range(n_bands):
        cnt = ndimage.convolve(((idx == b) & mask).astype(float), k, mode="constant")
        upd = cnt > best_n
        best_n[upd] = cnt[upd]
        best[upd] = b
    return np.where(mask, best, idx)


def strip_background_refined(
    img: Image.Image,
    st: Station,
    pal_rgb: np.ndarray,
    static_mask: np.ndarray | None = None,
    **kw,
) -> dict:
    p = {**DEFAULTS, **kw}
    plot = np.asarray(crop_plot(img, st), dtype=float)
    dist, idx = _distance_stack(plot, pal_rgb)

    # A. hysteresis
    mask = _hysteresis(dist, p["tol_core"], p["tol_edge"], p["grow_px"])

    if static_mask is not None:
        mask &= ~static_mask

    # B. ทำความสะอาดรูปทรง
    if p["min_blob_px"] > 1:
        mask = lab.remove_small(mask, p["min_blob_px"])
    if p["close_px"] > 0:
        mask = ndimage.binary_closing(mask, lab.disk(p["close_px"]))
    if p["fill_hole_px"] > 0:
        mask = lab.fill_small_holes(mask, p["fill_hole_px"])
    if p["drop_pale_blobs"]:
        # ตัวหนังสือ/สัญลักษณ์สีขาวบนแผนที่ จะถูกจับคู่กับแถบสีจางสุด (= dBZ สูงสุด)
        # แต่ echo จริงที่แรงขนาดนั้นเป็นไปไม่ได้ที่จะมีค่ากลางของทั้งก้อนอยู่แถบนั้น
        lbl = lab.label(mask, connectivity=2)
        med = lab.component_median(lbl, idx)
        pale = np.flatnonzero(med < p["drop_pale_blobs"])
        mask &= ~np.isin(lbl, pale[pale > 0])

    # pixel ที่เพิ่งได้มาจาก closing/fill ยังไม่มี band index ที่เชื่อถือได้
    # -> เติมด้วยค่าของเพื่อนบ้านที่ใกล้ที่สุด
    trusted = dist < p["tol_edge"]
    need = mask & ~trusted
    if need.any():
        _, near = ndimage.distance_transform_edt(~(mask & trusted), return_indices=True)
        idx = np.where(need, idx[tuple(near)], idx)

    # C. ลบ salt-and-pepper
    if p["mode_filter"]:
        idx = _mode_filter(idx, mask, len(pal_rgb))

    return {
        "plot": plot.astype(np.uint8),
        "mask": mask,
        "band_index": idx,
        "distance": dist,
        "coverage_pct": float(mask.mean() * 100.0),
        "_params": p,
    }


def _colors(res: dict, pal_rgb: np.ndarray) -> np.ndarray:
    """สีที่จะใช้วาด — snap ให้ตรง palette หรือใช้ RGB ดิบจากภาพ"""
    if res["_params"]["snap_palette"]:
        return pal_rgb[res["band_index"]].astype(float)
    return res["plot"].astype(float)


def _alpha(res: dict) -> np.ndarray:
    """0-1 float, ขอบนุ่มเล็กน้อยเพื่อไม่ให้เห็นรอยหยัก"""
    a = res["mask"].astype(float)
    s = res["_params"]["soft_edge"]
    if s > 0:
        a = np.clip(ndimage.gaussian_filter(a, s) * 1.25, 0, 1)
        a[res["mask"]] = 1.0          # ข้างในทึบเต็ม เบลอเฉพาะขอบด้านนอก
    return a


def render_alpha(res: dict, pal_rgb: np.ndarray) -> Image.Image:
    rgb = _colors(res, pal_rgb)
    a = _alpha(res)
    out = np.zeros((*res["mask"].shape, 4), np.uint8)
    out[..., :3] = np.rint(rgb).clip(0, 255).astype(np.uint8)
    out[..., 3] = np.rint(a * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def render_solid(res: dict, pal_rgb: np.ndarray, background=(0, 0, 0)) -> Image.Image:
    rgb = _colors(res, pal_rgb)
    a = _alpha(res)[..., None]
    bg = np.asarray(background, float)
    out = rgb * a + bg * (1 - a)
    return Image.fromarray(np.rint(out).clip(0, 255).astype(np.uint8), "RGB")
