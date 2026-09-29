"""
=== SmartThings Initial Authentication Script ===
Run this script once for first-time setup.
Token refresh is handled automatically by smartthings_worker.py (data-collection).

Usage:
    python smartthings_auth.py

Authentication steps:
    1. Browser will open.
    2. Log in with Samsung account and grant permissions.
    3. Browser redirects to https://httpbin.org/get?code=xxxx
    4. Copy the "code" value from the JSON and paste it into the terminal.
    5. Token will be issued and saved to file automatically.

Required:
    pip install aiohttp
"""

import json
import os
import webbrowser
import secrets
import aiohttp
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode
import base64
from pathlib import Path

# =============================================
# Load from config (config.local.json overrides config.json)
# =============================================
def _load_config():
    base = Path(__file__).parent
    local = base / "config.local.json"
    config_path = local if local.exists() else base / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            "config.json / config.local.json not found.\n"
            "Copy config.json to config.local.json and fill in CLIENT_ID and CLIENT_SECRET."
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)

_config       = _load_config()
CLIENT_ID     = _config["CLIENT_ID"]
CLIENT_SECRET = _config["CLIENT_SECRET"]
REDIRECT_URI  = "https://httpbin.org/get"
SCOPES        = "r:devices:* w:devices:* x:devices:* r:hubs:* r:locations:* w:locations:* x:locations:* r:scenes:* x:scenes:* r:rules:* w:rules:* r:installedapps w:installedapps"

TOKEN_FILE = os.path.abspath(_config["TOKEN_FILE"])
os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)

AUTH_URL  = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/oauth/token"

STATE = secrets.token_hex(16)


def make_basic_auth_header(client_id, client_secret):
    """Create Basic Auth header (same as curl -u)."""
    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


async def exchange_code_for_token(code: str) -> dict:
    """
    Exchange authorization code for access_token and refresh_token.
    SmartThings uses Basic Auth.
    curl -X POST "https://api.smartthings.com/oauth/token"
         -u "clientId:clientSecret"
         -d "grant_type=authorization_code&code=xxx&redirect_uri=xxx"
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": REDIRECT_URI,
                "client_id":    CLIENT_ID,
            },
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Authorization": make_basic_auth_header(CLIENT_ID, CLIENT_SECRET),
            }
        ) as resp:
            text = await resp.text()
            if resp.status == 200:
                return json.loads(text)
            else:
                raise Exception(f"Token exchange failed ({resp.status}): {text}")


def save_token(token_data: dict):
    """Save token data to file."""
    expires_in = int(token_data.get("expires_in", 86400))
    token_data["expires_at"] = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f, ensure_ascii=False, indent=4)
    print(f"\nToken saved: {TOKEN_FILE}")


def main():
    params = urlencode({
        "response_type": "code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
        "state":         STATE,
    })
    auth_url = f"{AUTH_URL}?{params}"

    print("=" * 60)
    print("SmartThings OAuth - Initial Authentication")
    print("=" * 60)
    print("\n[Step 1] Log in with Samsung account and grant permissions in the browser.")
    print("\nIf the browser does not open automatically, open this URL manually:")
    print(f"\n{auth_url}\n")

    webbrowser.open(auth_url)

    print("-" * 60)
    print("[Step 2] After granting permission, browser redirects to:")
    print("  https://httpbin.org/get?code=XXXXXX&state=XXXXXX")
    print('\nCopy the "code" value from the page JSON.')
    print('Example:  "code": "820gPa"  ->  copy 820gPa')
    print("-" * 60)

    code = input("\n>> Paste the code value here: ").strip()

    if not code:
        print(" No code entered. Please run again.")
        return

    print("\nExchanging for access token...")

    try:
        token_data = asyncio.run(exchange_code_for_token(code))
    except Exception as e:
        print(f"\n Error: {e}")
        return

    save_token(token_data)

    print("\n=== Issued Token Info ===")
    print(f"  access_token  : {str(token_data.get('access_token', ''))[:20]}...")
    print(f"  refresh_token : {str(token_data.get('refresh_token', ''))[:20]}...")
    print(f"  expires_in    : {token_data.get('expires_in')} seconds (24 hours)")
    print(f"  scope         : {token_data.get('scope')}")
    print(f"  expires_at    : {token_data.get('expires_at')}")
    print("\nDone! You can now run: python main.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
