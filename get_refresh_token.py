"""One-time helper: mint a Reddit refresh token for Reddit Stash.

Why you'd want this: it lets you back up Reddit without putting your account
PASSWORD in GitHub Actions secrets (or anywhere on a server). You authorize once
in a browser, get a long-lived refresh token, and PRAW renews the short-lived
access tokens for you from then on. What it does: walks the standard Reddit
OAuth "web app" flow using PRAW (already a dependency) and prints the token.

Requirements:
  - A Reddit app of type "web app" (NOT "script") with redirect uri
    http://localhost:8080 (set REDDIT_REDIRECT_URI to override).
  - client_id / client_secret from https://www.reddit.com/prefs/apps

Usage:
  python get_refresh_token.py
Then set the printed value as REDDIT_REFRESH_TOKEN (env var or settings.ini
[Configuration] refresh_token). You can drop REDDIT_PASSWORD once this works.
"""
import os
import sys

import praw

# Scopes Reddit Stash needs: identity (user.me), history (saved/upvoted lists),
# read (fetch content), save (save/unsave). Matches what the backup actually calls.
SCOPES = ["identity", "history", "read", "save"]
STATE = "reddit-stash"


def _extract_code(pasted, expected_state):
    """Pull the ?code= value out of a pasted redirect URL, or accept a bare code."""
    if "code=" in pasted:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(pasted).query)
        if expected_state and q.get("state", [None])[0] != expected_state:
            raise ValueError("state mismatch - possible CSRF, aborting")
        return q["code"][0].rstrip("#_")
    return pasted.rstrip("#_")


def main():
    client_id = os.getenv("REDDIT_CLIENT_ID") or input("client_id: ").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET") or input("client_secret: ").strip()
    redirect_uri = os.getenv("REDDIT_REDIRECT_URI", "http://localhost:8080")

    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_agent="reddit-stash token setup",
    )

    print("\n1. Open this URL, log in, and click 'Allow':\n")
    print("   " + reddit.auth.url(scopes=SCOPES, state=STATE, duration="permanent") + "\n")
    print("2. Your browser redirects to a localhost URL that fails to load - that's expected.")
    print("   Copy the FULL address bar URL (or just the code= value) and paste it below.\n")

    try:
        code = _extract_code(input("redirected URL or code: ").strip(), STATE)
        refresh_token = reddit.auth.authorize(code)
    except Exception as e:
        print(f"\n❌ Failed: {e}")
        return 1

    print("\n✅ Success. Set this as REDDIT_REFRESH_TOKEN:\n")
    print("   " + refresh_token + "\n")
    print("Verified as Reddit user:", reddit.user.me())
    return 0


def _selftest():
    assert _extract_code("http://localhost:8080/?state=reddit-stash&code=ABC123#_", "reddit-stash") == "ABC123"
    assert _extract_code("XYZ#_", None) == "XYZ"
    try:
        _extract_code("http://x/?state=bad&code=1", "reddit-stash")
        assert False, "expected state mismatch to raise"
    except ValueError:
        pass
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
