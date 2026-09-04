import streamlit as st
import requests
import json
import time

# -------------------------------------------------------------
# Configuration
# -------------------------------------------------------------
st.set_page_config(
    page_title="PHS Radar Nowcast | พยากรณ์ฝนเรดาร์พิษณุโลก",
    page_icon="🌧️",
    layout="wide"
)

REPO_OWNER = "jamorn12"
REPO_NAME = "tmd-radar-archive"

# -------------------------------------------------------------
# Data Loading Functions
# -------------------------------------------------------------
@st.cache_data(ttl=60)
def load_nowcast_data():
    ts = int(time.time())
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/data/nowcast/latest.json?t={ts}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_frame_image_url(relative_url):
    ts = int(time.time())
    clean_url = str(relative_url).strip().lstrip("/")
    return f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/data/nowcast/{clean_url}?t={ts}"

# -------------------------------------------------------------
# UI Layout
# -------------------------------------------------------------
st.title("🌧️ PHS Radar Nowcast | เรดาร์พิษณุโลก")
st.caption("ระบบพยากรณ์การเคลื่อนตัวของกลุ่มฝนระยะสั้น 0–60 นาที (Extrapolation Nowcast)")

data = load_nowcast_data()

if not data or "frames" not in data or len(data["frames"]) == 0:
    st.info("⏳ ระบบกำลังรอรอบการประมวลผล Nowcast รอบแรกจาก GitHub Actions (จะพร้อมใช้งานหลังจากบอทรอบถัดไปทำงานเสร็จ)")
    st.stop()

# ดึงข้อมูลภาพรวม
base_time = data.get("base_time_local", "N/A")
motion = data.get("motion", {})
motion_speed = motion.get("speed_kmh", 0.0)
motion_dir = motion.get("direction_deg", 0.0)
frames = data.get("frames", [])

# แถบแสดงสรุปข้อมูลสภาพอากาศด้านบน
m1, m2, m3, m4 = st.columns(4)
m1.metric("เวลาฐานข้อมูลล่าสุด (Local)", f"{base_time}")
m2.metric("ความเร็วการเคลื่อนที่", f"{motion_speed:.1f} กม./ชม.")
m3.metric("ทิศทางการเคลื่อนที่", f"{motion_dir:.0f}°")
m4.metric("จำนวนเฟรมทั้งหมด", f"{len(frames)} เฟรม")

st.markdown("---")

# -------------------------------------------------------------
# ตัวควบคุมการเล่นและเลือกเวลา
# -------------------------------------------------------------
frame_labels = []
for f in frames:
    offset = f.get("offset_min", 0)
    kind = "พยากรณ์" if f.get("kind") == "nowcast" else "ตรวจวัดจริง"
    sign = "+" if offset > 0 else ""
    frame_labels.append(f"{sign}{offset} นาที ({kind})")

selected_idx = st.select_slider(
    "⏱ เลื่อนดูเวลา (อดีต ➔ ปัจจุบัน ➔ พยากรณ์ล่วงหน้า)",
    options=list(range(len(frames))),
    format_func=lambda x: frame_labels[x],
    value=min(len(frames) - 1, 3) # ค่าเริ่มต้นมักอยู่ที่เฟรมปัจจุบัน (0 min)
)

cur_frame = frames[selected_idx]
is_forecast = cur_frame.get("kind") == "nowcast"
offset_min = cur_frame.get("offset_min", 0)
max_dbz = cur_frame.get("max_dbz", "-")
wet_pct = cur_frame.get("wet_pct", "-")

# แสดงสถานะเฟรมที่เลือก
badge_color = "orange" if is_forecast else "green"
kind_text = "FORECAST (พยากรณ์ล่วงหน้า)" if is_forecast else "OBSERVED (ภาพตรวจวัดจริง)"
st.markdown(f"### สถานะเฟรม: :{badge_color}[{kind_text}] | เวลา: **{frame_labels[selected_idx]}**")

c_img, c_stat = st.columns([2.5, 1], gap="medium")

with c_img:
    img_url = get_frame_image_url(cur_frame.get("url", ""))
    st.image(img_url, use_container_width=True)

with c_stat:
    with st.container(border=True):
        st.subheader("สถิติเฟรมนี้")
        st.write(f"**ความแรงสูงสุด:** `{max_dbz}` dBZ")
        st.write(f"**พื้นที่ฝนตก (Wet Area):** `{wet_pct}` %")
        st.write(f"**Lead Time:** `{offset_min}` นาที")
        
        st.markdown("---")
        st.caption("ℹ️ **คำแนะนำการอ่านเรดาร์:**")
        st.caption("- สีเขียว: ฝนตกเบาถึงปานกลาง (20-35 dBZ)")
        st.caption("- สีเหลือง/ส้ม: ฝนตกหนัก (35-45 dBZ)")
        st.caption("- สีแดง/ชมพู: ฝนหนักมาก / เสี่ยงลูกเห็บ (>45 dBZ)")

# ปุ่มรีเฟรชข้อมูล
if st.button("🔄 โหลดข้อมูลล่าสุด"):
    st.cache_data.clear()
    st.rerun()
