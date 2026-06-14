#!/usr/bin/env python3
"""
Dᴇɴɪᴀʟ Spreader Bot

Telegram bot for user account messaging, contacts, welcome sequences,
and broadcasts.

Features
--------
* Admin-gated access: only the admin (8390398005) can approve users.
* Forced join of @JayHCode before any feature can be used.
* New users get a free 1-hour trial after clicking "Request Approval".
* Admin can approve, reject, ban, and unban users.
* `/send` automatically broadcasts to ALL groups the account has joined.
* `/send_inbox` sends a DM to ALL users currently in the account's inbox.
* `/set_welcome` runs an auto-reply sequence on first DM from a user
  (no duplicate welcome to the same user).
* Persistent MTProto user session (Telethon) + aiogram bot UI.

Uses Telethon for the MTProto user client + aiogram for the bot interface.
"""

import os
import json
import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, List

import aiosqlite
from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
)

from aiogram import Bot, Dispatcher, F, html
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web

# ----------------------------------------------------------------------
# Configuration / Constants
# ----------------------------------------------------------------------
load_dotenv()

ADMIN_ID = 8390398005                  # Admin's Telegram user id
ADMIN_USERNAME = "@ItsJayHong"           # Used in some prompts only
REQUIRED_CHANNEL = "@JayHCode"         # Channel users must join
REQUIRED_CHANNEL_URL = "https://t.me/JayHCode"
START_IMAGE_URL = (
    "https://graph.org/file/937aaae992e37899ea348-7089a962cc8fc5edeb.jpg"
)
BOT_DISPLAY_NAME = "Dᴇɴɪᴀʟ Spreader Bot"
TRIAL_DURATION_SECONDS = 60 * 60       # 1 hour free trial
TRIAL_DURATION_LABEL = "1 hour"

# "Dᴇɴɪᴀʟ" (Unicode small caps) used for branding in messages
DENIAL_FONT = "Dᴇɴɪᴀʟ"

print("=== Dᴇɴɪᴀʟ Spreader Bot startup ===")
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

print(f"BOT_TOKEN present: {bool(BOT_TOKEN)} (len={len(BOT_TOKEN) if BOT_TOKEN else 0})")
print(f"API_ID present: {bool(API_ID_STR)} value='{API_ID_STR}'")
print(f"API_HASH present: {bool(API_HASH)}")
print(f"PORT: {os.getenv('PORT', '8080 (default)')}")

missing = []
if not BOT_TOKEN:
    missing.append("BOT_TOKEN")
if not API_ID_STR:
    missing.append("API_ID")
if not API_HASH:
    missing.append("API_HASH")
if missing:
    sys.stderr.write(
        f"CRITICAL: Missing env vars: {', '.join(missing)}\n"
        f"  Set BOT_TOKEN, API_ID, API_HASH in your hosting environment.\n"
    )
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
except ValueError:
    sys.stderr.write(f"CRITICAL: API_ID must be int. Got '{API_ID_STR}'\n")
    sys.exit(1)

print("All required environment variables loaded successfully.")
print("========================================")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("denial_bot")


# ----------------------------------------------------------------------
# Font helper: Dᴇɴɪᴀʟ-style Unicode small caps
# ----------------------------------------------------------------------
_SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ",
    "g": "ɢ", "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ",
    "s": "ꜱ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
}


def d(text):
    """Apply Dᴇɴɪᴀʟ-style Unicode small caps to ``text``.

    Lowercase ASCII letters are mapped to their small-cap Unicode
    equivalents; uppercase letters and non-letters pass through
    unchanged. HTML tags <...> and URLs are passed through verbatim.
    """
    if not isinstance(text, str):
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "<":
            j = text.find(">", i + 1)
            if j != -1 and (j - i) >= 2 and (text[i + 1].isalpha() or text[i + 1] == "/"):
                out.append(text[i:j + 1])
                i = j + 1
                continue
        out.append(_SMALL_CAPS_MAP.get(ch, ch))
        i += 1
    return "".join(out)


# ----------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------
DB_PATH = "bot_data.db"

login_clients: Dict[int, TelegramClient] = {}
active_group_broadcasts: Dict[int, asyncio.Task] = {}
active_inbox_broadcasts: Dict[int, asyncio.Task] = {}
user_clients: Dict[int, TelegramClient] = {}


# ----------------------------------------------------------------------
# FSM States
# ----------------------------------------------------------------------
class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class WelcomeStates(StatesGroup):
    waiting_messages = State()
    waiting_sticker = State()
    waiting_delay = State()


class GroupBroadcastStates(StatesGroup):
    waiting_message = State()
    waiting_delay = State()


class InboxBroadcastStates(StatesGroup):
    waiting_message = State()
    waiting_delay = State()


# ----------------------------------------------------------------------
# User access enum + DB
# ----------------------------------------------------------------------
class UserStatus(str, Enum):
    NONE = "none"             # never asked
    PENDING = "pending"       # awaiting admin approval
    REJECTED = "rejected"
    TRIAL = "trial"           # currently in 1h free trial
    APPROVED = "approved"
    BANNED = "banned"


async def init_db():
    """Initialize SQLite database with required tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                phone TEXT,
                session TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS welcome_settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                messages TEXT DEFAULT '[]',
                sticker TEXT,
                delay_between REAL DEFAULT 1.5,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS welcomed_users (
                chat_id INTEGER,
                user_id INTEGER,
                welcomed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                chat_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'none',
                trial_expires_at TEXT,
                requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                decided_by INTEGER,
                username TEXT,
                note TEXT
            )
            """
        )
        await db.commit()
    logger.info("Database initialized.")


# ----------------------------------------------------------------------
# User session (Telegram account) helpers
# ----------------------------------------------------------------------
async def save_user(chat_id: int, phone: str, session: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO users (chat_id, phone, session, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, phone, session, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_user_session(chat_id: int) -> tuple[Optional[str], Optional[str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, session FROM users WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return (row[0], row[1]) if row else (None, None)


async def delete_user(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        await db.commit()


# ----------------------------------------------------------------------
# Welcome settings helpers
# ----------------------------------------------------------------------
async def save_welcome_settings(
    chat_id: int, enabled: bool, messages: list, sticker: Optional[str],
    delay_between: float,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO welcome_settings
                (chat_id, enabled, messages, sticker, delay_between, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                1 if enabled else 0,
                json.dumps(messages or []),
                sticker,
                delay_between,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_welcome_settings(chat_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled, messages, sticker, delay_between "
            "FROM welcome_settings WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "enabled": bool(row[0]),
                "messages": json.loads(row[1]) if row[1] else [],
                "sticker": row[2],
                "delay_between": row[3] or 1.5,
            }


async def is_user_welcomed(chat_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM welcomed_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None


async def mark_user_welcomed(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO welcomed_users (chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id),
        )
        await db.commit()


async def clear_welcomed_users(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM welcomed_users WHERE chat_id = ?", (chat_id,))
        await db.commit()


async def delete_welcome_settings(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM welcome_settings WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM welcomed_users WHERE chat_id = ?", (chat_id,))
        await db.commit()


# ----------------------------------------------------------------------
# Approval system helpers
# ----------------------------------------------------------------------
async def get_user_record(chat_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, status, trial_expires_at, requested_at, "
            "decided_at, decided_by, username, note "
            "FROM approvals WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "chat_id": row[0],
                "status": row[1],
                "trial_expires_at": row[2],
                "requested_at": row[3],
                "decided_at": row[4],
                "decided_by": row[5],
                "username": row[6],
                "note": row[7],
            }


async def upsert_user_request(
    chat_id: int,
    status: str,
    trial_expires_at: Optional[str] = None,
    username: Optional[str] = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        # If a row already exists, keep requested_at; otherwise set it now
        async with db.execute(
            "SELECT requested_at FROM approvals WHERE chat_id = ?", (chat_id,)
        ) as cur:
            existing = await cur.fetchone()

        requested_at = (
            existing[0] if existing else datetime.now(timezone.utc).isoformat()
        )
        await db.execute(
            """
            INSERT INTO approvals
                (chat_id, status, trial_expires_at, requested_at, username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                status = excluded.status,
                trial_expires_at = excluded.trial_expires_at,
                username = COALESCE(excluded.username, approvals.username)
            """,
            (chat_id, status, trial_expires_at, requested_at, username),
        )
        await db.commit()


async def decide_user(
    chat_id: int,
    new_status: str,
    decided_by: int,
    note: Optional[str] = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE approvals SET
                status = ?,
                decided_at = ?,
                decided_by = ?,
                note = ?
            WHERE chat_id = ?
            """,
            (
                new_status,
                datetime.now(timezone.utc).isoformat(),
                decided_by,
                note,
                chat_id,
            ),
        )
        await db.commit()


async def list_by_status(status: str) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, status, trial_expires_at, requested_at, username "
            "FROM approvals WHERE status = ? ORDER BY requested_at ASC",
            (status,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "chat_id": r[0],
                    "status": r[1],
                    "trial_expires_at": r[2],
                    "requested_at": r[3],
                    "username": r[4],
                }
                for r in rows
            ]


def trial_active(record: Optional[Dict]) -> bool:
    if not record:
        return False
    if record["status"] != UserStatus.TRIAL.value:
        return False
    exp = record.get("trial_expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(exp)
    except ValueError:
        return False
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < exp_dt


def is_authorized(record: Optional[Dict]) -> bool:
    """True if user is approved or has an active trial."""
    if not record:
        return False
    if record["status"] == UserStatus.BANNED.value:
        return False
    if record["status"] == UserStatus.APPROVED.value:
        return True
    if record["status"] == UserStatus.TRIAL.value and trial_active(record):
        return True
    return False


def is_banned(record: Optional[Dict]) -> bool:
    return bool(record and record["status"] == UserStatus.BANNED.value)


# ----------------------------------------------------------------------
# Channel-membership check via aiogram Bot
# ----------------------------------------------------------------------
async def user_joined_channel(user_id: int) -> bool:
    """Check if user_id has joined the required channel."""
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        # member.status: creator, administrator, member, left, kicked, restricted
        if member.status in ("left", "kicked"):
            return False
        return True
    except Exception as e:
        # If the bot itself is not in the channel we cannot verify
        logger.warning(f"Channel membership check failed for {user_id}: {e}")
        # Fall back to True so we don't permanently lock users out if bot
        # lacks channel admin rights.
        return True


# ----------------------------------------------------------------------
# Telethon helpers (welcome events + persistent clients)
# ----------------------------------------------------------------------
async def attach_welcome_handler(owner_chat_id: int, client: TelegramClient):
    try:
        client.remove_event_handler(None, events.NewMessage)
    except Exception:
        pass

    @client.on(events.NewMessage(incoming=True))
    async def welcome_handler(event):
        if not event.is_private:
            return
        try:
            sender = await event.get_sender()
            if sender and (
                getattr(sender, "bot", False) or sender.id == owner_chat_id
            ):
                return
        except Exception:
            pass

        user_id = event.sender_id
        if not user_id:
            return

        settings = await get_welcome_settings(owner_chat_id)
        if not settings or not settings.get("enabled"):
            return

        # Skip if we already welcomed this user (no duplicate welcome sequence)
        if await is_user_welcomed(owner_chat_id, user_id):
            return

        messages = settings.get("messages", []) or []
        sticker = settings.get("sticker")
        delay = float(settings.get("delay_between", 1.5))

        try:
            for idx, text in enumerate(messages):
                if text and text.strip():
                    await client.send_message(user_id, text)
                if idx < len(messages) - 1 and delay > 0:
                    await asyncio.sleep(delay)
            if sticker:
                try:
                    await client.send_file(user_id, sticker)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Failed welcome sticker to {user_id}: {e}")

            await mark_user_welcomed(owner_chat_id, user_id)

            try:
                await bot.send_message(
                    owner_chat_id,
                    f"{d('✅ Welcome sequence sent to new user ')}"
                    f"<code>{user_id}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(
                f"Error in welcome handler for owner {owner_chat_id} -> "
                f"{user_id}: {e}"
            )

    logger.info(f"Attached welcome handler for chat_id={owner_chat_id}")


async def start_persistent_client(
    chat_id: int, session_str: Optional[str] = None
) -> Optional[TelegramClient]:
    if session_str is None:
        _, session_str = await get_user_session(chat_id)
    if not session_str:
        return None

    if chat_id in user_clients:
        try:
            await user_clients[chat_id].disconnect()
        except Exception:
            pass
        user_clients.pop(chat_id, None)

    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.start()
        if not await client.is_user_authorized():
            logger.warning(f"Persistent client {chat_id} not authorized.")
            await client.disconnect()
            await delete_user(chat_id)
            return None

        settings = await get_welcome_settings(chat_id)
        if settings and settings.get("enabled"):
            await attach_welcome_handler(chat_id, client)

        user_clients[chat_id] = client
        logger.info(f"Started persistent client for chat_id={chat_id}")
        return client
    except Exception as e:
        logger.error(f"Failed to start persistent client for {chat_id}: {e}")
        return None


async def get_authorized_client(chat_id: int) -> Optional[TelegramClient]:
    if chat_id in user_clients:
        client = user_clients[chat_id]
        try:
            if client.is_connected() and await client.is_user_authorized():
                return client
            await client.connect()
            if await client.is_user_authorized():
                return client
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            user_clients.pop(chat_id, None)

    phone, session_str = await get_user_session(chat_id)
    if not session_str:
        return None

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        logger.warning(f"On-demand client {chat_id} not authorized.")
        await client.disconnect()
        await delete_user(chat_id)
        return None
    return client


async def extract_contacts_numbers(client: TelegramClient) -> list:
    result = await client(GetContactsRequest(hash=0))
    numbers = []
    for u in result.users:
        if u.phone:
            phone = u.phone.strip()
            if not phone.startswith("+"):
                phone = "+" + phone
            numbers.append(phone)
    seen, unique = set(), []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


# ----------------------------------------------------------------------
# Auto-fetch targets: joined groups & inbox users
# ----------------------------------------------------------------------
async def get_joined_groups(client: TelegramClient) -> list:
    """Return entities of all groups/channels the account has joined."""
    try:
        await client.get_dialogs(limit=200)
    except Exception:
        pass

    targets = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if dialog.is_group or dialog.is_channel:
            targets.append(ent)
    return targets


async def get_inbox_users(client: TelegramClient) -> list:
    """Return non-bot user entities that have a DM history with the account."""
    try:
        await client.get_dialogs(limit=200)
    except Exception:
        pass

    targets = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if dialog.is_user and not getattr(ent, "bot", False):
            targets.append(ent)
    return targets


# ----------------------------------------------------------------------
# Broadcast runners
# ----------------------------------------------------------------------
async def run_group_broadcast(
    chat_id: int, message: str, delay: float
):
    """Continuously broadcast to every group the account has joined."""
    client = None
    cycle = 0
    try:
        client = await get_authorized_client(chat_id)
        if not client:
            await bot.send_message(
                chat_id, d("❌ Session lost. Group broadcast stopped.")
            )
            return

        while chat_id in active_group_broadcasts:
            try:
                groups = await get_joined_groups(client)
            except Exception as e:
                await bot.send_message(
                    chat_id, f"{d('❌ Could not list groups: ')}{str(e)[:120]}"
                )
                return

            if not groups:
                await bot.send_message(
                    chat_id,
                    d("❌ No joined groups found. Join some groups first."),
                )
                return

            cycle += 1
            for idx, ent in enumerate(groups):
                if chat_id not in active_group_broadcasts:
                    break
                try:
                    await client.send_message(ent, message)
                except FloodWaitError as e:
                    try:
                        await bot.send_message(
                            chat_id,
                            f"{d('⏳ Flood wait sleeping ')}{e.seconds}{d('s')}",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    try:
                        await bot.send_message(
                            chat_id,
                            f"{d('⚠️ Error sending to group: ')}{str(e)[:80]}",
                        )
                    except Exception:
                        pass

                if idx < len(groups) - 1 and delay > 0:
                    await asyncio.sleep(delay)

            if chat_id not in active_group_broadcasts:
                break

            if cycle % 5 == 0:
                try:
                    await bot.send_message(
                        chat_id,
                        f"{d('🔄 Group broadcast cycle #')}{cycle}{d(' done ')}"
                        f"{d('(')}{len(groups)}{d(' groups).')}",
                    )
                except Exception:
                    pass

            await asyncio.sleep(delay)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        try:
            await bot.send_message(chat_id, f"{d('❌ Group broadcast crashed: ')}{e}")
        except Exception:
            pass
    finally:
        if client and chat_id not in user_clients:
            try:
                await client.disconnect()
            except Exception:
                pass
        active_group_broadcasts.pop(chat_id, None)
        try:
            await bot.send_message(
                chat_id, d("🛑 Group broadcast has been stopped.")
            )
        except Exception:
            pass


async def run_inbox_broadcast(
    chat_id: int, message: str, delay: float
):
    """Send a DM to every user that is currently in the account's inbox."""
    client = None
    try:
        client = await get_authorized_client(chat_id)
        if not client:
            await bot.send_message(
                chat_id, d("❌ Session lost. Inbox broadcast stopped.")
            )
            return

        try:
            users = await get_inbox_users(client)
        except Exception as e:
            await bot.send_message(
                chat_id, f"{d('❌ Could not list inbox users: ')}{str(e)[:120]}"
            )
            return

        if not users:
            await bot.send_message(
                chat_id,
                d("❌ No inbox users found. Open some DMs first so they appear."),
            )
            return

        await bot.send_message(
            chat_id,
            f"{d('📨 Sending DM to ')}{len(users)}{d(' inbox user(s)...')}",
        )

        sent, failed = 0, 0
        for idx, ent in enumerate(users):
            if chat_id not in active_inbox_broadcasts:
                break
            try:
                await client.send_message(ent, message)
                sent += 1
            except FloodWaitError as e:
                await bot.send_message(
                    chat_id, f"{d('⏳ Flood wait sleeping ')}{e.seconds}{d('s')}"
                )
                await asyncio.sleep(e.seconds)
                try:
                    await client.send_message(ent, message)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1

            if idx < len(users) - 1 and delay > 0:
                await asyncio.sleep(delay)

        await bot.send_message(
            chat_id,
            f"{d('✅ Inbox broadcast finished.\n')}"
            f"{d('Sent: ')}{sent}{d('\nFailed: ')}{failed}",
        )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        try:
            await bot.send_message(chat_id, f"{d('❌ Inbox broadcast crashed: ')}{e}")
        except Exception:
            pass
    finally:
        if client and chat_id not in user_clients:
            try:
                await client.disconnect()
            except Exception:
                pass
        active_inbox_broadcasts.pop(chat_id, None)


# ----------------------------------------------------------------------
# Authorization gate (admin/trial/channel)
# ----------------------------------------------------------------------
async def gate(message: Message, require_session: bool = True) -> bool:
    """
    Centralized authorization check. Returns True if the user can
    continue, False if a notice was already sent.

    Order:
        1. Admin bypasses everything.
        2. Banned users get a rejection.
        3. Non-approved users get the approval-request screen.
        4. Approved/trial users must join REQUIRED_CHANNEL.
        5. If a feature requires a logged-in Telegram account, also
           check that a session exists.
    """
    cid = message.chat.id if message else 0
    rec = await get_user_record(cid)

    # Admin always allowed
    if cid == ADMIN_ID:
        return True

    if is_banned(rec):
        try:
            await message.answer(
                f"{d('🚫 You are ')}<b>{d('banned')}</b>{d(' from ')}{DENIAL_FONT}{d(' Spreader Bot.\n\n')}"
                f"{d('Contact the admin (')}{ADMIN_USERNAME}{d(') if you think this ')}"
                f"{d('is a mistake.')}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False

    if not is_authorized(rec):
        # Not approved and trial expired/inactive
        await show_approval_screen(message)
        return False

    # Authorized: check channel
    joined = await user_joined_channel(cid)
    if not joined:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        "📢 Join " + REQUIRED_CHANNEL,
                        url=REQUIRED_CHANNEL_URL,
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ I've joined", callback_data="check_channel"
                    )
                ],
            ]
        )
        await message.answer(
            f"{d('🔒 You must join ')}<b>{REQUIRED_CHANNEL}</b>{d(' to use ')}"
            f"{DENIAL_FONT}{d(' Spreader Bot.\n\n')}"
            f"{d('Join here: ')}{REQUIRED_CHANNEL_URL}{d('\n')}"
            f"{d('Then tap ')}<b>{d("I've joined")}</b>{d('.')}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return False

    if require_session:
        _, session = await get_user_session(cid)
        if not session:
            await message.answer(
                f"{d('❌ Please /login first to use your Telegram account.\n\n')}"
                f"{d('Use ')}{REQUIRED_CHANNEL_URL}{d(' for support.')}",
                parse_mode="HTML",
            )
            return False
    return True


async def show_approval_screen(message: Message):
    """Send the 'Request Approval' welcome image + button."""
    rec = await get_user_record(message.chat.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    "🙋 Request Approval",
                    callback_data="request_approval",
                )
            ]
        ]
    )

    text_lines = [
        f"👋 Welcome to <b>{DENIAL_FONT} Spreader Bot</b>!",
        "",
        f"This bot is <b>admin-approved only</b>.",
        f"Click the button below to send your request to the admin.",
        "",
        f"✅ After approval you get full access.",
        f"🎁 New users also get a free {TRIAL_DURATION_LABEL} trial.",
        f"📢 You must also join {REQUIRED_CHANNEL} to use the bot.",
    ]
    if rec and rec["status"] == UserStatus.PENDING.value:
        text_lines.append("\n⏳ Your approval request is <b>pending</b>.")
    elif rec and rec["status"] == UserStatus.REJECTED.value:
        text_lines.append(
            "\n❌ Your previous request was <b>rejected</b>. "
            "You can submit a new one."
        )

    text = "\n".join(text_lines)

    try:
        await message.answer_photo(
            photo=START_IMAGE_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        # Fallback if image fails to load
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ----------------------------------------------------------------------
# Bot handlers
# ----------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    cid = message.chat.id

    # Admin sees a richer dashboard
    if cid == ADMIN_ID:
        await message.answer_photo(
            photo=START_IMAGE_URL,
            caption=(
                f"{d('👑 Welcome, ')}<b>{d('Admin')}</b>{d('.\n\n')}"
                f"{d('You control ')}<b>{DENIAL_FONT}{d(' Spreader Bot')}</b>{d('.\n\n')}"
                f"{d('Admin commands:\n')}"
                f"{d('/pending - Pending approval requests\n')}"
                f"{d('/approved - Approved users\n')}"
                f"{d('/banned - Banned users\n')}"
                f"{d('/users - All known users\n')}"
                f"{d('/approve &lt;id&gt; - Approve user\n')}"
                f"{d('/reject &lt;id&gt; - Reject user\n')}"
                f"{d('/ban &lt;id&gt; - Ban user\n')}"
                f"{d('/unban &lt;id&gt; - Unban user\n\n')}"
                f"{d('User commands:\n')}"
                f"{d('/login /logout /status /send /send_inbox\n')}"
                f"{d('/set_welcome /list_welcome /clear_welcome\n')}"
                f"{d('/get_contacts /stop /cancel')}"
            ),
            parse_mode="HTML",
        )
        return

    # Gate everything else through the approval system
    rec = await get_user_record(cid)
    if is_banned(rec):
        await message.answer(
            f"{d('🚫 You are banned from ')}{DENIAL_FONT}{d(' Spreader Bot.')}",
            parse_mode="HTML",
        )
        return
    if not is_authorized(rec):
        await show_approval_screen(message)
        return

    # Authorized: still check channel
    if not await user_joined_channel(cid):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    "📢 Join " + REQUIRED_CHANNEL,
                    url=REQUIRED_CHANNEL_URL)],
                [InlineKeyboardButton(
                    "✅ I've joined", callback_data="check_channel")],
            ]
        )
        await message.answer_photo(
            photo=START_IMAGE_URL,
            caption=(
                f"{d('👋 Welcome back to ')}<b>{DENIAL_FONT}{d(' Spreader Bot')}</b>{d('!\n\n')}"
                f"{d('🔒 Please join ')}<b>{REQUIRED_CHANNEL}</b>{d(' to continue.\n\n')}"
                f"{d('Then tap ')}<b>{d("I've joined")}</b>{d('.')}"
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    await message.answer_photo(
        photo=START_IMAGE_URL,
        caption=(
            f"{d('👋 Welcome to ')}<b>{DENIAL_FONT}{d(' Spreader Bot')}</b>{d('!\n\n')}"
            f"{d('Use the menu below or type /help.\n\n')}"
            f"{d('/login - Log in your Telegram account\n')}"
            f"{d('/send - Broadcast to ALL your joined groups\n')}"
            f"{d('/send_inbox - DM ALL users in your inbox\n')}"
            f"{d('/set_welcome - Auto welcome new DMs\n')}"
            f"{d('/get_contacts - Export contacts\n')}"
            f"{d('/status - Show current status\n')}"
            f"{d('/stop - Stop running broadcast\n')}"
            f"{d('/cancel - Cancel current flow')}"
        ),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not await gate(message, require_session=False):
        return
    # Re-use cmd_start's user-facing welcome by sending help directly
    if message.chat.id == ADMIN_ID:
        await cmd_start(message)
    else:
        await message.answer(
            f"<b>{DENIAL_FONT}{d(' Spreader Bot')}</b>{d(' — Help\n\n')}"
            f"{d('/login - Log in your Telegram account (phone + OTP + 2FA)\n')}"
            f"{d('/logout - Log out & wipe session\n')}"
            f"{d('/status - Show current status\n')}"
            f"{d('/send - Broadcast to ALL groups your account has joined\n')}"
            f"{d('/send_inbox - Send a DM to ALL users in your inbox\n')}"
            f"{d('/stop - Stop active broadcasts\n')}"
            f"{d('/set_welcome - Set up auto-welcome sequence (no duplicates)\n')}"
            f"{d('/list_welcome - Show welcome settings\n')}"
            f"{d('/clear_welcome - Disable welcome sequence\n')}"
            f"{d('/get_contacts - Export contacts (numbers only)\n')}"
            f"{d('/cancel - Cancel current setup flow')}",
            parse_mode="HTML",
        )


# ----------------------------------------------------------------------
# Callback handlers (approval + channel check)
# ----------------------------------------------------------------------
@dp.callback_query(F.data == "request_approval")
async def cb_request_approval(callback: CallbackQuery):
    cid = callback.message.chat.id if callback.message else callback.from_user.id
    user = callback.from_user
    rec = await get_user_record(cid)

    # Already banned -> reject silently
    if is_banned(rec):
        await callback.answer("You are banned.", show_alert=True)
        return

    # If user already has an active trial or approved, just acknowledge
    if is_authorized(rec):
        await callback.answer("You already have access.", show_alert=True)
        return

    # Create / refresh the request and grant 1-hour trial
    trial_until = (
        datetime.now(timezone.utc) + timedelta(seconds=TRIAL_DURATION_SECONDS)
    ).isoformat()
    await upsert_user_request(
        chat_id=cid,
        status=UserStatus.PENDING.value,
        trial_expires_at=trial_until,
        username=user.username or user.full_name,
    )

    # Notify admin with approve/reject inline buttons
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"adm_approve:{cid}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"adm_reject:{cid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🚫 Ban",
                    callback_data=f"adm_ban:{cid}",
                ),
            ],
        ]
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"{d('📨 ')}<b>{d('New approval request')}</b>{d('\n\n')}"
            f"{d('👤 Name: ')}{html.quote(user.full_name)}{d('\n')}"
            f"{d('🆔 ID: ')}<code>{user.id}</code>{d('\n')}"
            f"{d('🔗 Username: ')}"
            f"{('@' + user.username) if user.username else '(none)'}{d('\n\n')}"
            f"{d('User has been granted a ')}{TRIAL_DURATION_LABEL}{d(' free trial ')}"
            f"{d('while pending. Approve for full access.')}",
            parse_mode="HTML",
            reply_markup=admin_kb,
        )
    except Exception as e:
        logger.error(f"Could not notify admin: {e}")

    # Acknowledge the user with channel join + trial info
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                "📢 Join " + REQUIRED_CHANNEL,
                url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton(
                "✅ I've joined", callback_data="check_channel")],
        ]
    )
    await callback.message.edit_caption(
        caption=(
            f"{d('✅ ')}<b>{d('Request sent!')}</b>{d('\n\n')}"
            f"{d('The admin has been notified. You now have a free ')}"
            f"{TRIAL_DURATION_LABEL}{d(' trial.\n\n')}"
            f"{d('📢 You must also join ')}<b>{REQUIRED_CHANNEL}</b>{d(' to use ')}"
            f"{d('the bot: ')}{REQUIRED_CHANNEL_URL}{d('\n\n')}"
            f"{d('Click ')}<b>{d("I've joined")}</b>{d(' once you have joined.')}"
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "check_channel")
async def cb_check_channel(callback: CallbackQuery):
    cid = callback.from_user.id
    rec = await get_user_record(cid)

    if is_banned(rec):
        await callback.answer("You are banned.", show_alert=True)
        return
    if not is_authorized(rec):
        await callback.answer(
            "Your approval/trial is not active. Send /start.",
            show_alert=True,
        )
        return

    if await user_joined_channel(cid):
        try:
            await callback.message.edit_caption(
                caption=(
                    f"{d('✅ Channel join confirmed!\n\n')}"
                    f"{d('You can now use ')}{DENIAL_FONT}{d(' Spreader Bot.\n')}"
                    f"{d('Type /help for the command list.')}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                f"{d('✅ Channel join confirmed. You can now use ')}"
                f"{DENIAL_FONT}{d(' Spreader Bot. Type /help.')}",
                parse_mode="HTML",
            )
        await callback.answer("Verified ✅")
    else:
        await callback.answer(
            f"You haven't joined {REQUIRED_CHANNEL} yet.", show_alert=True
        )


# Admin inline buttons
@dp.callback_query(F.data.startswith("adm_approve:"))
async def cb_admin_approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Admins only.", show_alert=True)
        return
    target = int(callback.data.split(":", 1)[1])
    await decide_user(target, UserStatus.APPROVED.value, ADMIN_ID)
    rec = await get_user_record(target)
    uname = (
        f" (@{rec['username']})" if rec and rec.get("username") else ""
    )
    try:
        await bot.send_message(
            target,
            f"{d('🎉 You have been ')}<b>{d('approved')}</b>{d('!\n\n')}"
            f"{d('You now have full access to ')}{DENIAL_FONT}{d(' Spreader Bot.\n')}"
            f"{d('Type /start to begin.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"{d('✅ Approved user ')}<code>{target}</code>{uname}{d('.')}",
        parse_mode="HTML",
    )
    await callback.answer("Approved.")


@dp.callback_query(F.data.startswith("adm_reject:"))
async def cb_admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Admins only.", show_alert=True)
        return
    target = int(callback.data.split(":", 1)[1])
    await decide_user(target, UserStatus.REJECTED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('❌ Your access request was ')}<b>{d('rejected')}</b>{d('.\n\n')}"
            f"{d('Contact the admin (')}{ADMIN_USERNAME}{d(') if needed.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"{d('❌ Rejected user ')}<code>{target}</code>{d('.')}")
    await callback.answer("Rejected.")


@dp.callback_query(F.data.startswith("adm_ban:"))
async def cb_admin_ban(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Admins only.", show_alert=True)
        return
    target = int(callback.data.split(":", 1)[1])
    await decide_user(target, UserStatus.BANNED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('🚫 You have been ')}<b>{d('banned')}</b>{d(' from ')}"
            f"{DENIAL_FONT}{d(' Spreader Bot.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"{d('🚫 Banned user ')}<code>{target}</code>{d('.')}")
    await callback.answer("Banned.")


# ----------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------
def admin_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.chat.id != ADMIN_ID:
            await message.answer(d("Admins only."))
            return
        return await func(message, *args, **kwargs)
    return wrapper


@dp.message(Command("pending"))
@admin_only
async def cmd_pending(message: Message):
    rows = await list_by_status(UserStatus.PENDING.value)
    if not rows:
        await message.answer(d("No pending requests."))
        return
    lines = ["📨 <b>Pending approval requests</b>\n"]
    for r in rows:
        exp = ""
        if r["trial_expires_at"]:
            exp_dt = datetime.fromisoformat(
                r["trial_expires_at"].replace("Z", "+00:00")
            )
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            remain = exp_dt - datetime.now(timezone.utc)
            mins = max(0, int(remain.total_seconds() // 60))
            exp = f" — trial ends in {mins}m"
        uname = (
            f" (@{r['username']})" if r.get("username") else ""
        )
        lines.append(f"• <code>{r['chat_id']}</code>{uname}{exp}")
    await message.answer(d("\n").join(lines), parse_mode="HTML")


@dp.message(Command("approved"))
@admin_only
async def cmd_approved(message: Message):
    rows = await list_by_status(UserStatus.APPROVED.value)
    if not rows:
        await message.answer(d("No approved users yet."))
        return
    lines = ["✅ <b>Approved users</b>\n"]
    for r in rows:
        uname = (
            f" (@{r['username']})" if r.get("username") else ""
        )
        lines.append(f"• <code>{r['chat_id']}</code>{uname}")
    await message.answer(d("\n").join(lines), parse_mode="HTML")


@dp.message(Command("banned"))
@admin_only
async def cmd_banned(message: Message):
    rows = await list_by_status(UserStatus.BANNED.value)
    if not rows:
        await message.answer(d("No banned users."))
        return
    lines = ["🚫 <b>Banned users</b>\n"]
    for r in rows:
        uname = (
            f" (@{r['username']})" if r.get("username") else ""
        )
        lines.append(f"• <code>{r['chat_id']}</code>{uname}")
    await message.answer(d("\n").join(lines), parse_mode="HTML")


@dp.message(Command("users"))
@admin_only
async def cmd_users(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT a.chat_id, a.status, a.username "
            "FROM approvals a ORDER BY a.status, a.chat_id"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer(d("No users yet."))
        return
    lines = ["👥 <b>All users</b>\n"]
    for cid, status, uname in rows:
        u = f" (@{uname})" if uname else ""
        lines.append(f"• <code>{cid}</code>{u} — {status}")
    await message.answer(d("\n").join(lines), parse_mode="HTML")


def _admin_id_from_msg(message: Message) -> Optional[int]:
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@dp.message(Command("approve"))
@admin_only
async def cmd_approve(message: Message):
    target = _admin_id_from_msg(message)
    if not target:
        await message.answer(d("Usage: /approve <chat_id>"))
        return
    rec = await get_user_record(target)
    if not rec:
        await upsert_user_request(target, UserStatus.APPROVED.value)
    else:
        await decide_user(target, UserStatus.APPROVED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('🎉 You have been ')}<b>{d('approved')}</b>{d(' by the admin!\n\n')}"
            f"{d('You now have full access to ')}{DENIAL_FONT}{d(' Spreader Bot.\n')}"
            f"{d('Type /start to begin.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(f"{d('✅ Approved ')}<code>{target}</code>{d('.')}")


@dp.message(Command("reject"))
@admin_only
async def cmd_reject(message: Message):
    target = _admin_id_from_msg(message)
    if not target:
        await message.answer(d("Usage: /reject <chat_id>"))
        return
    await decide_user(target, UserStatus.REJECTED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('❌ Your access request was ')}<b>{d('rejected')}</b>{d('.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(f"{d('❌ Rejected ')}<code>{target}</code>{d('.')}")


@dp.message(Command("ban"))
@admin_only
async def cmd_ban(message: Message):
    target = _admin_id_from_msg(message)
    if not target:
        await message.answer(d("Usage: /ban <chat_id>"))
        return
    await decide_user(target, UserStatus.BANNED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('🚫 You have been ')}<b>{d('banned')}</b>{d(' from ')}"
            f"{DENIAL_FONT}{d(' Spreader Bot.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(f"{d('🚫 Banned ')}<code>{target}</code>{d('.')}")


@dp.message(Command("unban"))
@admin_only
async def cmd_unban(message: Message):
    target = _admin_id_from_msg(message)
    if not target:
        await message.answer(d("Usage: /unban <chat_id>"))
        return
    # Unban => re-approve (admin must explicitly re-approve)
    await decide_user(target, UserStatus.APPROVED.value, ADMIN_ID)
    try:
        await bot.send_message(
            target,
            f"{d('✅ You have been ')}<b>{d('unbanned')}</b>{d(' by the admin. ')}"
            f"{d('You can use ')}{DENIAL_FONT}{d(' Spreader Bot again.')}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(f"{d('✅ Unbanned ')}<code>{target}</code>{d('.')}")


# ----------------------------------------------------------------------
# User commands (gated)
# ----------------------------------------------------------------------
@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not await gate(message, require_session=False):
        return
    cid = message.chat.id
    phone, session = await get_user_session(cid)
    rec = await get_user_record(cid)

    lines = []
    if session:
        lines.append(f"✅ Logged in as: {phone or 'unknown'}")
    else:
        lines.append("❌ Not logged in.")

    if rec:
        st = rec["status"]
        if st == UserStatus.APPROVED.value:
            lines.append("🟢 Access: APPROVED")
        elif st == UserStatus.TRIAL.value:
            if trial_active(rec):
                exp = datetime.fromisoformat(
                    rec["trial_expires_at"].replace("Z", "+00:00")
                )
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                mins = max(
                    0,
                    int((exp - datetime.now(timezone.utc)).total_seconds() // 60),
                )
                lines.append(
                    f"🎁 Access: FREE TRIAL (expires in {mins} min)"
                )
            else:
                lines.append(
                    "⏳ Access: TRIAL EXPIRED — waiting for admin approval"
                )
        elif st == UserStatus.PENDING.value:
            lines.append("⏳ Access: PENDING admin approval")
        elif st == UserStatus.REJECTED.value:
            lines.append("❌ Access: REJECTED — send /start to retry")
        elif st == UserStatus.BANNED.value:
            lines.append("🚫 Access: BANNED")

    if cid in active_group_broadcasts:
        lines.append("📡 Group broadcast: RUNNING (/stop to halt)")
    if cid in active_inbox_broadcasts:
        lines.append("📨 Inbox broadcast: RUNNING (/stop to halt)")

    s = await get_welcome_settings(cid)
    if s and s.get("enabled"):
        lines.append(
            f"👋 Welcome auto-reply: ON "
            f"({len(s.get('messages', []))} msgs, "
            f"sticker: {'yes' if s.get('sticker') else 'no'})"
        )
    else:
        lines.append("👋 Welcome auto-reply: off")

    lines.append(f"📢 Channel join: {'✅' if await user_joined_channel(cid) else '❌'}")
    await message.answer(d("\n").join(lines))


@dp.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext):
    if not await gate(message, require_session=False):
        return
    cid = message.chat.id
    if cid in login_clients:
        try:
            await login_clients[cid].disconnect()
        except Exception:
            pass
        login_clients.pop(cid, None)

    if cid in active_group_broadcasts:
        active_group_broadcasts[cid].cancel()
        active_group_broadcasts.pop(cid, None)
    if cid in active_inbox_broadcasts:
        active_inbox_broadcasts[cid].cancel()
        active_inbox_broadcasts.pop(cid, None)

    if cid in user_clients:
        try:
            await user_clients[cid].disconnect()
        except Exception:
            pass
        user_clients.pop(cid, None)

    await delete_user(cid)
    await state.clear()
    await message.answer(
        d("✅ Logged out. Session, broadcasts, and welcome listener ")
         + d("have been stopped.")
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    cid = message.chat.id
    current = await state.get_state()
    if current:
        if cid in login_clients:
            try:
                await login_clients[cid].disconnect()
            except Exception:
                pass
            login_clients.pop(cid, None)
        await state.clear()
        await message.answer(
            d("❌ Operation cancelled."), reply_markup=ReplyKeyboardRemove()
        )
    elif cid in active_group_broadcasts or cid in active_inbox_broadcasts:
        if cid in active_group_broadcasts:
            active_group_broadcasts[cid].cancel()
        if cid in active_inbox_broadcasts:
            active_inbox_broadcasts[cid].cancel()
        await message.answer(d("🛑 Broadcast stop requested."))
    else:
        await message.answer(d("No active operation to cancel."))


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if not await gate(message, require_session=False):
        return
    cid = message.chat.id
    stopped_any = False
    if cid in active_group_broadcasts:
        active_group_broadcasts[cid].cancel()
        stopped_any = True
    if cid in active_inbox_broadcasts:
        active_inbox_broadcasts[cid].cancel()
        stopped_any = True
    await message.answer(
        d("🛑 Stop signal sent.")
        if stopped_any
        else d("No active broadcast to stop.")
    )


# ----------------------------------------------------------------------
# Login flow
# ----------------------------------------------------------------------
@dp.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    if not await gate(message, require_session=False):
        return
    cid = message.chat.id

    if cid in login_clients:
        try:
            await login_clients[cid].disconnect()
        except Exception:
            pass
        login_clients.pop(cid, None)

    await state.set_state(LoginStates.waiting_phone)
    await message.answer(
        d("📱 Send your phone number in international format (with +).\n")
         + d("Example: +12345678901\n\n")
         + d("We will send a login code to your Telegram account."),
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(LoginStates.waiting_phone, F.text)
async def process_phone(message: Message, state: FSMContext):
    cid = message.chat.id
    phone = message.text.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
        await message.answer(
            d("❌ Invalid phone format. Must start with + and contain only ")
             + d("digits. Example: +12345678901")
        )
        return
    await state.update_data(phone=phone)
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        login_clients[cid] = client
        await state.update_data(phone_code_hash=sent.phone_code_hash)
        await state.set_state(LoginStates.waiting_code)
        await message.answer(
            d("✅ Login code has been sent (check your Telegram app).\n\n")
             + d("Reply with the code (digits only).\n")
             + d("Use /cancel to abort.")
        )
    except PhoneNumberInvalidError:
        await _cleanup_login(cid)
        await state.clear()
        await message.answer(d("❌ Invalid phone number."))
    except FloodWaitError as e:
        await _cleanup_login(cid)
        await state.clear()
        await message.answer(f"{d('⏳ Flood wait: please wait ')}{e.seconds}{d('s.')}")
    except Exception as e:
        await _cleanup_login(cid)
        await state.clear()
        await message.answer(f"{d('❌ Error: ')}{e}")
        logger.error(f"Login phone error for {cid}: {e}")


@dp.message(LoginStates.waiting_code, F.text)
async def process_code(message: Message, state: FSMContext):
    cid = message.chat.id
    code = message.text.strip().replace(" ", "")
    if not code.isdigit() or len(code) < 4:
        await message.answer(d("❌ Code must be numeric (5-6 digits)."))
        return
    data = await state.get_data()
    phone = data.get("phone")
    pch = data.get("phone_code_hash")
    client = login_clients.get(cid)
    if not client or not phone or not pch:
        await message.answer(d("❌ Session expired. /login again."))
        await state.clear()
        await _cleanup_login(cid)
        return
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=pch)
        session_str = client.session.save()
        await save_user(cid, phone, session_str)
        await client.disconnect()
        login_clients.pop(cid, None)
        await start_persistent_client(cid)
        await state.clear()
        await message.answer(
            f"{d('✅ Login successful!\n\n')}"
            f"{d('You can now use all features of ')}"
            f"{DENIAL_FONT}{d(' Spreader Bot.')}"
        )
    except SessionPasswordNeededError:
        await state.set_state(LoginStates.waiting_password)
        await message.answer(
            d("🔐 2FA is enabled. Enter your cloud password ")
             + d("(NOT your SMS code).")
        )
    except PhoneCodeInvalidError:
        await message.answer(d("❌ Invalid code. Try again or /cancel."))
    except PhoneCodeExpiredError:
        await message.answer(d("❌ Code expired. /cancel and /login again."))
    except Exception as e:
        await message.answer(f"{d('❌ Login error: ')}{e}")
        logger.error(f"Login code error for {cid}: {e}")
        await _cleanup_login(cid)
        await state.clear()


@dp.message(LoginStates.waiting_password, F.text)
async def process_password(message: Message, state: FSMContext):
    cid = message.chat.id
    password = message.text.strip()
    if not password:
        await message.answer(d("❌ Password cannot be empty."))
        return
    data = await state.get_data()
    phone = data.get("phone")
    client = login_clients.get(cid)
    if not client or not phone:
        await message.answer(d("❌ Session expired. /login again."))
        await state.clear()
        await _cleanup_login(cid)
        return
    try:
        await client.sign_in(password=password)
        session_str = client.session.save()
        await save_user(cid, phone, session_str)
        await client.disconnect()
        login_clients.pop(cid, None)
        await start_persistent_client(cid)
        await state.clear()
        await message.answer(d("✅ Login successful with 2FA!"))
    except PasswordHashInvalidError:
        await message.answer(d("❌ Incorrect 2FA password. Try again."))
    except Exception as e:
        await message.answer(f"{d('❌ 2FA error: ')}{e}")
        logger.error(f"Login password error for {cid}: {e}")
        await _cleanup_login(cid)
        await state.clear()


async def _cleanup_login(cid: int):
    if cid in login_clients:
        try:
            await login_clients[cid].disconnect()
        except Exception:
            pass
        login_clients.pop(cid, None)


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------
@dp.message(Command("get_contacts"))
async def cmd_get_contacts(message: Message):
    if not await gate(message):
        return
    cid = message.chat.id
    client = await get_authorized_client(cid)
    if not client:
        await message.answer(d("❌ Session invalid. /login again."))
        return
    try:
        await message.answer(d("⏳ Fetching contacts..."))
        numbers = await extract_contacts_numbers(client)
        if cid not in user_clients:
            await client.disconnect()
        if not numbers:
            await message.answer(d("No contacts with phone numbers found."))
            return
        data = {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "total_contacts_with_phone": len(numbers),
            "numbers": numbers,
        }
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        if len(json_str) < 3500:
            await message.answer(
                f"{d('📋 ')}<b>{d('Contacts (numbers only):')}</b>{d('\n\n')}"
                f"<pre><code class=\"json\">{html.quote(json_str)}"
                f"</code></pre>{d('\n\nTotal: ')}{len(numbers)}",
                parse_mode="HTML",
            )
        else:
            document = BufferedInputFile(
                json_str.encode("utf-8"), filename="contacts_numbers.json"
            )
            await message.answer_document(
                document,
                caption=f"📋 Contacts JSON. Total: {len(numbers)} numbers.",
            )
    except FloodWaitError as e:
        await message.answer(f"{d('⏳ Flood wait: ')}{e.seconds}{d('s')}")
        try:
            await client.disconnect()
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"{d('❌ Error: ')}{e}")
        logger.error(f"get_contacts error for {cid}: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass


# ----------------------------------------------------------------------
# /send — broadcasts to ALL joined groups automatically
# ----------------------------------------------------------------------
@dp.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if not await gate(message):
        return
    cid = message.chat.id
    if cid in active_group_broadcasts:
        await message.answer(
            d("⚠️ A group broadcast is already running. /stop first.")
        )
        return
    await state.set_state(GroupBroadcastStates.waiting_message)
    await message.answer(
        f"{d('📡 ')}<b>{d('Group broadcast')}</b>{d('\n\n')}"
        f"{d('This will repeatedly send the same message to ')}"
        f"<b>{d('every group your account has joined')}</b>{d(' until you /stop.\n\n')}"
        f"{d('Send the message you want to broadcast.')}",
        parse_mode="HTML",
    )


@dp.message(GroupBroadcastStates.waiting_message, F.text)
async def process_group_message(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer(d("❌ Message cannot be empty."))
        return
    await state.update_data(message=text)
    await state.set_state(GroupBroadcastStates.waiting_delay)
    await message.answer(
        d("✅ Message received.\n\n")
         + d("Now send the <b>delay in seconds</b> between sends.\n")
         + d("Recommended: 5–15 seconds.\n")
         + d("Enter 0 for no delay.")
    )


@dp.message(GroupBroadcastStates.waiting_delay, F.text)
async def process_group_delay(message: Message, state: FSMContext):
    cid = message.chat.id
    try:
        delay = float(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.answer(d("❌ Invalid delay. Enter a non-negative number."))
        return
    data = await state.get_data()
    text = data.get("message", "")
    await state.clear()
    if not text:
        await message.answer(d("❌ Session lost. /send again."))
        return
    if cid in active_group_broadcasts:
        await message.answer(d("⚠️ Already running. /stop first."))
        return
    task = asyncio.create_task(run_group_broadcast(cid, text, delay))
    active_group_broadcasts[cid] = task
    await message.answer(
        f"{d('🚀 ')}<b>{d('Group broadcast STARTED')}</b>{d('\n\n')}"
        f"{d('Targets: ALL groups your account has joined\n')}"
        f"{d('Delay: ')}{delay}{d('s\n')}"
        f"{d('Mode: loops forever until /stop.')}",
        parse_mode="HTML",
    )


# ----------------------------------------------------------------------
# /send_inbox — DMs every user in the inbox
# ----------------------------------------------------------------------
@dp.message(Command("send_inbox"))
async def cmd_send_inbox(message: Message, state: FSMContext):
    if not await gate(message):
        return
    cid = message.chat.id
    if cid in active_inbox_broadcasts:
        await message.answer(
            d("⚠️ An inbox broadcast is already running. /stop first.")
        )
        return
    await state.set_state(InboxBroadcastStates.waiting_message)
    await message.answer(
        f"{d('📨 ')}<b>{d('Inbox broadcast')}</b>{d('\n\n')}"
        f"{d('This will DM ')}<b>{d('every user currently in your Telegram ')}"
        f"{d('inbox')}</b>{d(' (your existing private chat history).\n\n')}"
        f"{d('Send the message you want to deliver.')}",
        parse_mode="HTML",
    )


@dp.message(InboxBroadcastStates.waiting_message, F.text)
async def process_inbox_message(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer(d("❌ Message cannot be empty."))
        return
    await state.update_data(message=text)
    await state.set_state(InboxBroadcastStates.waiting_delay)
    await message.answer(
        d("✅ Message received.\n\n")
         + d("Now send the <b>delay in seconds</b> between DMs.\n")
         + d("Recommended: 5–15 seconds.")
    )


@dp.message(InboxBroadcastStates.waiting_delay, F.text)
async def process_inbox_delay(message: Message, state: FSMContext):
    cid = message.chat.id
    try:
        delay = float(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.answer(d("❌ Invalid delay."))
        return
    data = await state.get_data()
    text = data.get("message", "")
    await state.clear()
    if not text:
        await message.answer(d("❌ Session lost. /send_inbox again."))
        return
    if cid in active_inbox_broadcasts:
        await message.answer(d("⚠️ Already running. /stop first."))
        return
    task = asyncio.create_task(run_inbox_broadcast(cid, text, delay))
    active_inbox_broadcasts[cid] = task
    await message.answer(
        f"{d('🚀 ')}<b>{d('Inbox broadcast STARTED')}</b>{d('\n\n')}"
        f"{d('Targets: ALL users in your inbox\n')}"
        f"{d('Delay: ')}{delay}{d('s')}",
        parse_mode="HTML",
    )


# ----------------------------------------------------------------------
# Welcome sequence
# ----------------------------------------------------------------------
@dp.message(Command("set_welcome"))
async def cmd_set_welcome(message: Message, state: FSMContext):
    if not await gate(message):
        return
    await state.set_state(WelcomeStates.waiting_messages)
    await state.update_data(welcome_messages=[])
    await message.answer(
        d("✉️ <b>Welcome Message Setup</b>\n\n")
         + d("Send the <b>first</b> welcome message.\n")
         + d("Then second, third, etc.\n")
         + d("When done, reply <b>/done</b>.\n")
         + d("Use /cancel to abort."),
        parse_mode="HTML",
    )


@dp.message(WelcomeStates.waiting_messages, F.text)
async def process_welcome_message(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.lower() in ("/done", "done"):
        data = await state.get_data()
        msgs = data.get("welcome_messages", [])
        if not msgs:
            await message.answer(
                d("❌ You need at least one message. ")
                 + d("Send a message or /cancel.")
            )
            return
        await state.update_data(welcome_messages=msgs)
        await state.set_state(WelcomeStates.waiting_sticker)
        await message.answer(
            f"{d('✅ Saved ')}{len(msgs)}{d(' welcome message(s).\n\n')}"
             + d("Now send a <b>sticker</b> (or type <b>skip</b> / <b>no</b>).")
        )
        return
    data = await state.get_data()
    msgs = data.get("welcome_messages", [])
    msgs.append(text)
    await state.update_data(welcome_messages=msgs)
    await message.answer(
        f"{d('✅ Message #')}{len(msgs)}{d(' saved. Send next or /done.')}"
    )


@dp.message(WelcomeStates.waiting_sticker, F.text | F.sticker)
async def process_welcome_sticker(message: Message, state: FSMContext):
    sticker_id = None
    if message.sticker:
        sticker_id = message.sticker.file_id
        await message.answer(d("✅ Sticker saved."))
    else:
        txt = message.text.strip().lower()
        if txt in ("skip", "no", "none"):
            await message.answer(d("✅ No sticker."))
        else:
            await message.answer(d("Please send a sticker or type 'skip'."))
            return
    await state.update_data(sticker=sticker_id)
    await state.set_state(WelcomeStates.waiting_delay)
    await message.answer(
        d("Now send the <b>delay in seconds</b> between welcome messages ")
         + d("(e.g. 1.5 or 2)."),
        parse_mode="HTML",
    )


@dp.message(WelcomeStates.waiting_delay, F.text)
async def process_welcome_delay(message: Message, state: FSMContext):
    cid = message.chat.id
    try:
        delay = float(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.answer(d("❌ Invalid number. e.g. 1.5"))
        return
    data = await state.get_data()
    messages = data.get("welcome_messages", [])
    sticker = data.get("sticker")
    await state.clear()
    await save_welcome_settings(cid, True, messages, sticker, delay)

    if cid in user_clients:
        try:
            await user_clients[cid].disconnect()
        except Exception:
            pass
        user_clients.pop(cid, None)
    client = await start_persistent_client(cid)
    if client:
        await message.answer(
            f"{d('✅ ')}<b>{d('Welcome sequence activated!')}</b>{d('\n\n')}"
            f"{d('Messages: ')}{len(messages)}{d('\n')}"
            f"{d('Sticker: ')}{'yes' if sticker else 'no'}{d('\n')}"
            f"{d('Delay: ')}{delay}{d('s\n\n')}"
            f"{d('Each new DM (once) will receive this sequence.')}",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            d("✅ Settings saved, but listener failed to start. ")
             + d("/logout and /login again.")
        )


@dp.message(Command("list_welcome"))
async def cmd_list_welcome(message: Message):
    if not await gate(message, require_session=False):
        return
    s = await get_welcome_settings(message.chat.id)
    if not s:
        await message.answer(d("No welcome settings. /set_welcome to add."))
        return
    msgs = s.get("messages", [])
    text = (
        f"<b>Welcome Settings</b> "
        f"({'✅ ENABLED' if s.get('enabled') else '❌ DISABLED'})\n\n"
        f"Delay: {s.get('delay_between', 1.5)}s\n"
        f"Sticker: {'Yes' if s.get('sticker') else 'No'}\n\n"
        f"<b>Messages:</b>\n"
    )
    for i, m in enumerate(msgs, 1):
        text += f"{i}. {m[:100]}{'...' if len(m) > 100 else ''}\n"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("clear_welcome"))
async def cmd_clear_welcome(message: Message):
    if not await gate(message, require_session=False):
        return
    cid = message.chat.id
    await delete_welcome_settings(cid)
    if cid in user_clients:
        try:
            await user_clients[cid].disconnect()
        except Exception:
            pass
        user_clients.pop(cid, None)
    await start_persistent_client(cid)
    await message.answer(
        d("✅ Welcome settings cleared and history reset. Auto-welcome is OFF.")
    )


# ----------------------------------------------------------------------
# Error handler + Web server
# ----------------------------------------------------------------------
@dp.error()
async def error_handler(event, exception):
    logger.error(f"Update {event} caused error: {exception}")


async def health_handler(request):
    return web.json_response(
        {
            "status": "ok",
            "service": "denial-spreader-bot",
            "bot": BOT_DISPLAY_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ping", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(
        f"🌐 Web server listening on http://0.0.0.0:{port} "
        f"(/, /health, /ping)"
    )


async def main():
    await init_db()

    web_task = asyncio.create_task(start_web_server())

    # Reconnect all previously logged-in users' persistent clients
    reconnected = 0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT chat_id FROM users") as cur:
                rows = await cur.fetchall()
                for (cid,) in rows:
                    try:
                        if await start_persistent_client(cid):
                            reconnected += 1
                    except Exception as e:
                        logger.error(
                            f"Failed to start persistent client for "
                            f"{cid}: {e}"
                        )
        logger.info(
            f"Reconnected {reconnected} persistent user client(s) on startup."
        )
    except Exception as e:
        logger.error(f"Error reconnecting persistent clients: {e}")

    logger.info("🤖 Starting Dᴇɴɪᴀʟ Spreader Bot polling...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        web_task.cancel()
        try:
            await web_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        logger.info("Bot stopped by user.")
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
