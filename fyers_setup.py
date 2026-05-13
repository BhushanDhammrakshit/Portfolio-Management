"""Fyers API — one-time access-token generator.

Run this script once to get your FYERS_ACCESS_TOKEN.
It will open a browser for you to log in, then exchange the auth code
for an access token and print it.

Usage:
    python fyers_setup.py
"""
import hashlib
import os
import re
import webbrowser

import requests
from dotenv import load_dotenv

load_dotenv()

_AUTH_BASE = "https://api-t1.fyers.in/api/v3"
_TOKEN_URL = f"{_AUTH_BASE}/validate-authcode"
_REDIRECT  = "http://127.0.0.1:5000/fyers/callback"


def main():
    print("=" * 60)
    print("  FYERS ACCESS TOKEN GENERATOR")
    print("=" * 60)

    # Step 1: Get App ID and Secret
    app_id = os.getenv("FYERS_APP_ID", "").strip()
    if not app_id:
        app_id = input("\n1. Enter your Fyers App ID (e.g. XC0ABCD-100): ").strip()
    else:
        print(f"\n1. Using App ID from .env: {app_id}")

    secret = input("2. Enter your Fyers Secret Key: ").strip()
    if not secret:
        print("   Secret Key is required. Find it on https://myapi.fyers.in/dashboard/")
        return

    # Step 2: Generate auth URL
    # Fyers v3 auth URL format
    auth_url = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={app_id}"
        f"&redirect_uri={_REDIRECT}"
        f"&response_type=code"
        f"&state=setup"
    )

    print("\n3. Opening your browser to log in to Fyers...")
    print(f"   If it doesn't open, copy this URL manually:\n")
    print(f"   {auth_url}\n")
    webbrowser.open(auth_url)

    print("-" * 60)
    print("After logging in, your browser will redirect to a URL like:")
    print(f"  {_REDIRECT}?auth_code=XXXXX&state=setup")
    print()
    print("The page will show an error (that's normal — nothing is")
    print("listening there). Just copy from the URL bar.")
    print("-" * 60)

    # Step 3: Get auth code from user
    raw = input("\n4. Paste the FULL redirect URL (or just the auth_code value): ").strip()

    # Extract auth_code
    auth_code = raw
    if "auth_code=" in raw:
        match = re.search(r"auth_code=([^&]+)", raw)
        if match:
            auth_code = match.group(1)

    if not auth_code:
        print("   Could not extract auth_code. Please try again.")
        return

    print(f"\n   Auth code: {auth_code[:20]}...")

    # Step 4: Exchange auth code for access token
    # Fyers v3 requires SHA-256 hash of `app_id:secret`
    app_id_hash = hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()

    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code,
    }

    print("\n5. Exchanging auth code for access token...")
    try:
        r = requests.post(_TOKEN_URL, json=payload, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"   Error: {e}")
        return

    if r.status_code != 200 or data.get("s") != "ok":
        print(f"   Failed: {data}")
        print("\n   Common causes:")
        print("   - Auth code expired (valid for ~60 seconds — retry from step 3)")
        print("   - Wrong Secret Key")
        print("   - App permissions not set (need Quotes & Historical Data)")
        return

    access_token = data.get("access_token")
    if not access_token:
        print(f"   No access_token in response: {data}")
        return

    print("\n" + "=" * 60)
    print("  SUCCESS!")
    print("=" * 60)
    print(f"\nFYERS_APP_ID={app_id}")
    print(f"FYERS_ACCESS_TOKEN={access_token}")

    # Step 5: Offer to update .env
    print("\n" + "-" * 60)
    update = input("Update .env file automatically? (y/n): ").strip().lower()
    if update == "y":
        _update_env(app_id, access_token)
        print("\n   .env updated! Restart your Flask app (python run.py).")
    else:
        print("\n   Add these lines to your .env manually:")
        print(f"   FYERS_APP_ID={app_id}")
        print(f"   FYERS_ACCESS_TOKEN={access_token}")
        print(f"   MARKET_DATA_PROVIDER=fyers")
        print(f"   MARKET_DATA_FALLBACK=yfinance")

    print("\nDone!")


def _update_env(app_id: str, token: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        print("   .env not found — create it manually.")
        return

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Update or append each key
    updates = {
        "FYERS_APP_ID": app_id,
        "FYERS_ACCESS_TOKEN": token,
        "MARKET_DATA_PROVIDER": "fyers",
        "MARKET_DATA_FALLBACK": "yfinance",
    }
    found_keys = set()
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            found_keys.add(key)
        else:
            new_lines.append(line)

    # Append any keys not already in .env
    for key, val in updates.items():
        if key not in found_keys:
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


if __name__ == "__main__":
    main()
