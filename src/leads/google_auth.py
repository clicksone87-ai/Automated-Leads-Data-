"""One-time Google OAuth sign-in for Gmail + Sheets, and credential loading.

Run once:

    uv run python -m leads.google_auth                 # on the host, opens a browser
    docker compose run --rm auth                       # in Docker (copy/paste flow)

Needs ``secrets/client_secret.json`` (OAuth client of type "Desktop app") and writes
``secrets/token.json``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from leads.settings import Settings, get_settings

# gmail.modify: create drafts, read threads, apply labels. The code never sends mail.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
MANUAL_REDIRECT_URI = "http://localhost:8765/"


def scopes(settings: Settings) -> list[str]:
    """Sheets access is asked for only when a Sheet is configured."""
    return [GMAIL_SCOPE, SHEETS_SCOPE] if settings.google_sheet_id else [GMAIL_SCOPE]


def load_credentials(settings: Settings | None = None) -> Credentials:
    """Load the saved token, refreshing it if expired."""
    settings = settings or get_settings()
    token_path = settings.token_path
    if not token_path.exists():
        raise RuntimeError(
            f"No Google token at {token_path}. Run: python -m leads.google_auth (see README)."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), scopes(settings))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # secrets/ is mounted read-only in the worker; the refreshed token then stays in memory.
        with contextlib.suppress(OSError):
            token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def gmail_service(settings: Settings | None = None) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=load_credentials(settings), cache_discovery=False)


def sheets_service(settings: Settings | None = None) -> Any:
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=load_credentials(settings), cache_discovery=False)


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def sign_in(settings: Settings | None = None, *, no_browser: bool = False) -> Path:
    """Run the OAuth consent flow and save the token. Returns the token path."""
    settings = settings or get_settings()
    secret = settings.client_secret_path
    if not secret.exists():
        raise SystemExit(
            f"Missing {secret}. Create an OAuth client (Desktop app) in Google Cloud Console, "
            "download the JSON and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes(settings))
    hint = {"login_hint": settings.gmail_account} if settings.gmail_account else {}
    if no_browser:
        flow.redirect_uri = MANUAL_REDIRECT_URI
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", **hint)
        print("\n1. Open this URL in your browser and approve access:\n")
        print(auth_url)
        print(
            "\n2. Your browser will then try to open http://localhost:8765/?... and show an error"
            "\n   page. That is expected. Copy the FULL address from the address bar.\n"
        )
        response = input("3. Paste the full address here: ").strip()
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # localhost redirect only
        flow.fetch_token(authorization_response=response)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent", **hint)

    token_path = settings.token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return token_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Google OAuth sign-in for lead-automation")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="copy/paste flow (default inside Docker)",
    )
    args = parser.parse_args(argv)
    path = sign_in(no_browser=args.no_browser or _running_in_container())
    print(f"Saved Google token to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
