#!/usr/bin/env python3
"""
Test script to demonstrate the Imgur retry queue functionality.
This script adds test items to the retry queue with different retry counts.
"""

import os
import json
import time
import datetime
import logging
from utils.file_path_validate import validate_and_set_directory
from utils.media.retry_queue import (
    add_to_imgur_retry_queue, 
    process_imgur_retry_queue, 
    update_retry_queue_save_directory,
    ensure_retry_queue_file_exists,
    debug_retry_queue_file,
    cleanup_imgur_retry_queue,
    MAX_RETRY_ATTEMPTS
)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    # Set the save directory
    save_directory = validate_and_set_directory('reddit/')
    
    # Update the retry queue save directory
    update_retry_queue_save_directory(save_directory)
    
    # Ensure the retry queue file exists
    ensure_retry_queue_file_exists()
    
    # Debug the retry queue file
    debug_retry_queue_file()
    
    # Clean up any existing items in the retry queue
    cleanup_imgur_retry_queue()
    
    # Add test items to the retry queue with different retry counts
    test_urls = [
        "https://imgur.com/test1",
        "https://imgur.com/test2",
        "https://imgur.com/test3",
        "https://imgur.com/test4",
        "https://imgur.com/test5",
    ]
    
    print(f"Adding {len(test_urls)} test items to the retry queue...")
    
    # Add all items first
    for i, url in enumerate(test_urls):
        add_to_imgur_retry_queue(url, save_directory, f"test{i+1}", "image", "imgur", f"test{i+1}")
    
    # Now update the retry counts
    print("Updating retry counts...")
    update_retry_count(test_urls[1], 2)
    update_retry_count(test_urls[2], 3)
    update_retry_count(test_urls[3], 4)
    update_retry_count(test_urls[4], 5)
    
    # Debug the retry queue file after adding test items
    print("\nRetry queue after adding test items:")
    debug_retry_queue_file()
    
    # Clean up the retry queue
    print("\nCleaning up the retry queue...")
    removed_count = cleanup_imgur_retry_queue()
    print(f"Removed {removed_count} items from the retry queue")
    
    # Debug the retry queue file after cleanup
    print("\nRetry queue after cleanup:")
    debug_retry_queue_file()
    
    # Process the retry queue
    print("\nProcessing the retry queue...")
    process_imgur_retry_queue()
    
    # Debug the retry queue file after processing
    print("\nRetry queue after processing:")
    debug_retry_queue_file()

def update_retry_count(url, count):
    """Update the retry count for a specific URL in the retry queue."""
    retry_queue_file = os.path.join('reddit', 'imgur_retry_queue.json')
    
    # Read the current queue
    with open(retry_queue_file, 'r') as f:
        queue = json.load(f)
    
    # Update the retry count for the specified URL
    updated = False
    for item in queue:
        if item['url'] == url:
            item['retry_count'] = count
            
            # Update next retry time based on the new count
            backoff_hours = min(168, 2 ** (count - 1))
            next_retry = time.time() + (backoff_hours * 3600)
            
            item['next_retry'] = next_retry
            item['next_retry_human'] = datetime.datetime.fromtimestamp(next_retry).strftime('%Y-%m-%d %H:%M:%S')
            
            logging.info(f"Manually updated retry count for {url} to {count}")
            updated = True
            break
    
    if not updated:
        logging.warning(f"Could not find URL {url} in the retry queue")
        return
    
    # Save the updated queue
    with open(retry_queue_file, 'w') as f:
        json.dump(queue, f, indent=2)
    
    logging.info(f"Saved updated retry queue with {len(queue)} items")

if __name__ == "__main__":
    main() 