import os
import glob
import json
import time
import traceback
import requests
from datetime import datetime, timezone, timedelta

# --- [ตั้งค่าระบบแจ้งเตือน Telegram Bot] ---
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"      

def send_telegram_alert(message):
    """ฟังก์ชันส่งข้อความแจ้งเตือนเข้า Telegram ทันทีเมื่อเกิดเหตุฉุกเฉิน"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"⚠️ [Telegram Alert Skipped]: {message}")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚨 *[ทันฝน System Alert]*\n{message}",
            "parse_mode": "Markdown"
        }
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            print("⚠️ ไม่สามารถส่งการแจ้งเตือนไป Telegram ได้")
    except Exception as e:
        print(f"⚠️ เกิดข้อผิดพลาดในการส่ง Alert: {e}")

def update_manifest_dynamically(folder_path="nowcast/PHS"):
    """
    สแกนไฟล์ภาพเรดาร์ทั้งหมดในโฟลเดอร์ แล้วสร้าง/อัปเดตไฟล์ latest.json ใหม่โดยอัตโนมัติ
    แก้ปัญหาเวลาบนหน้าเว็บไม่ตรงกับภาพล่าสุด หรือข้อมูลขาดหาย
    """
    if not os.path.exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)
        
    # ค้นหาไฟล์ภาพทั้งหมด (รองรับทั้ง _solid.png และ .jpg)
    patterns = [
        os.path.join(folder_path, "PHS_*_solid.png"), 
        os.path.join(folder_path, "PHS_*.jpg"),
        os.path.join(folder_path, "*.png"),
        os.path.join(folder_path, "*.jpg")
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
        
    files = list(set(files)) # กรองไฟล์ซ้ำ
    
    if not files:
        print("⚠️ ไม่พบไฟล์ภาพเรดาร์ในโฟลเดอร์สำหรับสร้าง Manifest")
        return

    frames = []
    for file_path in sorted(files):
        filename = os.path.basename(file_path)
        # ข้ามไฟล์ที่ไม่ใช่รูปภาพหลักของเรดาร์
        if "icon" in filename or "temp" in filename:
            continue
            
        try:
            # รูปแบบชื่อไฟล์ตัวอย่าง: PHS_20260908_0315Z_solid.png หรือ PHS_20260908_0315Z.jpg
            parts = filename.split("_")
            if len(parts) >= 2:
                date_str = parts[1] # เช่น 20260908
                time_str = parts[2].replace("Z", "").split(".")[0] # เช่น 0315
                
                dt_str = f"{date_str} {time_str}"
                dt = datetime.strptime(dt_str, "%Y%m%d %H%M").replace(tzinfo=timezone.utc)
                timestamp = int(dt.timestamp())
                
                frames.append({
                    "t": timestamp,
                    "url": filename,
                    "offset_min": 0,
                    "kind": "obs"
                })
        except Exception as e:
            print(f"⚠️ ข้ามไฟล์ {filename} เนื่องจากแปลงเวลาไม่ได้: {e}")
            
    if not frames:
        print("⚠️ ไม่สามารถสกัด Timestamp จากชื่อไฟล์ภาพได้เลย")
        return
        
    # เรียงลำดับเฟรมตามเวลาจากอดีตไปปัจจุบัน
    frames.sort(key=lambda x: x["t"])
    
    # คำนวณ offset_min และกำหนดชนิดของเฟรม (obs หรือ nowcast)
    total_frames = len(frames)
    for i, frame in enumerate(frames):
        offset = (i - (total_frames - 1)) * 15
        frame["offset_min"] = offset
        frame["kind"] = "obs" if offset <= 0 else "nowcast"

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
        
    print(f"✅ อัปเดตไฟล์ {manifest_path} สำเร็จ! (รวมทั้งหมด {total_frames} เฟรม | ล่าสุด: {frames[-1]['url']})")

def run_with_retry_and_recovery(task_func, task_name="ประมวลผลเรดาร์รอบปัจจุบัน"):
    """
    ฟังก์ชันแม่ข่ายควบคุมความปลอดภัย:
    1. ระบบลองใหม่แบบอัตโนมัติ (Retry) สูงสุด 3 ครั้ง
    2. ระบบกู้คืนอัตโนมัติ (Auto-Recovery)
    3. ระบบแจ้งเตือนผ่าน Telegram
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 เริ่มงาน: {task_name} (ความพยายามครั้งที่ {attempt}/{max_retries})")
            
            task_func()
            
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ สำเร็จ: {task_name}")
            return True
            
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"❌ พลาดในรอบที่ {attempt}: {str(e)}")
            print(error_detail)
            
            if attempt < max_retries:
                sleep_time = attempt * 20
                print(f"⏳ กำลังรอ {sleep_time} วินาทีก่อนลองใหม่...")
                time.sleep(sleep_time)
            else:
                fail_msg = f"❌ *{task_name}* ล้มเหลวขั้นวิกฤตหลังพยายาม 3 ครั้ง!\nกำลังเริ่มกระบวนการกู้คืนระบบ...\n\n`{str(e)[:300]}`"
                send_telegram_alert(fail_msg)
                
                success_recovered = execute_auto_recovery_routine()
                if success_recovered:
                    send_telegram_alert("🛠️ *Auto-Recovery สำเร็จ:* ระบบได้กู้คืนสถานะพร้อมทำงานต่อแล้ว")
                else:
                    send_telegram_alert("🚨 *Auto-Recovery ล้มเหลว:* ระบบไม่สามารถกู้คืนตัวเองได้อัตโนมัติ ต้องตรวจสอบด่วน!")
                return False

def execute_auto_recovery_routine():
    """ขั้นตอนการกู้คืนระบบ (Auto-Recovery)"""
    try:
        print("🛠️ กำลังดำเนินการกู้คืนระบบ (Auto-Recovery Routine)...")
        temp_files = ["districts_temp.geojson", "nowcast/PHS/temp_frame.png", "temp_radar.png"]
        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)
                print(f"🗑️ ลบไฟล์ขยะเคลียร์ระบบ: {f}")
        
        # สั่งรีเฟรชสร้าง Manifest ใหม่ทันที
        update_manifest_dynamically("nowcast/PHS")
        print("✨ กู้คืนสถานะระบบสำเร็จเรียบร้อย")
        return True
    except Exception as recovery_err:
        print(f"🔥 กู้คืนระบบไม่สำเร็จเนื่องจาก: {recovery_err}")
        return False

def main_radar_pipeline():
    """
    ฟังก์ชันหลักสำหรับดึงข้อมูลเรดาร์ ประมวลผล และอัปเดตระบบ
    """
    print("🛰️ กำลังดึงภาพเรดาร์และข้อมูล Nowcast ล่าสุด...")
    
    # --- [ใส่โค้ดดาวน์โหลด / ประมวลผลภาพเรดาร์ของคุณตรงนี้] ---
    # ตัวอย่าง: ดาวน์โหลดภาพจาก TMD มาเก็บไว้ในโฟลเดอร์ nowcast/PHS/
    
    # --------------------------------------------------------
    
    # [จุดสำคัญ]: ทุกครั้งที่ดาวน์โหลดภาพเสร็จ ให้สั่งอัปเดต Manifest ทันที
    update_manifest_dynamically("nowcast/PHS")
    print("📥 บันทึกภาพและอัปเดต Manifest เรียบร้อย")

if __name__ == "__main__":
    print("🚀 ระบบ Watcher หลังบ้านเริ่มต้นทำงาน (พร้อมระบบป้องกัน, กู้คืน และซิงค์ Manifest)")
    send_telegram_alert("🟢 *ระบบหลังบ้าน (Radar Watcher)* เริ่มต้นทำงานและสแตนด์บายดูแล 24 ชม. แล้ว")
    
    while True:
        run_with_retry_and_recovery(main_radar_pipeline, "ดึงและประมวลผลเรดาร์รอบเวลาปัจจุบัน")
        
        print("⏳ พักรอนรอบถัดไปในอีก 5 นาที...")
        time.sleep(300)
