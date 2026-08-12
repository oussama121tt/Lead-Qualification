"""Gmail API sending with an installed-app OAuth2 flow.

One-time setup (run once from the repo root, browser opens for consent):
    python setup_gmail.py
That stores the refresh token in token.json (gitignored). From then on,
send_email() refreshes the access token automatically and never needs
interaction again.
"""
import base64
import pathlib
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_PATH = pathlib.Path(__file__).parent / "credentials.json"
TOKEN_PATH = pathlib.Path(__file__).parent / "token.json"
THROTTLE_SECONDS = 10  # pause between two sends, to stay under Gmail's spam radar

_service = None


class GmailNotConfigured(RuntimeError):
    """Raised when token.json is missing: run `python setup_gmail.py` once."""


def get_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise GmailNotConfigured(
            "Gmail is not connected yet. Run `python setup_gmail.py` once from the repo folder."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        global _service
        _service = None
    return creds


def _get_service():
    """Lazily-built singleton Gmail service (cached until the token changes)."""
    global _service
    if _service is None:
        from googleapiclient.discovery import build

        _service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
    return _service


def send_email(to_address: str, subject: str, body: str) -> None:
    """Sends an email via the Gmail API. Raises on failure, and raises
    GmailNotConfigured before any send when token.json is missing."""
    if not to_address:
        raise ValueError("no email address on this lead")
    msg = EmailMessage()
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _get_service().users().messages().send(userId="me", body={"raw": raw}).execute()