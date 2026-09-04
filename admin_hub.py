import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
import time

# -------------------------------------------------------------
# Configuration & Setup
# -------------------------------------------------------------
st.set_page_config(page_title="TMD Radar | Admin Hub", layout="wide", page_icon="📡")

GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN", "")
REPO_OWNER = st.secrets.get("REPO_OWNER", "jamorn12")
REPO_NAME = st.secrets.get("REPO_NAME", "tmd-radar-archive")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# -------------------------------------------------------------
# API & Data Helper Functions
# -------------------------------------------------------------
def get_pipeline_status():
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/variables/PIPELINE_ENABLED"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            return r.json().get("value", "true") == "true"
    except Exception:
        pass
    return True

def set_pipeline_status(enabled: bool):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/variables/PIPELINE_ENABLED"
    val = "true" if enabled else "false"
    try:
        r = requests.patch(url, headers=HEADERS, json={"name": "PIPELINE_ENABLED", "value": val}, timeout=5)
        return r.status_code in [200, 204]
    except Exception:
        return False

def get_latest_action_run():
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs?per_page=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code == 200 and r.json().get("workflow_runs"):
            latest = r.json()["workflow_runs"][0]
            return latest.get("conclusion") or latest.get("status")
    except Exception:
        pass
    return "Unknown"

def get_repo_metrics():
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            size_kb = r.json().get("size", 0)
            return size_kb / 1024
    except Exception:
        pass
    return 0.0

@st.cache_data(ttl=30)
def load_activity_log():
    # ใส่ timestamp ป้องกัน CDN Fastly ของ GitHub แคชไฟล์เก่า
    ts = int(time.time())
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/data/log/PHS_index.csv?t={ts}"
    try:
        df = pd.read_csv(url)
        return df
    except Exception:
        return pd.DataFrame()

def calculate_next_run():
    now = datetime.now(timezone(timedelta(hours=7)))
    run_minutes = [5, 20, 35, 50]
    current_minute = now.minute
    
    next_min = next((m for m in run_minutes if m > current_minute), None)
    if next_min is not None:
        target = now.replace(minute=next_min, second=0, microsecond=0)
    else:
        target = (now + timedelta(hours=1)).replace(minute=5, second=0, microsecond=0)
        
    diff = target - now
    minutes, seconds = divmod(int(diff.total_seconds()), 60)
    return f"{minutes:02d}:{seconds:02d} นาที"

def fetch_image_bytes(clean_relative_path):
    """ดึง Content ภาพโดยตรงจาก GitHub (พร้อม Cache-Buster)"""
    if not clean_relative_path:
        return None
    p = str(clean_relative_path).strip().replace("\\", "/").lstrip("/")
    if not p.startswith("data/"):
        p = f"data/{p}"
    
    ts = int(time.time())
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{p}?t={ts}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            return res.content
    except Exception:
        pass
    return None

def fetch_solid_image(row):
    """คำนวณและดึงภาพ solid จากโฟลเดอร์ data/processed/PHS/solid/YYYY/MM/"""
    candidates = []
    
    # 1. ดึงจาก timestamp_utc
    ts_str = str(row.get("timestamp_utc", "")).strip()
    if ts_str and ts_str != "Unknown":
        try:
            dt = pd.to_datetime(ts_str)
            ym = dt.strftime("%Y/%m")
            filename = f"PHS_{dt.strftime('%Y%m%d_%H%M')}Z_solid.png"
            candidates.append(f"data/processed/PHS/solid/{ym}/{filename}")
        except Exception:
            pass

    # 2. แปลงพาธจากคอลัมน์ raw_file
    raw_val = str(row.get("raw_file", "")).strip().replace("\\", "/").lstrip("/")
    if raw_val:
        parts = raw_val.split("/")
        if len(parts) >= 4:
            year_part, month_part = parts[-3], parts[-2]
            filename_only = parts[-1].rsplit(".", 1)[0]
            sub_parts = filename_only.split("_")
            if len(sub_parts) >= 3:
                date_str = sub_parts[1]
                time_str = sub_parts[2][:4]
                candidates.append(f"data/processed/PHS/solid/{year_part}/{month_part}/PHS_{date_str}_{time_str}Z_solid.png")
            candidates.append(f"data/processed/PHS/solid/{year_part}/{month_part}/{filename_only}_solid.png")

    for cand in candidates:
        content = fetch_image_bytes(cand)
        if content:
            return content, cand

    fallback = candidates[0] if candidates else "data/processed/PHS/solid/..."
    return None, fallback

# -------------------------------------------------------------
# Fragment Setup (สำหรับ Auto-Refresh ทุกๆ 60 วินาที)
# -------------------------------------------------------------
if hasattr(st, "fragment"):
    auto_refresh_decorator = st.fragment(run_every=60)
elif hasattr(st, "experimental_fragment"):
    auto_refresh_decorator = st.experimental_fragment(run_every=60)
else:
    def auto_refresh_decorator(func):
        return func

# -------------------------------------------------------------
# Main Dashboard Function
# -------------------------------------------------------------
@auto_refresh_decorator
def render_dashboard():
    col_left, col_right = st.columns([1, 2.3], gap="large")

    # ================= 1 & 2. แผงควบคุมและข้อมูลพื้นที่ฝั่งซ้าย =================
    with col_left:
        st.subheader("1. System Control")
        current_status = get_pipeline_status()
        
        with st.container(border=True):
            is_on = st.toggle("System Power [ ON / OFF ]", value=current_status)
            
            if is_on != current_status:
                if set_pipeline_status(is_on):
                    st.toast(f"อัปเดตสถานะ: {'เปิดระบบ (ACTIVE)' if is_on else 'ปิดระบบ (PAUSED)'}")
                    st.rerun()
                else:
                    st.error("ไม่สามารถเชื่อมต่อเพื่อเปลี่ยนค่าบน GitHub ได้")

            status_color = "green" if current_status else "red"
            status_text = "ACTIVE" if current_status else "PAUSED"
            st.markdown(f"**System Status:** :{status_color}[{status_text}]")
            st.caption(f"⏱ รอบการดึงภาพถัดไป: **{calculate_next_run()}**")

        st.subheader("2. Storage Monitor")
        df_log = load_activity_log()
        total_frames = len(df_log) if not df_log.empty else 0
        repo_mb = get_repo_metrics()
        latest_run_status = get_latest_action_run()

        with st.container(border=True):
            st.metric("จำนวนเฟรมทั้งหมดในระบบ", f"{total_frames:,} frames")
            st.metric("ขนาดพื้นที่ Repo โดยประมาณ", f"{repo_mb:.2f} MB")
            
            status_badge = "✅ สำเร็จ (Success)" if latest_run_status == "success" else f"⚠️ {latest_run_status.title()}"
            st.write(f"**สถานะบอทล่าสุด:** {status_badge}")
            
            if st.button("🔄 รีเฟรชทันที (Refresh)", use_container_width=True):
                st.cache_data.clear()
                st.rerun()

    # ================= 3. ส่วนแสดงผลภาพเรดาร์ฝั่งขวา =================
    with col_right:
        st.subheader("3. Latest Frame Preview (PHS Station)")
        
        if not df_log.empty:
            latest_row = df_log.iloc[-1]
            timestamp_str = latest_row.get("timestamp_utc", "Unknown")
            raw_path = str(latest_row.get("raw_file", ""))

            # ดึงภาพดิบและภาพ Solid
            raw_img_bytes = fetch_image_bytes(raw_path)
            solid_img_bytes, matched_path = fetch_solid_image(latest_row)

            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Raw TMD Image** ({timestamp_str} UTC)")
                if raw_img_bytes:
                    st.image(raw_img_bytes, use_container_width=True)
                else:
                    st.error(f"ไม่พบไฟล์: {raw_path}")

            with c2:
                st.markdown("**Processed Solid Image** (.png)")
                if solid_img_bytes:
                    st.image(solid_img_bytes, use_container_width=True)
                else:
                    st.warning(f"ยังไม่พบไฟล์: {matched_path}")
        else:
            st.info("กำลังรอการเชื่อมต่อฐานข้อมูล...")

    # ================= 4. ตาราง Activity Log ด้านล่าง =================
    st.markdown("---")
    st.subheader("4. Activity Log")
    if not df_log.empty:
        st.dataframe(
            df_log.tail(15).iloc[::-1],
            use_container_width=True,
            hide_index=True
        )
    else:
        st.write("ยังไม่มีประวัติ Log ในระบบ")

# -------------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------------
st.title(f"{REPO_OWNER}/{REPO_NAME} | ADMIN HUB")
st.markdown("---")

render_dashboard()
