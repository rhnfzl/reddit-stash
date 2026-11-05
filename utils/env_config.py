import os
import configparser

invalid_config = (None, '', "None")

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'settings.ini')

_CREDENTIAL_SOURCES = (
    ('client_id', 'REDDIT_CLIENT_ID'),
    ('client_secret', 'REDDIT_CLIENT_SECRET'),
    ('username', 'REDDIT_USERNAME'),
    ('password', 'REDDIT_PASSWORD'),
)


class MissingCredentialsError(Exception):
    """Reddit API credentials are in neither settings.ini nor the environment."""


def load_settings() -> configparser.ConfigParser:
    """Parse the repo-root settings.ini, wherever the process was started; a missing file gives an empty config."""
    config_parser = configparser.ConfigParser()
    config_parser.read(SETTINGS_PATH)
    return config_parser


def load_reddit_credentials() -> tuple[str, str, str, str]:
    """Load Reddit credentials from settings.ini, falling back to environment variables for unset values."""
    config_parser = load_settings()

    credentials = {}
    for key, env_name in _CREDENTIAL_SOURCES:
        value = config_parser.get('Configuration', key, fallback=None)
        credentials[env_name] = value if value not in invalid_config else os.getenv(env_name)

    missing = [env_name for env_name, value in credentials.items() if not value]
    if missing:
        msg = (
            f"Missing Reddit API credentials: {', '.join(missing)}\n\n"
            "Since November 2025, Reddit requires pre-approval to create new API apps.\n"
            "If you need new credentials, apply here (2-4 week wait):\n"
            "  https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164\n\n"
            "Existing credentials (created before Nov 2025) still work normally.\n"
            "Set credentials via environment variables (REDDIT_CLIENT_ID, etc.) or in settings.ini.\n\n"
            "Alternative: Set process_api=false and process_gdpr=true in settings.ini\n"
            "to process your Reddit GDPR export data without API credentials."
        )
        raise MissingCredentialsError(msg)

    return tuple(credentials.values())
