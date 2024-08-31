import os
import re
import sys
import dropbox
import requests
import hashlib
import configparser
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata

# Import the validate_and_set_directory function from utils
from utils.file_path_validate import validate_and_set_directory


class DropboxContentHasher:
    """Implements Dropbox content hashing as per the provided reference code."""

    BLOCK_SIZE = 4 * 1024 * 1024

    def __init__(self):
        self._overall_hasher = hashlib.sha256()
        self._block_hasher = hashlib.sha256()
        self._block_pos = 0

        self.digest_size = self._overall_hasher.digest_size

    def update(self, new_data):
        if self._overall_hasher is None:
            raise AssertionError(
                "can't use this object anymore; you already called digest()")

        assert isinstance(new_data, bytes), (
            "Expecting a byte string, got {!r}".format(new_data))

        new_data_pos = 0
        while new_data_pos < len(new_data):
            if self._block_pos == self.BLOCK_SIZE:
                self._overall_hasher.update(self._block_hasher.digest())
                self._block_hasher = hashlib.sha256()
                self._block_pos = 0

            space_in_block = self.BLOCK_SIZE - self._block_pos
            part = new_data[new_data_pos:(new_data_pos+space_in_block)]
            self._block_hasher.update(part)

            self._block_pos += len(part)
            new_data_pos += len(part)

    def _finish(self):
        if self._overall_hasher is None:
            raise AssertionError(
                "can't use this object anymore; you already called digest() or hexdigest()")

        if self._block_pos > 0:
            self._overall_hasher.update(self._block_hasher.digest())
            self._block_hasher = None
        h = self._overall_hasher
        self._overall_hasher = None  # Make sure we can't use this object anymore.
        return h

    def digest(self):
        return self._finish().digest()

    def hexdigest(self):
        return self._finish().hexdigest()


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

# Fetch the check_type from the settings.ini file with a fallback
check_type = config_parser.get('Settings', 'check_type', fallback='LOG').upper()

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

def calculate_local_content_hash(file_path):
    """Calculate the Dropbox content hash for a local file."""
    hasher = DropboxContentHasher()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if len(chunk) == 0:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def list_dropbox_files_with_hashes(dbx, dropbox_folder):
    """List all files in the specified Dropbox folder along with their content hashes."""
    file_metadata = {}
    try:
        result = dbx.files_list_folder(dropbox_folder, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, FileMetadata):
                    file_metadata[entry.path_lower] = entry.content_hash
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)
    except ApiError as err:
        print(f"Failed to list files in Dropbox folder {dropbox_folder}: {err}")
    return file_metadata

def upload_directory_to_dropbox(local_directory, dropbox_folder="/"):
    """Uploads all files in the specified local directory to Dropbox, replacing only changed files."""
    dbx = dropbox.Dropbox(os.getenv('DROPBOX_TOKEN'))

    # List all files currently in the Dropbox folder along with their content hashes
    dropbox_files = list_dropbox_files_with_hashes(dbx, dropbox_folder)

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

            local_content_hash = calculate_local_content_hash(file_path)

            # Check if the file exists and is the same on Dropbox
            if dropbox_path.lower() in dropbox_files and dropbox_files[dropbox_path.lower()] == local_content_hash:
                skipped_count += 1
                continue

            # Upload the file since it doesn't exist or has changed
            try:
                with open(file_path, "rb") as f:
                    file_size = os.path.getsize(file_path)
                    dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode.overwrite)
                    uploaded_count += 1
                    uploaded_size += file_size
            except ApiError as e:
                print(f"Failed to upload {file_path} to Dropbox: {e}")

    print(f"Upload completed. {uploaded_count} files uploaded ({uploaded_size / (1024 * 1024):.2f} MB).")
    print(f"{skipped_count} files were skipped (already existed or unchanged).")

def download_directory_from_dropbox(dbx, dropbox_folder, local_directory):
    """Downloads all files in the specified Dropbox folder to the local directory, replacing only changed files."""
    downloaded_count = 0
    downloaded_size = 0
    skipped_count = 0

    # List all files currently in the Dropbox folder along with their content hashes
    dropbox_files = list_dropbox_files_with_hashes(dbx, dropbox_folder)

    try:
        for dropbox_path, dropbox_hash in dropbox_files.items():
            local_path = os.path.join(local_directory, dropbox_path[len(dropbox_folder):].lstrip('/'))

            if os.path.exists(local_path):
                local_content_hash = calculate_local_content_hash(local_path)
                if local_content_hash == dropbox_hash:
                    skipped_count += 1
                    continue

            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                metadata, res = dbx.files_download(dropbox_path)
                f.write(res.content)
                downloaded_count += 1
                downloaded_size += metadata.size
    except ApiError as err:
        print(f"Failed to download files from Dropbox folder {dropbox_folder}: {err}")

    print(f"Download completed. {downloaded_count} files downloaded ({downloaded_size / (1024 * 1024):.2f} MB).")
    print(f"{skipped_count} files were skipped (already existed or unchanged).")

def download_log_file_from_dropbox(dbx, dropbox_folder, local_directory):
    """Download only the log file from Dropbox."""
    log_file_path = os.path.join(local_directory, 'file_log.json')

    try:
        # Download the log file
        metadata, res = dbx.files_download(f"{dropbox_folder}/file_log.json")
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, "wb") as f:
            f.write(res.content)
        print(f"Log file downloaded successfully to {log_file_path}.")
    except ApiError as err:
        print(f"Failed to download the log file from Dropbox: {err}")

if __name__ == "__main__":
    # Refresh the access token because it expires
    refresh_dropbox_token()
    dbx = dropbox.Dropbox(os.getenv('DROPBOX_TOKEN'))

    if '--download' in sys.argv:
        if check_type == 'LOG':
            print("Downloading only the log file as check_type is LOG.")
            download_log_file_from_dropbox(dbx, dropbox_folder, local_dir)
        elif check_type == 'DIR':
            print("Downloading the entire directory as check_type is DIR.")
            download_directory_from_dropbox(dbx, dropbox_folder, local_dir)
        else:
            raise ValueError(f"Unknown check_type: {check_type}")
    elif '--upload' in sys.argv:
        upload_directory_to_dropbox(local_dir, dropbox_folder)