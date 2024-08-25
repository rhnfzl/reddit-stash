import os
import re
import sys
import dropbox

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
                continue

            try:
                with open(file_path, "rb") as f:
                    dbx.files_upload(f.read(), dropbox_path)
            except dropbox.exceptions.ApiError as e:
                print(f"Failed to upload {file_path} to Dropbox: {e}")

def download_directory_from_dropbox(dbx, dropbox_folder, local_directory):
    """Downloads all files in the specified Dropbox folder to the local directory."""
    try:
        result = dbx.files_list_folder(dropbox_folder, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    local_path = os.path.join(local_directory, entry.path_lower[len(dropbox_folder):].lstrip('/'))
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        metadata, res = dbx.files_download(entry.path_lower)
                        f.write(res.content)
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)
    except dropbox.exceptions.ApiError as err:
        print(f"Failed to download files from Dropbox folder {dropbox_folder}: {err}")

if __name__ == "__main__":
    dbx = dropbox.Dropbox(os.getenv('DROPBOX_TOKEN'))
    local_dir = 'reddit/'  # Local directory for Reddit data
    dropbox_folder = "/reddit"  # Dropbox folder where Reddit data is stored

    if '--download' in sys.argv:
        download_directory_from_dropbox(dbx, dropbox_folder, local_dir)
    elif '--upload' in sys.argv:
        upload_directory_to_dropbox(local_dir, dropbox_folder)