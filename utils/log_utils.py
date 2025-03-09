import os
import json
import logging

class DuplicateFilter(logging.Filter):
    """Filter to deduplicate log messages."""
    
    def __init__(self):
        super().__init__()
        self.seen = set()
        
    def filter(self, record):
        # Get the message after formatting
        message = record.getMessage()
        
        # For warnings and errors about recovery and download failures,
        # check if we've seen this message before
        if (record.levelno >= logging.WARNING and 
            ("failed for" in message or "All recovery methods failed" in message)):
            
            # Create a key from the level and message
            key = f"{record.levelno}:{message}"
            
            # If we've seen this message before, filter it out
            if key in self.seen:
                return False
                
            # Otherwise, add it to the seen set
            self.seen.add(key)
            
        # Allow all other messages
        return True

def setup_logging():
    """Set up logging with the duplicate filter."""
    # Get the root logger
    root_logger = logging.getLogger()
    
    # Add the duplicate filter
    duplicate_filter = DuplicateFilter()
    root_logger.addFilter(duplicate_filter)
    
    # Return the filter in case it needs to be removed later
    return duplicate_filter

def get_log_file_path(save_directory):
    """Return the path to the log file inside the save_directory."""
    return os.path.join(save_directory, 'file_log.json')

def load_file_log(save_directory):
    """Load the file log from a JSON file in the specified directory."""
    log_file_path = get_log_file_path(save_directory)
    if os.path.exists(log_file_path):
        with open(log_file_path, 'r') as f:
            return json.load(f)
    return {}

def save_file_log(log_data, save_directory):
    """Save the file log to a JSON file in the specified directory."""
    log_file_path = get_log_file_path(save_directory)
    with open(log_file_path, 'w') as f:
        json.dump(log_data, f, indent=4)

def is_file_logged(log_data, unique_key):
    """Check if a unique key is already logged."""
    return unique_key in log_data

def log_file(log_data, unique_key, file_info, save_directory):
    """Add a file information to the log with the provided unique key."""
    
    # Convert the absolute file path to a relative one
    relative_file_path = os.path.relpath(file_info['file_path'], start=save_directory)
    
    # Update the file info with the relative path
    file_info['file_path'] = relative_file_path
    
    # Add the file info to the log with the unique key
    log_data[unique_key] = file_info
    
    # Save the updated log
    save_file_log(log_data, save_directory)

def convert_to_absolute_path(relative_path, save_directory):
    """Convert a relative path from the log back to an absolute path."""
    return os.path.join(save_directory, relative_path)