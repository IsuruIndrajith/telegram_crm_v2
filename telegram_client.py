"""
telegram_client.py — Telethon async wrapper (v2)
Supports: text messages, images with captions, links in text.
Runs a persistent background event loop thread safe for Flask.
"""

import asyncio
import threading
import time
import os
import random
import math
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError,
    InputUserDeactivatedError, PeerFloodError,
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError,
    ChatAdminRequiredError,
)
from telethon.tl.types import Channel as TLChannel, InputPeerUser


class TelegramManager:
    def __init__(self):
        self._loop   = None
        self._client = None
        self._ready  = False
        self._error  = None

    # ──────────────────────── Lifecycle ────────────────────────────

    def start(self, session_name: str, api_id: int, api_hash: str):
        """Call once at app startup — spins up the background event loop."""
        self._session_name = session_name
        self._api_id       = api_id
        self._api_hash     = api_hash

        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

        for _ in range(30):          # wait up to 15 s
            if self._ready or self._error:
                break
            time.sleep(0.5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_client())
        self._loop.run_forever()

    async def _init_client(self):
        try:
            self._client = TelegramClient(
                self._session_name, self._api_id, self._api_hash
            )
            await self._client.connect()
            self._ready = True
        except Exception as e:
            self._error = str(e)
            self._ready = True

    def _run(self, coro, timeout: int = 30):
        """Submit a coroutine to the background loop, block until done."""
        if not self._loop:
            raise RuntimeError("TelegramManager not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ─────────────────────── Auth status ───────────────────────────

    def is_authorized(self) -> bool:
        try:
            return self._run(self._client.is_user_authorized())
        except Exception:
            return False

    def get_me(self) -> dict | None:
        try:
            me = self._run(self._client.get_me())
            if me:
                return {
                    "id": me.id,
                    "first_name": me.first_name or "",
                    "last_name":  me.last_name  or "",
                    "username":   me.username   or "",
                    "phone":      me.phone      or "",
                }
        except Exception:
            pass
        return None

    def stop(self):
        """Disconnect the Telegram client (called on session logout)."""
        try:
            if self._client:
                self._run(self._client.disconnect(), timeout=10)
        except Exception:
            pass
        self._ready = False
        self._client = None

    # ─────────────────────── Web-based auth ────────────────────────

    def send_code(self, phone: str) -> dict:
        """
        Step 1 of web login: send OTP to the phone number.
        Returns {"ok": True, "phone_code_hash": "..."} or {"ok": False, "error": "..."}.
        """
        try:
            result = self._run(self._client.send_code_request(phone), timeout=30)
            return {"ok": True, "phone_code_hash": result.phone_code_hash}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def sign_in(self, phone: str, code: str, phone_code_hash: str) -> dict:
        """
        Step 2 of web login: verify OTP code.
        Returns {"ok": True} or {"ok": False, "need_password": True} or {"ok": False, "error": "..."}.
        """
        try:
            self._run(
                self._client.sign_in(phone, code, phone_code_hash=phone_code_hash),
                timeout=30
            )
            return {"ok": True}
        except SessionPasswordNeededError:
            return {"ok": False, "need_password": True}
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return {"ok": False, "error": "Invalid or expired code. Please try again."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def sign_in_2fa(self, password: str) -> dict:
        """
        Step 3 (optional): provide 2FA password if required.
        Returns {"ok": True} or {"ok": False, "error": "..."}.
        """
        try:
            self._run(self._client.sign_in(password=password), timeout=30)
            return {"ok": True}
        except PasswordHashInvalidError:
            return {"ok": False, "error": "Incorrect 2FA password. Please try again."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ─────────────────────── Lookup user ───────────────────────────

    def get_entity_info(self, identifier) -> dict | None:
        try:
            entity = self._run(self._fetch_entity(identifier))
            if entity:
                name = " ".join(filter(None, [
                    getattr(entity, "first_name", ""),
                    getattr(entity, "last_name",  ""),
                ])).strip() or getattr(entity, "username", str(identifier))
                return {
                    "id":       entity.id,
                    "name":     name,
                    "username": getattr(entity, "username", ""),
                }
        except Exception:
            pass
        return None

    async def _fetch_entity(self, identifier):
        return await self._client.get_entity(identifier)

    # ─────────────────────── Group member scraping ─────────────────

    def get_group_members(self, identifier: str, limit: int = 1000) -> list:
        """
        Fetch members from a group or channel the user belongs to.
        identifier: @username, t.me/link, or https://t.me/+inviteHash
        Returns list of {telegram_id, username, name}.
        Raises RuntimeError with a user-friendly message on failure.
        """
        try:
            members = self._run(self._fetch_participants(identifier, limit), timeout=120)
        except RuntimeError:
            raise
        except ChatAdminRequiredError:
            raise RuntimeError(
                "This is a broadcast channel — only channel admins can fetch the subscriber list. "
                "Try a group/supergroup instead, or use a channel where you are an admin."
            )
        except Exception as e:
            msg = str(e)
            if "No user has" in msg or "Cannot find" in msg or "Could not find" in msg:
                raise RuntimeError(f"'{identifier}' not found. Make sure you are a member and the identifier is correct.")
            if "flood" in msg.lower():
                raise RuntimeError("Telegram flood limit hit. Wait a few minutes and try again.")
            if "admin" in msg.lower():
                raise RuntimeError(
                    "This is a broadcast channel — only admins can fetch subscribers. "
                    "Try a group/supergroup, or a channel where you are an admin."
                )
            if "private" in msg.lower() or "invite" in msg.lower():
                raise RuntimeError("This is a private group. You must be a member to scrape it.")
            raise RuntimeError(msg)

        result = []
        for m in members:
            if getattr(m, "bot", False):
                continue  # skip bots
            name = " ".join(filter(None, [
                getattr(m, "first_name", "") or "",
                getattr(m, "last_name",  "") or "",
            ])).strip() or getattr(m, "username", "") or str(m.id)
            result.append({
                "telegram_id": str(m.id),
                "username":    getattr(m, "username", "") or "",
                "name":        name,
                "access_hash": str(m.access_hash) if getattr(m, "access_hash", None) else "",
            })
        return result

    async def _fetch_participants(self, identifier: str, limit: int):
        entity = await self._client.get_entity(identifier)
        # Broadcast channels (not megagroups) block non-admin participant fetching
        if isinstance(entity, TLChannel) and getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            # Try anyway — succeeds if the user is an admin; raises ChatAdminRequiredError otherwise
            pass
        participants = await self._client.get_participants(entity, limit=limit)
        if len(participants) == 0 and isinstance(entity, TLChannel) and getattr(entity, "broadcast", False):
            raise RuntimeError(
                "This is a broadcast channel — only channel admins can fetch the subscriber list. "
                "Try a group/supergroup instead, or use a channel where you are an admin."
            )
        return participants

    # ─────────────────────── Broadcasting ──────────────────────────

    def run_broadcast(
        self,
        broadcast_id: int,
        clients:      list,
        message:      str,
        db,
        min_delay:    float = 3.0,
        max_delay:    float = 7.0,
        media_path:   str   = None,
        batch_size:   int   = 50,
        batch_pause:  int   = 300,
    ):
        """
        Fire-and-forget — returns immediately.
        Sends in batches of batch_size with a batch_pause second pause between them.
        Progress and phase are written to the DB in real time.
        """
        asyncio.run_coroutine_threadsafe(
            self._broadcast_async(broadcast_id, clients, message, db,
                                  min_delay, max_delay, media_path,
                                  batch_size, batch_pause),
            self._loop
        )

    async def _broadcast_async(self, broadcast_id, clients, message, db,
                               min_delay, max_delay, media_path,
                               batch_size, batch_pause):
        sent = failed = 0
        total_batches = max(1, math.ceil(len(clients) / batch_size))

        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch       = clients[batch_start: batch_start + batch_size]

            db.update_broadcast_phase(broadcast_id, "sending", current_batch=batch_idx + 1)

            for client in batch:
                raw_id      = client.get("telegram_id")
                username    = client.get("username")
                access_hash = client.get("access_hash")

                # Prefer @username (always resolvable), then InputPeerUser with
                # access_hash, then bare numeric ID as last resort.
                if username:
                    identifier = f"@{username}"
                elif raw_id and str(raw_id).lstrip("-").isdigit() and access_hash:
                    try:
                        identifier = InputPeerUser(int(raw_id), int(access_hash))
                    except (ValueError, TypeError):
                        identifier = int(raw_id)
                elif raw_id and str(raw_id).lstrip("-").isdigit():
                    identifier = int(raw_id)
                else:
                    identifier = None

                if identifier is None:
                    db.log_message(broadcast_id, client["id"], "failed", "No valid identifier")
                    failed += 1
                    db.update_broadcast_progress(broadcast_id, sent, failed)
                    continue

                try:
                    if media_path and os.path.isfile(media_path):
                        await self._client.send_file(
                            identifier, media_path,
                            caption=message if message else None,
                        )
                    else:
                        await self._client.send_message(identifier, message)

                    db.log_message(broadcast_id, client["id"], "sent")
                    sent += 1

                except FloodWaitError as e:
                    wait = e.seconds + 5
                    await asyncio.sleep(wait)
                    try:
                        if media_path and os.path.isfile(media_path):
                            await self._client.send_file(identifier, media_path, caption=message or None)
                        else:
                            await self._client.send_message(identifier, message)
                        db.log_message(broadcast_id, client["id"], "sent")
                        sent += 1
                    except Exception as retry_e:
                        db.log_message(broadcast_id, client["id"], "failed", str(retry_e))
                        failed += 1

                except (UserPrivacyRestrictedError, InputUserDeactivatedError) as e:
                    db.log_message(broadcast_id, client["id"], "failed", type(e).__name__)
                    failed += 1

                except PeerFloodError:
                    failed += 1
                    db.log_message(broadcast_id, client["id"], "failed",
                                   "Flood limit reached — broadcast stopped to protect account")
                    db.update_broadcast_progress(broadcast_id, sent, failed)
                    db.abort_broadcast(
                        broadcast_id,
                        "PeerFloodError: Telegram blocked further messages. "
                        "Your account was sending to too many strangers too quickly. "
                        "Wait several hours before trying again and use a longer delay (8–20s)."
                    )
                    return

                except Exception as e:
                    db.log_message(broadcast_id, client["id"], "failed", str(e)[:200])
                    failed += 1

                db.update_broadcast_progress(broadcast_id, sent, failed)
                await asyncio.sleep(random.uniform(min_delay, max_delay))

            # Pause between batches (not after the last one)
            if batch_idx < total_batches - 1:
                resume_at = (datetime.now(timezone.utc) + timedelta(seconds=batch_pause)) \
                            .strftime("%Y-%m-%d %H:%M:%S")
                db.update_broadcast_phase(broadcast_id, "pausing",
                                          current_batch=batch_idx + 1,
                                          resume_at=resume_at)
                await asyncio.sleep(batch_pause)

        db.complete_broadcast(broadcast_id)
