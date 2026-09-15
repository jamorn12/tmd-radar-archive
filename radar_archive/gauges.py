"""สถานีวัดน้ำฝนภาคพื้น — ดึงจาก API สาธารณะของ สสน. (thaiwater.net)

    python -m radar_archive.gauges --station PHS                  # ดึงแล้วเขียนไฟล์
    python -m radar_archive.gauges --station PHS --all-provinces  # สแกนทั้ง 77 จังหวัด
    python -m radar_archive.gauges --station PHS --dry-run        # ดูผลเฉย ๆ ไม่เขียนไฟล์

ทำไมต้องมีโมดูลนี้
    หน้าเว็บมีแผง "ฝนสถานีโทรมาตร" แต่ไม่เคยมีข้อมูลต่อเข้ามาเลย — ค่าที่แสดงเป็น 0.0
    คงที่ตลอด และคอลัมน์ Bias จึงเป็น "ค่าเรดาร์ลบศูนย์" ไม่ใช่ bias จริง
    โมดูลนี้เติมข้อมูลจริงเข้าไป และเก็บประวัติไว้ทำ validation ต่อ

บทบาทของข้อมูลชุดนี้ — อ่านให้ชัด
    gauge **ไม่ใช่ input ของ nowcast** ระบบยังพึ่งภาพ TMD อย่างเดียวเหมือนเดิม
    ถ้า thaiwater ล่ม pipeline ยังเดินครบทุกขั้น แค่ไม่มีตัวเลขมาเทียบ
    gauge เป็น **ตัววัดผล** ที่เป็นอิสระจาก TMD โดยสิ้นเชิง — วัดฝนที่ตกถึงพื้นจริง
    ไม่ได้มาจากเรดาร์ ไม่ได้มาจากภาพที่เราถอดสี จึงใช้ตอบคำถามที่ reviewer
    อยากรู้จริง ๆ ได้ว่า "dBZ ที่กู้มาตรงกับฝนจริงหรือเปล่า"

ข้อควรรู้เรื่อง API (ทดสอบแล้ว 15 ก.ย. 2569)
    - เปิดสาธารณะ ไม่ต้องใช้ API key
    - **ถ้าไม่ใส่ province_code จะได้แค่ top 100 ทั้งประเทศ** ไม่ใช่รายการเต็ม
      จึงต้องวนทีละจังหวัดเสมอ (ยืนยันแล้ว: สมุทรสงคราม = 6 สถานี ไม่ใช่ 100
      แปลว่า 100 ไม่ใช่เพดาน แต่เป็นจำนวนจริงของจังหวัดนั้น)
    - `rain_24h` มีเกือบทุกสถานี ส่วน `rain_1h` **ว่างเป็นส่วนใหญ่**
      หน้าเว็บจึงควรให้ 24 ชม. เป็นค่าหลัก ไม่ใช่ 1 ชม.
    - `rainfall_datetime` เป็น **เวลาท้องถิ่น (UTC+7)** ไม่ใช่ UTC
      ต่างจากทุกอย่างในโปรเจกต์นี้ที่เป็น UTC — แปลงทันทีที่รับเข้ามา กันพลาด
    - หนึ่งสถานีอาจโผล่หลายจังหวัดไม่ได้ แต่ id ซ้ำได้ถ้า API เปลี่ยน — dedupe ด้วย id

มารยาทในการเรียก
    เรียกทีละจังหวัดตาม NEAR list (ราว 25 ครั้ง/รอบ) ไม่ใช่ทั้ง 77 จังหวัด
    และหน่วงระหว่างคำขอเล็กน้อย เป็น API ของหน่วยงานรัฐ ไม่ควรถล่ม
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .config import CONFIG_PATH, get_station

DATA = Path(__file__).resolve().parent.parent / "data"
DOCS = Path(__file__).resolve().parent.parent / "docs"

API = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public/rain_24h"
TIMEOUT = 25
PAUSE_S = 0.4          # หน่วงระหว่างจังหวัด — อย่ายิงรัวใส่ API ของหน่วยงานรัฐ
TH_OFFSET = timedelta(hours=7)

SOURCE = {
    "name_th": "สถาบันสารสนเทศทรัพยากรน้ำ (องค์การมหาชน)",
    "short_th": "สสน.",
    "name_en": "Hydro-Informatics Institute",
    "short_en": "HII",
    "site": "https://www.thaiwater.net",
    "api": API,
}

# รหัสจังหวัดตามมาตรฐาน TIS-1099
PROVINCES = {
    "10": "กรุงเทพมหานคร", "11": "สมุทรปราการ", "12": "นนทบุรี", "13": "ปทุมธานี",
    "14": "พระนครศรีอยุธยา", "15": "อ่างทอง", "16": "ลพบุรี", "17": "สิงห์บุรี",
    "18": "ชัยนาท", "19": "สระบุรี", "20": "ชลบุรี", "21": "ระยอง", "22": "จันทบุรี",
    "23": "ตราด", "24": "ฉะเชิงเทรา", "25": "ปราจีนบุรี", "26": "นครนายก",
    "27": "สระแก้ว", "30": "นครราชสีมา", "31": "บุรีรัมย์", "32": "สุรินทร์",
    "33": "ศรีสะเกษ", "34": "อุบลราชธานี", "35": "ยโสธร", "36": "ชัยภูมิ",
    "37": "อำนาจเจริญ", "38": "บึงกาฬ", "39": "หนองบัวลำภู", "40": "ขอนแก่น",
    "41": "อุดรธานี", "42": "เลย", "43": "หนองคาย", "44": "มหาสารคาม",
    "45": "ร้อยเอ็ด", "46": "กาฬสินธุ์", "47": "สกลนคร", "48": "นครพนม",
    "49": "มุกดาหาร", "50": "เชียงใหม่", "51": "ลำพูน", "52": "ลำปาง",
    "53": "อุตรดิตถ์", "54": "แพร่", "55": "น่าน", "56": "พะเยา", "57": "เชียงราย",
    "58": "แม่ฮ่องสอน", "60": "นครสวรรค์", "61": "อุทัยธานี", "62": "กำแพงเพชร",
    "63": "ตาก", "64": "สุโขทัย", "65": "พิษณุโลก", "66": "พิจิตร", "67": "เพชรบูรณ์",
    "70": "ราชบุรี", "71": "กาญจนบุรี", "72": "สุพรรณบุรี", "73": "นครปฐม",
    "74": "สมุทรสาคร", "75": "สมุทรสงคราม", "76": "เพชรบุรี", "77": "ประจวบคีรีขันธ์",
    "80": "นครศรีธรรมราช", "81": "กระบี่", "82": "พังงา", "83": "ภูเก็ต",
    "84": "สุราษฎร์ธานี", "85": "ระนอง", "86": "ชุมพร", "90": "สงขลา", "91": "สตูล",
    "92": "ตรัง", "93": "พัทลุง", "94": "ปัตตานี", "95": "ยะลา", "96": "นราธิวาส",
}

# จังหวัดที่ "มีโอกาส" มีสถานีในรัศมี 240 กม. จาก PHS — เป็น superset โดยตั้งใจ
# กรองจริงด้วยระยะทางอีกที จังหวัดที่ไม่เข้าเกณฑ์จะถูกตัดออกเอง
# ใช้ --all-provinces เพื่อสแกนทั้ง 77 จังหวัดแล้วดูว่ารายชื่อนี้ขาดอะไรไปไหม
NEAR_PHS = ["14", "15", "16", "17", "18", "19", "36", "39", "40", "42",
            "50", "51", "52", "53", "54", "55", "56",
            "60", "61", "62", "63", "64", "65", "66", "67", "72"]


# ---------------------------------------------------------------- 1. ระยะทาง

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """ระยะทางบนผิวโลก — พอสำหรับการกรองรัศมี 240 กม.

    ไม่ใช้ระยะแบบ aeqd ของ grid.py เพราะตรงนี้แค่ต้องการรู้ว่า "อยู่ในโดม
    เรดาร์ไหม" ความคลาดระดับไม่กี่ร้อยเมตรไม่มีผลกับการตัดสินใจนั้น
    ส่วนตอนจับคู่ pixel กับสถานีค่อยใช้ projection จริงของ grid.py
    """
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- 2. ดึงข้อมูล

def fetch_province(code: str, session: requests.Session | None = None) -> list:
    """ดึงสถานีของหนึ่งจังหวัด คืน list ของ record ดิบ

    ไม่ throw เมื่อจังหวัดใดล้ม — คืน [] แล้วให้ตัวเรียกรายงานรวมทีเดียว
    ถ้าจังหวัดเดียวพังแล้วล้มทั้งรอบ จะเสียข้อมูลอีก 24 จังหวัดไปเปล่า ๆ
    """
    s = session or requests
    try:
        r = s.get(API, params={"province_code": code}, timeout=TIMEOUT)
        r.raise_for_status()
        doc = r.json()
    except Exception as e:
        print(f"    [!] {code} {PROVINCES.get(code, '?')}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return []
    if not isinstance(doc, dict) or "data" not in doc:
        print(f"    [!] {code}: รูปแบบผลลัพธ์ไม่ใช่ที่คาด", file=sys.stderr)
        return []
    return doc["data"] or []


def _num(v):
    """คืน float หรือ None — API ส่ง null, "", และ 0 ปนกัน ต้องแยก 0 ออกจาก 'ไม่มีข้อมูล'"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_record(rec: dict, lat0: float, lon0: float) -> dict | None:
    """แปลง record ดิบเป็น dict แบน ๆ พร้อมระยะทางจากเรดาร์ — คืน None ถ้าข้อมูลไม่ครบ"""
    stn = rec.get("station") or {}
    lat, lon = _num(stn.get("tele_station_lat")), _num(stn.get("tele_station_long"))
    if lat is None or lon is None:
        return None

    geo = rec.get("geocode") or {}
    ag = (rec.get("agency") or {}).get("agency_shortname") or {}

    when_local = rec.get("rainfall_datetime")
    when_utc = None
    if when_local:
        try:                                     # เวลาที่ API ส่งมาเป็น UTC+7
            when_utc = (datetime.strptime(when_local, "%Y-%m-%d %H:%M")
                        - TH_OFFSET).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    def th(d, k):
        v = (d.get(k) or {})
        return v.get("th") if isinstance(v, dict) else v

    return dict(
        id=stn.get("id"),
        code=stn.get("tele_station_oldcode") or "",
        name=th(stn, "tele_station_name") or "",
        lat=lat, lon=lon,
        dist_km=round(haversine_km(lat0, lon0, lat, lon), 2),
        rain_1h=_num(rec.get("rain_1h")),
        rain_24h=_num(rec.get("rain_24h")),
        obs_local=when_local,
        obs_utc=when_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if when_utc else None,
        agency=ag.get("th") or "",
        agency_en=ag.get("en") or "",
        tumbon=th(geo, "tumbon_name") or "",
        amphoe=th(geo, "amphoe_name") or "",
        province=th(geo, "province_name") or "",
        basin=th(rec.get("basin") or {}, "basin_name") or "",
    )


def collect(lat0: float, lon0: float, range_km: float,
            provinces: list | None = None, verbose: bool = True) -> tuple:
    """วนจังหวัด -> กรองระยะ -> dedupe  คืน (stations, report)"""
    codes = provinces if provinces is not None else NEAR_PHS
    session = requests.Session()
    session.headers["User-Agent"] = "tmd-radar-archive/gauges (research; contact via GitHub)"

    seen, out = set(), []
    per_prov, failed = {}, []
    for i, code in enumerate(codes):
        raw = fetch_province(code, session)
        if not raw:
            failed.append(code)
        kept = 0
        for rec in raw:
            g = parse_record(rec, lat0, lon0)
            if g is None or g["id"] in seen:
                continue
            if g["dist_km"] > range_km:
                continue
            seen.add(g["id"])
            out.append(g)
            kept += 1
        per_prov[code] = dict(name=PROVINCES.get(code, "?"), got=len(raw), kept=kept)
        if verbose:
            print(f"    {code} {PROVINCES.get(code,'?'):<16} ได้ {len(raw):3d} "
                  f"อยู่ในรัศมี {kept:3d}")
        if i < len(codes) - 1:
            time.sleep(PAUSE_S)

    out.sort(key=lambda g: (-(g["rain_24h"] or -1), g["dist_km"]))
    report = dict(
        n_stations=len(out),
        n_provinces=sum(1 for v in per_prov.values() if v["kept"]),
        with_rain_1h=sum(1 for g in out if g["rain_1h"] is not None),
        with_rain_24h=sum(1 for g in out if g["rain_24h"] is not None),
        failed_provinces=failed,
        per_province=per_prov,
    )
    return out, report


# ---------------------------------------------------------------- 3. เขียนให้หน้าเว็บ

WEB_FIELDS = ["id", "code", "name", "lat", "lon", "dist_km",
              "rain_1h", "rain_24h", "obs_utc", "agency",
              "tumbon", "amphoe", "province"]


def write_web(out_dir: Path, st, stations: list, report: dict) -> Path:
    """เขียน docs/gauges/<CODE>.json

    เก็บเป็น **array ของ array + fields** ไม่ใช่ array ของ object
    ลดขนาด payload ลงเกือบครึ่งที่ 300+ สถานี — สำคัญมากกับมือถือ
    (วิธีเดียวกับที่ ONWR ใช้กับสถานี 4,436 แห่ง)
    """
    out_dir = Path(out_dir) / "gauges"
    out_dir.mkdir(parents=True, exist_ok=True)

    agencies = {}
    for g in stations:
        if g["agency"] and g["agency"] not in agencies:
            agencies[g["agency"]] = g["agency_en"]

    doc = {
        "generated": int(datetime.now(timezone.utc).timestamp()),
        "source": SOURCE,
        "radar": {"code": st.code, "lat": st.lat, "lon": st.lon,
                  "range_km": st.range_km},
        "stale_after_min": 90,
        "note_th": ("rain_1h มีไม่ครบทุกสถานี ให้ใช้ rain_24h เป็นค่าหลัก · "
                    "obs_utc เป็น UTC แล้ว (API ต้นทางส่งมาเป็น UTC+7)"),
        "agencies": agencies,
        "counts": {k: report[k] for k in
                   ("n_stations", "n_provinces", "with_rain_1h", "with_rain_24h")},
        "fields": WEB_FIELDS,
        "stations": [[g[k] for k in WEB_FIELDS] for g in stations],
    }
    p = out_dir / f"{st.code}.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------- 4. เก็บประวัติ

HIST_COLS = ["station_id", "code", "name", "agency", "province",
             "lat", "lon", "dist_km", "obs_utc", "rain_1h", "rain_24h"]


def append_history(root: Path, st, stations: list) -> tuple:
    """เขียนต่อท้าย data/gauges/<CODE>_YYYYMM.csv — หนึ่งแถวต่อหนึ่ง (สถานี, เวลาสังเกต)

    dedupe ด้วย (station_id, obs_utc) เพราะ pipeline รันทุก 15 นาที แต่สถานี
    รายงานทุกชั่วโมง ถ้าไม่กัน จะได้แถวซ้ำ 4 เท่าและค่าสะสมจะเพี้ยนตอนเอาไปวิเคราะห์

    แยกไฟล์รายเดือนเพื่อไม่ให้ไฟล์เดียวโตจนเปิดไม่ไหว และ git diff อ่านง่าย
    """
    out_dir = Path(root) / "gauges"
    out_dir.mkdir(parents=True, exist_ok=True)
    month = datetime.now(timezone.utc).strftime("%Y%m")
    path = out_dir / f"{st.code}_{month}.csv"

    seen = set()
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                seen.add((row["station_id"], row["obs_utc"]))

    rows = []
    for g in stations:
        key = (str(g["id"]), g["obs_utc"] or "")
        if not g["obs_utc"] or key in seen:
            continue
        seen.add(key)
        rows.append({
            "station_id": g["id"], "code": g["code"], "name": g["name"],
            "agency": g["agency"], "province": g["province"],
            "lat": g["lat"], "lon": g["lon"], "dist_km": g["dist_km"],
            "obs_utc": g["obs_utc"],
            "rain_1h": "" if g["rain_1h"] is None else g["rain_1h"],
            "rain_24h": "" if g["rain_24h"] is None else g["rain_24h"],
        })

    new = path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS)
        if not new:
            w.writeheader()
        w.writerows(rows)
    return path, len(rows)


# ---------------------------------------------------------------- 5. CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="radar_archive.gauges",
        description="ดึงสถานีวัดน้ำฝนภาคพื้นจาก API ของ สสน. ในรัศมีเรดาร์")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--station", default="PHS")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--docs", default=str(DOCS))
    p.add_argument("--range-km", type=float, default=None,
                   help="ทับรัศมีของสถานี (ค่าเริ่มต้นใช้ range_km ใน stations.yml)")
    p.add_argument("--all-provinces", action="store_true",
                   help="สแกนทั้ง 77 จังหวัด — ใช้ตรวจว่ารายชื่อ NEAR_PHS ขาดจังหวัดไหนไหม")
    p.add_argument("--dry-run", action="store_true", help="ไม่เขียนไฟล์ แสดงผลอย่างเดียว")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    st = get_station(a.station, a.config)
    rng = a.range_km if a.range_km else st.range_km
    codes = list(PROVINCES) if a.all_provinces else NEAR_PHS

    print(f"=== {st.code} ({st.lat:.4f}, {st.lon:.4f}) รัศมี {rng:.0f} กม. "
          f"· {len(codes)} จังหวัด ===")
    stations, rep = collect(st.lat, st.lon, rng, codes, verbose=not a.quiet)

    print(f"\nสถานีในรัศมี {rep['n_stations']} แห่ง จาก {rep['n_provinces']} จังหวัด"
          f" · มี rain_24h {rep['with_rain_24h']} · มี rain_1h {rep['with_rain_1h']}")
    if rep["failed_provinces"]:
        print(f"[!] ดึงไม่สำเร็จ {len(rep['failed_provinces'])} จังหวัด: "
              f"{', '.join(rep['failed_provinces'])}")

    by_ag = {}
    for g in stations:
        by_ag[g["agency"] or "(ไม่ระบุ)"] = by_ag.get(g["agency"] or "(ไม่ระบุ)", 0) + 1
    print("หน่วยงาน: " + " · ".join(f"{k} {v}" for k, v in
                                    sorted(by_ag.items(), key=lambda x: -x[1])))

    top = [g for g in stations if g["rain_24h"]][:5]
    if top:
        print("\nฝนสูงสุด 24 ชม.")
        for g in top:
            print(f"    {g['rain_24h']:6.1f} มม.  {g['name'][:34]:<34} "
                  f"{g['agency']:<6} {g['dist_km']:5.1f} กม.  {g['obs_utc']}")

    if a.dry_run:
        print("\n--dry-run: ไม่เขียนไฟล์")
        return 0

    web = write_web(Path(a.docs), st, stations, rep)
    hist, n_new = append_history(Path(a.data), st, stations)
    print(f"\n-> {web}  ({web.stat().st_size/1024:.1f} KB)")
    print(f"-> {hist}  (+{n_new} แถวใหม่)")

    if a.all_provinces:
        extra = [c for c, v in rep["per_province"].items()
                 if v["kept"] and c not in NEAR_PHS]
        if extra:
            print(f"\n[i] จังหวัดที่มีสถานีในรัศมีแต่ไม่อยู่ใน NEAR_PHS: "
                  f"{', '.join(extra)} — ควรเพิ่มเข้าไปในโค้ด")
        else:
            print("\n[ok] NEAR_PHS ครอบคลุมครบแล้ว ไม่ขาดจังหวัดไหน")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
