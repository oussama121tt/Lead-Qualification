"""One-time Gmail OAuth setup (run from the repo root).

Two steps:
    python setup_gmail.py --url          # prints the Google consent URL
    python setup_gmail.py <redirect-url> # paste the full http://localhost... URL here

Or run it interactively (no argument) and paste the URL when prompted.
"""
import json
import pathlib
import sys
import urllib.parse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_sender import CREDENTIALS_PATH, SCOPES, TOKEN_PATH

VERIFIER_PATH = TOKEN_PATH.with_name(".code_verifier")


def _make_flow(code_verifier=None) -> InstalledAppFlow:
    kwargs = {"code_verifier": code_verifier} if code_verifier else {}
    return InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), SCOPES, redirect_uri="http://localhost", **kwargs
    )


def _exchange(redirect_url: str) -> int:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url.strip()).query)
    code = query.get("code")
    if not code:
        print("No 'code' found in the pasted URL. Make sure you copied the full address.")
        return 1
    if not VERIFIER_PATH.exists():
        print("No saved code verifier. Re-run: python setup_gmail.py --url")
        return 1
    config = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    client = config.get("installed") or config.get("web")
    if not client:
        print("credentials.json has an unexpected format (no 'installed'/'web' section).")
        return 1
    verifier = VERIFIER_PATH.read_text(encoding="utf-8").strip()
    resp = requests.post(
        client["token_uri"],
        data={
            "code": code[0],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": "http://localhost",
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=60,
    )
    payload = resp.json()
    if "access_token" not in payload:
        print(
            "Token exchange failed:",
            resp.status_code,
            payload.get("error"),
            payload.get("error_description") or "",
        )
        print("If the code was already used or expired, re-run: python setup_gmail.py --url")
        return 1
    token = {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": payload["refresh_token"],
        "token": payload["access_token"],
        "token_uri": client["token_uri"],
        "scopes": SCOPES,
        "type": "authorized_user",
    }
    TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    print("token.json saved. Your app can now send emails with Gmail.")
    return 0


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print("credentials.json is missing. Download it from the Google Cloud console")
        print("(APIs & Services > Credentials > OAuth client, type 'Desktop app') and place")
        print("it in the app folder next to this script.")
        return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--url":
        flow = _make_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        VERIFIER_PATH.write_text(flow.code_verifier or "", encoding="utf-8")
        print(auth_url)
        return 0

    if len(sys.argv) > 1:
        return _exchange(sys.argv[1])

    # Interactive fallback
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    VERIFIER_PATH.write_text(flow.code_verifier or "", encoding="utf-8")
    print("1. Open this URL in your browser and grant access:\n")
    print(auth_url)
    print("\n2. After granting access, the browser will redirect to a page that")
    print("   cannot be displayed (http://localhost). Copy the FULL address from")
    print("   the address bar (it starts with http://localhost) and paste it here:")
    redirect_url = input("URL: ").strip()
    return _exchange(redirect_url)


if __name__ == "__main__":
    sys.exit(main())