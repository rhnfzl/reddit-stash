import os

def validate_and_set_directory(path, fallback_path='reddit/'):
    """
    Validates the save_directory path, creates it if necessary, and ensures it is writable.
    
    Parameters:
    path (str): The directory path to validate.
    fallback_path (str): The fallback path to use if validation fails.

    Returns:
    str: A valid directory path that is either the original or the fallback.
    """
    # Check if the path is absolute, if not, convert to absolute path
    path = os.path.abspath(path)

    # Check if the directory exists
    if not os.path.exists(path):
        try:
            # Attempt to create the directory
            os.makedirs(path)
            print(f"Directory {path} did not exist, so it was created.")
        except OSError as e:
            print(f"Error: Unable to create directory {path}. {e}")
            # Fallback to the provided default directory if creation fails
            fallback_path = os.path.abspath(fallback_path)
            if not os.path.exists(fallback_path):
                os.makedirs(fallback_path)
            print(f"Falling back to default directory: {fallback_path}")
            return fallback_path
    elif not os.access(path, os.W_OK):
        print(f"Error: Directory {path} is not writable.")
        # Fallback to the provided default directory if not writable
        fallback_path = os.path.abspath(fallback_path)
        if not os.path.exists(fallback_path):
            os.makedirs(fallback_path)
        print(f"Falling back to default directory: {fallback_path}")
        return fallback_path

    return path