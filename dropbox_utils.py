import os
import re
import sys
import dropbox
import requests
import configparser
# Import the validate_and_set_directory function from utils
from utils.file_path_validate import validate_and_set_directory

def refresh_dropbox_token():
    refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')
    client_id = os.getenv('DROPBOX_APP_KEY')
    client_secret = os.getenv('DROPBOX_APP_SECRET')

    response = requests.post('https://api.dropboxapi.com/oauth2/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret,
    })

    if response.status_code == 200:
        new_access_token = response.json().get('access_token')
        os.environ['DROPBOX_TOKEN'] = new_access_token
        print(" -- Access Token Refreshed -- ")
        return new_access_token
    else:
        raise Exception("Failed to refresh Dropbox token")

# Load configuration
config_parser = configparser.ConfigParser()
config_parser.read('settings.ini')

# Local directory for Reddit data
# Fetch the local_dir from the settings.ini file with a fallback
local_dir = config_parser.get('Settings', 'save_directory', fallback='reddit/')

# Validate and set the local directory using the utility function
local_dir = validate_and_set_directory(local_dir)

# Fetch the dropbox_folder from the settings.ini file with a fallback
dropbox_folder = config_parser.get('Settings', 'dropbox_directory', fallback='/reddit')

def sanitize_filename(filename):
    """Sanitize the filename to be Dropbox-compatible."""
    sanitized_name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', filename)  # Also remove control characters
    sanitized_name = sanitized_name.strip()  # Remove leading and trailing spaces
    reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM2", "LPT2", "COM3", "LPT3",
                      "COM4", "LPT4", "COM5", "LPT5", "COM6", "LPT6", "COM7", "LPT7", "COM8", "LPT8",
                      "COM9", "LPT9"}
    if sanitized_name.upper() in reserved_names:
        sanitized_name = "_" + sanitized_name  # Prefix with underscore to avoid reserved names
    
    return sanitized_name

def list_dropbox_files(dbx, dropbox_folder):
    """List all files in the specified Dropbox folder."""
    file_names = set()
    try:
        result = dbx.files_list_folder(dropbox_folder, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    file_names.add(entry.path_lower)
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)
    except dropbox.exceptions.ApiError as err:
        print(f"Failed to list files in Dropbox folder {dropbox_folder}: {err}")
    return file_names

def upload_directory_to_dropbox(local_directory, dropbox_folder="/"):
    """Uploads all files in the specified local directory to Dropbox without overwriting."""
    dbx = dropbox.Dropbox(os.getenv('DROPBOX_TOKEN'))

    # List all files currently in the Dropbox folder
    existing_files = list_dropbox_files(dbx, dropbox_folder)

    uploaded_count = 0
    uploaded_size = 0
    skipped_count = 0

    for root, dirs, files in os.walk(local_directory):
        for file_name in files:
            # Skip .DS_Store and other hidden files
            if file_name.startswith('.'):
                continue
            
            sanitized_name = sanitize_filename(file_name)
            file_path = os.path.join(root, file_name)
            dropbox_path = f"{dropbox_folder}/{os.path.relpath(file_path, local_directory).replace(os.path.sep, '/')}"

            # Adjust for sanitized name
            dropbox_path = dropbox_path.replace(file_name, sanitized_name)

            if dropbox_path.lower() in existing_files:
                skipped_count += 1
                continue

            try:
                with open(file_path, "rb") as f:
                    file_size = os.path.getsize(file_path)
                    dbx.files_upload(f.read(), dropbox_path)
                    uploaded_count += 1
                    uploaded_size += file_size
            except dropbox.exceptions.ApiError as e:
                print(f"Failed to upload {file_path} to Dropbox: {e}")

    print(f"Upload completed. {uploaded_count} files uploaded ({uploaded_size / (1024 * 1024):.2f} MB).")
    print(f"{skipped_count} files were skipped (already existed).")

def download_directory_from_dropbox(dbx, dropbox_folder, local_directory):
    """Downloads all files in the specified Dropbox folder to the local directory."""
    downloaded_count = 0
    downloaded_size = 0
    skipped_count = 0

    try:
        result = dbx.files_list_folder(dropbox_folder, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    local_path = os.path.join(local_directory, entry.path_lower[len(dropbox_folder):].lstrip('/'))

                    # Skip the download if the file already exists locally
                    if os.path.exists(local_path):
                        skipped_count += 1
                        continue
                    
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        metadata, res = dbx.files_download(entry.path_lower)
                        f.write(res.content)
                        downloaded_count += 1
                        downloaded_size += metadata.size
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)
    except dropbox.exceptions.ApiError as err:
        print(f"Failed to download files from Dropbox folder {dropbox_folder}: {err}")

    print(f"Download completed. {downloaded_count} files downloaded ({downloaded_size / (1024 * 1024):.2f} MB).")
    print(f"{skipped_count} files were skipped (i.e. they already existed).")

if __name__ == "__main__":
    # Refresh the access token because it expires
    refresh_dropbox_token()
    dbx = dropbox.Dropbox(os.getenv('DROPBOX_TOKEN'))

    if '--download' in sys.argv:
        download_directory_from_dropbox(dbx, dropbox_folder, local_dir)
    elif '--upload' in sys.argv:
        upload_directory_to_dropbox(local_dir, dropbox_folder)