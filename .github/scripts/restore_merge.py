"""รวมไฟล์จาก artifact pending-raw-* กลับเข้าคลัง (เรียกโดย restore_pending.sh)

โครงสร้าง artifact (สร้างโดย commit_push.sh เมื่อ push ไม่ผ่าน):
    files/<path ใน repo>        ไฟล์ใหม่ เช่น files/data/raw/PHS/2026/10/PHS_20261007_1500Z.jpg
    rows/<path ของ csv>         แถวที่เพิ่มใน csv รอบนั้น (ไม่มี \\r)

กติกา: ไฟล์ที่มีอยู่แล้วไม่เขียนทับ · แถว csv ที่ (คอลัมน์ 1, คอลัมน์ 2) ซ้ำกับที่มีอยู่ถูกข้าม
       แถวใหม่แทรกแล้วเรียงตาม timestamp_utc · คงรูปแบบท้ายบรรทัดเดิมของไฟล์ (CRLF/LF)
"""
import csv
import shutil
import sys
from pathlib import Path


def first_cols(line: str, n: int = 2):
    return tuple(next(csv.reader([line]))[:n])


def main(src: Path) -> int:
    n_files = n_rows = 0
    for f in sorted((src / "files").rglob("*")):
        if not f.is_file():
            continue
        dst = f.relative_to(src / "files")
        if dst.parts[0] != "data":            # รับเฉพาะใต้ data/
            continue
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            n_files += 1
            print(f"+ {dst}")
    for f in sorted((src / "rows").rglob("*.csv")):
        dst = f.relative_to(src / "rows")
        if dst.parts[0] != "data" or not dst.exists():
            continue
        raw = dst.read_bytes()
        eol = "\r\n" if b"\r\n" in raw else "\n"
        lines = raw.decode("utf-8").splitlines()
        if not lines:
            continue
        header, body = lines[0], [l for l in lines[1:] if l.strip()]
        have = {first_cols(l) for l in body}
        add = []
        for l in f.read_text(encoding="utf-8").splitlines():
            if not l.strip() or l == header:
                continue
            k = first_cols(l)
            if k not in have:
                have.add(k)
                add.append(l)
        if not add:
            continue
        body += add
        cols = next(csv.reader([header]))
        if "timestamp_utc" in cols:
            i = cols.index("timestamp_utc")
            body.sort(key=lambda l: next(csv.reader([l]))[i])     # sort แบบ stable
        dst.write_bytes((eol.join([header] + body) + eol).encode("utf-8"))
        n_rows += len(add)
        print(f"+ {len(add)} แถว → {dst}")
    print(f"ดึงกลับ: ไฟล์ {n_files} · แถว log {n_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
