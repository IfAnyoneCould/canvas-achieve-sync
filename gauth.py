"""Shared Google OAuth for every Google API this app touches.

Scopes live here so the target (Tasks, read/write) and the calendar source
(read-only) are authorized in one consent, with one token file.

Adding a scope invalidates an existing token: a refresh returns the *old*
scopes, so `credentials()` checks the stored token actually covers SCOPES and
forces a fresh consent when it does not.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from common import ROOT

# What the sync cannot run without.
BASE_SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Only the homework packets need these: writing the event onto the calendar,
# and storing the packet itself. drive.file is deliberately the narrow one - it
# grants access only to files this app creates, not to the rest of Drive.
#
# They are kept separate because adding a scope invalidates the stored token.
# If they were simply required, granting them would take the whole sync down
# until the next interactive login; as it is, a token from before they existed
# still runs everything else, and only the packets wait for consent.
EXTRA_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.file",
]

SCOPES = BASE_SCOPES + EXTRA_SCOPES  # what a fresh consent asks for

CLIENT_SECRET_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


def _stored() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        # Deliberately no scopes argument: passing them *overrides* what the
        # file records, so has_scopes() would trivially pass and a token
        # granted only `tasks` would sail through to a 403 at call time.
        return Credentials.from_authorized_user_file(str(TOKEN_PATH))
    except ValueError:
        return None


def authorized_for(scopes: list[str]) -> bool:
    """Whether the saved token already covers these scopes - asking nothing."""
    creds = _stored()
    return bool(creds and creds.has_scopes(scopes))


def credentials(
    interactive: bool, logger: logging.Logger, need: list[str] | None = None
) -> Credentials:
    need = BASE_SCOPES if need is None else need
    creds = _stored()
    if creds is None and TOKEN_PATH.exists():
        logger.debug("stored token unreadable")

    needs_consent = creds is None or not creds.has_scopes(need)
    if creds is not None and needs_consent:
        logger.info("Saved token is missing a required scope; re-consent needed.")

    if not needs_consent and creds and creds.valid:
        return creds

    if not needs_consent and creds and creds.expired and creds.refresh_token:
        logger.debug("refreshing Google credentials")
        creds.refresh(Request())
    else:
        if not interactive:
            raise SystemExit(
                "Google authorization is missing, expired, or lacks a required scope, "
                "and this is a non-interactive run.\n"
                "Run `python sync.py --login` once to re-authorize."
            )
        if not CLIENT_SECRET_PATH.exists():
            raise SystemExit(
                f"Missing {CLIENT_SECRET_PATH}. Download your OAuth desktop client "
                "JSON from Google Cloud Console. See README step 2."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")

    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        Path(TOKEN_PATH).chmod(0o600)
    except OSError:
        pass
    return creds


def service(
    api: str,
    version: str,
    interactive: bool,
    logger: logging.Logger,
    need: list[str] | None = None,
):
    return build(
        api,
        version,
        credentials=credentials(interactive, logger, need),
        cache_discovery=False,
    )
