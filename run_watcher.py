import subprocess
import time
from datetime import datetime

STATION = "PHS"
CHECK_INTERVAL_SECONDS = 60  # ตรวจสอบทุกๆ 60 วินาที

def execute(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, capture_output=True, text=True)

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] เริ่มต้น TMD Radar Watcher ({STATION})")
    print(f"รอบเวลาการวนตรวจ: ทุก {CHECK_INTERVAL_SECONDS} วินาที\n")

    while True:
        utc_now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # 1. สั่งดึงข้อมูลเรดาร์
        fetch_res = execute(f"python -m radar_archive.cli --station {STATION} fetch")
        
        # 2. ตรวจสอบว่ามีไฟล์ใหม่ในโฟลเดอร์ data/ หรือไม่
        status_res = execute("git status --porcelain data/")
        
        if status_res.stdout.strip():
            print(f"[{utc_now}] >>> พบภาพเรดาร์ใหม่! กำลังบันทึกและ Push ขึ้น GitHub...")
            execute("git add data/")
            execute('git commit -m "archive: update radar data [skip ci]"')
            push_res = execute("git push")
            
            if push_res.returncode == 0:
                print(f"[{utc_now}] >>> Push สำเร็จเรียบร้อย\n")
            else:
                print(f"[{utc_now}] [Warning] Push ล้มเหลว: {push_res.stderr}\n")
        else:
            print(f"[{utc_now}] ตรวจสอบแล้ว: เว็บ TMD ยังไม่มีภาพใหม่ พัก {CHECK_INTERVAL_SECONDS} วินาที...")

        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
