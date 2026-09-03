"""ตำแหน่งดวงอาทิตย์ — ใช้แยกว่า radial spike ที่เจอเป็น sun spike หรือ RFI

sun spike (sun strobe) เกิดตอนดวงอาทิตย์อยู่ต่ำใกล้ขอบฟ้าและอยู่ในแนวลำคลื่นพอดี
เรดาร์รับรังสีไมโครเวฟจากดวงอาทิตย์เข้ามาโดยตรง เห็นเป็นเส้นพุ่งไปทางทิศดวงอาทิตย์
เกิดวันละ 2 ช่วงสั้น ๆ (เช้า/เย็น) เท่านั้น ต่างจาก RFI ที่ประจำอยู่มุมเดิมได้ทั้งวัน

การแยกสองอย่างนี้ออกจากกันสำคัญสำหรับการรายงาน QC ในเปเปอร์
เพราะ sun spike เป็นเรื่องปกติที่คาดการณ์ได้ ส่วน RFI เป็นปัญหาของสภาพแวดล้อมสถานี

สูตรจาก NOAA Solar Calculator (ความแม่นราว 0.1° — เกินพอสำหรับงานนี้)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def _julian_day(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    d = (dt.day + (dt.hour + (dt.minute + dt.second / 60) / 60) / 24)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def solar_position(dt: datetime, lat: float, lon: float) -> tuple[float, float]:
    """คืน (azimuth, elevation) เป็นองศา — azimuth วัดตามเข็มนาฬิกาจากทิศเหนือ"""
    jc = (_julian_day(dt) - 2451545.0) / 36525.0

    gml = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360           # mean longitude
    gma = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)                   # mean anomaly
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    gma_r = math.radians(gma)
    ctr = (math.sin(gma_r) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
           + math.sin(2 * gma_r) * (0.019993 - 0.000101 * jc)
           + math.sin(3 * gma_r) * 0.000289)
    true_long = gml + ctr
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    eps0 = (23 + (26 + ((21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813)))) / 60) / 60)
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    eps_r, app_r = math.radians(eps), math.radians(app_long)

    decl = math.asin(math.sin(eps_r) * math.sin(app_r))

    y = math.tan(eps_r / 2) ** 2
    gml_r = math.radians(gml)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * gml_r)
        - 2 * ecc * math.sin(gma_r)
        + 4 * ecc * y * math.sin(gma_r) * math.cos(2 * gml_r)
        - 0.5 * y * y * math.sin(4 * gml_r)
        - 1.25 * ecc * ecc * math.sin(2 * gma_r)
    )

    dt = dt.astimezone(timezone.utc)
    minutes = dt.hour * 60 + dt.minute + dt.second / 60
    true_solar_time = (minutes + eq_time + 4 * lon) % 1440
    ha_deg = true_solar_time / 4 - 180          # -180..180 : ลบ = ก่อนเที่ยงสุริยะ
    ha = math.radians(ha_deg)

    lat_r = math.radians(lat)
    cos_zen = (math.sin(lat_r) * math.sin(decl)
               + math.cos(lat_r) * math.cos(decl) * math.cos(ha))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zenith = math.acos(cos_zen)
    elev = 90.0 - math.degrees(zenith)

    if abs(math.sin(zenith)) < 1e-9:
        az = 0.0
    else:
        c = ((math.sin(lat_r) * math.cos(zenith) - math.sin(decl))
             / (math.cos(lat_r) * math.sin(zenith)))
        c = max(-1.0, min(1.0, c))
        az = math.degrees(math.acos(c))
        az = (180 + az) % 360 if ha_deg > 0 else (540 - az) % 360

    # การหักเหของบรรยากาศ สำคัญตรงขอบฟ้าพอดี
    if -1 < elev < 15:
        te = math.tan(math.radians(max(elev, 0.05)))
        if elev > 5:
            refr = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
        else:
            refr = 1735 + elev * (-518.2 + elev * (103.4 + elev * (-12.79 + elev * 0.711)))
        elev += refr / 3600.0

    return az % 360, elev


def is_sun_spike(dt: datetime, lat: float, lon: float, azimuth_deg: float,
                 az_tol: float = 5.0, elev_range: tuple[float, float] = (-1.5, 5.0)) -> bool:
    """spike ที่มุมนี้ ตรงกับทิศดวงอาทิตย์ตอนอยู่ต่ำใกล้ขอบฟ้าหรือเปล่า"""
    az_sun, elev = solar_position(dt, lat, lon)
    if not (elev_range[0] <= elev <= elev_range[1]):
        return False
    d = abs((azimuth_deg - az_sun + 180) % 360 - 180)
    return d <= az_tol
