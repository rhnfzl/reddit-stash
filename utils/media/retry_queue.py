"""
Imgur Retry Queue Module

This module handles the Imgur retry queue functionality, which was previously part of
the monolithic media_utils.py file. It provides functionality to:
1. Load and save the Imgur retry queue from/to a JSON file
2. Add failed Imgur downloads to the retry queue
3. Process items in the retry queue that are due for retry
4. Update the save directory for all items in the queue

REFERENCE NOTES:
---------------
1. Original implementation: See utils/media_utils.py.bak (lines ~1800-2326)
2. This module is imported and re-exported by utils/media/__init__.py
3. The compatibility layer in utils/media_utils.py provides backward compatibility
   for existing code that imports these functions from the old location

Key Functions:
- load_imgur_retry_queue(): Loads the retry queue from a JSON file
- save_imgur_retry_queue(): Saves the retry queue to a JSON file
- add_to_imgur_retry_queue(): Adds a failed Imgur download to the retry queue
- process_imgur_retry_queue(): Processes items in the retry queue that are due for retry
- update_retry_queue_save_directory(): Updates the save directory for all items in the queue

Usage in main script:
In reddit_stash.py, the main script imports process_imgur_retry_queue and
update_retry_queue_save_directory to process the retry queue at startup.
"""

import os
import json
import time
import logging
import datetime
import random
import requests
from .media_core import save_directory, BASE_DIR, ignore_ssl_errors, download_failure_errors

# Path for the retry queue file
RETRY_QUEUE_FILE = os.path.join(save_directory, 'imgur_retry_queue.json')

# Initialize the retry queue
imgur_retry_queue = []

# Maximum number of retries before giving up
MAX_RETRY_ATTEMPTS = 5  # Changed from 10 to 5 as suggested

# Minimum time between retries (in seconds)
MIN_RETRY_INTERVAL = 3600  # 1 hour

# Maximum number of items to retry in a single run
MAX_RETRIES_PER_RUN = 20

def load_imgur_retry_queue():
    """Load the Imgur retry queue from the JSON file."""
    global imgur_retry_queue
    
    # Check for the file in the new location (save_directory)
    if os.path.exists(RETRY_QUEUE_FILE):
        try:
            with open(RETRY_QUEUE_FILE, 'r') as f:
                loaded_queue = json.load(f)
            
            # Validate the loaded queue
            if not isinstance(loaded_queue, list):
                logging.error(f"Imgur retry queue file is corrupted (not a list). Starting with empty queue.")
                imgur_retry_queue = []
                # Backup the corrupted file
                backup_file = f"{RETRY_QUEUE_FILE}.bak.{int(time.time())}"
                os.rename(RETRY_QUEUE_FILE, backup_file)
                logging.info(f"Backed up corrupted queue file to {backup_file}")
                return
            
            # Filter out any invalid items
            valid_items = []
            for item in loaded_queue:
                if not isinstance(item, dict):
                    logging.warning(f"Skipping invalid item in retry queue (not a dict): {item}")
                    continue
                
                # Check for required fields
                if 'url' not in item or 'save_directory' not in item or 'item_id' not in item:
                    logging.warning(f"Skipping item missing required fields: {item}")
                    continue
                
                valid_items.append(item)
            
            imgur_retry_queue = valid_items
            logging.info(f"Loaded {len(imgur_retry_queue)} valid items from Imgur retry queue")
            
            # If we filtered out items, save the cleaned queue
            if len(valid_items) < len(loaded_queue):
                logging.warning(f"Filtered out {len(loaded_queue) - len(valid_items)} invalid items from retry queue")
                save_imgur_retry_queue()
                
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse Imgur retry queue file (corrupted JSON): {e}")
            imgur_retry_queue = []
            # Backup the corrupted file
            backup_file = f"{RETRY_QUEUE_FILE}.bak.{int(time.time())}"
            os.rename(RETRY_QUEUE_FILE, backup_file)
            logging.info(f"Backed up corrupted queue file to {backup_file}")
        except Exception as e:
            logging.error(f"Failed to load Imgur retry queue: {e}")
            imgur_retry_queue = []
    else:
        # Check for the file in the old location (root directory)
        old_queue_file = os.path.join(BASE_DIR, 'imgur_retry_queue.json')
        if os.path.exists(old_queue_file):
            logging.info(f"Found Imgur retry queue file in old location: {old_queue_file}")
            try:
                with open(old_queue_file, 'r') as f:
                    loaded_queue = json.load(f)
                
                # Validate and process the queue as above
                if not isinstance(loaded_queue, list):
                    logging.error(f"Imgur retry queue file in old location is corrupted. Starting with empty queue.")
                    imgur_retry_queue = []
                    return
                
                # Filter out any invalid items
                valid_items = []
                for item in loaded_queue:
                    if not isinstance(item, dict):
                        continue
                    
                    # Check for required fields
                    if 'url' not in item or 'save_directory' not in item or 'item_id' not in item:
                        continue
                    
                    # Update the save directory to the new location
                    item['save_directory'] = save_directory
                    valid_items.append(item)
                
                imgur_retry_queue = valid_items
                logging.info(f"Loaded {len(imgur_retry_queue)} valid items from old Imgur retry queue location")
                
                # Save to the new location
                save_imgur_retry_queue()
                
                # Remove the old file
                try:
                    os.remove(old_queue_file)
                    logging.info(f"Removed old Imgur retry queue file after migration: {old_queue_file}")
                except Exception as e:
                    logging.warning(f"Failed to remove old Imgur retry queue file: {e}")
                
            except Exception as e:
                logging.error(f"Failed to load Imgur retry queue from old location: {e}")
                imgur_retry_queue = []
        else:
            logging.info("No Imgur retry queue file found. Starting with empty queue.")
            imgur_retry_queue = []

def save_imgur_retry_queue():
    """Save the Imgur retry queue to the JSON file."""
    try:
        # Log the current state of the queue and file path
        logging.info(f"Attempting to save Imgur retry queue with {len(imgur_retry_queue)} items to {RETRY_QUEUE_FILE}")
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(RETRY_QUEUE_FILE), exist_ok=True)
        
        # Always create the file, even if the queue is empty
        with open(RETRY_QUEUE_FILE, 'w') as f:
            json.dump(imgur_retry_queue, f, indent=2)
        
        logging.info(f"Successfully saved {len(imgur_retry_queue)} items to Imgur retry queue at {RETRY_QUEUE_FILE}")
    except Exception as e:
        logging.error(f"Failed to save Imgur retry queue: {e}")
        # Log more details about the error
        import traceback
        logging.error(f"Error details: {traceback.format_exc()}")

# Add a new function to create an empty queue file
def ensure_retry_queue_file_exists():
    """Ensure that the retry queue file exists, creating it if necessary."""
    if not os.path.exists(RETRY_QUEUE_FILE):
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(RETRY_QUEUE_FILE), exist_ok=True)
            
            # Create an empty queue file
            with open(RETRY_QUEUE_FILE, 'w') as f:
                json.dump([], f, indent=2)
            
            logging.info(f"Created empty Imgur retry queue file at {RETRY_QUEUE_FILE}")
            return True
        except Exception as e:
            logging.error(f"Failed to create empty Imgur retry queue file: {e}")
            return False
    return True

def add_to_imgur_retry_queue(url, save_directory, item_id, media_type='image', service=None, content_id=None):
    """Add a failed Imgur download to the retry queue."""
    # Check if this item is already in the queue
    for item in imgur_retry_queue:
        if item['url'] == url and item['item_id'] == item_id:
            # Update the retry count and next retry time
            item['retry_count'] = item.get('retry_count', 0) + 1
            
            # Check if we've reached the maximum retry attempts
            if item['retry_count'] >= MAX_RETRY_ATTEMPTS:
                logging.warning(f"Item {url} has reached maximum retry attempts ({MAX_RETRY_ATTEMPTS}). Not updating retry queue.")
                # Remove the item from the queue since it's reached max retries
                imgur_retry_queue.remove(item)
                save_imgur_retry_queue()
                return
            
            # Calculate next retry time with exponential backoff
            retry_count = item['retry_count']
            # Exponential backoff: 1h, 2h, 4h, 8h, 16h, etc. up to 7 days
            backoff_hours = min(168, 2 ** (retry_count - 1))
            next_retry = time.time() + (backoff_hours * 3600)
            
            item['next_retry'] = next_retry
            item['next_retry_human'] = datetime.datetime.fromtimestamp(next_retry).strftime('%Y-%m-%d %H:%M:%S')
            
            logging.info(f"Updated retry queue item for {url}. Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}, next retry at {item['next_retry_human']}")
            save_imgur_retry_queue()
            return
    
    # If not in queue, add it
    retry_item = {
        'url': url,
        'save_directory': save_directory,
        'item_id': item_id,
        'media_type': media_type,
        'service': service,
        'content_id': content_id,
        'retry_count': 1,
        'added_time': time.time(),
        'next_retry': time.time() + MIN_RETRY_INTERVAL,
        'next_retry_human': datetime.datetime.fromtimestamp(time.time() + MIN_RETRY_INTERVAL).strftime('%Y-%m-%d %H:%M:%S'),
        'last_error': None
    }
    
    imgur_retry_queue.append(retry_item)
    logging.info(f"Added {url} to Imgur retry queue. Will retry after {retry_item['next_retry_human']} (attempt 1/{MAX_RETRY_ATTEMPTS})")
    save_imgur_retry_queue()

def process_imgur_retry_queue():
    """Process items in the Imgur retry queue that are due for retry."""
    if not imgur_retry_queue:
        logging.info("Imgur retry queue is empty. Nothing to process.")
        return
    
    logging.info(f"Processing Imgur retry queue. {len(imgur_retry_queue)} items in queue.")
    
    # Get the current save directory from settings
    current_save_dir = save_directory
    
    # Check if any items have a different save directory and update them
    updated_dirs = 0
    for item in imgur_retry_queue:
        if item.get('save_directory') != current_save_dir:
            old_dir = item.get('save_directory', 'unknown')
            item['save_directory'] = current_save_dir
            updated_dirs += 1
            logging.info(f"Updated save directory for item {item.get('url')} from {old_dir} to {current_save_dir}")
    
    if updated_dirs > 0:
        logging.info(f"Updated save directory for {updated_dirs} items in the retry queue")
        save_imgur_retry_queue()
    
    # Sort the queue by next retry time
    imgur_retry_queue.sort(key=lambda x: x.get('next_retry', 0))
    
    current_time = time.time()
    retried_count = 0
    successful_items = []
    failed_items = []  # New list to track items that have exceeded max retries
    
    for item in imgur_retry_queue:
        # Check if this item is due for retry
        if item.get('next_retry', 0) <= current_time:
            # Check if we've reached the maximum retries per run
            if retried_count >= MAX_RETRIES_PER_RUN:
                logging.info(f"Reached maximum retries per run ({MAX_RETRIES_PER_RUN}). Will process remaining items in next run.")
                break
            
            # Check if we've reached the maximum retry attempts for this item
            if item.get('retry_count', 0) >= MAX_RETRY_ATTEMPTS:
                logging.warning(f"Item {item['url']} has exceeded maximum retry attempts ({MAX_RETRY_ATTEMPTS}). Removing from queue.")
                failed_items.append(item)  # Add to failed items list instead of successful
                continue
            
            logging.info(f"Retrying download for {item['url']} (attempt {item.get('retry_count', 1)})")
            
            # Try to download the image
            try:
                # Import here to avoid circular imports
                from .download import download_media
                
                result = download_media(
                    item['url'], 
                    item['save_directory'], 
                    item['item_id'], 
                    item.get('media_type', 'image'),
                    item.get('service'),
                    item.get('content_id')
                )
                
                if result:
                    logging.info(f"Successfully downloaded {item['url']} after {item.get('retry_count', 1)} attempts")
                    successful_items.append(item)
                else:
                    # Update retry count and next retry time
                    item['retry_count'] = item.get('retry_count', 0) + 1
                    
                    # Check if we've now reached the maximum retry attempts
                    if item['retry_count'] >= MAX_RETRY_ATTEMPTS:
                        logging.warning(f"Item {item['url']} has now reached maximum retry attempts ({MAX_RETRY_ATTEMPTS}). Removing from queue.")
                        failed_items.append(item)
                    else:
                        # Calculate next retry time with exponential backoff
                        retry_count = item['retry_count']
                        # Exponential backoff: 1h, 2h, 4h, 8h, 16h, etc. up to 7 days
                        backoff_hours = min(168, 2 ** (retry_count - 1))
                        next_retry = time.time() + (backoff_hours * 3600)
                        
                        item['next_retry'] = next_retry
                        item['next_retry_human'] = datetime.datetime.fromtimestamp(next_retry).strftime('%Y-%m-%d %H:%M:%S')
                        
                        logging.info(f"Download still failed for {item['url']}. Will retry at {item['next_retry_human']} (attempt {retry_count}/{MAX_RETRY_ATTEMPTS})")
            except Exception as e:
                # Update retry count and next retry time
                item['retry_count'] = item.get('retry_count', 0) + 1
                
                # Check if we've now reached the maximum retry attempts
                if item['retry_count'] >= MAX_RETRY_ATTEMPTS:
                    logging.warning(f"Item {item['url']} has reached maximum retry attempts ({MAX_RETRY_ATTEMPTS}) after error: {e}. Removing from queue.")
                    failed_items.append(item)
                else:
                    # Calculate next retry time with exponential backoff
                    retry_count = item['retry_count']
                    # Exponential backoff: 1h, 2h, 4h, 8h, 16h, etc. up to 7 days
                    backoff_hours = min(168, 2 ** (retry_count - 1))
                    next_retry = time.time() + (backoff_hours * 3600)
                    
                    item['next_retry'] = next_retry
                    item['next_retry_human'] = datetime.datetime.fromtimestamp(next_retry).strftime('%Y-%m-%d %H:%M:%S')
                    item['last_error'] = str(e)
                    
                    logging.warning(f"Error during retry for {item['url']}: {e}. Next retry at {item['next_retry_human']} (attempt {retry_count}/{MAX_RETRY_ATTEMPTS})")
            
            retried_count += 1
    
    # Remove successful and failed items from the queue
    for item in successful_items:
        imgur_retry_queue.remove(item)
    
    for item in failed_items:
        imgur_retry_queue.remove(item)
    
    # Log summary of processing
    if successful_items or failed_items:
        logging.info(f"Processed {retried_count} items from Imgur retry queue.")
        if successful_items:
            logging.info(f"{len(successful_items)} items were successfully downloaded and removed from the queue.")
        if failed_items:
            logging.info(f"{len(failed_items)} items exceeded the maximum retry attempts ({MAX_RETRY_ATTEMPTS}) and were removed from the queue.")
    
    # Save the updated queue
    save_imgur_retry_queue()

def update_retry_queue_save_directory(new_save_directory):
    """Update the save directory for all items in the retry queue.
    
    This function should be called when the save directory changes, for example
    when the script is run with a different save directory than before.
    
    Args:
        new_save_directory: The new save directory to use
    """
    global save_directory, RETRY_QUEUE_FILE
    
    # Update the global save directory
    old_save_directory = save_directory
    save_directory = new_save_directory
    
    # Update the retry queue file path
    old_retry_queue_file = RETRY_QUEUE_FILE
    RETRY_QUEUE_FILE = os.path.join(save_directory, 'imgur_retry_queue.json')
    
    logging.info(f"Updating retry queue save directory from {old_save_directory} to {save_directory}")
    
    # If the queue is already loaded, update all items
    if imgur_retry_queue:
        updated_count = 0
        for item in imgur_retry_queue:
            if item.get('save_directory') != save_directory:
                item['save_directory'] = save_directory
                updated_count += 1
        
        if updated_count > 0:
            logging.info(f"Updated save directory for {updated_count} items in the retry queue")
            save_imgur_retry_queue()
    
    # If the old file exists and is different from the new file, migrate it
    if os.path.exists(old_retry_queue_file) and old_retry_queue_file != RETRY_QUEUE_FILE:
        if not os.path.exists(RETRY_QUEUE_FILE):
            try:
                # Ensure the directory exists
                os.makedirs(os.path.dirname(RETRY_QUEUE_FILE), exist_ok=True)
                
                # Copy the file to the new location
                with open(old_retry_queue_file, 'r') as f_old:
                    queue_data = json.load(f_old)
                
                # Update save directories in the queue
                if isinstance(queue_data, list):
                    for item in queue_data:
                        if isinstance(item, dict) and 'save_directory' in item:
                            item['save_directory'] = save_directory
                
                # Save to the new location
                with open(RETRY_QUEUE_FILE, 'w') as f_new:
                    json.dump(queue_data, f_new, indent=2)
                
                logging.info(f"Migrated retry queue file from {old_retry_queue_file} to {RETRY_QUEUE_FILE}")
                
                # Remove the old file
                try:
                    os.remove(old_retry_queue_file)
                    logging.info(f"Removed old retry queue file after migration: {old_retry_queue_file}")
                except Exception as e:
                    logging.warning(f"Failed to remove old retry queue file: {e}")
            except Exception as e:
                logging.error(f"Failed to migrate retry queue file: {e}")
    
    # Reload the queue from the new location
    load_imgur_retry_queue()

# Load the retry queue at module initialization
load_imgur_retry_queue()

# Add a function to check the retry queue file
def debug_retry_queue_file():
    """Check if the retry queue file exists and print its contents for debugging."""
    if os.path.exists(RETRY_QUEUE_FILE):
        try:
            with open(RETRY_QUEUE_FILE, 'r') as f:
                contents = json.load(f)
            logging.info(f"Retry queue file exists at {RETRY_QUEUE_FILE}")
            logging.info(f"Retry queue file contains {len(contents)} items")
            if contents:
                logging.info(f"First item in queue: {contents[0]}")
            return True
        except Exception as e:
            logging.error(f"Error reading retry queue file: {e}")
            return False
    else:
        logging.warning(f"Retry queue file does not exist at {RETRY_QUEUE_FILE}")
        return False

# Add a function to clean up the retry queue
def cleanup_imgur_retry_queue():
    """Remove items from the retry queue that have exceeded the maximum retry count."""
    if not imgur_retry_queue:
        return 0
    
    items_to_remove = []
    for item in imgur_retry_queue:
        if item.get('retry_count', 0) >= MAX_RETRY_ATTEMPTS:
            items_to_remove.append(item)
    
    if items_to_remove:
        for item in items_to_remove:
            imgur_retry_queue.remove(item)
            logging.info(f"Removed {item['url']} from retry queue (exceeded {MAX_RETRY_ATTEMPTS} retry attempts)")
        
        save_imgur_retry_queue()
        logging.info(f"Cleaned up retry queue: removed {len(items_to_remove)} items that exceeded maximum retry attempts")
    
    return len(items_to_remove) 