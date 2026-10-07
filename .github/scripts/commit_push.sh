#!/usr/bin/env bash
# commit แล้ว push แบบทนต่อ 3 ปัญหาที่เคยทำให้ระบบหยุด
#
#   ใช้:  bash .github/scripts/commit_push.sh "<ข้อความ commit>" <path> [<path> ...]
#
#   1. ไฟล์ใหญ่เกินเพดาน GitHub (100 MB)  — 25 ก.ย. 2569 ไฟล์ตำบลชนเพดาน push ไม่ผ่านทุกรอบ 3.5 ชม.
#      ไฟล์ที่ ≥ HARD_MB ถูกเอาออกจาก commit รอบนั้น (ที่เหลือยังขึ้นได้) + ::error:: ให้ watchdog เห็น
#      ไฟล์ที่ ≥ WARN_MB แจ้ง ::warning:: ล่วงหน้า
#   2. รันซ้อนกันแล้ว rebase ชน  — CSV ที่เขียนต่อท้าย: เก็บบรรทัดทั้งสองฝั่ง (merge=union ใน .gitattributes)
#                                  ไฟล์สถานะ docs/*.json: ใช้ -X theirs (ถือไฟล์ของรอบนี้ ซึ่งใหม่กว่า)
#   3. rebase ค้างจนลองซ้ำไม่ได้  — rebase --abort ก่อนลองรอบถัดไป
#   4. GitHub ขัดข้องชั่วคราว  — 7 ต.ค. 2569 22:05 น. push ไม่ผ่าน 4 ครั้งใน ~100 วินาที (GitHub incident
#      Git Operations 15:14–16:25 UTC) ภาพ 22:00 น. หายถาวร → ลองนานขึ้น (ค่าเริ่มต้น 8 ครั้ง ~5.5 นาที)
#      และถ้าตั้ง PENDING_DIR ไว้ เมื่อ push ไม่ผ่านจริง ๆ จะคัดไฟล์ใหม่ใต้ data/raw + แถวใหม่ของ data/log/*.csv
#      ไปไว้ใน PENDING_DIR ให้ workflow เก็บเป็น artifact → รอบถัดไปดึงกลับเข้าคลัง (restore_pending.sh)
#
# path ที่ไม่มีอยู่จริงถูกข้าม (ขั้นก่อนหน้าที่เป็น continue-on-error อาจไม่ได้สร้าง)
# ไม่มีอะไรเปลี่ยน = จบแบบสำเร็จ (exit 0) · มีไฟล์ถูกงดเพราะใหญ่เกิน = exit 3 (หลัง push ส่วนที่เหลือแล้ว)
set -uo pipefail

MSG="$1"; shift
WARN_MB="${WARN_MB:-80}"
HARD_MB="${HARD_MB:-95}"
TRIES="${TRIES:-8}"

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

paths=()
for p in "$@"; do
  [ -e "$p" ] && paths+=("$p")
done
if [ ${#paths[@]} -eq 0 ]; then
  echo "ไม่มี path ให้ commit ($MSG)"
  exit 0
fi

git add -A -- "${paths[@]}"

# ── ตรวจขนาดไฟล์ที่กำลังจะ commit
EXCLUDED=0
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  sz=$(stat -c %s "$f")
  mb=$(( sz / 1048576 ))
  if [ "$mb" -ge "$HARD_MB" ]; then
    echo "::error title=ไฟล์ใหญ่เกินเพดาน::$f ขนาด ${mb} MB (เพดาน GitHub 100 MB) — ไม่ commit ไฟล์นี้รอบนี้ ข้อมูลอื่นยังขึ้นตามปกติ ต้องแยก/บีบอัดไฟล์นี้"
    git reset -q -- "$f"
    EXCLUDED=$((EXCLUDED + 1))
  elif [ "$mb" -ge "$WARN_MB" ]; then
    echo "::warning title=ไฟล์ใกล้เพดาน::$f ขนาด ${mb} MB — ถึง ${HARD_MB} MB จะถูกงด commit"
  fi
done < <(git diff --cached --name-only -z --diff-filter=AM)

# มีไฟล์ถูกงด = ต้องมีคนมาแก้ → จบด้วย exit 3 หลัง push ส่วนที่เหลือเสร็จ
# run จะขึ้นแดง watchdog นับเป็น run ล้มและเปิด Issue ให้
finish() { if [ "$EXCLUDED" -gt 0 ]; then echo "::error::งด commit $EXCLUDED ไฟล์เพราะใหญ่เกินเพดาน"; exit 3; fi; exit 0; }

if git diff --cached --quiet; then
  echo "ไม่มีอะไรใหม่ให้ commit ($MSG)"
  finish
fi

git commit -q -m "$MSG"
echo "commit: $(git log -1 --format='%h') · $MSG"

for i in $(seq 1 "$TRIES"); do
  if git pull -q --rebase --autostash -X theirs origin main; then
    if git push -q origin HEAD:main; then
      echo "push สำเร็จ (ครั้งที่ $i)"
      finish
    fi
  else
    git rebase --abort 2>/dev/null || true
  fi
  [ "$i" -eq "$TRIES" ] && break
  wait_s=$(( i * 15 )); [ "$wait_s" -gt 60 ] && wait_s=60       # 15 30 45 60 60 60 60 → รวม ~5.5 นาที
  echo "push ครั้งที่ $i ไม่สำเร็จ รอ ${wait_s} วินาที"
  sleep "$wait_s"
done

echo "::error title=push ไม่สำเร็จ::ลอง $TRIES ครั้งแล้วไม่ผ่าน ($MSG)"

# ── เก็บของที่หายถาวรไม่ได้ไว้นอก git (ภาพดิบใหม่ + แถว log ใหม่) ให้ workflow อัปโหลดเป็น artifact
if [ -n "${PENDING_DIR:-}" ] && git rev-parse -q --verify HEAD~1 >/dev/null; then
  mkdir -p "$PENDING_DIR/files" "$PENDING_DIR/rows"
  n=0
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    mkdir -p "$PENDING_DIR/files/$(dirname "$f")"; cp -p "$f" "$PENDING_DIR/files/$f"; n=$((n + 1))
  done < <(git diff --name-only -z --diff-filter=A HEAD~1 HEAD -- data/raw)
  while IFS= read -r -d '' f; do
    mkdir -p "$PENDING_DIR/rows/$(dirname "$f")"
    git diff -U0 HEAD~1 HEAD -- "$f" | grep -a '^+' | grep -av '^+++' | cut -c2- | tr -d '\r' > "$PENDING_DIR/rows/$f"
  done < <(git diff --name-only -z HEAD~1 HEAD -- 'data/log/*.csv')
  echo "::warning title=เก็บภาพไว้รอ push::คัดภาพดิบ $n ไฟล์ + แถว log ไว้ที่ $PENDING_DIR — รอบถัดไปจะดึงกลับเข้าคลัง"
fi
exit 1
