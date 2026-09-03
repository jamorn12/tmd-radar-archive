"""ฟังก์ชันสีและ connected-component ที่เขียนเอง — เพื่อไม่ต้องพึ่ง scikit-image

ทำไมต้องเขียนเอง

  scikit-image ดึง matplotlib เข้ามาตอน import (ผ่าน `_dependency_checks`) ทำให้ท่อของเรา
  ไปผูกกับ matplotlib โดยไม่ได้ตั้งใจ ถ้าเครื่องไหนมี matplotlib เก่าที่คอมไพล์กับ NumPy 1.x
  อยู่ ระบบจะพังตั้งแต่ import ด้วยข้อความ `_ARRAY_API not found` ซึ่งไม่เกี่ยวกับงานเราเลย

  นอกจากนี้ signature ของ skimage เปลี่ยนบ่อย (remove_small_objects, remove_small_holes,
  binary_closing เปลี่ยนหมดในรอบปีเดียว) ซึ่งเป็นความเสี่ยงที่ไม่ควรมีในระบบที่รันอัตโนมัติ
  ทุก 15 นาทีโดยไม่มีคนดู

  ที่ใช้จริงมีแค่ 4 อย่าง เขียนเองแล้วเหลือ dependency แค่ numpy + scipy

ตรวจสอบแล้วว่าให้ผลตรงกับ skimage:
  rgb2lab  ต่างสูงสุด < 0.02 หน่วย Lab (tolerance ที่เราใช้คือ 8-16 จึงไม่มีผล)
  label    ให้ผลเหมือนกันทุกประการ (ต่างแค่ลำดับหมายเลข label ซึ่งไม่มีผล)
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

# เมทริกซ์ sRGB -> XYZ และจุดขาว D65 ชุดเดียวกับที่ skimage ใช้
_XYZ_FROM_RGB = np.array([
    [0.412453, 0.357580, 0.180423],
    [0.212671, 0.715160, 0.072169],
    [0.019334, 0.119193, 0.950227],
])
_WHITE_D65 = np.array([0.95047, 1.0, 1.08883])

CONN4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
CONN8 = np.ones((3, 3), bool)


def rgb2lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (ค่า 0-1) -> CIELAB  รับรูปร่าง (..., 3) คืนรูปร่างเดิม

    ใช้ observer 2 องศา แสง D65 เหมือน skimage.color.rgb2lab ทุกประการ
    """
    a = np.asarray(rgb, dtype=np.float64)
    # 1) ถอด gamma ของ sRGB ให้เป็นความสว่างเชิงเส้น
    lin = np.where(a > 0.04045, ((a + 0.055) / 1.055) ** 2.4, a / 12.92)
    # 2) เข้าสู่ปริภูมิ XYZ แล้วหารด้วยจุดขาว
    xyz = lin @ _XYZ_FROM_RGB.T / _WHITE_D65
    # 3) ฟังก์ชันบีบอัดแบบ CIE
    eps, kappa = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > eps, np.cbrt(np.maximum(xyz, 0.0)), (kappa * xyz + 16.0) / 116.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack([116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1)


def label(mask: np.ndarray, connectivity: int = 2) -> np.ndarray:
    """แทน skimage.measure.label — 0 คือพื้นหลัง ก้อนเริ่มนับที่ 1

    connectivity=1 คือติดกันแบบ 4 ทิศ, =2 คือ 8 ทิศ (ความหมายเดียวกับ skimage)
    """
    lbl, _ = ndimage.label(mask, structure=CONN4 if connectivity == 1 else CONN8)
    return lbl


def component_sizes(lbl: np.ndarray) -> np.ndarray:
    """จำนวน pixel ของแต่ละ label — index 0 คือพื้นหลัง"""
    return np.bincount(lbl.ravel())


def remove_small(mask: np.ndarray, min_px: int, connectivity: int = 2) -> np.ndarray:
    """ลบก้อนที่เล็กกว่า min_px ออกจาก mask"""
    if min_px <= 1:
        return mask
    lbl = label(mask, connectivity)
    sizes = component_sizes(lbl)
    small = np.flatnonzero(sizes < min_px)
    return mask & ~np.isin(lbl, small[small > 0])


def component_median(lbl: np.ndarray, values: np.ndarray) -> np.ndarray:
    """ค่ามัธยฐานของ values ในแต่ละก้อน — คืนอาร์เรย์ยาว lbl.max()+1 (index 0 ไม่ใช้)"""
    n = int(lbl.max())
    out = np.zeros(n + 1, float)
    if n:
        out[1:] = ndimage.labeled_comprehension(
            values, lbl, np.arange(1, n + 1), np.median, float, 0.0)
    return out


def disk(radius: int) -> np.ndarray:
    """footprint วงกลม เหมือน skimage.morphology.disk (radius 1 = รูปกากบาท 3x3)"""
    r = int(radius)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def fill_small_holes(mask: np.ndarray, max_px: int) -> np.ndarray:
    """อุดรูที่พื้นที่ไม่เกิน max_px — รูใหญ่กว่านั้นปล่อยไว้

    เขียนเองแทน skimage.morphology.remove_small_holes เพราะพารามิเตอร์
    (area_threshold / max_size) เปลี่ยนความหมายไปตามเวอร์ชัน
    """
    if max_px <= 0:
        return mask
    holes = ndimage.binary_fill_holes(mask) & ~mask
    hl = label(holes, connectivity=1)
    sizes = component_sizes(hl)
    small = np.flatnonzero(sizes <= max_px)
    return mask | (holes & np.isin(hl, small[small > 0]))
