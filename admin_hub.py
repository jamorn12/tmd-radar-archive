import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

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
# API Helper Functions
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

@st.cache_data(ttl=60)
def load_activity_log():
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/data/log/PHS_index.csv"
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

def build_raw_github_url(file_path, default_subfolder="raw"):
    if not file_path or pd.isna(file_path):
        return None
    file_str = str(file_path).strip().replace("\\", "/")
    
    if file_str.startswith("http"):
        return file_str
    if file_str.startswith("data/"):
        return f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{file_str}"
    
    # กรณีเป็น path ย่อยหรือเฉพาะชื่อไฟล์
    return f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/data/{default_subfolder}/PHS/{file_str}"

# -------------------------------------------------------------
# Dashboard UI
# -------------------------------------------------------------
st.title(f"{REPO_OWNER}/{REPO_NAME} | ADMIN HUB")
st.markdown("---")

col_left, col_right = st.columns([1, 2.3], gap="large")

# ================= แผงควบคุมฝั่งซ้าย =================
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

# ================= ส่วนแสดงผลภาพเรดาร์ฝั่งขวา =================
with col_right:
    st.subheader("3. Latest Frame Preview (PHS Station)")
    
    if not df_log.empty:
        latest_row = df_log.iloc[-1]
        timestamp_str = latest_row.get("timestamp_utc", "Unknown")

        # ค้นหาคอลัมน์ชื่อไฟล์ Raw อัตโนมัติ
        raw_col = next((c for c in ["raw_file", "raw_filename", "raw_path"] if c in df_log.columns), None)
        raw_val = latest_row.get(raw_col, "") if raw_col else ""
        raw_url = build_raw_github_url(raw_val, "raw")

        # ค้นหาคอลัมน์ชื่อไฟล์ Processed / Alpha อัตโนมัติ
        proc_col = next((c for c in ["alpha_file", "processed_file", "proc_file", "solid_file"] if c in df_log.columns), None)
        proc_val = latest_row.get(proc_col, "") if proc_col else ""
        proc_url = build_raw_github_url(proc_val, "processed")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Raw TMD Image** ({timestamp_str} UTC)")
            if raw_url:
                st.image(raw_url, use_container_width=True)
            else:
                st.info("ไม่พบคอลัมน์ raw_file")

        with c2:
            st.markdown("**Processed Alpha Mask** (Transparent)")
            if proc_url:
                st.image(proc_url, use_container_width=True)
            else:
                st.info("ไม่พบคอลัมน์ alpha_file")
    else:
        st.info("กำลังรอการเชื่อมต่อฐานข้อมูล...")

# ================= ตาราง Activity Log ด้านล่าง =================
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
