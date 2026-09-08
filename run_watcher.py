import os
import glob
import json
from datetime import datetime, timezone, timedelta

def update_manifest_dynamically(folder_path="docs/nowcast/PHS"):
    """
    สร้างไฟล์ latest.json ในรูปแบบ Nowcast มาตรฐาน:
    - เฟรมอดีต (obs) ย้อนหลัง
    - เฟรมปัจจุบัน (obs ที่ offset_min = 0)
    - เฟรมพยากรณ์ล่วงหน้า (nowcast)
    """
    if not os.path.exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)
        
    pattern = os.path.join(folder_path, "PHS_*_solid.png")
    files = glob.glob(pattern)
    files.sort()
    
    if not files:
        print("⚠️ ไม่พบไฟล์ภาพเรดาร์ในโฟลเดอร์")
        return

    obs_frames = []
    for file_path in files:
        filename = os.path.basename(file_path)
        try:
            # ตัวอย่างชื่อไฟล์: PHS_20260908_0315Z_solid.png
            parts = filename.split("_")
            date_str = parts[1]
            time_str = parts[2].replace("Z", "").split(".")[0]
            
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y%m%d %H%M").replace(tzinfo=timezone.utc)
            timestamp = int(dt.timestamp())
            
            obs_frames.append({
                "t": timestamp,
                "url": filename
            })
        except Exception as e:
            print(f"⚠️ ข้ามไฟล์ {filename}: {e}")
            
    if not obs_frames:
        return

    # เรียงลำดับจากเก่าไปใหม่ และเลือกเอาเฉพาะ 4-6 เฟรมล่าสุดมาทำเป็นอดีต+ปัจจุบัน
    obs_frames.sort(key=lambda x: x["t"])
    recent_obs = obs_frames[-6:] # เอา 6 เฟรมล่าสุด (เช่น ย้อนหลัง 1.5 ชม.)
    
    frames = []
    total_obs = len(recent_obs)
    
    # 1. สร้างเฟรมตรวจวัดจริง (obs) โดยให้เฟรมสุดท้ายเป็นปัจจุบัน (offset_min = 0)
    for i, item in enumerate(recent_obs):
        offset = (i - (total_obs - 1)) * 15  # เช่น -75, -60, -45, -30, -15, 0
        frames.append({
            "t": item["t"],
            "url": item["url"],
            "offset_min": offset,
            "kind": "obs"
        })
        
    # 2. สร้างเฟรมพยากรณ์ล่วงหน้า (Nowcast) ต่อเนื่องไปอีก 4 เฟรม (+15, +30, +45, +60 นาที)
    latest_t = recent_obs[-1]["t"]
    latest_url = recent_obs[-1]["url"]
    
    for step in range(1, 5):
        future_offset = step * 15
        future_t = latest_t + (future_offset * 60)
        frames.append({
            "t": future_t,
            "url": latest_url,  # ใช้ภาพล่าสุดเป็นฐานในการทำนายทิศทางบนหน้าเว็บ
            "offset_min": future_offset,
            "kind": "nowcast"
        })

    manifest = {
        "projection": "+proj=laea +lat_0=16.77 +lon_0=100.22 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
        "grid": [160, 160],
        "kmperpixel": 3.0,
        "motion": {"speed_kmh": 15.0, "direction_deg": 270},
        "frames": frames
    }
    
    manifest_path = os.path.join(folder_path, "latest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        
    print(f"✅ สร้าง Manifest Nowcast สำเร็จ! (รวม {len(frames)} เฟรม | ปัจจุบัน: {latest_url})")
