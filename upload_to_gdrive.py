import json
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("GDRIVE_SA_KEY")

def get_drive_service():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_or_create_subfolder(service, parent_id, folder_name):
    query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def upload_file_if_not_exists(service, folder_id, file_path):
    filename = os.path.basename(file_path)
    query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    if results.get("files"):
        return

    media = MediaFileUpload(file_path, resumable=True)
    metadata = {"name": filename, "parents": [folder_id]}
    service.files().create(body=metadata, media_body=media).execute()
    print(f">>> [Drive 5TB] Uploaded: {filename}")

def main():
    if not FOLDER_ID or not SERVICE_ACCOUNT_JSON:
        print("[Skip] Missing GDRIVE_FOLDER_ID or GDRIVE_SA_KEY")
        return

    service = get_drive_service()
    data_dir = "data"

    for root, _, files in os.walk(data_dir):
        for file in files:
            if not (file.endswith(".jpg") or file.endswith(".png") or file.endswith(".csv")):
                continue

            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(root, data_dir)

            target_folder_id = FOLDER_ID
            if rel_path != ".":
                parts = rel_path.split(os.sep)
                for part in parts:
                    target_folder_id = get_or_create_subfolder(service, target_folder_id, part)

            upload_file_if_not_exists(service, target_folder_id, full_path)

if __name__ == "__main__":
    main()
