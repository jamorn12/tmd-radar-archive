"""watchdog.py — ตรวจสุขภาพระบบทันฝน แจ้งเตือนผ่าน GitHub Issue และสั่งรันซ่อมเองเมื่อทำได้

รันโดย .github/workflows/watchdog.yml ทุก 30 นาที (หรือกดรันเอง)

ตรวจ 6 อย่าง
    1. ภาพเรดาร์ล่าสุดในคลัง (data/log/PHS_index.csv)     เก่ากว่า FRAME_MAX_MIN  → ปัญหา
    2. ภาพพยากรณ์บนเว็บ (docs/nowcast/PHS/latest.json)       เก่ากว่า NOWCAST_MAX_MIN → ปัญหา
    3. ข้อมูลสถานีวัดน้ำฝน (docs/gauges/PHS.json)           เก่ากว่า GAUGE_MAX_MIN  → เตือน
    4. run ของ archive.yml ล้มติดกัน                         ≥ FAIL_STREAK รอบ      → ปัญหา
    5. ไม่มี run ของ archive.yml เริ่มเลยใน SILENT_MAX_MIN   → ตัวตั้งเวลาเงียบ → สั่งรันเอง
    6. ไฟล์ใน repo ใหญ่ใกล้เพดาน 100 MB                     ≥ 80 MB เตือน · ≥ 95 MB ปัญหา

การแจ้งเตือน (label: watchdog)
    มีปัญหา + ยังไม่มี Issue เปิดอยู่  → เปิด Issue ใหม่ + mention เจ้าของ repo (GitHub ส่งอีเมล/แอปแจ้ง)
    มีปัญหา + มี Issue เปิดอยู่       → แก้เนื้อหา Issue ให้เป็นสถานะล่าสุด
                                         ถ้าชุดปัญหาเปลี่ยน → comment เพิ่ม (แจ้งอีกครั้ง)
    ปกติ + มี Issue เปิดอยู่          → comment "กลับมาปกติ" แล้วปิด Issue
    PIPELINE_ENABLED = false            → ไม่แจ้ง (ปิดระบบโดยตั้งใจจาก Admin Hub)

การซ่อมเอง
    ตัวตั้งเวลาภายนอกเงียบ (ข้อ 5) → gh workflow run archive.yml
    ภาพเก่าแต่ run ล่าสุดสำเร็จ (ข้อ 1 + run ok) → น่าจะเป็นฝั่ง TMD ไม่อัปเดตภาพ ระบบเราแก้ไม่ได้ แจ้งอย่างเดียว

ใช้: python .github/scripts/watchdog.py [--dry-run]
    --dry-run  ไม่เรียก gh (ไม่เปิด Issue ไม่สั่งรัน) — พิมพ์รายงานอย่างเดียว ใช้ทดสอบในเครื่อง
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

STATION = "PHS"
FRAME_MAX_MIN = 45          # TMD สแกนทุก 15 นาที ขาด 3 รอบติด = ผิดปกติ
NOWCAST_MAX_MIN = 60
GAUGE_MAX_MIN = 150         # สสน. รายชั่วโมง + เผื่อความล่าช้าของแหล่งข้อมูล
FAIL_STREAK = 2
SILENT_MAX_MIN = 35         # ตัวตั้งเวลายิงทุก 15 นาที + cron สำรอง เงียบเกิน 35 นาที = ผิดปกติ
WARN_MB, HARD_MB = 80, 95
LABEL = "watchdog"
# ไฟล์ที่ปิดแล้ว (ไม่ถูกเขียนต่อ) แม้ใหญ่ใกล้เพดานก็ไม่เป็นปัญหา — ไม่ต้องแจ้ง
FROZEN = {
    "data/areal/PHS_tambon_forecast.csv",      # 17–25 ก.ย. 2569 · หลังจากนั้นแยกไฟล์รายวัน
    "data/areal/PHS_amphoe_forecast.csv",
    "data/areal/PHS_province_forecast.csv",
}
TH = timezone(timedelta(hours=7))
NOW = datetime.now(timezone.utc)


def sh(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr}")
    return r.stdout


def th(t: datetime | None) -> str:
    return "-" if t is None else t.astimezone(TH).strftime("%d/%m %H:%M น.")


def age_min(t: datetime | None) -> float | None:
    return None if t is None else (NOW - t).total_seconds() / 60


# ─────────────────────────────────────────────────────────── อ่านสถานะ
def last_frame() -> datetime | None:
    p = f"data/log/{STATION}_index.csv"
    if not os.path.exists(p):
        return None
    last = None
    with open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last = row
    if not last:
        return None
    return datetime.strptime(last["timestamp_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def nowcast_time() -> datetime | None:
    p = f"docs/nowcast/{STATION}/latest.json"
    if not os.path.exists(p):
        return None
    m = json.load(open(p, encoding="utf-8"))
    return datetime.fromisoformat(m["base_time_utc"].replace("Z", "+00:00"))


def gauge_time() -> datetime | None:
    p = f"docs/gauges/{STATION}.json"
    if not os.path.exists(p):
        return None
    m = json.load(open(p, encoding="utf-8"))
    g = m.get("generated")
    return datetime.fromtimestamp(g, timezone.utc) if g else None


def archive_runs(dry: bool) -> list[dict]:
    if dry:
        return []
    out = sh(["gh", "run", "list", "--workflow", "archive.yml", "-L", "8",
              "--json", "databaseId,status,conclusion,createdAt,event,url,displayTitle"])
    runs = json.loads(out)
    for r in runs:
        r["createdAt"] = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
    return runs


def big_files(dry: bool) -> list[tuple[str, float]]:
    """ไฟล์ที่ใหญ่ ≥ WARN_MB ใน main — อ่านจาก git tree API ไม่ต้องโหลดไฟล์ทั้ง repo"""
    if dry:
        out = sh(["git", "ls-tree", "-r", "-l", "HEAD"])
        rows = [ln.split(None, 4) for ln in out.splitlines()]
        items = [(r[4], int(r[3])) for r in rows if r[1] == "blob" and r[3].isdigit()]
    else:
        repo = os.environ["GITHUB_REPOSITORY"]
        t = json.loads(sh(["gh", "api", f"repos/{repo}/git/trees/main?recursive=1"]))
        items = [(x["path"], x.get("size", 0)) for x in t.get("tree", []) if x.get("type") == "blob"]
    return sorted([(p, s / 1048576) for p, s in items
                   if s / 1048576 >= WARN_MB and p not in FROZEN], key=lambda x: -x[1])


# ─────────────────────────────────────────────────────────── ประเมิน
def assess(dry: bool) -> dict:
    fr, nc, gg = last_frame(), nowcast_time(), gauge_time()
    runs = archive_runs(dry)
    big = big_files(dry)
    problems, warnings, actions = [], [], []

    # 4 · run ล้มติดกัน (นับเฉพาะรอบที่จบแล้ว)
    done = [r for r in runs if r["status"] == "completed"]
    streak = 0
    for r in done:
        if r["conclusion"] == "failure":
            streak += 1
        else:
            break
    last_ok = next((r for r in done if r["conclusion"] == "success"), None)
    if streak >= FAIL_STREAK:
        problems.append(("RUN_FAIL", f"archive.yml ล้มติดกัน {streak} รอบ — ล่าสุด {done[0]['url']}"))

    # 1 · ภาพเรดาร์
    a = age_min(fr)
    if a is None or a > FRAME_MAX_MIN:
        hint = ""
        if done and done[0]["conclusion"] == "success":
            hint = " · run ล่าสุดสำเร็จ แต่ไม่มีภาพใหม่ → น่าจะเป็นฝั่ง TMD ไม่อัปเดตภาพ (ระบบเราแก้ไม่ได้)"
        problems.append(("FRAME_STALE", f"ภาพเรดาร์ล่าสุด {th(fr)} (เก่า {a or 0:.0f} นาที){hint}"))

    # 2 · ภาพพยากรณ์
    b = age_min(nc)
    if b is None or b > NOWCAST_MAX_MIN:
        hint = ""
        if a is not None and a <= FRAME_MAX_MIN:
            hint = " · ภาพสังเกตมาแล้ว แต่ nowcast ยังไม่ขยับ → ตรวจ log ขั้น Nowcast (หลังภาพขาดช่วง ต้องมีภาพต่อเนื่อง 2 เฟรมก่อน)"
        problems.append(("NOWCAST_STALE", f"ภาพพยากรณ์บนเว็บ {th(nc)} (เก่า {b or 0:.0f} นาที){hint}"))

    # 3 · สถานี
    c = age_min(gg)
    if c is None or c > GAUGE_MAX_MIN:
        warnings.append(("GAUGE_STALE", f"ข้อมูลสถานีวัดน้ำฝน {th(gg)} (เก่า {c or 0:.0f} นาที) — แหล่งภายนอก (สสน.) ไม่กระทบเรดาร์"))

    # 5 · ตัวตั้งเวลาเงียบ → สั่งรันเอง
    if runs:
        silent = age_min(runs[0]["createdAt"])
        if silent > SILENT_MAX_MIN:
            problems.append(("SCHEDULER_SILENT", f"ไม่มี run ของ archive.yml เริ่มเลยใน {silent:.0f} นาที — ตัวตั้งเวลาภายนอกอาจหยุด"))
            actions.append("dispatch")

    # 6 · ไฟล์ใหญ่
    for p, mb in big:
        (problems if mb >= HARD_MB else warnings).append(
            ("BIG_FILE" if mb >= HARD_MB else "BIG_FILE_WARN",
             f"`{p}` ขนาด {mb:.1f} MB (เพดาน GitHub 100 MB)"))

    return dict(frame=fr, nowcast=nc, gauge=gg, runs=runs, last_ok=last_ok, streak=streak,
                problems=problems, warnings=warnings, actions=actions)


def report(s: dict) -> str:
    ok = "✅" if not s["problems"] else "⚠️"
    L = [f"{ok} ตรวจเมื่อ {th(NOW)}", "",
         "| รายการ | ล่าสุด | อายุ |", "|---|---|---|",
         f"| ภาพเรดาร์ในคลัง | {th(s['frame'])} | {age_min(s['frame']) or 0:.0f} นาที |",
         f"| ภาพพยากรณ์บนเว็บ | {th(s['nowcast'])} | {age_min(s['nowcast']) or 0:.0f} นาที |",
         f"| สถานีวัดน้ำฝน | {th(s['gauge'])} | {age_min(s['gauge']) or 0:.0f} นาที |"]
    if s["runs"]:
        r0 = s["runs"][0]
        L.append(f"| run ล่าสุดของ archive.yml | {th(r0['createdAt'])} | {r0['status']} / {r0['conclusion'] or '-'} |")
    if s["last_ok"]:
        L.append(f"| run สำเร็จล่าสุด | {th(s['last_ok']['createdAt'])} | [เปิด]({s['last_ok']['url']}) |")
    if s["problems"]:
        L += ["", "### ปัญหา"] + [f"- **{k}** · {m}" for k, m in s["problems"]]
    if s["warnings"]:
        L += ["", "### เตือน"] + [f"- {k} · {m}" for k, m in s["warnings"]]
    if s["actions"]:
        L += ["", "### ระบบทำเองแล้ว"] + [f"- สั่งรัน archive.yml ใหม่ ({a})" for a in s["actions"]]
    return "\n".join(L)


# ─────────────────────────────────────────────────────────── แจ้งเตือน / ซ่อม
def open_issue() -> dict | None:
    out = sh(["gh", "issue", "list", "--label", LABEL, "--state", "open", "-L", "1",
              "--json", "number,body"])
    xs = json.loads(out)
    return xs[0] if xs else None


def notify(s: dict, body: str) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    key = ",".join(sorted(k for k, _ in s["problems"]))
    marker = f"<!-- watchdog-key: {key} -->"
    sh(["gh", "label", "create", LABEL, "--color", "D93F0B",
        "--description", "แจ้งเตือนอัตโนมัติจาก watchdog.yml", "--force"], check=False)
    iss = open_issue()
    if s["problems"]:
        full = f"{marker}\n{body}\n\n@{owner}"
        title = "⚠️ ระบบทันฝนผิดปกติ: " + " · ".join(k for k, _ in s["problems"])
        if iss is None:
            sh(["gh", "issue", "create", "--title", title, "--label", LABEL, "--body", full])
            print("เปิด Issue ใหม่")
        else:
            old_key = (iss["body"] or "").split("watchdog-key:")[-1].split("-->")[0].strip() \
                if "watchdog-key:" in (iss["body"] or "") else ""
            sh(["gh", "issue", "edit", str(iss["number"]), "--title", title, "--body", full])
            if old_key != key:
                sh(["gh", "issue", "comment", str(iss["number"]), "--body",
                    f"ชุดปัญหาเปลี่ยน: `{old_key or '-'}` → `{key}`\n\n{body}\n\n@{owner}"])
            print(f"อัปเดต Issue #{iss['number']}")
    elif iss is not None:
        sh(["gh", "issue", "comment", str(iss["number"]), "--body", f"กลับมาปกติแล้ว\n\n{body}"])
        sh(["gh", "issue", "close", str(iss["number"])])
        print(f"ปิด Issue #{iss['number']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    s = assess(a.dry_run)
    body = report(s)
    print(body)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("## Watchdog\n\n" + body + "\n")

    if a.dry_run:
        return 0
    if os.environ.get("PIPELINE_ENABLED", "") == "false":
        print("PIPELINE_ENABLED = false — ปิดระบบโดยตั้งใจ ไม่แจ้งเตือน/ไม่สั่งรัน")
        return 0
    if "dispatch" in s["actions"]:
        sh(["gh", "workflow", "run", "archive.yml"], check=False)
        print("สั่งรัน archive.yml แล้ว")
    notify(s, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
