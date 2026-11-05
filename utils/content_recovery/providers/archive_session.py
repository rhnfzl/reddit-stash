"""HTTP session setup shared by the archive API providers."""

import requests


def archive_session() -> requests.Session:
    """Return a session that identifies this tool to archive JSON APIs."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Reddit Stash Content Recovery/1.0 (Personal Archive Tool)',
        'Accept': 'application/json',
    })
    return session
