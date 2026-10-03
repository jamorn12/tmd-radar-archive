"""keeper.py — ตัวตั้งเวลาสำรองที่ไม่ต้องใช้ token ส่วนตัว (รันใน .github/workflows/keeper.yml)

ปัญหาที่แก้ (3 ต.ค. 2569)
    ตัวตั้งเวลาภายนอกสั่งรันด้วย personal access token → token หมดอายุ = ระบบหยุดทันที
    GitHub cron สำรองเลื่อน/ข้ามรอบบ่อย (วันนั้นรันแค่ทุก 2–3 ชม.) จึงพยากรณ์ต่อไม่ได้
    watchdog เองก็รันด้วย cron จึงช่วยไม่ทัน

วิธีทำงาน
    job เดียวที่รันยาว ~5 ชม. 35 นาที ใช้ GITHUB_TOKEN ของ repo (ไม่มีวันหมดอายุ)
    · ทุกรอบ :02 :17 :32 :47 — รอ 4 นาที ถ้ายังไม่มี run ของ archive.yml เริ่มในรอบนั้น → สั่งรันแทน
      (ตัวตั้งเวลาภายนอกยังเป็นตัวหลัก keeper สั่งเฉพาะรอบที่ตัวหลักพลาด ไม่รันซ้อน)
    · ทุก :12 :42 — ดึงไฟล์สถานะล่าสุดแล้วรัน watchdog.py (watchdog ไม่ต้องพึ่ง cron อีกต่อไป)
    · ใกล้ครบเวลา — สั่ง keeper.yml ตัวใหม่ (ตัวใหม่ยกเลิกตัวเก่าผ่าน concurrency) เป็นสายต่อกันไม่ขาด
    watchdog.yml (cron) คอยดูว่า keeper ยังรันอยู่ ถ้าหลุดจะสั่งเริ่มใหม่ — สองตัวเฝ้ากันและกัน

ใช้: python .github/scripts/keeper.py [--dry-run] [--minutes N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

SLOTS = (2, 17, 32, 47)          # นาทีที่ตัวตั้งเวลาภายนอกยิง (ก่อนภาพออก ~2 นาที — ดู archive.yml)
GRACE_MIN = 4                    # รอตัวหลักกี่นาทีก่อนสั่งแทน (4 = ไม่ชนกับตัวตั้งเวลาที่ยังยิง :05 แบบเดิม)
WATCHDOG_AT = (12, 42)
LIFETIME_MIN = 335               # 5 ชม. 35 นาที (job จำกัด 6 ชม.)
STATUS_FILES = ["data/log/PHS_index.csv", "docs/nowcast/PHS/latest.json", "docs/gauges/PHS.json"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def sh(cmd: list[str], check: bool = False) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr}")
    if r.returncode != 0:
        print(f"[warn] {' '.join(cmd[:4])}… → {r.stderr.strip()[:200]}", flush=True)
    return r.stdout


def last_slot(t: datetime) -> datetime:
    """รอบ :02 :17 :32 :47 ล่าสุดที่ ≤ t"""
    base = t.replace(second=0, microsecond=0)
    for back in range(0, 61):
        c = base - timedelta(minutes=back)
        if c.minute in SLOTS:
            return c
    return base


def last_mark(t: datetime, marks) -> datetime:
    base = t.replace(second=0, microsecond=0)
    for back in range(0, 61):
        c = base - timedelta(minutes=back)
        if c.minute in marks:
            return c
    return base


def archive_runs_since(t: datetime) -> list[dict]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    out = sh(["gh", "api", f"repos/{repo}/actions/workflows/archive.yml/runs?per_page=6"])
    try:
        runs = json.loads(out or "{}").get("workflow_runs", [])
    except json.JSONDecodeError:
        return [{"created_at": "?"}]           # อ่านไม่ได้ → ถือว่ามีแล้ว ไม่สั่งซ้ำมั่ว
    keep = []
    for r in runs:
        c = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        if c >= t:
            keep.append(r)
    return keep


def refresh_status_files() -> None:
    sh(["git", "fetch", "--depth=1", "--filter=blob:none", "origin", "main"])
    sh(["git", "checkout", "FETCH_HEAD", "--", *STATUS_FILES])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minutes", type=float, default=LIFETIME_MIN)
    a = ap.parse_args()
    start = now()
    end = start + timedelta(minutes=a.minutes)
    done_slots, done_wd = set(), set()
    print(f"keeper เริ่ม {start:%H:%M}Z · ทำงานถึง {end:%H:%M}Z", flush=True)

    while True:
        t = now()
        if t >= end:
            print("ครบเวลา → ส่งต่อให้ keeper ตัวใหม่", flush=True)
            if not a.dry_run:
                sh(["gh", "workflow", "run", "keeper.yml"])
            return 0

        s = last_slot(t)
        if t >= s + timedelta(minutes=GRACE_MIN) and s not in done_slots:
            done_slots.add(s)
            runs = [] if a.dry_run else archive_runs_since(s - timedelta(minutes=2))
            if runs:
                who = runs[-1].get("triggering_actor", {}).get("login", "?") if isinstance(runs[-1], dict) else "?"
                print(f"{s:%H:%M}Z มี run แล้ว ({len(runs)} · {who})", flush=True)
            else:
                print(f"{s:%H:%M}Z ตัวหลักไม่ได้สั่ง → keeper สั่ง archive.yml แทน", flush=True)
                if not a.dry_run:
                    sh(["gh", "workflow", "run", "archive.yml"])

        w = last_mark(t, WATCHDOG_AT)
        if t >= w and w not in done_wd and (t - w) < timedelta(minutes=10):
            done_wd.add(w)
            print(f"{w:%H:%M}Z รัน watchdog", flush=True)
            if not a.dry_run:
                refresh_status_files()
                r = subprocess.run(["python3", ".github/scripts/watchdog.py", "--from-keeper"],
                                   capture_output=True, text=True)
                print((r.stdout or "")[-1500:], (r.stderr or "")[-500:], flush=True)

        time.sleep(20 if not a.dry_run else 0.01)
        if a.dry_run and (now() - start) > timedelta(seconds=2):
            print("dry-run จบ", flush=True)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
