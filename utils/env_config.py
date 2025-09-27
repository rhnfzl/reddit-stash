import os
import configparser

invalid_config = (None, '', "None")

def load_config_and_env():
    """Load configuration from settings.ini and fall back to environment variables if necessary."""
    config_parser = configparser.ConfigParser()

    # Dynamically determine the path to the root directory of the repository
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Construct the full path to the settings.ini file
    config_file_path = os.path.join(BASE_DIR, 'settings.ini')

    # Read the settings.ini file
    config_parser.read(config_file_path)

    # Load from settings.ini, but treat "None" or empty strings as invalid
    client_id = config_parser.get('Configuration', 'client_id', fallback=None)
    client_secret = config_parser.get('Configuration', 'client_secret', fallback=None)
    username = config_parser.get('Configuration', 'username', fallback=None)
    password = config_parser.get('Configuration', 'password', fallback=None)

    # If the values from the config are "None" (as strings) or empty, fallback to environment variables
    client_id = client_id if client_id and client_id not in invalid_config else os.getenv('REDDIT_CLIENT_ID')
    client_secret = client_secret if client_secret and client_secret not in invalid_config else os.getenv('REDDIT_CLIENT_SECRET')
    username = username if username and username not in invalid_config else os.getenv('REDDIT_USERNAME')
    password = password if password and password not in invalid_config else os.getenv('REDDIT_PASSWORD')

    # Check if any required credentials are still missing
    if not all([client_id, client_secret, username, password]):
        raise Exception("One or more required credentials for Reddit API are missing.")

    return client_id, client_secret, username, password

def get_ignore_tls_errors():
    """Load the ignore_tls_errors setting from settings.ini."""
    config_parser = configparser.ConfigParser()

    # Dynamically determine the path to the root directory of the repository
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Construct the full path to the settings.ini file
    config_file_path = os.path.join(BASE_DIR, 'settings.ini')

    # Read the settings.ini file
    config_parser.read(config_file_path)

    # Get the ignore_tls_errors setting, default to False for security
    ignore_tls_errors = config_parser.getboolean('Settings', 'ignore_tls_errors', fallback=False)

    return ignore_tls_errors