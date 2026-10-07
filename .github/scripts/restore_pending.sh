#!/usr/bin/env bash
# ดึงภาพดิบที่รอบก่อน ๆ push ไม่ผ่าน (เก็บไว้เป็น artifact ชื่อ pending-raw-*) กลับเข้าคลัง
#
#   ใช้ใน archive.yml ก่อนขั้น "Save Raw Frame First" — ไฟล์ที่ดึงกลับจะถูก commit ไปพร้อมภาพของรอบนี้
#   ต้องมี GH_TOKEN (github.token) และ permissions: actions: write
#   ทำงานแบบไม่ทำให้ pipeline ล้ม: ผิดพลาดตรงไหนก็แค่เตือน แล้วปล่อยให้รอบถัดไปลองใหม่
#
#   ผลลัพธ์: ไฟล์ใหม่ใต้ data/raw (ข้ามถ้ามีอยู่แล้ว) + แถวใหม่ใน data/log/*.csv (ข้ามแถวที่มีแล้ว เรียงตามเวลา)
#            รายการ id ของ artifact ที่ดึงสำเร็จ → $RESTORED_IDS (ให้ขั้นหลัง push สำเร็จลบทิ้ง)
set -uo pipefail

REPO="${GITHUB_REPOSITORY:?}"
OUT="${RESTORED_IDS:-restored_ids.txt}"
WORK="$RUNNER_TEMP/pending_restore"
: > "$OUT"

ids=$(gh api "repos/$REPO/actions/artifacts?per_page=100" \
        --jq '.artifacts[] | select(.name | startswith("pending-raw-")) | select(.expired | not) | .id' 2>/dev/null) || {
  echo "::warning::อ่านรายการ artifact ไม่ได้รอบนี้ — ข้ามการดึงภาพค้าง"; exit 0; }
[ -z "$ids" ] && { echo "ไม่มีภาพค้างจากรอบก่อน"; exit 0; }

for id in $ids; do
  rm -rf "$WORK" && mkdir -p "$WORK"
  if ! gh api "repos/$REPO/actions/artifacts/$id/zip" > "$WORK/a.zip" 2>/dev/null || ! unzip -q "$WORK/a.zip" -d "$WORK/x"; then
    echo "::warning::ดาวน์โหลด artifact $id ไม่ได้ — รอบหน้าลองใหม่"; continue
  fi
  if python3 .github/scripts/restore_merge.py "$WORK/x"; then
    echo "$id" >> "$OUT"
  else
    echo "::warning::รวม artifact $id เข้าคลังไม่สำเร็จ — รอบหน้าลองใหม่"
  fi
done
echo "ดึงกลับ $(wc -l < "$OUT") artifact"
