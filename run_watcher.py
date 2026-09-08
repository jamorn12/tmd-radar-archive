import os
import time
import traceback
import requests
import json
from datetime import datetime

# --- [ตั้งค่าระบบแจ้งเตือน Telegram Bot] ---
# แนะนำให้สร้าง Bot ผ่าน @BotFather บน Telegram แล้วนำ Token กับ Chat ID มาใส่ตรงนี้
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


def run_with_retry_and_recovery(task_func, task_name="ประมวลผลเรดาร์รอบปัจจุบัน"):
    """
    ฟังก์ชันแม่ข่ายควบคุมความปลอดภัย (Wrapper):
    1. ระบบลองใหม่แบบอัตโนมัติ (Retry) สูงสุด 3 ครั้ง พร้อมเว้นระยะเวลา (Exponential Backoff)
    2. ระบบกู้คืนอัตโนมัติ (Auto-Recovery) เมื่อพยายามครบแล้วยังล้มเหลว
    3. ระบบแจ้งเตือนผ่าน Telegram (Notification)
    """
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 เริ่มงาน: {task_name} (ความพยายามครั้งที่ {attempt}/{max_retries})")
            
            # รันฟังก์ชันหลัก
            task_func()
            
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ สำเร็จ: {task_name}")
            return True
            
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"❌ พลาดในรอบที่ {attempt}: {str(e)}")
            print(error_detail)
            
            if attempt < max_retries:
                sleep_time = attempt * 20  # รอบแรกเว้น 20 วิ, รอบสองเว้น 40 วิ
                print(f"⏳ กำลังรอ {sleep_time} วินาทีก่อนลองใหม่...")
                time.sleep(sleep_time)
            else:
                # --- เมื่อพยายามครบ 3 ครั้งแล้วยังพัง เข้าสู่กระบวนการกู้คืนระบบ (Auto-Recovery) ---
                fail_msg = f"❌ *{task_name}* ล้มเหลวขั้นวิกฤตหลังพยายาม 3 ครั้ง!\nกำลังเริ่มกระบวนการกู้คืนระบบอัตโนมัติ...\n\n`{str(e)[:300]}`"
                print(fail_msg)
                send_telegram_alert(fail_msg)
                
                success_recovered = execute_auto_recovery_routine()
                if success_recovered:
                    recovery_msg = f"🛠️ *Auto-Recovery สำเร็จ:* ระบบได้เคลียร์ไฟล์ขยะและกู้คืนสถานะพร้อมทำงานต่อแล้ว"
                    print(recovery_msg)
                    send_telegram_alert(recovery_msg)
                else:
                    critical_msg = f"🚨 *Auto-Recovery ล้มเหลว:* ระบบไม่สามารถกู้คืนตัวเองได้อัตโนมัติ ต้องให้ผู้ดูแลตรวจสอบด่วน!"
                    print(critical_msg)
                    send_telegram_alert(critical_msg)
                
                return False

def execute_auto_recovery_routine():
    """ขั้นตอนการกู้คืนระบบ (Auto-Recovery) เมื่อหลังบ้านเกิดอาการน็อค"""
    try:
        print("🛠️ กำลังดำเนินการกู้คืนระบบ (Auto-Recovery Routine)...")
        
        # 1. ลบไฟล์ชั่วคราวหรือไฟล์ขยะที่อาจค้างและเสียหาย
        temp_files = ["districts_temp.geojson", "nowcast/PHS/temp_frame.png", "temp_radar.png"]
        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)
                print(f"🗑️ ลบไฟล์ขยะเคลียร์ระบบ: {f}")
        
        # 2. ตรวจสอบความถูกต้องของไฟล์ manifest (latest.json)
        manifest_path = "nowcast/PHS/latest.json"
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as file_check:
                content = file_check.read().strip()
                if not content:
                    raise ValueError("ไฟล์ latest.json ว่างเปล่า (Corrupted)")
                json.loads(content) # เช็คว่า JSON ถูกต้องไหม
        
        print("✨ กู้คืนสถานะระบบสำเร็จเรียบร้อย")
        return True
        
    except Exception as recovery_err:
        print(f"🔥 กู้คืนระบบไม่สำเร็จเนื่องจาก: {recovery_err}")
        return False


def main_radar_pipeline():
    """
    ฟังก์ชันหลักสำหรับดึงข้อมูลเรดาร์ ประมวลผล และอัปเดตระบบ
    (แทนที่ส่วนการทำงานเดิมของคุณไว้ในนี้)
    """
    print("🛰️ กำลังเชื่อมต่อเพื่อดึงภาพเรดาร์และข้อมูล Nowcast ล่าสุดจาก TMD...")
    
    # --- ใส่โค้ดหลักเดิมของคุณตรงนี้ ---
    # ตัวอย่างเช่น:
    # 1. โหลดภาพเรดาร์จากเซิร์ฟเวอร์ TMD
    # 2. ประมวลผลภาพ (Image Processing / Masking)
    # 3. อัปเดตไฟล์ลงในโฟลเดอร์ /nowcast/PHS/ และบันทึก manifest
    # --------------------------------
    
    # จำลองการทำงาน (สมมติว่าดึงสำเร็จ)
    time.sleep(2)
    print("📥 ดาวน์โหลดและประมวลผลภาพเรดาร์สำเร็จ")


if __name__ == "__main__":
    print("🚀 ระบบ Watcher หลังบ้านเริ่มต้นทำงาน (พร้อมระบบป้องกันและกู้คืนอัตโนมัติ)")
    
    # ส่งแจ้งเตือนเมื่อบอทเริ่มสตาร์ทระบบ
    send_telegram_alert("🟢 *ระบบหลังบ้าน (Radar Watcher)* เริ่มต้นทำงานและสแตนด์บายดูแล 24 ชม. แล้ว")
    
    while True:
        # สั่งรันงานโดยมีระบบ Auto-Retry & Recovery คอยคุ้ม 24 ชม.
        run_with_retry_and_recovery(main_radar_pipeline, "ดึงและประมวลผลเรดาร์รอบเวลาปัจจุบัน")
        
        # หน่วงเวลาก่อนรอบถัดไป (เช่น ทุกๆ 5 นาที หรือ 300 วินาที)
        print("⏳ พักรอนรอบถัดไปในอีก 5 นาที...")
        time.sleep(300)
