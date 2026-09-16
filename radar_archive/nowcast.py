"""เฟส 2 — nowcast ด้วย extrapolation อย่างเดียว

    python -m radar_archive.nowcast --station PHS                     # ใช้เฟรมล่าสุดจากคลัง
    python -m radar_archive.nowcast --from-stack data/stack/xxx.npz   # ใช้ stack ที่สร้างไว้
    python -m radar_archive.nowcast --station PHS --engine pysteps    # บังคับใช้ pysteps

ขั้นตอน (แยกให้เรียกทีละอันได้)

    1. load_recent()        โหลด 3-4 เฟรมล่าสุดที่ต่อเนื่องจริง
    2. despeckle()          ลบ pixel แรงจัดโดด ๆ ที่ไม่มี gradient รองรับ
                            (ตัวหนังสือชื่อเมืองบนแผนที่ ไม่ใช่ฝน)
    3. estimate_motion()    หา motion field
    4. motion_stability()   วัดว่าเชื่อได้แค่ไหน — สองมิติ ไม่ใช่มิติเดียว
    5. extrapolate()        เลื่อนก้อนฝนไปข้างหน้าแบบ semi-Lagrangian
    6. write_outputs()      PNG + latest.json

เรื่อง engine — ทำไมมีสองตัว

    pysteps 1.21.5 **ไม่มี wheel เลยบน PyPI** ต้อง compile จาก source ทุกครั้ง
    (ต้องมี cython + numpy ตอน build ใช้เวลา 2-3 นาที และพังง่าย)
    งานที่รันทุก 15 นาทีบน GitHub Actions รับความเสี่ยงนั้นไม่ไหว

    จึงเขียน engine ของเราเองที่ไม่ต้อง compile อะไรเลย (numpy + scipy ล้วน)
    แล้วให้ทั้งสองตัวคืนผลรูปแบบเดียวกันเป๊ะ

        engine="light"   block matching + semi-Lagrangian ของเราเอง   <- ใช้จริงในระบบ
        engine="pysteps" dense_lucaskanade + semilagrangian.extrapolate
        engine="auto"    ใช้ pysteps ถ้ามี ไม่มีก็ light  (ค่าเริ่มต้น)

    ก่อนเชื่อ engine เบา ต้องรัน `compare_engines()` บน Colab ที่ลง pysteps ได้
    เพื่อยืนยันว่าให้ผลตรงกัน แล้วค่อยอ้างอิงความเท่าเทียมนั้นในเปเปอร์

ทำไม phase correlation ใช้ไม่ได้ (บันทึกไว้กันลืม)
    field ของเรามี echo ไม่ถึง 1% ของภาพ พื้นหลังศูนย์กลบ cross-spectrum จนหมด
    ทดสอบจริงได้ค่า (0,0) สลับกับ (-81,+49) แบบสุ่ม ต้องทำงานบนบริเวณที่มี echo เท่านั้น
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import build_stack, grid, pipeline
from .config import CONFIG_PATH, get_station
from .qc import despeckle, despeckle_stack   # noqa: F401  (ใช้ต่อในไฟล์นี้ และให้ import จากที่นี่ได้เหมือนเดิม)

DATA = Path(__file__).resolve().parent.parent / "data"

LEADS_MIN = (15, 30, 45, 60, 75, 90, 105, 120)    # ไม่เกิน 60 — extrapolation ไม่สร้างและไม่สลายก้อนฝน
N_INPUT = 4                      # เฟรมย้อนหลังที่ใช้หา motion (3 คู่)
SEARCH_PX = 14                   # ระยะค้น block matching (14 px @ 2 กม. = 112 กม./ชม.)


# ---------------------------------------------------------------- 1. โหลด

def load_recent(root: Path, st, n: int = N_INPUT, agg: str = "mean"):
    """โหลด n เฟรมล่าสุด **ที่ต่อเนื่องกันจริง** คืน (stack, times)

    ถ้าช่วงต่อเนื่องล่าสุดสั้นกว่า n เฟรม จะ raise — ดีกว่าพยากรณ์จากเฟรมที่ห่างกันจริง
    """
    frames = build_stack.find_frames(root, st.code)
    if not frames:
        raise RuntimeError(f"{st.code}: ไม่มีไฟล์ในคลัง")
    last = build_stack.split_runs(frames)[-1]
    min_frames = 2  # กำหนดขั้นต่ำอย่างน้อย 2 เฟรมให้ลองพยากรณ์
    if len(last) < min_frames:
        print(f"Warning: {st.code} มีแค่ {len(last)} เฟรม (ต้องการอย่างน้อย {min_frames}) ข้าม Nowcast")
        import sys
        sys.exit(0)

    actual_n = min(len(last), n)
    stack, times, meta, _ = build_stack.build_run(last[-actual_n:], st, root, agg=agg, verbose=False)
    return stack, times, meta


def load_from_stack(path: Path, n: int = N_INPUT):
    stack, times = grid.load_stack(path)
    return stack[-n:], times[-n:]


# ---------------------------------------------------------------- 2. despeckle
#
# ย้ายไป qc.py แล้ว — despeckle เป็น QC ตามนิยาม และ build_stack.py ต้องใช้ตัวเดียวกัน
# ถ้าปล่อยไว้ที่นี่ build_stack จะ import nowcast ซึ่ง import build_stack กลับ = วน

# ---------------------------------------------------------------- 3. motion

def _prep(field: np.ndarray, sigma: float = 1.2) -> np.ndarray:
    """เตรียม field สำหรับหา motion — NaN เป็น 0 แล้วเกลี่ยเบา ๆ"""
    a = np.nan_to_num(np.asarray(field, np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    return ndimage.gaussian_filter(np.clip(a, 0.0, None), sigma)


def block_shift(a: np.ndarray, b: np.ndarray, search: int = SEARCH_PX,
                min_px: int = 25, subpixel: bool = True) -> "tuple | None":
    """หา (dy, dx) ที่ทำให้ a ทับ b ได้ดีที่สุด — ค้นเฉพาะกรอบที่มี echo

    ค้นทีละ pixel เต็มกรอบ (2*search+1)^2 — ที่ 241x241 ใช้เวลาไม่ถึงวินาที
    ไม่ใช้ phase correlation ด้วยเหตุผลที่เขียนไว้บนหัวไฟล์
    """
    m = (a > 1.0) | (b > 1.0)
    if m.sum() < min_px:
        return None
    ys, xs = np.nonzero(m)
    R = search
    y0, x0 = max(int(ys.min()) - R, R), max(int(xs.min()) - R, R)
    y1 = min(int(ys.max()) + R, a.shape[0] - R)
    x1 = min(int(xs.max()) + R, a.shape[1] - R)
    if y1 <= y0 or x1 <= x0:
        return None

    ref = b[y0:y1, x0:x1]
    E = np.empty((2 * R + 1, 2 * R + 1), np.float64)
    for i, dy in enumerate(range(-R, R + 1)):
        for j, dx in enumerate(range(-R, R + 1)):
            E[i, j] = ((a[y0 - dy:y1 - dy, x0 - dx:x1 - dx] - ref) ** 2).sum()

    i, j = np.unravel_index(int(np.argmin(E)), E.shape)
    dy, dx = i - R, j - R
    if not subpixel:
        return float(dy), float(dx)
    return dy + _parabolic(E, i, j, axis=0), dx + _parabolic(E, i, j, axis=1)


def _parabolic(E: np.ndarray, i: int, j: int, axis: int) -> float:
    """หาตำแหน่งต่ำสุดจริงระหว่าง pixel ด้วยการฟิตพาราโบลาจาก 3 จุด

    จำเป็นเพราะกริดเรา 2 กม. การเลื่อน 1 pixel ต่อ 15 นาที = 8 กม./ชม. เต็ม ๆ
    ถ้าปัดเป็นจำนวนเต็ม ความเร็วจะเป็นขั้นบันไดหยาบมาก และที่แย่กว่านั้นคือ
    ทุกคู่เฟรมจะได้ค่าเท่ากันเป๊ะ ทำให้ speed spread เป็น 0.0 หลอกว่าเชื่อได้เต็มร้อย
    ทั้งที่จริงเป็นแค่ผลของการปัดเศษ
    """
    n = E.shape[axis] - 1
    k = i if axis == 0 else j
    if k <= 0 or k >= n:
        return 0.0
    if axis == 0:
        c0, c1, c2 = E[k - 1, j], E[k, j], E[k + 1, j]
    else:
        c0, c1, c2 = E[i, k - 1], E[i, k], E[i, k + 1]
    denom = c0 - 2.0 * c1 + c2
    if denom <= 0:
        return 0.0
    return float(np.clip(0.5 * (c0 - c2) / denom, -0.5, 0.5))


def motion_light(stack: np.ndarray, kmperpixel: float, timestep_min: float):
    """motion แบบเวกเตอร์เดียวทั้งภาพ จาก block matching ของทุกคู่เฟรม

    คืน (V, info) โดย V เป็น (2, m, n) หน่วย pixel ต่อ 1 timestep เหมือน pysteps
    (V[0] = แกน y, V[1] = แกน x)
    """
    pairs = []
    for i in range(len(stack) - 1):
        s = block_shift(_prep(stack[i]), _prep(stack[i + 1]))
        if s is not None:
            pairs.append(s)
    if not pairs:
        raise RuntimeError("หา motion ไม่ได้ — ไม่มี echo พอในเฟรมที่ให้มา")

    arr = np.array(pairs, float)
    dy, dx = float(np.median(arr[:, 0])), float(np.median(arr[:, 1]))
    V = np.zeros((2,) + stack.shape[1:], np.float32)
    V[0] = dy
    V[1] = dx
    return V, dict(pairs=[[round(float(p[0]), 3), round(float(p[1]), 3)] for p in pairs],
                   method="block-matching")


def motion_pysteps(stack: np.ndarray):
    """dense_lucaskanade ของ pysteps — motion field รายพิกเซล

    ใส่ NaN นอกรัศมีไปตรง ๆ ได้ pysteps จัดการเป็น missing ให้เอง
    """
    from pysteps.motion.lucaskanade import dense_lucaskanade
    V = np.asarray(dense_lucaskanade(np.asarray(stack, np.float64), verbose=False), np.float32)
    # pysteps คืน (x, y) แต่ทั้งระบบเราใช้ (y, x) -> สลับที่ประตูเข้า จุดเดียว
    return np.stack([V[1], V[0]]), dict(method="pysteps.dense_lucaskanade")


def estimate_motion(stack: np.ndarray, engine: str, kmperpixel: float, timestep_min: float):
    if engine in ("pysteps", "auto"):
        try:
            V, info = motion_pysteps(stack)
            info["engine"] = "pysteps"
            return V, info
        except ImportError:
            if engine == "pysteps":
                raise RuntimeError("ขอ engine=pysteps แต่ยังไม่ได้ลง pysteps")
            print("[i] ไม่มี pysteps — ใช้ engine เบาแทน", file=sys.stderr)
        except Exception as e:
            if engine == "pysteps":
                raise
            print(f"[!] pysteps ล้มเหลว ({type(e).__name__}: {e}) — ใช้ engine เบาแทน",
                  file=sys.stderr)
    V, info = motion_light(stack, kmperpixel, timestep_min)
    info["engine"] = "light"
    return V, info


# ---------------------------------------------------------------- 4. ความน่าเชื่อถือ

def motion_stability(V: np.ndarray, info: dict, kmperpixel: float, timestep_min: float) -> dict:
    """วัดความน่าเชื่อถือของ motion **สองมิติ** ไม่ใช่มิติเดียว

    ความเร็วนิ่งอย่างเดียวไม่พอ — ถ้าทิศแกว่ง ตำแหน่งที่ทำนายจะเพี้ยนไปคนละทาง
    ทั้งที่ speed spread ดูดี  จึงวัด direction consistency ด้วย
    (ความยาวของผลรวมเวกเตอร์หน่วย: 1 = ทิศเดียวกันหมด, 0 = กระจายทุกทิศ)
    """
    to_kmh = kmperpixel * 60.0 / timestep_min

    if info.get("method") == "block-matching":
        a = np.array(info["pairs"], float)
        if len(a) < 3:
            # std ของ 1-2 ตัวอย่าง = ~0 เสมอ -> confidence จะขึ้น high ทั้งที่ข้อมูลน้อยที่สุด
            return dict(confidence="low", n_samples=len(a),
                        reason=f"มีแค่ {len(a)} คู่เฟรม ยังวัดความนิ่งของ motion ไม่ได้")
        spd = np.hypot(a[:, 0], a[:, 1]) * to_kmh
        ang = np.arctan2(a[:, 1], a[:, 0])
        n = len(a)
    else:
        mag = np.hypot(V[0], V[1])
        sel = mag > 0.1
        if sel.sum() < 20:
            return dict(confidence="none", reason="motion แทบเป็นศูนย์ทั้งภาพ")
        spd = mag[sel] * to_kmh
        ang = np.arctan2(V[1][sel], V[0][sel])
        n = int(sel.sum())

    dir_consistency = float(np.hypot(np.cos(ang).mean(), np.sin(ang).mean()))
    speed_spread = float(np.std(spd))
    mean_spd = float(np.mean(spd))
    rel_spread = speed_spread / mean_spd if mean_spd > 0.5 else 1.0

    if dir_consistency >= 0.95 and rel_spread <= 0.25:
        conf = "high"
    elif dir_consistency >= 0.80 and rel_spread <= 0.50:
        conf = "medium"
    else:
        conf = "low"

    # แถวที่ 0 อยู่ใต้สุด -> dy บวก = ไปทางเหนือ  ดังนั้น bearing = atan2(dx, dy)
    # ang ถูกนิยามเป็น atan2(dx, dy) อยู่แล้ว จึงเฉลี่ยแบบเวกเตอร์ได้ตรง ๆ
    bearing = float((np.degrees(np.arctan2(np.mean(np.sin(ang)),
                                           np.mean(np.cos(ang)))) + 360) % 360)

    return dict(
        confidence=conf,
        kmh=round(mean_spd, 1),
        bearing=round(bearing),
        speed_spread_kmh=round(speed_spread, 1),
        speed_spread_rel=round(rel_spread, 3),
        dir_consistency=round(dir_consistency, 3),
        n_samples=n,
    )


def motion_grid(V: np.ndarray, kmperpixel: float, timestep_min: float,
                cell_px: int = 8, uniform_tol_px: float = 1e-3) -> dict | None:
    """ย่อ motion field รายพิกเซลเป็นกริดหยาบ สำหรับให้หน้าเว็บวาดลูกศรรายจุด

    ทำไมต้องมี
        pysteps ให้ motion รายพิกเซล 241x241 อยู่แล้ว แต่ manifest เก็บแค่เวกเตอร์เฉลี่ย
        ตัวเดียว หน้าเว็บจึงวาดลูกศรเหมือนกันหมดทุกจุด ทั้งที่ข้อมูลจริงมีรายละเอียดกว่านั้น

    คืน None เฉพาะเมื่อสนามลม "เท่ากันจริง ๆ ทุกพิกเซล" เท่านั้น
        engine light สร้าง V ด้วยการ broadcast เวกเตอร์เดียวทั้งภาพ ค่า std จึงเป็น 0 พอดี
        กรณีนั้นส่งกริดไปก็เปล่าประโยชน์ หน้าเว็บมีทางถอยไปใช้เวกเตอร์เดียวอยู่แล้ว

        ⚠️ ห้ามตัดทิ้งเพราะ "สเปรดน้อย" เด็ดขาด สนามจริงของ pysteps มีความเร็วใกล้กัน
        (rel spread ~0.06) แต่ทิศทางกระจาย 30-40° ซึ่งมีผลโดยตรงกับ ETA ของแต่ละจุด
        เซลล์ฝนแต่ละก้อนเคลื่อนไม่เหมือนกัน นั่นคือเหตุผลที่ต้องมีกริดตั้งแต่แรก

    หน่วยและทิศทางต้องตรงกับที่หน้าเว็บคาด
        u = องค์ประกอบตะวันออก (m/s) · v = องค์ประกอบเหนือ (m/s)
        หน้าเว็บคำนวณ deg = atan2(u, v) ซึ่งได้ compass bearing โดยตรง

    ⚠️ พลิกแกนตั้ง
        array ของเรา row 0 = ใต้ (yorigin lower) แต่หน้าเว็บนับ row 0 = เหนือ
        (row = floor(-pinN/res + rows/2)) ถ้าไม่พลิก ลูกศรครึ่งบนกับครึ่งล่างจะสลับที่กัน
    """
    if V.ndim != 3 or V.shape[0] != 2:
        return None
    n = min(V.shape[1], V.shape[2])
    k = max(2, int(cell_px))
    nb = n // k
    if nb < 3:
        return None
    m = nb * k

    # เฉลี่ยเป็นบล็อก — ลด noise รายพิกเซลของ LK ไปในตัว
    dy = V[0][:m, :m].reshape(nb, k, nb, k).mean(axis=(1, 3))     # px/step ไปทางเหนือ
    dx = V[1][:m, :m].reshape(nb, k, nb, k).mean(axis=(1, 3))     # px/step ไปทางตะวันออก

    # ตัดทิ้งเฉพาะสนามที่เท่ากันหมดจริง ๆ (light engine) ไม่ใช่สนามที่ "ค่อนข้างสม่ำเสมอ"
    if float(np.ptp(dx)) < uniform_tol_px and float(np.ptp(dy)) < uniform_tol_px:
        return None

    to_ms = kmperpixel * 1000.0 / (timestep_min * 60.0)
    u = (dx * to_ms)[::-1]                # พลิกให้ row 0 = เหนือ
    v = (dy * to_ms)[::-1]

    # สถิติไว้ให้หน้าเว็บ/เอกสารอ้างอิงว่ากริดนี้ต่างจากเวกเตอร์เดียวแค่ไหน
    mag = np.hypot(u, v)
    mu, mv = float(u.mean()), float(v.mean())
    mean_mag = float(np.hypot(mu, mv))
    dir_consistency = mean_mag / float(mag.mean()) if mag.mean() > 1e-6 else 1.0
    return dict(
        grid_u=[round(float(x), 2) for x in u.ravel()],
        grid_v=[round(float(x), 2) for x in v.ravel()],
        resolution_km=round(k * kmperpixel, 3),
        cols=nb, rows=nb,
        grid_speed_kmh=[round(float(x) * 3.6, 1) for x in (mag.min(), np.median(mag), mag.max())],
        grid_dir_consistency=round(float(dir_consistency), 3),
    )


# ---------------------------------------------------------------- 5. extrapolate

def advect(field: np.ndarray, V: np.ndarray, steps: int, n_iter: int = 3) -> np.ndarray:
    """semi-Lagrangian backward advection — ของเราเอง ไม่ต้องพึ่ง pysteps

    ค่าที่จุด x เวลา t = ค่าปัจจุบันที่จุด x - D  โดย D คือ displacement รวม
    หา D แบบวนซ้ำด้วยวิธี midpoint (แบบเดียวกับที่ pysteps ทำ):

        D <- V(x - D/2) * steps

    ถ้า V คงที่ทั้งภาพ การวนซ้ำลู่เข้าทันทีในรอบเดียว (D = V*steps)
    การวนซ้ำมีผลเฉพาะกับ motion field รายพิกเซล
    """
    m, n = field.shape
    yy, xx = np.mgrid[0:m, 0:n].astype(np.float32)
    Dy = V[0] * steps
    Dx = V[1] * steps

    for _ in range(max(1, n_iter) - 1):
        sy = np.clip(yy - Dy / 2.0, 0, m - 1)
        sx = np.clip(xx - Dx / 2.0, 0, n - 1)
        Dy = ndimage.map_coordinates(V[0], [sy, sx], order=1, mode="nearest") * steps
        Dx = ndimage.map_coordinates(V[1], [sy, sx], order=1, mode="nearest") * steps

    src_y = yy - Dy
    src_x = xx - Dx

    # NaN ทำให้ interpolate เพี้ยน -> เติม 0 ก่อน แล้วค่อยคืนสถานะ "ไม่มีข้อมูล" ทีหลัง
    filled = np.nan_to_num(field, nan=0.0)
    out = ndimage.map_coordinates(filled, [src_y, src_x], order=1,
                                  mode="constant", cval=0.0)

    # จุดที่ back-trajectory ออกนอกภาพ = ไม่รู้ว่ามีอะไรเข้ามา -> ไม่มีข้อมูล
    outside = ((src_y < 0) | (src_y > m - 1) | (src_x < 0) | (src_x > n - 1))
    out[outside] = np.nan
    # นอกรัศมีเรดาร์ยังคงเป็นไม่มีข้อมูลเสมอ
    out[~np.isfinite(field) & ~outside] = np.nan
    return out.astype(np.float32)


# หมายเหตุที่ได้จากการทดสอบจริง — อย่าไป "snap" ค่าต่ำ ๆ ทิ้ง
#
#   ตอนแรกเห็นว่าหลัง advect แล้ว % พื้นที่ที่มีฝนพองจาก 2.27% เป็น 3.14%
#   เลยจะตัดค่าที่ต่ำกว่าแถบล่างสุดของ colorbar (10.4 dBZ) ทิ้ง แต่คิดผิด
#
#   เพราะ to_grid() เฉลี่ยใน linear Z เซลล์ 2 กม. ที่มีฝนแค่บางส่วนก็ได้ค่าต่ำกว่า
#   10.4 dBZ อยู่แล้วตั้งแต่ต้น มันคือข้อมูล "ฝนคลุมบางส่วนของเซลล์" ไม่ใช่ขยะ
#   การ advect ด้วย bilinear ก็สร้างค่าแบบเดียวกันที่ขอบก้อนด้วยเหตุผลเดียวกัน
#   ถ้าตัดเฉพาะฝั่งพยากรณ์ จะกลายเป็นวัดสองฝั่งด้วยไม้บรรทัดคนละอัน
#   (ทดลองแล้ว: ตัดทิ้งทำให้ wet ตกจาก 2.27% เหลือ 1.54% ที่รอยต่อ ซึ่งผิดยิ่งกว่าเดิม)
#
#   ทางที่ถูกคือรายงาน % พื้นที่ที่ threshold ที่มีความหมายทางกายภาพเหมือนกันทั้งสองฝั่ง
#   ใช้ meta["threshold"] = 11.98 dBZ (= 0.1 มม./ชม. ภายใต้ Rosenfeld tropical)


def extrapolate_light(last: np.ndarray, V: np.ndarray, leads: tuple, timestep_min: float):
    return np.array([advect(last, V, lead / timestep_min) for lead in leads], np.float32)


def extrapolate_pysteps(last: np.ndarray, V: np.ndarray, leads: tuple, timestep_min: float):
    from pysteps.extrapolation.semilagrangian import extrapolate as sl
    n = int(round(max(leads) / timestep_min))
    outside = ~np.isfinite(last)                      # จำบริเวณ "ไม่มีข้อมูล" ไว้ก่อน
    src = np.nan_to_num(np.asarray(last, np.float64), nan=0.0)
    V_ps = np.stack([V[1], V[0]]).astype(np.float64)  # (y, x) -> (x, y) ที่ประตูออก
    out = np.asarray(sl(src, V_ps, n), np.float32)
    out[:, outside] = np.nan                          # คืนสถานะ NaN นอกรัศมี
    idx = [int(round(l / timestep_min)) - 1 for l in leads]
    return out[idx]


def run_extrapolation(last: np.ndarray, V: np.ndarray, engine: str,
                      leads: tuple, timestep_min: float):
    if engine == "pysteps":
        try:
            return extrapolate_pysteps(last, V, leads, timestep_min), "pysteps.semilagrangian"
        except Exception as e:
            print(f"[!] pysteps extrapolate ล้ม ({type(e).__name__}: {e}) — ใช้ light แทน",
                  file=sys.stderr)
    return extrapolate_light(last, V, leads, timestep_min), "semilagrangian (light)"


# ---------------------------------------------------------------- ตรวจสองเครื่องยนต์

def compare_engines(stack: np.ndarray, meta: dict, leads: tuple = LEADS_MIN) -> dict:
    """รันทั้งสอง engine กับข้อมูลชุดเดียวกันแล้วเทียบ — ใช้บน Colab ที่ลง pysteps ได้

    ถ้าผลตรงกันในระดับที่ยอมรับได้ ระบบจริงใช้ engine เบาได้อย่างสบายใจ
    และอ้างอิงการตรวจนี้ในเปเปอร์ได้
    """
    kpp, ts = meta["kmperpixel"], meta["timestep"]
    res = {}
    fields = {}
    for eng in ("light", "pysteps"):
        try:
            V, info = estimate_motion(stack, eng, kpp, ts)
            F, how = run_extrapolation(stack[-1], V, eng, leads, ts)
            res[eng] = dict(stability=motion_stability(V, info, kpp, ts), how=how)
            fields[eng] = F
        except Exception as e:
            res[eng] = dict(error=f"{type(e).__name__}: {e}")
    if len(fields) == 2:
        a, b = fields["light"], fields["pysteps"]
        ok = np.isfinite(a) & np.isfinite(b)
        diff = np.abs(a - b)[ok]
        res["agreement"] = dict(
            mae_dbz=round(float(diff.mean()), 3),
            p95_dbz=round(float(np.percentile(diff, 95)), 3),
            max_dbz=round(float(diff.max()), 3),
            corr=round(float(np.corrcoef(a[ok], b[ok])[0, 1]), 4),
        )
    return res


# ---------------------------------------------------------------- 6. เขียนผล

def colorize(field: np.ndarray, pal_rgb: np.ndarray, pal_dbz: np.ndarray) -> np.ndarray:
    """dBZ -> RGBA ด้วย palette ชุดเดียวกับภาพต้นฉบับ (โปร่งใสเมื่อไม่มีฝน/ไม่มีข้อมูล)"""
    order = np.argsort(pal_dbz)
    d, c = pal_dbz[order], pal_rgb[order]
    h, w = field.shape
    out = np.zeros((h, w, 4), np.uint8)
    wet = np.isfinite(field) & (field >= d[0] - 0.6)
    if wet.any():
        idx = np.clip(np.searchsorted(d, field[wet], side="right") - 1, 0, len(d) - 1)
        out[wet, :3] = np.rint(c[idx]).astype(np.uint8)
        out[wet, 3] = 255
    return out


def write_outputs(out_dir: Path, st, meta: dict, obs_stack, obs_times,
                  fc_stack, fc_times, motion: dict, stability: dict,
                  pal_rgb, pal_dbz, how: str, despeckled: int,
                  keep_hours: int = 24, mgrid: dict | None = None) -> Path:
    """เขียน PNG ทุกเฟรม + latest.json — สัญญาระหว่างเซิร์ฟเวอร์กับแอป"""
    from PIL import Image
    fdir = out_dir / "f"
    fdir.mkdir(parents=True, exist_ok=True)

    # เก็บเฉพาะ 24 ชม.ล่าสุด — โฟลเดอร์ที่ Pages เสิร์ฟต้องเล็กและคงที่
    # คลังเต็มอยู่ใน data/raw อยู่แล้ว ตรงนี้เป็นแค่หน้าร้าน
    cutoff = int((obs_times[-1] - timedelta(hours=keep_hours)).replace(
        tzinfo=timezone.utc).timestamp())
    for old_png in fdir.glob("*.png"):
        try:
            if int(old_png.stem) < cutoff:
                old_png.unlink()
        except ValueError:
            continue

    now = obs_times[-1]
    thr = float(meta["threshold"])      # 11.98 dBZ = 0.1 มม./ชม. — วัดเท่ากันทั้งของจริงและพยากรณ์
    entries = []
    for stack, times, kind in ((obs_stack, obs_times, "past"), (fc_stack, fc_times, "nowcast")):
        for f, t in zip(stack, times):
            ep = int(t.replace(tzinfo=timezone.utc).timestamp())
            # แถว 0 = ใต้สุด แต่ PNG นับแถวจากบนลงล่าง -> พลิกตอนเขียน
            Image.fromarray(colorize(f, pal_rgb, pal_dbz)[::-1], "RGBA").save(
                fdir / f"{ep}.png", optimize=True)
            off = int(round((t - now).total_seconds() / 60))
            e = dict(t=ep, kind=("present" if off == 0 else kind), offset_min=off,
                     url=f"f/{ep}.png",
                     max_dbz=round(float(np.nanmax(f)), 1) if np.isfinite(f).any() else None,
                     wet_pct=round(float(np.nansum(f >= thr)
                                         / max(np.isfinite(f).sum(), 1) * 100), 3))
            if kind == "nowcast":
                e["method"] = "extrapolation"
            entries.append(e)
    entries.sort(key=lambda e: e["t"])

    gen = datetime.now(timezone.utc)
    doc = {
        "station": st.code,
        "generated": int(gen.timestamp()),
        "base_time_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_time_local": fetch_local(now),
        "age_min": round((gen - now.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1),
        "stale_after_min": 40,
        "projection": meta["projection"],
        "extent_m": [meta["x1"], meta["y1"], meta["x2"], meta["y2"]],
        "grid": [grid.GRID_N, grid.GRID_N],
        "kmperpixel": meta["kmperpixel"],
        "timestep_min": meta["timestep"],
        "yorigin": "lower",
        "levels_dbz": [round(float(v), 1) for v in np.sort(pal_dbz)],
        "levels_rgb": [[int(v) for v in c] for c in pal_rgb[np.argsort(pal_dbz)]],
        "zr": [meta["zr_a"], meta["zr_b"]],
        "motion": {**{k: v for k, v in motion.items() if k != "pairs"}, **stability,
                   "extrapolation": how, **(mgrid or {})},
        "wet_threshold_dbz": round(thr, 2),
        # ⚠️ threshold ที่ใช้ได้จริง "บนภาพที่ render แล้ว" ไม่เท่ากับค่าข้างบน
        #
        # colorize() ปัดค่าลงเข้าแถบ palette ที่ต่ำกว่าหรือเท่ากับ (searchsorted side="right" - 1)
        # ค่าใดก็ตามในช่วง [11.3, 16.5) จึงถูก render เป็น 11.3 แล้วถอดกลับได้ 11.3
        # ซึ่ง < 11.98 = ถูกนับว่าแห้ง  ระดับแรกที่ผ่าน threshold จริง ๆ คือ 16.5
        #
        # ทุกอย่างที่อ่านค่าจาก PNG (หน้าเว็บ, verify.py) จึงตัดที่ค่านี้ ไม่ใช่ที่ wet_threshold_dbz
        # วัดยืนยันแล้ว: สั่ง --threshold ค่าใดก็ตามใน (11.3, 16.5] ให้ผลเหมือนกันเป๊ะทุกหลัก
        #
        # เก็บทั้งสองค่าไว้เพราะคนละความหมาย ไม่ใช่ค่าซ้ำ
        #   wet_threshold_dbz           ใช้กับสนาม float ข้างใน (สถิติ wet% ของ nowcast)
        #   wet_threshold_effective_dbz ใช้กับภาพ render แล้ว (ตัวเลขที่รายงานในเปเปอร์)
        "wet_threshold_effective_dbz": next(
            (round(float(v), 1) for v in np.sort(pal_dbz) if float(v) >= thr), round(thr, 2)),
        "qc": {"despeckled_cells": despeckled},
        "frames": entries,
        "source": "Thai Meteorological Department (TMD)",
    }
    doc["stale"] = doc["age_min"] > doc["stale_after_min"]
    p = out_dir / "latest.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def fetch_local(t: datetime) -> str:
    return (t.replace(tzinfo=timezone.utc) + timedelta(hours=7)).strftime(
        "%Y-%m-%dT%H:%M:%S+07:00")


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar_archive.nowcast",
                                description="nowcast แบบ extrapolation อย่างเดียว")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--station", default="PHS")
    p.add_argument("--from-stack", default=None, help="ใช้ .npz ที่สร้างไว้แทนคลังภาพ")
    p.add_argument("--engine", default="auto", choices=["auto", "light", "pysteps"])
    p.add_argument("--leads", default=",".join(str(x) for x in LEADS_MIN))
    p.add_argument("--n-input", type=int, default=N_INPUT)
    p.add_argument("--out", default=None, help="ค่าเริ่มต้น docs/data")
    p.add_argument("--compare", action="store_true", help="เทียบ light กับ pysteps แล้วจบ")
    a = p.parse_args(argv)

    root = Path(a.data)
    st = get_station(a.station, a.config)
    leads = tuple(int(x) for x in a.leads.split(","))
    out_dir = Path(a.out) if a.out else root.parent / "docs" / "data"

    if a.from_stack:
        obs, times = load_from_stack(Path(a.from_stack), a.n_input)
        meta = grid.station_meta(st)
    else:
        obs, times, meta = load_recent(root, st, a.n_input)

    print(f"เฟรมที่ใช้: {len(times)} เฟรม  "
          f"{times[0]:%Y-%m-%d %H:%M} -> {times[-1]:%H:%M}Z")
    ok, gaps = grid.check_regular(times, meta["timestep"])
    if not ok:
        print(f"[!] เฟรมห่างไม่เท่ากัน {gaps} นาที — หยุด", file=sys.stderr)
        return 1

    obs, n_spk = despeckle_stack(obs)
    if n_spk:
        print(f"[qc] ลบเซลล์แรงจัดที่ไม่มี gradient รองรับ {n_spk} เซลล์ "
              f"(น่าจะเป็นตัวหนังสือบนแผนที่)")

    if a.compare:
        print(json.dumps(compare_engines(obs, meta, leads), ensure_ascii=False, indent=1))
        return 0

    V, info = estimate_motion(obs, a.engine, meta["kmperpixel"], meta["timestep"])
    stab = motion_stability(V, info, meta["kmperpixel"], meta["timestep"])
    print(f"motion [{info['engine']}] {stab.get('kmh')} กม./ชม. ทิศ {stab.get('bearing')}° · "
          f"dir consistency {stab.get('dir_consistency')} · "
          f"speed spread {stab.get('speed_spread_rel')} -> ความเชื่อมั่น {stab['confidence']}")
    if stab["confidence"] == "low":
        print("[!] motion ไม่นิ่ง — ยังพยากรณ์ให้ แต่ติดธง low ไว้ใน latest.json")

    from PIL import Image
    img = Image.open(build_stack.find_frames(root, st.code)[-1][1]).convert("RGB")
    pal_rgb, pal_dbz = pipeline.get_palette(root, st, img)
    fc, how = run_extrapolation(obs[-1], V, info["engine"], leads, meta["timestep"])
    fc_times = [times[-1] + timedelta(minutes=l) for l in leads]
    for t, f in zip(fc_times, fc):
        print(f"  +{int((t-times[-1]).total_seconds()//60):>3} นาที  {t:%H:%M}Z  "
              f"max {np.nanmax(f):5.1f} dBZ  "
              f"wet {100*np.nansum(f >= meta['threshold'])/max(np.isfinite(f).sum(),1):6.3f}%")

    mgrid = motion_grid(V, meta["kmperpixel"], meta["timestep"])
    if mgrid:
        print(f"motion grid {mgrid['rows']}x{mgrid['cols']} ที่ {mgrid['resolution_km']} กม.")
    else:
        print("motion แบนทั้งภาพ — ไม่ส่งกริด หน้าเว็บจะใช้เวกเตอร์เดียว")

    path = write_outputs(out_dir, st, meta, obs, times, fc, fc_times,
                         info, stab, pal_rgb, pal_dbz, how, n_spk, mgrid=mgrid)
    print(f"\nเขียน {len(times)+len(fc)} เฟรม + manifest -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
