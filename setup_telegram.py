"""
setup_telegram.py — Run once to authenticate your Telegram account.
Creates a .session file that the web app reuses permanently.

Usage:
    python setup_telegram.py
"""

import os, sys
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

def main():
    load_dotenv()
    api_id   = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session  = os.getenv("SESSION_NAME", "nexora_session")

    if not api_id or not api_hash:
        print("\n❌  API_ID or API_HASH not found in .env\n")
        sys.exit(1)

    print("\n──────────────────────────────────────")
    print("  Nexora Pips — Telegram Setup")
    print("──────────────────────────────────────\n")

    with TelegramClient(session, int(api_id), api_hash) as client:
        if client.is_user_authorized():
            me = client.get_me()
            print(f"✅  Already authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or '—'})")
            print(f"    Telegram ID: {me.id}")
            print(f"\n    Session file: {session}.session")
            print("\nAll good — start the app with:  python app.py\n")
            return

        phone = input("Enter your phone number (e.g. +94711234567): ").strip()
        client.send_code_request(phone)
        code = input("Enter the code Telegram sent you: ").strip()
        try:
            client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input("2FA password: ").strip()
            client.sign_in(password=pw)

        me = client.get_me()
        print(f"\n✅  Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or '—'})")
        print(f"    Session saved: {session}.session")
        print("\nNow start the app with:  python app.py\n")

if __name__ == "__main__":
    main()
