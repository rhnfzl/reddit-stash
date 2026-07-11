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

    # Load from settings.ini, falling back to env vars; treat "None"/empty as unset.
    def _load(key, env):
        value = config_parser.get('Configuration', key, fallback=None)
        return value if value and value not in invalid_config else os.getenv(env)

    client_id = _load('client_id', 'REDDIT_CLIENT_ID')
    client_secret = _load('client_secret', 'REDDIT_CLIENT_SECRET')
    username = _load('username', 'REDDIT_USERNAME')
    password = _load('password', 'REDDIT_PASSWORD')
    refresh_token = _load('refresh_token', 'REDDIT_REFRESH_TOKEN')

    # Two auth modes: a refresh_token (preferred - no password in secrets, survives)
    # OR username+password. client_id/client_secret are required either way.
    have_token_auth = all([client_id, client_secret, refresh_token])
    have_password_auth = all([client_id, client_secret, username, password])

    if not (have_token_auth or have_password_auth):
        missing = []
        if not client_id:
            missing.append("REDDIT_CLIENT_ID")
        if not client_secret:
            missing.append("REDDIT_CLIENT_SECRET")
        if not refresh_token and not (username and password):
            missing.append("REDDIT_REFRESH_TOKEN (or REDDIT_USERNAME + REDDIT_PASSWORD)")

        msg = (
            f"Missing Reddit API credentials: {', '.join(missing)}\n\n"
            "Since November 2025, Reddit requires pre-approval to create new API apps.\n"
            "If you need new credentials, apply here (2-4 week wait):\n"
            "  https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164\n\n"
            "Existing credentials (created before Nov 2025) still work normally.\n\n"
            "Auth options:\n"
            "  1. REDDIT_REFRESH_TOKEN (preferred) - run get_refresh_token.py once to mint it.\n"
            "  2. REDDIT_USERNAME + REDDIT_PASSWORD.\n"
            "Set credentials via environment variables or in settings.ini.\n\n"
            "Alternative: Set process_api=false and process_gdpr=true in settings.ini\n"
            "to process your Reddit GDPR export data without API credentials."
        )
        raise Exception(msg)

    return client_id, client_secret, username, password, refresh_token

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