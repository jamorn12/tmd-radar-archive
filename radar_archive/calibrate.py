"""หา geometry ของภาพเรดาร์จากตัวภาพเอง — ใช้วงรัศมีที่ TMD วาดไว้เป็นไม้บรรทัด

วิธีเดิม (เดาตำแหน่งจากป้ายกำกับ) ให้ค่าที่ผิด 2.5% ซึ่งที่ขอบ 240 กม. เพี้ยนไป 6 กม.
วิธีนี้ฟิตวงกลมจริงกับเส้นวงรัศมีทั้ง 4 วง แล้วได้ทั้งจุดศูนย์กลางและสเกลพร้อมกัน

ทำไมวงรัศมีใช้เป็นไม้บรรทัดได้
  TMD วาดวงที่ระยะจริง 30 / 48 / 120 / 240 กม. ถ้าภาพเป็น azimuthal equidistant
  (ซึ่งเป็น projection ธรรมชาติของภาพ PPI) รัศมีเป็น pixel จะแปรผันตรงกับระยะเป็น กม.
  ถ้าไม่ใช่ — เช่นเป็น equirectangular — วงจะกลายเป็นวงรี (ยืดตามลองจิจูด 1/cos(lat))
  ที่ละติจูด 16.8° จะยืดถึง 4.4% หรือ 17 pixel ที่วง 240 กม. ซึ่งการฟิตจะจับได้ทันที

    python -m radar_archive.calibrate PHS data/raw/PHS/2026/09/PHS_20260902_1145Z.jpg
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

# วงรัศมีที่ TMD วาดไว้ในภาพ 240 กม. — (ระยะจริง กม., สีของเส้น)
DEFAULT_RINGS = ((30.0, "blue"), (48.0, "blue"), (120.0, "blue"), (240.0, "red"))


def colour_fields(img: Image.Image) -> dict:
    """แยก 'ความน้ำเงิน' และ 'ความแดง' ออกมาเป็นฟิลด์ต่อเนื่อง

    ใช้ค่าต่อเนื่องแทนการ threshold เพราะเส้นวงถูกวาดทับแผนที่แบบ anti-alias
    การ threshold จะทิ้ง pixel ขอบเส้นไปเกือบหมดจนฟิตไม่ได้
    """
    a = np.asarray(img.convert("RGB"), dtype=float)
    return {
        "blue": a[..., 2] - (a[..., 0] + a[..., 1]) / 2,
        "red": a[..., 0] - (a[..., 1] + a[..., 2]) / 2,
    }


def radial_profile(field: np.ndarray, cx: float, cy: float,
                   r_max: float = 470.0, step: float = 0.25, n_az: int = 1440):
    """ค่ามัธยฐานของฟิลด์ตามรัศมี — มัธยฐานข้ามมุมกวาดทำให้เส้นวงเด่นขึ้นมาเอง

    วงกลมจริงยกค่าที่รัศมีนั้นขึ้น *ทุกมุม* ค่ามัธยฐานจึงขยับ
    ส่วนแม่น้ำหรือ echo กระทบแค่ไม่กี่มุม มัธยฐานไม่รู้สึก — เป็นตัวกรองที่แข็งแรงมาก
    """
    rr = np.arange(3.0, r_max, step)
    az = np.linspace(0, 2 * np.pi, n_az, endpoint=False)
    R, A = np.meshgrid(rr, az, indexing="ij")
    X, Y = cx + R * np.sin(A), cy - R * np.cos(A)
    ok = (X >= 0) & (X < field.shape[1]) & (Y >= 0) & (Y < field.shape[0])
    P = ndimage.map_coordinates(field, [Y, X], order=1, mode="constant", cval=np.nan)
    P[~ok] = np.nan
    return rr, np.nanmedian(P, axis=1)


def find_ring_radii(fields: dict, cx: float, cy: float, rings=DEFAULT_RINGS,
                    min_amp: float = 8.0, tol: float = 0.15) -> list[float]:
    """หารัศมีคร่าว ๆ ของแต่ละวง จากยอดในโปรไฟล์รัศมี

    ยึดวงนอกสุดเป็นหลัก (ยาวที่สุด เห็นชัดที่สุด และคลาดเคลื่อนสัมพัทธ์น้อยที่สุด)
    แล้วใช้สเกลเบื้องต้นจากวงนั้นไปหายอดของวงที่เหลือ
    """
    from scipy.signal import find_peaks
    peaks = {}
    for col in {c for _, c in rings}:
        rr, p = radial_profile(fields[col], cx, cy)
        base = ndimage.median_filter(np.nan_to_num(p), size=61)
        d = np.nan_to_num(p - base, nan=-99.0)
        pk, props = find_peaks(d, height=min_amp, distance=12)
        peaks[col] = (rr[pk], props["peak_heights"])

    anchor_km, anchor_col = max(rings, key=lambda t: t[0])
    cand, amp = peaks[anchor_col]
    if len(cand) == 0:
        raise RuntimeError(f"หาเส้นวงนอกสุด ({anchor_km:.0f} กม., สี {anchor_col}) ไม่เจอ")
    r_anchor = float(cand[int(np.argmax(amp))])
    k0 = anchor_km / r_anchor

    out = []
    for km, col in rings:
        cand, _ = peaks[col]
        if len(cand) == 0:
            out.append(np.nan)
            continue
        expect = km / k0
        r = float(cand[int(np.argmin(np.abs(cand - expect)))])
        out.append(r if abs(r - expect) <= tol * expect else np.nan)
    return out


def ring_points(field: np.ndarray, cx: float, cy: float, r0: float,
                win: float = 5.0, n_az: int = 1440, min_amp: float = 8.0):
    """หาตำแหน่งเส้นวงระดับ sub-pixel ในทุกมุมกวาด (ยอดพาราโบลา)"""
    az = np.linspace(0, 2 * np.pi, n_az, endpoint=False)
    rr = np.arange(r0 - win, r0 + win + 1e-9, 0.2)
    n = len(rr)
    R, A = np.meshgrid(rr, az, indexing="ij")
    X, Y = cx + R * np.sin(A), cy - R * np.cos(A)
    P = ndimage.map_coordinates(field, [Y, X], order=1, mode="constant", cval=-99.0)
    k = np.argmax(P, axis=0)
    j = np.arange(n_az)
    good = (P[k, j] > min_amp) & (k > 0) & (k < n - 1)
    kc = np.clip(k, 1, n - 2)
    y0, y1, y2 = P[kc - 1, j], P[kc, j], P[kc + 1, j]
    den = y0 - 2 * y1 + y2
    off = np.clip(np.where(den != 0, 0.5 * (y0 - y2) / np.where(den == 0, 1, den), 0.0), -1, 1)
    r_fit = rr[kc] + off * 0.2
    good &= np.isfinite(r_fit)
    ang, rg = az[good], r_fit[good]
    return cx + rg * np.sin(ang), cy - rg * np.cos(ang), int(good.sum())


def fit_circle(x: np.ndarray, y: np.ndarray):
    """ฟิตวงกลมแบบ algebraic (Kasa) — คืน (cx, cy, R, rms ของ residual)"""
    A = np.c_[2 * x, 2 * y, np.ones(len(x))]
    sol, *_ = np.linalg.lstsq(A, x ** 2 + y ** 2, rcond=None)
    cx, cy = float(sol[0]), float(sol[1])
    R = float(np.sqrt(sol[2] + cx ** 2 + cy ** 2))
    return cx, cy, R, float(np.std(np.hypot(x - cx, y - cy) - R))


def calibrate(img: Image.Image, cx0: float, cy0: float, rings=DEFAULT_RINGS,
              n_iter: int = 4) -> dict:
    """คืน geometry ที่ฟิตได้จากวงรัศมี

    keys: center_px, km_per_px, rings (รายวง), circularity_rms_px, scale_spread_pct
    """
    fields = colour_fields(img)
    cx, cy = float(cx0), float(cy0)
    radii = find_ring_radii(fields, cx, cy, rings)
    live = [(km, col, r0) for (km, col), r0 in zip(rings, radii) if np.isfinite(r0)]
    if not live:
        raise RuntimeError("หาเส้นวงรัศมีไม่เจอเลย — ตรวจ cx0/cy0 หรือรายการวงที่ให้มา")
    fits = []
    for _ in range(n_iter):
        fits = []
        for km, col, r0 in live:
            x, y, n = ring_points(fields[col], cx, cy, r0)
            if n < 100:
                continue
            fx, fy, fR, sd = fit_circle(x, y)
            fits.append({"km": km, "col": col, "cx": fx, "cy": fy,
                         "r_px": fR, "rms_px": sd, "n_az": n})
        if not fits:
            raise RuntimeError("ฟิตวงรัศมีไม่สำเร็จ — ตรวจ cx0/cy0 ที่ให้มา")
        cx = float(np.mean([f["cx"] for f in fits]))
        cy = float(np.mean([f["cy"] for f in fits]))
        live = [(f["km"], f["col"], f["r_px"]) for f in fits]

    r = np.array([f["r_px"] for f in fits])
    k = np.array([f["km"] for f in fits])
    km_per_px = float((r * k).sum() / (r * r).sum())      # least squares ผ่านจุดกำเนิด
    per_ring = k / r
    for f in fits:
        f["km_fit"] = f["r_px"] * km_per_px
        f["err_km"] = f["km_fit"] - f["km"]
    return {
        "center_px": (cx, cy),
        "km_per_px": km_per_px,
        "rings": fits,
        "circularity_rms_px": float(np.mean([f["rms_px"] for f in fits])),
        "scale_spread_pct": float((per_ring.max() - per_ring.min()) / per_ring.mean() * 100),
    }


def report(res: dict) -> str:
    lines = [f"{'วง (กม.)':>9} {'R (px)':>9} {'ความกลม rms':>12} {'มุมที่ใช้':>9} {'km/px':>8} {'ผิด (กม.)':>10}"]
    for f in res["rings"]:
        lines.append(f"{f['km']:9.0f} {f['r_px']:9.3f} {f['rms_px']:12.3f} "
                     f"{f['n_az']:9d} {f['km']/f['r_px']:8.4f} {f['err_km']:+10.3f}")
    cx, cy = res["center_px"]
    lines += [
        "",
        f"center_px : [{cx:.2f}, {cy:.2f}]",
        f"km_per_px : {res['km_per_px']:.5f}",
        f"ความกลมเฉลี่ย : {res['circularity_rms_px']:.2f} px "
        f"(ถ้าเกิน ~2 px แปลว่าไม่ใช่วงกลม -> projection ไม่ใช่ azimuthal equidistant)",
        f"สเกลจากแต่ละวงต่างกัน : {res['scale_spread_pct']:.2f} % "
        f"(ถ้าเกิน ~1% แปลว่ารัศมีไม่แปรผันตรงกับระยะ -> ไม่ใช่ equidistant)",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 2
    code, path = argv[0], Path(argv[1])
    img = Image.open(path)
    print(f"ภาพ: {path.name}  ขนาด {img.size}")
    # เดาจุดศูนย์กลางหยาบ ๆ จากเส้น crosshair สีแดง แล้วให้การฟิตวงจัดการต่อ
    a = np.asarray(img.convert("RGB"), dtype=int)
    red = (a[..., 0] > 110) & (a[..., 0] - a[..., 1] > 60) & (a[..., 0] - a[..., 2] > 55)
    cy0 = float(np.argmax(red.sum(axis=1)))
    cx0 = float(np.argmax(red.sum(axis=0)))
    print(f"เดาจาก crosshair: ({cx0:.0f}, {cy0:.0f})\n")
    res = calibrate(img, cx0, cy0)
    print(report(res))
    print(f"\nเอาไปใส่ config/stations.yml ใต้ stations.{code}:")
    print(f"    center_px: [{res['center_px'][0]:.2f}, {res['center_px'][1]:.2f}]")
    print(f"    km_per_px: {res['km_per_px']:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
