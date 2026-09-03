"""tmd-radar-archive — เก็บภาพเรดาร์ TMD อัตโนมัติ และตัด background เหลือเฉพาะค่า reflectivity"""
import sys as _sys

__version__ = "0.3.1"

# ตรวจเวอร์ชัน Python ก่อนจะไป import อะไรที่หนักกว่านี้
# ถ้าไม่เช็ค ผู้ใช้จะเจอ error ของ numpy/scipy ที่อ่านไม่รู้เรื่องแทนที่จะรู้ว่าใช้ Python ผิดตัว
if _sys.version_info < (3, 9):
    raise SystemExit(
        "\n[!] tmd-radar-archive ต้องใช้ Python 3.9 ขึ้นไป "
        f"แต่ตอนนี้กำลังรันด้วย {_sys.version.split()[0]}\n"
        f"    ({_sys.executable})\n\n"
        "    ดูว่าเครื่องมี Python ตัวไหนบ้าง:   py -0\n"
        "    แล้วสร้าง virtual environment แยกของโปรเจกต์นี้ (แทนที่ 3.12 ด้วยตัวที่มี):\n"
        "      py -3.12 -m venv .venv\n"
        "      .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        "      .\\.venv\\Scripts\\python.exe -m radar_archive.cli fetch\n"
    )
