MONTHLY_START_TICKET = 290
import os
import mimetypes
import re
import sys
import asyncio
import csv
import html
import json
import sqlite3
import tempfile
import zipfile
import shutil
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    from telethon import TelegramClient, utils as telethon_utils
except Exception:
    TelegramClient = None
    telethon_utils = None

from betslip import create_betslip_image
from leaderboard import create_monthly_leaderboard_image
from cards import (
    create_settlement_image,
    create_profile_image,
    create_stats_image,
    create_market_image,
    create_market_settlement_image,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "helloworld")
DB_FILE = os.getenv("DB_FILE", "bets.db")
RE_DB_FILE = os.getenv("RE_DB_FILE", "re.db")
USERS_FILE = os.getenv("USERS_FILE", "users.json")
TICKET_CONFIG_FILE = os.getenv("TICKET_CONFIG_FILE", "ticket_config.json")
AVATAR_DIR = os.getenv("AVATAR_DIR", "avatars")
CURRENCY = os.getenv("CURRENCY", "$")
DEFAULT_STAKE = float(os.getenv("DEFAULT_STAKE", "15"))
TICKET_TIMEZONE = os.getenv("TICKET_TIMEZONE", "Europe/London")
LEADERBOARD_TIMEZONE = os.getenv("LEADERBOARD_TIMEZONE", "Europe/London")
TEST_BET_TAG = "TEST BET"
# Real ticket IDs should continue from your normal ledger and ignore accidental huge IDs.
# Default: next real ticket starts after #278 unless bets.db already has a higher normal ticket.
TICKET_SEQUENCE_FORCE_AFTER = int(os.getenv("TICKET_SEQUENCE_FORCE_AFTER", "278") or "0")
TICKET_SEQUENCE_IGNORE_ABOVE = int(os.getenv("TICKET_SEQUENCE_IGNORE_ABOVE", "999999") or "999999")

RECENT_PHOTOS_KEY = "recent_photos"

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DEFAULT_CONDITIONS = [
    "Any retirement = bet void.",
    "Odds are final once accepted.",
    "Void legs are removed from final odds.",
    "Parlay loses if any active leg loses.",
]

# -------------------------
# AI BETSLIP PARSER CONFIG
# -------------------------
# AI_BETSLIP_PROVIDER can be: gemini, venice, off
# Gemini: set GEMINI_API_KEY. Optional GEMINI_MODEL, default gemini-2.0-flash
# Venice: set VENICE_API_KEY. Optional VENICE_MODEL, default llama-3.3-70b
AI_BETSLIP_PROVIDER = os.getenv("AI_BETSLIP_PROVIDER", "venice").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
VENICE_API_KEY = os.getenv("VENICE_API_KEY", "").strip()
VENICE_MODEL = os.getenv("VENICE_MODEL", "zai-org-glm-5-1").strip()
AI_BETSLIP_TIMEOUT = int(os.getenv("AI_BETSLIP_TIMEOUT", "90"))
AI_BETSLIP_DEBUG = os.getenv("AI_BETSLIP_DEBUG", "0").strip() == "1"

AI_ASSISTANT_MAX_CHARS = int(os.getenv("AI_ASSISTANT_MAX_CHARS", "12000"))
AI_ASSISTANT_WEB_SEARCH = os.getenv("AI_ASSISTANT_WEB_SEARCH", "on").strip().lower()
AI_BETSLIP_WEB_SEARCH = os.getenv("AI_BETSLIP_WEB_SEARCH", "on").strip().lower()


def money(x: float) -> str:
    return f"{CURRENCY}{float(x):,.2f}"


def row_value(row, key: str, default=None):
    """Safely read a sqlite row/dict value, even if an older DB is missing a column."""
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
        return default if value is None else value
    except Exception:
        try:
            value = row.get(key, default)
            return default if value is None else value
        except Exception:
            return default


def is_void_bet(row) -> bool:
    """Voided bets return stake, so they must not count in wager volume or stats."""
    return str(row_value(row, "result", "") or "").strip().lower() == "void"


def is_test_bet(row) -> bool:
    """Test bets are saved only for testing tickets/settlement and never count as real action."""
    try:
        return int(row_value(row, "is_test", 0) or 0) == 1
    except Exception:
        return False


def active_bet_rows(rows):
    """Rows used for real betting stats/wager totals. VOID and TEST bets are excluded completely."""
    return [r for r in rows if not is_void_bet(r) and not is_test_bet(r)]


def safe_text(x: str) -> str:
    return html.escape(str(x))


TELEGRAM_TEXT_SAFE_LIMIT = 3600


def split_text_blocks(header: str, blocks: List[str], limit: int = TELEGRAM_TEXT_SAFE_LIMIT) -> List[str]:
    """
    Split a long Telegram HTML message into safe chunks.
    Keeps each bet block intact so HTML tags are not broken.
    """
    header = str(header or "")
    chunks = []
    current = header

    for block in blocks:
        block = str(block or "")

        if len(current) + len(block) <= limit:
            current += block
            continue

        if current.strip():
            chunks.append(current)

        if len(header) + len(block) <= limit:
            current = header + block
        else:
            # Fallback for an unusually huge single block.
            current = header
            for i in range(0, len(block), limit - len(header) - 50):
                part = block[i:i + limit - len(header) - 50]
                chunks.append(header + part)
            current = header

    if current.strip() and current != header:
        chunks.append(current)

    return chunks or [header.strip() or "No data."]


async def reply_html_in_chunks(message, header: str, blocks: List[str], limit: int = TELEGRAM_TEXT_SAFE_LIMIT):
    """Reply with a long HTML list as multiple Telegram-safe messages."""
    chunks = split_text_blocks(header, blocks, limit=limit)

    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        if total > 1:
            chunk = chunk.replace("━━━━━━━━━━━━━━━━━━\n\n", f"━━━━━━━━━━━━━━━━━━\nPage {idx}/{total}\n\n", 1)
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)


def now_str() -> str:
    try:
        tz = ZoneInfo(TICKET_TIMEZONE)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_ticket_datetime() -> datetime:
    try:
        return datetime.now(ZoneInfo(TICKET_TIMEZONE))
    except Exception:
        return datetime.now()


def current_leaderboard_datetime() -> datetime:
    try:
        return datetime.now(ZoneInfo(LEADERBOARD_TIMEZONE))
    except Exception:
        return current_ticket_datetime()


def leaderboard_tz_label() -> str:
    if LEADERBOARD_TIMEZONE == "Europe/London":
        return "BST/London"
    return LEADERBOARD_TIMEZONE


def month_bounds_from_arg(month_arg: str = "") -> Tuple[str, str, str]:
    """
    Return (start_str, end_str, label) for a leaderboard month.
    The window is always first day 00:00:00 up to next month 00:00:00 in LEADERBOARD_TIMEZONE.
    Supported:
    - /monthly = current month in LEADERBOARD_TIMEZONE
    - /monthly 2026-06
    - /monthly 06/2026
    - /monthly last
    """
    raw = str(month_arg or "").strip().lower()
    now = current_leaderboard_datetime()

    if raw in ["last", "previous", "prev"]:
        year = now.year
        month = now.month - 1
        if month <= 0:
            month = 12
            year -= 1
    elif raw:
        m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", raw)
        if not m:
            m = re.fullmatch(r"(\d{1,2})[-/](\d{4})", raw)
            if m:
                month = int(m.group(1))
                year = int(m.group(2))
            else:
                raise ValueError("Use /monthly or /monthly YYYY-MM, example: /monthly 2026-06")
        else:
            year = int(m.group(1))
            month = int(m.group(2))

        if month < 1 or month > 12:
            raise ValueError("Month must be between 1 and 12. Example: /monthly 2026-06")
    else:
        year = now.year
        month = now.month

    start = datetime(year, month, 1, 0, 0, 0)

    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0)

    label = f"{start.strftime('%B %Y')} • 12:00 AM {leaderboard_tz_label()} to 12:00 AM {leaderboard_tz_label()}"
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), label


def normalize_user(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "", str(name).strip().lower())


def normalize_telegram_username(username: str) -> str:
    username = str(username or "").strip()

    if not username:
        return ""

    username = username.replace("https://t.me/", "").replace("t.me/", "").strip()

    if username.startswith("@"):
        username = username[1:]

    username = re.sub(r"[^a-zA-Z0-9_]", "", username)

    if not username:
        return ""

    return f"@{username}"


def display_name_from_key(key: str) -> str:
    key = str(key or "unknown")
    return key[:1].upper() + key[1:]


def parse_float(x: str) -> float:
    return float(str(x).replace(",", "").replace("x", "").replace("$", "").strip())


def parse_money_amount(value: str) -> float:
    raw = str(value or "").strip().replace(",", "").replace("$", "")
    if not raw:
        raise ValueError("Missing amount.")
    return float(raw)


def parse_starting_wager_token(value: str) -> Optional[float]:
    """Parse /add baseline monthly wager tokens like w-$200, w-200, wager:$200."""
    raw = str(value or "").strip().lower().replace(",", "")

    if not raw:
        return None

    m = re.fullmatch(r"(?:w|wager|start|starting)\s*[-:=+]?\s*\$?\s*(-?\d+(?:\.\d+)?)", raw)

    if not m:
        return None

    try:
        amount = float(m.group(1))
    except Exception:
        return None

    # In commands like w-$200, the dash is a separator, not a negative sign.
    # Keep leaderboard starting wager non-negative. Use w-0 to clear it.
    return max(0.0, abs(amount))



def parse_bookie_balance_token(value: str) -> Optional[float]:
    """Parse /add bookie balance tokens like b-$200, bal:200, ledger=-50."""
    raw = str(value or "").strip().lower().replace(",", "")

    if not raw:
        return None

    m = re.fullmatch(r"(?:b|bal|balance|ledger|bookie)\s*[-:=+]?\s*\$?\s*(-?\d+(?:\.\d+)?)", raw)

    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def multiply_odds(legs: List[Dict]) -> float:
    total = 1.0

    for leg in legs:
        odds = float(leg.get("odds") or 0)

        if odds <= 1:
            continue

        total *= odds

    return round(total, 4)


def is_admin(update: Update) -> bool:
    if not ADMIN_IDS:
        return True

    return update.effective_user and update.effective_user.id in ADMIN_IDS


async def try_delete_message(message: Optional[Message]):
    if not message:
        return

    try:
        await message.delete()
    except Exception:
        pass


# -------------------------
# USERS JSON
# -------------------------


def ensure_users_file():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": {}}, f, indent=2)


def load_users() -> Dict:
    ensure_users_file()

    with open(USERS_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = {"users": {}}

    if "users" not in data:
        data["users"] = {}

    return data


def save_users(data: Dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def alias_key(value: str) -> str:
    """Normalize any nickname / @username / display name into a lookup token."""
    return normalize_user(str(value or "").replace("@", ""))


def user_aliases_for_record(key: str, record: Dict) -> List[str]:
    """Return every token that should point to this one canonical user key."""
    aliases = set()
    key_token = alias_key(key)
    if key_token:
        aliases.add(key_token)

    display_token = alias_key(record.get("display", ""))
    if display_token:
        aliases.add(display_token)

    telegram_token = alias_key(record.get("telegram", ""))
    if telegram_token:
        aliases.add(telegram_token)

    for item in record.get("aliases", []) or []:
        item_token = alias_key(item)
        if item_token:
            aliases.add(item_token)

    return sorted(aliases)


def ensure_user_aliases(data: Dict) -> Dict:
    """Add missing aliases fields and keep key/display/@telegram searchable."""
    users = data.setdefault("users", {})
    changed = False

    for key, record in list(users.items()):
        if not isinstance(record, dict):
            users[key] = {"display": display_name_from_key(key), "telegram": "", "aliases": [key], "added_at": now_str()}
            changed = True
            continue

        aliases = set(user_aliases_for_record(key, record))
        if sorted(record.get("aliases", []) or []) != sorted(aliases):
            record["aliases"] = sorted(aliases)
            changed = True

    if changed:
        save_users(data)

    return data


def resolve_user_key(user_key: str, data: Optional[Dict] = None) -> str:
    """
    Resolve nicknames and Telegram usernames to the one canonical users.json key.
    Examples:
      anchor / Anchor / @anchorthis -> anchor
      sand / andrew / @sandrcw      -> andrew
    """
    token = alias_key(user_key)
    if not token:
        return ""

    data = data or load_users()
    users = data.get("users", {})

    # Direct key wins first.
    if token in users:
        return token

    # Then search aliases/display/telegram, case-insensitive.
    for key, record in users.items():
        if token in user_aliases_for_record(key, record):
            return key

    return token


def user_exists(user_key: str) -> bool:
    data = load_users()
    key = resolve_user_key(user_key, data)
    return key in data.get("users", {})


def get_user_record(user_key: str) -> Dict:
    data = load_users()
    key = resolve_user_key(user_key, data)
    return data.get("users", {}).get(key, {})


def is_hidden_user(user_key: str) -> bool:
    record = get_user_record(user_key)
    try:
        return bool(record.get("hidden", False))
    except Exception:
        return False


def set_user_hidden(user_key: str, hidden: bool) -> Tuple[bool, str]:
    data = load_users()
    key = resolve_user_key(user_key, data)

    if not key:
        return False, "Invalid user."

    if key not in data.get("users", {}):
        return False, f"{display_name_from_key(key)} is not in users.json."

    data["users"][key]["hidden"] = bool(hidden)
    save_users(data)

    action = "hidden" if hidden else "visible"
    return True, f"{display_name_from_key(key)} is now {action}."


def delete_user_from_json(user_key: str) -> Tuple[bool, str]:
    data = load_users()
    key = resolve_user_key(user_key, data)

    if not key:
        return False, "Invalid user."

    if key not in data.get("users", {}):
        return False, f"{display_name_from_key(key)} is not in users.json."

    data["users"].pop(key, None)
    save_users(data)

    return True, f"{display_name_from_key(key)} deleted from users.json. Bet history was not deleted."


def preferred_ledger_label(user_key: str) -> str:
    """
    Display the saved alias first.

    In users.json, the canonical user key is the ledger/profile alias
    (for example: melon, arc, zenn). Only fall back to Telegram username
    when there is no saved user/alias record.
    """
    data = load_users()
    resolved = resolve_user_key(user_key, data)
    users = data.get("users", {})

    if resolved and resolved in users:
        return display_name_from_key(resolved)

    record = get_user_record(user_key)
    telegram = str(record.get("telegram", "") or "").strip()
    if telegram:
        return telegram

    return display_name_from_key(resolved or normalize_user(user_key) or "unknown")


def bettor_matches_user(row, target_key: str) -> bool:
    """
    Match an imported or newly-created bet to a users.json account through
    bettor name, alias, display name, or Telegram username.
    """
    target = canonicalize_bettor(target_key)
    if not target:
        return False

    bettor_value = str(row["bettor"] or "").strip()
    username_value = str(row["username"] or "").strip()

    candidates = [bettor_value, username_value, username_value.lstrip("@")]

    for value in candidates:
        if value and canonicalize_bettor(value) == target:
            return True

    # Also compare normalized values against every alias token.
    record = get_user_record(target)
    accepted = set(user_aliases_for_record(target, record))
    accepted.add(alias_key(target))

    return any(alias_key(value) in accepted for value in candidates if value)


def get_user_telegram(user_key: str) -> str:
    record = get_user_record(user_key)
    return str(record.get("telegram", "") or "").strip()


def normalize_avatar_filename(value: str) -> str:
    """Keep avatar file names safe. Use examples like melon.png or users/melon.png is not allowed."""
    value = str(value or "").strip()

    if not value:
        return ""

    # Only allow simple file names inside AVATAR_DIR, not paths like ../../secret.txt.
    value = os.path.basename(value)
    value = re.sub(r"[^a-zA-Z0-9_.-]", "", value)

    if not value.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return ""

    return value[:80]


def get_user_avatar(user_key: str) -> str:
    record = get_user_record(user_key)
    return str(record.get("avatar", "") or "").strip()


def avatar_exists(avatar: str) -> bool:
    avatar = normalize_avatar_filename(avatar)

    if not avatar:
        return False

    return os.path.exists(os.path.join(AVATAR_DIR, avatar))


def add_user_to_json(name: str, telegram_username: str = "", avatar: str = "", starting_wager: Optional[float] = None, bookie_balance: Optional[float] = None) -> Tuple[bool, str]:
    raw_key = normalize_user(name)

    if not raw_key:
        return False, "Invalid user name."

    tg = normalize_telegram_username(telegram_username)
    avatar = normalize_avatar_filename(avatar)
    starting_wager_value = None
    if starting_wager is not None:
        try:
            starting_wager_value = max(0.0, float(starting_wager))
        except Exception:
            starting_wager_value = None

    bookie_balance_value = None
    if bookie_balance is not None:
        try:
            bookie_balance_value = float(bookie_balance)
        except Exception:
            bookie_balance_value = None

    data = load_users()
    ensure_user_aliases(data)
    key = resolve_user_key(raw_key, data)

    # If the nickname/@username already belongs to another user, update that canonical user
    # instead of creating a duplicate ledger.
    if key in data["users"]:
        changed = []
        record = data["users"][key]

        if tg:
            record["telegram"] = tg
            changed.append(tg)

        if avatar:
            record["avatar"] = avatar
            changed.append(f"avatar {avatar}")

        if starting_wager_value is not None:
            record["starting_wager"] = round(starting_wager_value, 2)
            changed.append(f"starting wager {money(starting_wager_value)}")

        if bookie_balance_value is not None:
            record["bookie_balance"] = round(bookie_balance_value, 2)
            changed.append(f"bookie balance {money(bookie_balance_value)}")

        # Always keep the typed name and telegram username as aliases.
        aliases = set(user_aliases_for_record(key, record))
        aliases.add(alias_key(raw_key))
        if tg:
            aliases.add(alias_key(tg))
        aliases.discard("")
        record["aliases"] = sorted(aliases)

        if changed:
            save_users(data)
            msg = f"{display_name_from_key(key)} updated with {', '.join(changed)}."

            if avatar and not avatar_exists(avatar):
                msg += f" Put {avatar} inside {AVATAR_DIR}/ before using the leaderboard image."

            return True, msg

        save_users(data)
        return False, f"{display_name_from_key(key)} already exists. Aliases: {', '.join(record.get('aliases', []))}"

    aliases = {alias_key(raw_key)}
    if tg:
        aliases.add(alias_key(tg))
    aliases.discard("")

    data["users"][key] = {
        "display": display_name_from_key(key),
        "telegram": tg,
        "aliases": sorted(aliases),
        "avatar": avatar,
        "starting_wager": round(starting_wager_value or 0.0, 2),
        "bookie_balance": round(bookie_balance_value or 0.0, 2),
        "hidden": False,
        "added_at": now_str(),
    }

    save_users(data)

    bits = []
    if tg:
        bits.append(tg)
    if avatar:
        bits.append(f"avatar {avatar}")
    if starting_wager_value is not None:
        bits.append(f"starting wager {money(starting_wager_value)}")
    if bookie_balance_value is not None:
        bits.append(f"bookie balance {money(bookie_balance_value)}")

    msg = f"{display_name_from_key(key)} added"
    if bits:
        msg += f" with {', '.join(bits)}"
    msg += "."

    if avatar and not avatar_exists(avatar):
        msg += f" Put {avatar} inside {AVATAR_DIR}/ before using the leaderboard image."

    return True, msg


def add_aliases_to_user(user_key: str, aliases: List[str]) -> Tuple[bool, str]:
    data = load_users()
    ensure_user_aliases(data)
    key = resolve_user_key(user_key, data)

    if not key or key not in data.get("users", {}):
        return False, f"{display_name_from_key(normalize_user(user_key))} is not in users.json."

    cleaned = []
    for item in aliases:
        token = alias_key(item)
        if token and token not in cleaned:
            cleaned.append(token)

    if not cleaned:
        return False, "No valid aliases given."

    # Do not allow an alias that is already attached to another canonical user.
    conflicts = []
    for token in cleaned:
        existing = resolve_user_key(token, data)
        if existing in data.get("users", {}) and existing != key:
            conflicts.append(f"{token} -> {existing}")

    if conflicts:
        return False, "Alias conflict: " + ", ".join(conflicts)

    record = data["users"][key]
    current = set(user_aliases_for_record(key, record))
    before = set(current)
    current.update(cleaned)
    current.discard("")
    record["aliases"] = sorted(current)
    save_users(data)

    added = sorted(current - before)
    if added:
        return True, f"{display_name_from_key(key)} aliases added: {', '.join(added)}"
    return True, f"{display_name_from_key(key)} already had those aliases."


def canonicalize_bettor(value: str) -> str:
    """Use this before saving/searching bets so aliases do not create double ledgers."""
    return resolve_user_key(value)


# -------------------------
# DATABASE
# -------------------------


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def re_db():
    conn = sqlite3.connect(RE_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def is_re_bet_tag(tag: str) -> bool:
    return str(tag or "").strip().lower().replace(" ", "") in {"orbet", "frbet"}


def re_bet_counts_monthly(tag: str) -> bool:
    # ORBET counts towards monthly wager. FRBET is saved in re.db but excluded from monthly wager.
    return str(tag or "").strip().lower().replace(" ", "") == "orbet"


def column_exists(conn, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def add_column_if_missing(conn, table: str, column: str, definition: str):
    if not column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                bettor TEXT,
                stake REAL NOT NULL,
                total_odds REAL NOT NULL,
                payout REAL NOT NULL,
                profit REAL NOT NULL,
                status TEXT DEFAULT 'open',
                result TEXT DEFAULT NULL,
                pnl REAL DEFAULT 0,
                conditions TEXT,
                note TEXT,
                market_id INTEGER DEFAULT NULL,
                source_photo_message_id INTEGER,
                accept_message_id INTEGER,
                slip_message_id INTEGER,
                summary_message_id INTEGER,
                settled_by INTEGER,
                settled_at TEXT,
                created_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bet_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id INTEGER NOT NULL,
                event TEXT,
                selection TEXT NOT NULL,
                market TEXT DEFAULT 'Winner',
                sport TEXT DEFAULT 'other',
                odds REAL NOT NULL,
                result TEXT DEFAULT 'open',
                FOREIGN KEY (bet_id) REFERENCES bets(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                bettor TEXT,
                stake REAL NOT NULL,
                total_odds REAL NOT NULL,
                payout REAL NOT NULL,
                profit REAL NOT NULL,
                status TEXT DEFAULT 'open',
                result TEXT DEFAULT NULL,
                pnl REAL DEFAULT 0,
                conditions TEXT,
                note TEXT,
                bet_tag TEXT,
                source_photo_message_id INTEGER,
                accept_message_id INTEGER,
                slip_message_id INTEGER,
                summary_message_id INTEGER,
                settled_by INTEGER,
                settled_at TEXT,
                created_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_bet_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_bet_id INTEGER NOT NULL,
                event TEXT,
                selection TEXT NOT NULL,
                market TEXT DEFAULT 'Winner',
                sport TEXT DEFAULT 'other',
                odds REAL NOT NULL,
                result TEXT DEFAULT 'open',
                FOREIGN KEY (test_bet_id) REFERENCES test_bets(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                odds REAL NOT NULL,
                rules TEXT,
                status TEXT DEFAULT 'open',
                result TEXT DEFAULT NULL,
                market_message_id INTEGER,
                created_by INTEGER,
                created_at TEXT,
                settled_by INTEGER,
                settled_at TEXT
            )
            """
        )

        add_column_if_missing(conn, "bets", "bettor", "TEXT")
        add_column_if_missing(conn, "bets", "source_photo_message_id", "INTEGER")
        add_column_if_missing(conn, "bets", "accept_message_id", "INTEGER")
        add_column_if_missing(conn, "bets", "slip_message_id", "INTEGER")
        add_column_if_missing(conn, "bets", "summary_message_id", "INTEGER")
        add_column_if_missing(conn, "bets", "settled_by", "INTEGER")
        add_column_if_missing(conn, "bets", "settled_at", "TEXT")
        add_column_if_missing(conn, "bets", "note", "TEXT")
        add_column_if_missing(conn, "bets", "conditions", "TEXT")
        add_column_if_missing(conn, "bets", "market_id", "INTEGER")
        add_column_if_missing(conn, "bets", "bet_tag", "TEXT")
        add_column_if_missing(conn, "bets", "is_test", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "bet_legs", "sport", "TEXT DEFAULT 'other'")
        add_column_if_missing(conn, "test_bet_legs", "sport", "TEXT DEFAULT 'other'")



def init_re_db():
    """Separate re-ledger for ORBET/FRBET tickets. First ticket id is #0."""
    with re_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS re_bets (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                bettor TEXT,
                stake REAL NOT NULL,
                total_odds REAL NOT NULL,
                payout REAL NOT NULL,
                profit REAL NOT NULL,
                status TEXT DEFAULT 'open',
                result TEXT DEFAULT NULL,
                pnl REAL DEFAULT 0,
                conditions TEXT,
                note TEXT,
                bet_tag TEXT,
                wager_counts_monthly INTEGER DEFAULT 0,
                source_photo_message_id INTEGER,
                accept_message_id INTEGER,
                slip_message_id INTEGER,
                summary_message_id INTEGER,
                settled_by INTEGER,
                settled_at TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS re_bet_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                re_bet_id INTEGER NOT NULL,
                event TEXT,
                selection TEXT NOT NULL,
                market TEXT DEFAULT 'Winner',
                sport TEXT DEFAULT 'other',
                odds REAL NOT NULL,
                result TEXT DEFAULT 'open',
                FOREIGN KEY (re_bet_id) REFERENCES re_bets(id)
            )
            """
        )
        add_column_if_missing(conn, "re_bets", "bet_tag", "TEXT")
        add_column_if_missing(conn, "re_bets", "wager_counts_monthly", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "re_bet_legs", "sport", "TEXT DEFAULT 'other'")


def allocate_next_re_ticket_id(conn) -> int:
    max_id = conn.execute("SELECT MAX(id) FROM re_bets").fetchone()[0]
    if max_id is None:
        return 0
    return int(max_id) + 1


def create_re_bet(
    chat_id: int,
    admin_user_id: int,
    admin_username: str,
    bettor: str,
    stake: float,
    total_odds: float,
    legs: List[Dict],
    source_photo_message_id: int,
    accept_message_id: int,
    conditions_list: Optional[List[str]] = None,
    note: Optional[str] = None,
    bet_tag: str = "",
) -> int:
    payout = round(stake * total_odds, 2)
    profit = round(payout - stake, 2)
    conditions_list = conditions_list or DEFAULT_CONDITIONS
    conditions = "\n".join(conditions_list)
    clean_tag = clean_bet_tag(bet_tag)

    with re_db() as conn:
        next_ticket_id = allocate_next_re_ticket_id(conn)
        cur = conn.execute(
            """
            INSERT INTO re_bets
            (
                id, chat_id, user_id, username, bettor,
                stake, total_odds, payout, profit,
                conditions, note, bet_tag, wager_counts_monthly,
                source_photo_message_id, accept_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_ticket_id,
                chat_id,
                admin_user_id,
                admin_username,
                bettor,
                stake,
                total_odds,
                payout,
                profit,
                conditions,
                note or (f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"),
                clean_tag,
                1 if re_bet_counts_monthly(clean_tag) else 0,
                source_photo_message_id,
                accept_message_id,
                now_str(),
            ),
        )
        re_bet_id = cur.lastrowid
        for leg in legs:
            conn.execute(
                """
                INSERT INTO re_bet_legs
                (re_bet_id, event, selection, market, sport, odds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    re_bet_id,
                    leg.get("event", ""),
                    leg.get("selection", ""),
                    leg.get("market", "Winner"),
                    normalize_sport_slug(leg.get("sport") or infer_sport_from_leg_text(leg.get("event", ""), leg.get("selection", ""), leg.get("market", "Winner"))),
                    float(leg.get("odds", 1)),
                ),
            )
    return int(re_bet_id)


def get_re_bet_with_legs(re_bet_id: int):
    with re_db() as conn:
        bet = conn.execute("SELECT * FROM re_bets WHERE id = ?", (re_bet_id,)).fetchone()
        legs = conn.execute("SELECT * FROM re_bet_legs WHERE re_bet_id = ? ORDER BY id ASC", (re_bet_id,)).fetchall()
    return bet, legs


def update_re_ticket_message_ids(re_bet_id: int, slip_message_id: int = None, summary_message_id: int = None):
    with re_db() as conn:
        if slip_message_id is not None:
            conn.execute("UPDATE re_bets SET slip_message_id = ? WHERE id = ?", (slip_message_id, re_bet_id))
        if summary_message_id is not None:
            conn.execute("UPDATE re_bets SET summary_message_id = ? WHERE id = ?", (summary_message_id, re_bet_id))


def fetch_re_monthly_rows(start_str: str, end_str: str) -> List[Dict]:
    if not os.path.exists(RE_DB_FILE):
        return []
    query = """
        SELECT
            bettor,
            COUNT(*) AS total_bets,
            SUM(COALESCE(stake, 0)) AS total_wager,
            SUM(CASE WHEN LOWER(COALESCE(status, 'open')) = 'open' THEN 1 ELSE 0 END) AS open_bets,
            SUM(CASE WHEN LOWER(COALESCE(result, '')) = 'win' THEN 1 ELSE 0 END) AS won,
            SUM(CASE WHEN LOWER(COALESCE(result, '')) = 'loss' THEN 1 ELSE 0 END) AS lost,
            SUM(COALESCE(pnl, 0)) AS user_pnl
        FROM re_bets
        WHERE COALESCE(created_at, '') >= ?
          AND COALESCE(created_at, '') < ?
          AND LOWER(COALESCE(result, '')) != 'void'
          AND COALESCE(wager_counts_monthly, 0) = 1
        GROUP BY bettor
        HAVING total_wager > 0
    """
    with re_db() as conn:
        return [dict(r) for r in conn.execute(query, (start_str, end_str)).fetchall()]


# -------------------------
# OCR
# -------------------------


def clean_ocr_text(text: str) -> str:
    text = text.replace("×", "x")
    text = text.replace("|", " ")
    text = text.replace("€", "$")
    text = text.replace("£", "$")
    return text


def ocr_space(image_path: str) -> str:
    with open(image_path, "rb") as f:
        response = requests.post(
            "https://api.ocr.space/parse/image",
            files={"filename": f},
            data={
                "apikey": OCR_SPACE_API_KEY,
                "language": "eng",
                "isOverlayRequired": False,
                "OCREngine": 2,
                "scale": True,
                "detectOrientation": True,
            },
            timeout=90,
        )

    data = response.json()

    if data.get("IsErroredOnProcessing"):
        raise RuntimeError(data.get("ErrorMessage"))

    results = data.get("ParsedResults") or []

    if not results:
        return ""

    return results[0].get("ParsedText", "")


async def remember_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    if RECENT_PHOTOS_KEY not in context.chat_data:
        context.chat_data[RECENT_PHOTOS_KEY] = []

    photo = update.message.photo[-1]

    context.chat_data[RECENT_PHOTOS_KEY].append(
        {
            "message_id": update.message.message_id,
            "file_id": photo.file_id,
        }
    )

    context.chat_data[RECENT_PHOTOS_KEY] = context.chat_data[RECENT_PHOTOS_KEY][-20:]


async def download_photo_from_message(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> str:
    if not message.photo:
        raise RuntimeError("The replied message does not contain a photo.")

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
        await file.download_to_drive(temp.name)
        return temp.name


async def download_photo_file_id(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    file = await context.bot.get_file(file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
        await file.download_to_drive(temp.name)
        return temp.name


async def ocr_acceptance_photos(
    reply_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    pages: int,
) -> str:
    pages = max(1, min(int(pages or 1), 10))

    if pages <= 1:
        img_path = None
        try:
            img_path = await download_photo_from_message(reply_message, context)
            return ocr_space(img_path)
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:
                    pass

    recent = context.chat_data.get(RECENT_PHOTOS_KEY, [])
    reply_id = reply_message.message_id
    index = None

    for i, item in enumerate(recent):
        if item["message_id"] == reply_id:
            index = i
            break

    if index is None:
        img_path = None
        try:
            img_path = await download_photo_from_message(reply_message, context)
            return ocr_space(img_path)
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:
                    pass

    selected = recent[max(0, index - pages + 1):index + 1]

    if len(selected) < pages:
        selected = recent[-pages:]

    all_text = []

    for item in selected:
        img_path = None
        try:
            img_path = await download_photo_file_id(item["file_id"], context)
            text = ocr_space(img_path)
            all_text.append(text)
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:
                    pass

    return "\n".join(all_text)


# -------------------------
# PARSING HELPERS
# -------------------------


def clean_bet_tag(tag: str) -> str:
    tag = str(tag or "").strip()
    tag = re.sub(r"(?i)\b(accepted|approved)\b", " ", tag)
    tag = re.sub(r"[^a-zA-Z0-9 _+\-/]", " ", tag)
    tag = re.sub(r"\s+", " ", tag).strip()

    compact = tag.lower().replace(" ", "")

    if compact in {"free", "freebet", "freebets"}:
        return "FREE BET"

    if compact in {"special", "specialbet", "specialbets"}:
        return "SPECIAL BET"

    if compact in {"boosted", "boostedodd", "boostedodds", "oddsboost", "boost"}:
        return "BOOSTED ODDS"

    if compact == "bet":
        return ""

    tag = tag[:28].strip()
    return tag.upper()


def parse_acceptance_text(text: str) -> Optional[Dict]:
    raw = str(text or "").strip()

    pages = 1
    pages_match = re.search(
        r"(?i)\b(?:pages|page|slips|slip|screenshots|screens)\s*(\d+)\b",
        raw,
    )

    if pages_match:
        pages = int(pages_match.group(1))
        raw = re.sub(
            r"(?i)\b(?:pages|page|slips|slip|screenshots|screens)\s*\d+\b",
            "",
            raw,
        ).strip()

    amount = r"\$?\s*(\d+(?:\.\d+)?)\s*\$?"
    odds = r"(\d+(?:\.\d+)?)\s*x?"
    user = r"([a-zA-Z0-9_]+)"
    action = r"(?:accepted|approved)"

    def build(bettor_raw: str, stake_raw: str, odds_raw: str, tag_raw: str = "") -> Optional[Dict]:
        bettor_key = canonicalize_bettor(bettor_raw)
        stake = parse_float(stake_raw)
        total_odds = parse_float(odds_raw)
        bet_tag = clean_bet_tag(tag_raw)

        if stake > 0 and total_odds > 1:
            return {
                "bettor": bettor_key,
                "stake": stake,
                "total_odds": total_odds,
                "pages": max(1, min(pages, 10)),
                "bet_tag": bet_tag,
            }

        return None

    # melon bet accepted $5 at 5x
    m = re.search(
        rf"(?i)^\s*{user}\s+bet\s+{action}\s+{amount}\s*(?:at|@)\s*{odds}\s*$",
        raw,
    )
    if m:
        return build(m.group(1), m.group(2), m.group(3))

    # melon free bet accepted $5 at 5x
    # melon special bet approved $5 at 5x
    m = re.search(
        rf"(?i)^\s*{user}\s+(.+?)\s+{action}\s+{amount}\s*(?:at|@)\s*{odds}\s*$",
        raw,
    )
    if m:
        return build(m.group(1), m.group(3), m.group(4), m.group(2))

    # melon free bet accepted at $5 @ 5x (tolerant variant)
    m = re.search(
        rf"(?i)^\s*{user}\s+(.+?)\s+{action}\s+(?:stake\s*)?{amount}\s*(?:at|@)\s*{odds}\s*$",
        raw,
    )
    if m:
        return build(m.group(1), m.group(3), m.group(4), m.group(2))

    # $5 at 5x melon bet accepted
    m = re.search(
        rf"(?i)^\s*{amount}\s*(?:at|@)\s*{odds}\s+{user}\s+bet\s+{action}\s*$",
        raw,
    )
    if m:
        return build(m.group(3), m.group(1), m.group(2))

    # $5 at 5x melon free bet accepted
    m = re.search(
        rf"(?i)^\s*{amount}\s*(?:at|@)\s*{odds}\s+{user}\s+(.+?)\s+{action}\s*$",
        raw,
    )
    if m:
        return build(m.group(3), m.group(1), m.group(2), m.group(4))

    return None

def looks_like_market_detail_general(value: str) -> bool:
    low = str(value or "").lower().strip()

    detail_keywords = [
        "goalscorer",
        "anytime",
        "total",
        "booking",
        "bookings",
        "card",
        "cards",
        "1x2",
        "1 x 2",
        "corner",
        "corners",
        "half",
        "spread",
        "handicap",
        "winner",
        "moneyline",
        "match result",
        "player",
        "shots",
        "assists",
        "offside",
    ]

    return any(k in low for k in detail_keywords)


def looks_like_event_line_general(value: str) -> bool:
    value = str(value or "").strip()
    low = value.lower()

    if " - " not in value:
        return False

    blocked = [
        "1st half",
        "2nd half",
        "1x2",
        "corner",
        "corners",
        "total",
        "winner",
        "spread",
        "handicap",
        "market",
        "booking",
        "bookings",
        "card",
        "cards",
    ]

    if any(b in low for b in blocked):
        return False

    return True


def infer_market_general(selection: str) -> str:
    low = str(selection or "").lower().strip()

    if re.match(r"(?i)^(over|under)\s+\d+(?:\.\d+)?", selection.strip()):
        return "Total"

    if "corner" in low or "corners" in low:
        return "Corners"

    if "booking" in low or "bookings" in low:
        return "Total Bookings"

    if "card" in low or "cards" in low:
        return "Cards"

    if "goalscorer" in low:
        return "Anytime Goalscorer"

    if "1x2" in low or "1 x 2" in low:
        return "Match Result"

    return "Winner"


def parse_structured_bet_lines(lines: List[str]) -> List[Dict]:
    blacklist = [
        "stake shield",
        "choose your protection",
        "win if",
        "bet slip",
        "betting slip",
        "bet accepted",
        "payout",
        "return",
        "profit",
        "total odds",
        "total stake",
        "cashout",
        "balance",
        "receipt",
        "generated",
        "accepted",
        "wager",
        "odds are final",
        "void legs",
        "any retirement",
        "parlay loses",
        "single bet",
        "multi",
        "lenny book",
        "ticket",
        "booking settled",
        "settled",
        "private ticket",
        "date",
        "time",
        "place bet",
        "clear",
        "cash out",
        "cashout",
        "good luck",
    ]

    def is_over_under_selection(value: str) -> bool:
        return bool(re.search(r"(?i)\b(over|under)\s+\d+(?:\.\d+)?\b", str(value or "")))

    clean_text_lines = []
    odds_pool = []

    for line in lines:
        line = str(line or "").strip()
        low = line.lower().strip()

        if not low:
            continue

        if any(b in low for b in blacklist):
            continue

        if re.fullmatch(r"x|X|×|⌃|⌄|\^", line):
            continue

        if re.fullmatch(r"\d+\.\d{1,4}", line):
            odd = parse_float(line)

            if 1 < odd < 100:
                odds_pool.append(odd)

            continue

        trailing = re.search(r"(.+?)\s+(\d+\.\d{1,4})$", line)

        if trailing and not is_over_under_selection(line):
            clean_part = trailing.group(1).strip()
            odd = parse_float(trailing.group(2))

            if clean_part:
                clean_text_lines.append(clean_part)

            if 1 < odd < 100:
                odds_pool.append(odd)

            continue

        clean_text_lines.append(line)

    legs = []
    current_event = ""
    pending_selection = None
    pending_market = None

    for line in clean_text_lines:
        line = line.strip()

        if not line:
            continue

        if looks_like_event_line_general(line):
            if pending_selection and odds_pool:
                legs.append(
                    {
                        "event": current_event or pending_selection,
                        "selection": pending_selection,
                        "market": pending_market or infer_market_general(pending_selection),
                        "odds": odds_pool.pop(0),
                    }
                )

            current_event = line
            pending_selection = None
            pending_market = None
            continue

        if looks_like_market_detail_general(line) and not is_over_under_selection(line):
            if pending_selection:
                pending_market = line

                if odds_pool:
                    legs.append(
                        {
                            "event": current_event or pending_selection,
                            "selection": pending_selection,
                            "market": pending_market,
                            "odds": odds_pool.pop(0),
                        }
                    )

                    pending_selection = None
                    pending_market = None

            continue

        if pending_selection and odds_pool:
            legs.append(
                {
                    "event": current_event or pending_selection,
                    "selection": pending_selection,
                    "market": pending_market or infer_market_general(pending_selection),
                    "odds": odds_pool.pop(0),
                }
            )

        pending_selection = line
        pending_market = None

    if pending_selection and odds_pool:
        legs.append(
            {
                "event": current_event or pending_selection,
                "selection": pending_selection,
                "market": pending_market or infer_market_general(pending_selection),
                "odds": odds_pool.pop(0),
            }
        )

    seen = set()
    unique_legs = []

    for leg in legs:
        key = (
            str(leg["event"]).lower(),
            str(leg["selection"]).lower(),
            str(leg["market"]).lower(),
            float(leg["odds"]),
        )

        if key in seen:
            continue

        seen.add(key)
        unique_legs.append(leg)

    return unique_legs


def parse_bet_text(text: str) -> Optional[Dict]:
    raw_original = str(text or "").strip()
    raw = clean_ocr_text(raw_original)

    if not raw:
        return None

    lines = [x.strip() for x in raw_original.replace("×", "x").splitlines() if x.strip()]

    shield_value = ""

    shield_patterns = [
        r"(?i)win\s+if\s+(\d+\s*(?:[-–—]|to)\s*\d+|\d+)\s+legs?\s+los(?:e|es)",
        r"(?i)win\s+if\s+(\d+\s*(?:[-–—]|to)\s*\d+|\d+)",
        r"(?i)choose\s+your\s+protection.*?win\s+if\s+(\d+\s*(?:[-–—]|to)\s*\d+|\d+)",
    ]

    for pattern in shield_patterns:
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            shield_value = m.group(1).strip()
            break

    shield_value = re.sub(r"\s*(?:[-–—]|to)\s*", "-", shield_value)
    is_shield_bet = bool(shield_value)

    def shield_leg_word() -> str:
        if "-" in shield_value:
            return "legs"

        try:
            return "leg" if int(shield_value) == 1 else "legs"
        except Exception:
            return "legs"

    def build_conditions():
        if is_shield_bet:
            return [
                f"Shield Bet: Win if {shield_value} {shield_leg_word()} lose."
            ] + DEFAULT_CONDITIONS

        return DEFAULT_CONDITIONS

    def build_note(default_note: str):
        if is_shield_bet:
            return f"Shield Bet • Win if {shield_value} {shield_leg_word()} lose"

        return default_note

    stake = None
    manual_total_odds = None

    stake_match = re.search(
        r"(?i)(?:stake\s*)?\$?(\d+(?:\.\d+)?)\s*(?:at|@)\s*(\d+(?:\.\d+)?)\s*x?",
        raw,
    )

    if stake_match:
        stake = parse_float(stake_match.group(1))
        manual_total_odds = parse_float(stake_match.group(2))

    if stake is None:
        for pattern in [
            r"(?i)(?:stake|total stake|bet amount|amount)\s*[:\-]?\s*\$?(\d+(?:\.\d+)?)",
            r"(?i)\$?(\d+(?:\.\d+)?)\s*(?:stake|bet)",
            r"^\s*\$?(\d+(?:\.\d+)?)",
        ]:
            m = re.search(pattern, raw)

            if m:
                stake = parse_float(m.group(1))
                break

    if stake is None:
        stake = DEFAULT_STAKE

    if manual_total_odds is None:
        for pattern in [
            r"(?i)(?:total odds|combined odds|odds total)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*x?",
            r"(?i)(\d+)\s*leg\s*same\s*game\s*multi\s*(\d+(?:\.\d+)?)",
        ]:
            m = re.search(pattern, raw)

            if m:
                possible = parse_float(m.group(m.lastindex))

                if possible > 1:
                    manual_total_odds = possible
                    break

    legs = parse_structured_bet_lines(lines)

    if not legs:
        return None

    calculated_odds = multiply_odds(legs)
    total_odds = manual_total_odds or calculated_odds

    return {
        "stake": stake,
        "total_odds": round(total_odds, 4),
        "legs": legs,
        "conditions": build_conditions(),
        "note": build_note(f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"),
    }



# -------------------------
# AI BETSLIP PARSER
# -------------------------

BETSLIP_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "note": {"type": "string"},
        "conditions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "legs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "selection": {"type": "string"},
                    "market": {"type": "string"},
                    "sport": {
                        "type": "string",
                        "enum": [
                            "football",
                            "tennis",
                            "cricket",
                            "basketball",
                            "baseball",
                            "volleyball",
                            "boxing",
                            "mma",
                            "badminton",
                            "american_football",
                            "other",
                        ],
                    },
                    "odds": {"type": "number"},
                },
                "required": ["event", "selection", "market", "sport", "odds"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ok", "note", "conditions", "legs"],
    "additionalProperties": False,
}


def build_ai_betslip_prompt(ocr_text: str, stake: float, total_odds: float) -> str:
    return f"""
You are parsing sportsbook source text for a Telegram bet ledger bot.
The source can be OCR from an image OR plain text from a replied Telegram message.
Your job is to decide the real bet legs only. Ignore UI words, balances, buttons, cashout,
headers, receipts, timestamps, ads, ticket numbers, and duplicated OCR noise.

IMPORTANT SPORT DETECTION:
- For EACH leg, identify the correct sport.
- Use web/search knowledge when available to look up team/player names.
- If a matchup contains football national teams/clubs like Netherlands, Spain, Belgium, England, France, Japan, etc. with markets like Match Winner - Threeway, classify it as "football".
- If names are tennis players, classify it as "tennis".
- If teams are baseball teams like Hanshin Tigers / Chunichi Dragons, classify it as "baseball".
- If teams are cricket teams/franchises or markets mention overs/innings/runs/wickets, classify it as "cricket".
- Do NOT assign one sport to every leg unless every leg is actually the same sport.
- If uncertain, use "other".

Return ONLY valid JSON matching this structure:
{{
  "ok": true,
  "note": "3 Leg Multi" or "Single Bet" or "Shield Bet - ...",
  "conditions": ["Any retirement = bet void.", "Odds are final once accepted.", "Void legs are removed from final odds.", "Parlay loses if any active leg loses."],
  "legs": [
    {{"event":"Team A - Team B", "selection":"Team A", "market":"Match Winner - Threeway", "sport":"football", "odds":1.83}}
  ]
}}

Allowed sport values:
football, tennis, cricket, basketball, baseball, volleyball, boxing, mma, badminton, american_football, other

Rules:
- Include ONLY actual wager legs.
- Every leg must have event, selection, market, sport, and decimal odds.
- Decimal odds must be a number above 1.00.
- If this is a plain sportsbook offer/boost with no leg odds shown, create ONE leg and use the admin total odds as that leg odds.
- If event is missing, infer the best event from nearby source text. If impossible, use the selection as event.
- If market is missing, infer it from the selection, e.g. Winner, Match Result, Outright, To Reach Final, Total, Corners, Cards, Anytime Goalscorer, Handicap.
- Do not include stake, payout, return, balance, total odds, or cashout as legs.
- Do not invent extra legs that are not supported by the OCR.
- Remove duplicate legs.
- If the bet has shield/protection text, add a first condition that explains it.
- If no reliable legs can be found, return: {{"ok": false, "note": "Could not parse", "conditions": [], "legs": []}}

Accepted by admin:
Stake: {stake}
Total odds to use for ticket: {total_odds}

SOURCE TEXT / OCR TEXT:
{ocr_text[:12000]}
""".strip()

def extract_json_object(text: str) -> Dict:
    raw = str(text or "").strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = re.sub(r"```$", "", raw).strip()

    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")

    if start >= 0 and end > start:
        return json.loads(raw[start:end + 1])

    raise ValueError("AI did not return JSON.")


def call_gemini_betslip_ai(prompt: str) -> Dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing.")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": BETSLIP_JSON_SCHEMA,
        },
    }

    response = requests.post(url, json=payload, timeout=AI_BETSLIP_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise RuntimeError(f"Bad Gemini response: {str(data)[:500]}")

    return extract_json_object(text)


def call_venice_betslip_ai(prompt: str) -> Dict:
    if not VENICE_API_KEY:
        raise RuntimeError("VENICE_API_KEY missing.")

    url = "https://api.venice.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {VENICE_API_KEY}",
        "Content-Type": "application/json",
    }

    base_payload = {
        "model": VENICE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract sportsbook bet legs from OCR text. "
                    "Return only strict JSON. No markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_completion_tokens": 1800,
        "venice_parameters": {
            "enable_web_search": AI_BETSLIP_WEB_SEARCH,
            "include_venice_system_prompt": False,
            "disable_thinking": True,
            "strip_thinking_response": True,
        },
    }

    # Venice structured outputs use OpenAI-style json_schema. Some accounts/models may reject
    # response_format, so we automatically retry once without it instead of crashing the bot.
    payloads = []
    structured_payload = dict(base_payload)
    structured_payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "betslip_response",
            "strict": True,
            "schema": BETSLIP_JSON_SCHEMA,
        },
    }
    payloads.append(structured_payload)
    payloads.append(base_payload)

    last_error = None

    for payload in payloads:
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=AI_BETSLIP_TIMEOUT,
            )

            if response.status_code >= 400:
                body = response.text[:1000]
                last_error = RuntimeError(f"Venice HTTP {response.status_code}: {body}")
                if AI_BETSLIP_DEBUG:
                    print(last_error)
                continue

            data = response.json()

            try:
                message = data["choices"][0]["message"]
                text = message.get("content") or message.get("reasoning_content") or ""
            except Exception:
                raise RuntimeError(f"Bad Venice response: {str(data)[:500]}")

            return extract_json_object(text)
        except Exception as e:
            last_error = e
            if AI_BETSLIP_DEBUG:
                print(f"Venice parser request failed: {e}")

    raise RuntimeError(str(last_error) if last_error else "Venice request failed.")



def normalize_sport_slug(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer": "football",
        "fifa": "football",
        "footy": "football",
        "american_football": "american_football",
        "nfl": "american_football",
        "basketball": "basketball",
        "nba": "basketball",
        "baseball": "baseball",
        "mlb": "baseball",
        "cricket": "cricket",
        "tennis": "tennis",
        "volleyball": "volleyball",
        "boxing": "boxing",
        "mma": "mma",
        "ufc": "mma",
        "badminton": "badminton",
        "other": "other",
        "sports": "other",
    }
    if raw in aliases:
        return aliases[raw]
    allowed = {"football", "tennis", "cricket", "basketball", "baseball", "volleyball", "boxing", "mma", "badminton", "american_football", "other"}
    return raw if raw in allowed else "other"


def infer_sport_from_leg_text(event: str = "", selection: str = "", market: str = "") -> str:
    text = f" {event} {selection} {market} ".lower()

    if any(x in text for x in [" cricket ", " t20 ", " odi ", " ipl ", " wicket", " wickets", " runs", " innings", " overs "]):
        return "cricket"
    if any(x in text for x in [" basketball ", " nba ", " rebounds", " assists", " points ", " quarter "]):
        return "basketball"
    if any(x in text for x in [" baseball ", " mlb ", " home run", " strikeout", " pitcher", " hanshin tigers", " chunichi dragons"]):
        return "baseball"
    if any(x in text for x in [" volleyball ", " volley ", " total sets", " sets handicap"]):
        return "volleyball"
    if any(x in text for x in [" tennis ", " atp ", " wta ", " aces", " double faults", " total games", " game handicap", " set betting", "medvedev", "opelka"]):
        return "tennis"
    if any(x in text for x in [" boxing ", " boxer "]):
        return "boxing"
    if any(x in text for x in [" mma ", " ufc "]):
        return "mma"
    if any(x in text for x in [" badminton "]):
        return "badminton"
    if any(x in text for x in [" football ", " soccer ", " fifa ", " corner", " goals", " match winner", " threeway", " three-way", " both teams to score"]):
        return "football"

    football_entities = [
        "netherlands", "spain", "belgium", "england", "france", "japan", "germany", "italy",
        "portugal", "brazil", "argentina", "arsenal", "chelsea", "liverpool", "barcelona",
        "madrid", "bayern", "psg", "united", "city", "fc ",
    ]
    if any(x in text for x in football_entities):
        return "football"

    return "other"


def normalize_ai_leg(leg: Dict) -> Optional[Dict]:
    event = str(leg.get("event") or "").strip()
    selection = str(leg.get("selection") or "").strip()
    market = str(leg.get("market") or "").strip() or "Winner"

    try:
        odds = float(leg.get("odds"))
    except Exception:
        return None

    if odds <= 1 or odds >= 100:
        return None

    if not selection:
        return None

    if not event:
        event = selection

    sport = normalize_sport_slug(leg.get("sport") or "")
    if sport == "other":
        sport = infer_sport_from_leg_text(event, selection, market)

    return {
        "event": event[:120],
        "selection": selection[:120],
        "market": market[:80],
        "sport": sport,
        "odds": round(odds, 4),
    }


def validate_ai_betslip(data: Dict, stake: float, total_odds: float) -> Optional[Dict]:
    if not isinstance(data, dict) or not data.get("ok"):
        return None

    raw_legs = data.get("legs") or []

    if not isinstance(raw_legs, list):
        return None

    legs = []
    seen = set()

    for raw_leg in raw_legs:
        if not isinstance(raw_leg, dict):
            continue

        leg = normalize_ai_leg(raw_leg)

        if not leg:
            continue

        key = (
            leg["event"].lower(),
            leg["selection"].lower(),
            leg["market"].lower(),
            str(leg.get("sport", "other")).lower(),
            float(leg["odds"]),
        )

        if key in seen:
            continue

        seen.add(key)
        legs.append(leg)

    if not legs:
        return None

    conditions = data.get("conditions") or DEFAULT_CONDITIONS

    if not isinstance(conditions, list):
        conditions = DEFAULT_CONDITIONS

    conditions = [str(x).strip() for x in conditions if str(x).strip()]

    if not conditions:
        conditions = DEFAULT_CONDITIONS

    note = str(data.get("note") or "").strip()

    if not note:
        note = f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"

    return {
        "stake": float(stake),
        "total_odds": round(float(total_odds), 4),
        "legs": legs,
        "conditions": conditions,
        "note": note,
    }


def parse_bet_text_with_ai(text: str, stake: float, total_odds: float) -> Optional[Dict]:
    provider = AI_BETSLIP_PROVIDER

    if provider in ["", "off", "none", "false", "0"]:
        return None

    prompt = build_ai_betslip_prompt(text, stake, total_odds)

    try:
        if provider == "gemini":
            data = call_gemini_betslip_ai(prompt)
        elif provider == "venice":
            data = call_venice_betslip_ai(prompt)
        else:
            raise RuntimeError(f"Unknown AI_BETSLIP_PROVIDER: {provider}")

        parsed = validate_ai_betslip(data, stake, total_odds)

        if parsed:
            return parsed

        raise RuntimeError(f"AI returned no valid legs: {data}")
    except Exception as e:
        if AI_BETSLIP_DEBUG:
            print(f"AI betslip parser failed: {e}")

        return None


# -------------------------
# VENICE /AI SPORTS BETTING ASSISTANT
# -------------------------


def call_venice_sports_ai(question: str, source_text: str) -> str:
    """
    Short sports-betting-only helper for /ai.
    Uses Venice web search to check recent sports form/news where possible.
    Returns Telegram-safe HTML made locally, so the reply stays short and clean.
    """
    if not VENICE_API_KEY:
        raise RuntimeError("VENICE_API_KEY missing.")

    question = str(question or "").strip()
    source_text = str(source_text or "").strip()

    if not question:
        question = "Briefly assess this bet. Can this win?"

    prompt = f"""
User question:
{question[:1000]}

OCR / replied text from bet image or message:
{source_text[:AI_ASSISTANT_MAX_CHARS]}

Task:
- This is ONLY for sports betting slip analysis.
- Identify the sport, match/team/player/selection from the supplied bet text.
- Use web search when available to check recent form, fixture context, injuries/team news, matchup context, and odds/value.
- Give a Lenny confidence/value rating from 1 to 5.
- Stay positive and engaging even for low ratings; do not sound harsh or discouraging.
- Briefly describe team/player conditions: form, lineup/injury/news, matchup edge, or price/value.
- Keep it short, goofy, and useful.
- No doom language. No generic warnings.
- Do NOT say: "don't get cooked", "get cooked", "melon alert", "melon meter", "avoid", "bad bet", "dead slip", or "trash".

Return ONLY valid JSON with this exact shape:
{{
  "sports_betting": true,
  "rating": 3,
  "vibe": "positive tiny goofy verdict, max 4 words, never melon alert/melon meter",
  "reason": "max 20 words, briefly explain team/player conditions and game/form/value"
}}

If this is not sports betting, return:
{{
  "sports_betting": false,
  "rating": 0,
  "vibe": "No bet talk",
  "reason": "Lenny only talks sports betting"
}}
""".strip()

    payload = {
        "model": VENICE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Lenny's goofy but sharp sports betting assistant. Their lord is named Lenny. "
                    "Only answer sports betting analysis questions about the supplied bet/slip/match. "
                    "Use current web research when available: recent form, injuries/team news, fixture context, "
                    "H2H, player/team availability, and market risk. "
                    "Be very short. No essays. No tables. No guarantee language. "
                    "Stay positive and engaging, even for low ratings. Do not over-warn or scare users away. "
                    "Never say: don't get cooked, get cooked, melon alert, melon meter, avoid, bad bet, dead slip, or trash. "
                    "Briefly explain the actual game/match context and team/player conditions. "
                    "Do not discuss anything outside sports betting. Return only valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_completion_tokens": 140,
        "venice_parameters": {
            "enable_web_search": AI_ASSISTANT_WEB_SEARCH,
            "include_venice_system_prompt": False,
            "disable_thinking": True,
            "strip_thinking_response": True,
        },
    }

    response = requests.post(
        "https://api.venice.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {VENICE_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=AI_BETSLIP_TIMEOUT,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Venice HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()

    try:
        message = data["choices"][0]["message"]
        raw_answer = message.get("content") or message.get("reasoning_content") or ""
    except Exception:
        raise RuntimeError(f"Bad Venice response: {str(data)[:500]}")

    try:
        parsed = extract_json_from_text(str(raw_answer or ""))
    except Exception:
        parsed = {}

    sports_betting = bool(parsed.get("sports_betting", True))

    if not sports_betting:
        return "🍉 <b>Lenny says:</b> sports betting only, melon brain."

    try:
        rating = int(float(parsed.get("rating", 3)))
    except Exception:
        rating = 3

    rating = max(1, min(rating, 5))

    vibe = re.sub(r"\s+", " ", str(parsed.get("vibe") or "Live nibble")).strip()
    reason = re.sub(
        r"\s+",
        " ",
        str(parsed.get("reason") or "Form and team news look playable; price decides the sauce.")
    ).strip()

    banned_phrases = [
        "don't get cooked",
        "don’t get cooked",
        "get cooked",
        "melon alert",
        "melon meter",
        "avoid",
        "bad bet",
        "dead slip",
        "trash",
        "awful",
        "terrible",
    ]

    for banned in banned_phrases:
        vibe = re.sub(re.escape(banned), "live nibble", vibe, flags=re.IGNORECASE)
        reason = re.sub(re.escape(banned), "thin value", reason, flags=re.IGNORECASE)

    vibe = vibe[:38].strip()
    reason = reason[:155].strip()

    if rating <= 2:
        emoji = "🌱"
    elif rating == 3:
        emoji = "👀"
    elif rating == 4:
        emoji = "✅"
    else:
        emoji = "🔥"

    return (
        f"🍉 <b>Lenny Rating:</b> <b>{rating}/5</b> {emoji} <b>{safe_text(vibe)}</b>\n"
        f"🧠 {safe_text(reason)}"
    )


async def get_replied_ai_source_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (source_text, error_message) for /ai replied-to image/text."""
    if not update.message or not update.message.reply_to_message:
        return None, "Reply to a bet image or bet text with: <code>/ai can this win?</code>"

    replied = update.message.reply_to_message

    text_bits = []

    if getattr(replied, "text", None):
        text_bits.append(replied.text)

    if getattr(replied, "caption", None):
        text_bits.append(replied.caption)

    if text_bits:
        return "\n".join(text_bits).strip(), None

    if getattr(replied, "photo", None):
        img_path = None
        try:
            img_path = await download_photo_from_message(replied, context)
            text = ocr_space(img_path)
            text = clean_ocr_text(text)

            if not text.strip():
                return None, "OCR could not read this image. Try a clearer screenshot."

            return text.strip(), None
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except Exception:
                    pass

    return None, "Reply to a bet image or bet text with: <code>/ai can this win?</code>"

# -------------------------
# BET DB ACTIONS
# -------------------------


def load_ticket_config() -> Dict:
    default = {
        "start_after": int(TICKET_SEQUENCE_FORCE_AFTER or 0),
        "ignore_above": int(TICKET_SEQUENCE_IGNORE_ABOVE or 999999),
        # Hide old imported tickets from user-facing commands without deleting them.
        # If enabled with hide_before=280, tickets #1-#279 stay in DB but are not shown/counted.
        "hide_before_enabled": False,
        "hide_before": 0,
        "updated_at": "",
    }
    try:
        if os.path.exists(TICKET_CONFIG_FILE):
            with open(TICKET_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                default.update(data)
    except Exception:
        pass

    try:
        default["start_after"] = int(default.get("start_after") or 0)
    except Exception:
        default["start_after"] = int(TICKET_SEQUENCE_FORCE_AFTER or 0)

    try:
        default["ignore_above"] = int(default.get("ignore_above") or 999999)
    except Exception:
        default["ignore_above"] = int(TICKET_SEQUENCE_IGNORE_ABOVE or 999999)

    try:
        default["hide_before"] = int(default.get("hide_before") or 0)
    except Exception:
        default["hide_before"] = 0

    default["hide_before_enabled"] = bool(default.get("hide_before_enabled", False))

    return default


def save_ticket_config(data: Dict) -> None:
    cfg = load_ticket_config()
    cfg.update(data or {})
    cfg["updated_at"] = now_str()
    with open(TICKET_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def hidden_ticket_filter() -> Tuple[bool, int]:
    """Return whether old-ticket hiding is enabled and the first visible ticket id."""
    cfg = load_ticket_config()
    try:
        first_visible = int(cfg.get("hide_before") or 0)
    except Exception:
        first_visible = 0
    enabled = bool(cfg.get("hide_before_enabled")) and first_visible > 0
    return enabled, max(0, first_visible)


def is_ticket_hidden_by_config(ticket_id: int) -> bool:
    enabled, first_visible = hidden_ticket_filter()
    try:
        return enabled and int(ticket_id) < int(first_visible)
    except Exception:
        return False


def ticket_visibility_sql(alias: str = "") -> Tuple[str, List[int]]:
    """SQL snippet for user-facing commands that should hide old tickets."""
    enabled, first_visible = hidden_ticket_filter()
    if not enabled:
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}id >= ?", [int(first_visible)]


def hidden_ticket_status_text() -> str:
    enabled, first_visible = hidden_ticket_filter()
    if enabled:
        return f"ON — showing tickets #{first_visible} and later only"
    if first_visible > 0:
        return f"OFF — saved threshold is #{first_visible}"
    return "OFF"


def allocate_next_real_ticket_id(conn) -> int:
    """
    Allocate the next normal ticket ID manually.

    ticket_config.json controls the floor:
      {"start_after": 250} means future tickets start at #251.

    Huge accidental IDs can still be ignored with ignore_above so #123456793
    does not force all future tickets to continue from there.
    """
    cfg = load_ticket_config()
    start_after = int(cfg.get("start_after") or 0)
    ignore_above = int(cfg.get("ignore_above") or TICKET_SEQUENCE_IGNORE_ABOVE or 999999)

    max_normal = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM bets WHERE id <= ?",
        (ignore_above,),
    ).fetchone()[0]

    next_id = max(int(max_normal or 0), start_after) + 1

    # Safety: never overwrite an existing ticket. If the candidate exists, move up.
    while conn.execute("SELECT 1 FROM bets WHERE id = ? LIMIT 1", (next_id,)).fetchone():
        next_id += 1

    return int(next_id)


def create_bet(
    chat_id: int,
    admin_user_id: int,
    admin_username: str,
    bettor: str,
    stake: float,
    total_odds: float,
    legs: List[Dict],
    source_photo_message_id: int,
    accept_message_id: int,
    conditions_list: Optional[List[str]] = None,
    note: Optional[str] = None,
    market_id: Optional[int] = None,
    bet_tag: str = "",
    is_test: bool = False,
) -> int:
    payout = round(stake * total_odds, 2)
    profit = round(payout - stake, 2)

    conditions_list = conditions_list or DEFAULT_CONDITIONS
    conditions = "\n".join(conditions_list)

    with db() as conn:
        next_ticket_id = allocate_next_real_ticket_id(conn)

        cur = conn.execute(
            """
            INSERT INTO bets
            (
                id, chat_id, user_id, username, bettor,
                stake, total_odds, payout, profit,
                conditions, note, market_id, bet_tag, is_test,
                source_photo_message_id, accept_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_ticket_id,
                chat_id,
                admin_user_id,
                admin_username,
                bettor,
                stake,
                total_odds,
                payout,
                profit,
                conditions,
                note or (f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"),
                market_id,
                clean_bet_tag(bet_tag),
                1 if is_test else 0,
                source_photo_message_id,
                accept_message_id,
                now_str(),
            ),
        )

        bet_id = cur.lastrowid

        # Keep sqlite_sequence aligned with the normal sequence, not rogue huge IDs.
        try:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'bets'", (int(bet_id),))
        except Exception:
            pass

        for leg in legs:
            conn.execute(
                """
                INSERT INTO bet_legs
                (bet_id, event, selection, market, sport, odds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bet_id,
                    leg.get("event", ""),
                    leg.get("selection", ""),
                    leg.get("market", "Winner"),
                    normalize_sport_slug(leg.get("sport") or infer_sport_from_leg_text(leg.get("event", ""), leg.get("selection", ""), leg.get("market", "Winner"))),
                    float(leg.get("odds", 1)),
                ),
            )

    return bet_id


def update_ticket_message_ids(
    bet_id: int,
    slip_message_id: int = None,
    summary_message_id: int = None,
):
    with db() as conn:
        if slip_message_id is not None:
            conn.execute(
                "UPDATE bets SET slip_message_id = ? WHERE id = ?",
                (slip_message_id, bet_id),
            )

        if summary_message_id is not None:
            conn.execute(
                "UPDATE bets SET summary_message_id = ? WHERE id = ?",
                (summary_message_id, bet_id),
            )


# -------------------------
# CHAT EXPORT RECOVERY
# -------------------------

RECOVERY_EXPORT_DIR = os.getenv("RECOVERY_EXPORT_DIR", "recovery_exports")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "").strip()
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "recovery_session").strip()
TELEGRAM_RECOVERY_MEDIA_DIR = os.getenv("TELEGRAM_RECOVERY_MEDIA_DIR", "telethon_recovery_media").strip()
TELEGRAM_RECOVERY_LIMIT = int(os.getenv("TELEGRAM_RECOVERY_LIMIT", "0") or "0")
RECOVERY_BOOK_BOT_USER_ID = int(os.getenv("RECOVERY_BOOK_BOT_USER_ID", "0") or "0")
VENICE_RECOVERY_MODEL = os.getenv("VENICE_RECOVERY_MODEL", VENICE_MODEL).strip()
RECOVERY_BOOK_USE_OCR = True  # forced: /recoverbetsbook never sends images to Venice

# -------------------------
# /send ACCEPTED BET FORWARDER
# -------------------------
# /send scans the current chat with the Telethon self account and forwards every
# message/caption containing SEND_ACCEPTED_PHRASE to SEND_ACCEPTED_TARGET_CHAT_ID.
SEND_ACCEPTED_TARGET_CHAT_ID = int(os.getenv("SEND_ACCEPTED_TARGET_CHAT_ID", "-5361332507") or "-5361332507")
SEND_ACCEPTED_PHRASE = os.getenv("SEND_ACCEPTED_PHRASE", "bet has been accepted.").strip().lower()
SEND_ACCEPTED_SCAN_LIMIT = int(os.getenv("SEND_ACCEPTED_SCAN_LIMIT", "0") or "0")
SEND_ACCEPTED_BOT_USER_ID = int(os.getenv("SEND_ACCEPTED_BOT_USER_ID", "0") or "0")

# /sendregen scans for regenerated tickets, then sends each direct reply as a real
# reply to the regenerated ticket message in the target group.
SEND_REGEN_TARGET_CHAT_ID = int(os.getenv("SEND_REGEN_TARGET_CHAT_ID", str(SEND_ACCEPTED_TARGET_CHAT_ID)) or str(SEND_ACCEPTED_TARGET_CHAT_ID))
SEND_REGEN_SCAN_LIMIT = int(os.getenv("SEND_REGEN_SCAN_LIMIT", str(SEND_ACCEPTED_SCAN_LIMIT)) or "0")
SEND_REGEN_BOT_USER_ID = int(os.getenv("SEND_REGEN_BOT_USER_ID", "0") or "0")



def telegram_export_text(value) -> str:
    """Telegram Desktop JSON exports store text/captions as strings or rich-text arrays."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("text", "")))
        return "".join(out)
    return str(value)


def recovery_message_text(msg: Dict) -> str:
    parts = []
    for key in ("text", "caption"):
        t = telegram_export_text(msg.get(key)).strip()
        if t:
            parts.append(t)
    return "\n".join(parts).strip()


def recovery_parse_datetime(msg: Dict) -> str:
    raw = str(msg.get("date") or "").strip()
    if not raw:
        return now_str()
    try:
        # Telegram export commonly uses: 2026-07-01T12:34:56
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw.replace("T", " ")[:19] or now_str()


def recovery_extract_ticket_id(text: str) -> Optional[int]:
    m = re.search(r"(?i)\bTicket\s*#\s*(\d+)\b", str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def recovery_extract_bettor_from_accept_caption(text: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", str(text or ""))
    raw = html.unescape(raw)
    raw = raw.replace("✅", " ").strip()
    m = re.search(r"(?i)^\s*(.*?)\s*(?:,)?\s+(?:[a-z0-9_ -]+\s+)?bet\s+has\s+been\s+accepted", raw)
    if not m:
        return "unknown"
    label = m.group(1).strip()
    label = re.sub(r"(?i)^(test|special|market)\s+", "", label).strip()
    label = label.split()[0] if label.startswith("@") else label
    return canonicalize_bettor(label) or "unknown"


def recovery_is_accept_slip(text: str) -> bool:
    low = str(text or "").lower()
    return "bet has been accepted" in low and recovery_extract_ticket_id(text) is not None


def recovery_extract_settlement(text: str) -> Tuple[Optional[str], Optional[float]]:
    low = str(text or "").lower()
    adjusted = parse_adjusted_win_text(low)
    if "void" in low or "voided" in low:
        return "void", None
    if "lost" in low or "loss" in low:
        return "loss", None
    if "won" in low or re.search(r"\bwin\b", low):
        return "win", adjusted
    return None, None


def recovery_is_settlement(text: str) -> bool:
    low = str(text or "").lower()
    return "bet settled" in low or "settled:" in low


def recovery_extract_manual_settlement_command(text: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Reads the old admin reply command used to settle a ticket.
    Examples: won, win, w, lost, lose, l, void, won 2.7x.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, None

    # Ignore normal betslip/summary captions.
    low = raw.lower()
    if "bet has been accepted" in low or "open bet" in low or "bet settled" in low or "settled:" in low:
        return None, None

    adjusted = parse_adjusted_win_text(raw)
    if adjusted is not None:
        return "win", adjusted

    # Be tolerant of emojis/punctuation around tiny commands like "w ✅" or "loss."
    cleaned = re.sub(r"[^a-z0-9.×x@ ]+", " ", raw.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    adjusted = parse_adjusted_win_text(cleaned)
    if adjusted is not None:
        return "win", adjusted

    result = normalize_settlement(cleaned)
    if result:
        return result, None

    # Also accept the first word only when the message is basically a settlement command.
    parts = cleaned.split()
    if 1 <= len(parts) <= 3:
        result = normalize_settlement(parts[0])
        if result:
            return result, None

    return None, None


async def recovery_get_replied_bet_from_telethon(client, msg, bot_chat_id: int):
    """
    Find the ticket that an old settlement belongs to by following replies.

    Old flow normally was:
      1) Lenny Book posts accepted betslip: Ticket #123
      2) admin replies to that betslip: won / lost / void / w / l
      3) bot replies to admin command with settlement card

    So this checks both:
      - the message this settlement command replies to
      - if that replied message is itself a command, the message THAT command replied to

    It settles by the Ticket # found in the replied-to betslip first, then falls
    back to matching the Telegram message id stored during /recoverbets.
    """
    checked_ids = set()

    async def bet_from_message(m):
        if not m:
            return None
        mid = int(getattr(m, "id", 0) or 0)
        if mid in checked_ids:
            return None
        checked_ids.add(mid)

        m_text = telethon_message_text(m)
        tid = recovery_extract_ticket_id(m_text)
        if tid:
            bet = get_bet_by_ticket_id(tid)
            if bet:
                return bet

        if mid:
            bet = get_bet_by_message(bot_chat_id, mid)
            if bet:
                return bet

        return None

    try:
        first = await msg.get_reply_message()
    except Exception:
        first = None

    bet = await bet_from_message(first)
    if bet:
        return bet

    # Handles: settlement card -> admin command -> accepted betslip.
    if first:
        try:
            second = await first.get_reply_message()
        except Exception:
            second = None
        bet = await bet_from_message(second)
        if bet:
            return bet

    return None


def recovery_settle_one_candidate(
    chat_id: int,
    reply_message_id: int,
    text: str,
    admin_user_id: int,
) -> str:
    """
    Returns one of: settled, already, no_bet, no_result
    """
    if not reply_message_id:
        return "no_bet"

    result, adjusted_odds = recovery_extract_manual_settlement_command(text)
    if not result and recovery_is_settlement(text):
        result, adjusted_odds = recovery_extract_settlement(text)

    if not result:
        return "no_result"

    bet = get_bet_by_message(chat_id, reply_message_id)
    if not bet:
        tid = recovery_extract_ticket_id(text)
        bet = get_bet_by_ticket_id(tid) if tid else None
    if not bet:
        return "no_bet"

    ok, msg, _ = settle_bet(int(bet["id"]), result, admin_user_id, adjusted_odds=adjusted_odds)
    if ok:
        return "settled"
    if "already settled" in str(msg).lower():
        return "already"
    return "no_bet"


def recovery_photo_path(msg: Dict, base_dir: str) -> str:
    for key in ("photo", "file", "thumbnail"):
        val = msg.get(key)
        if isinstance(val, str) and val:
            path = os.path.join(base_dir, val)
            if os.path.exists(path):
                return path
    return ""


def recovery_extract_stake_total_from_text(text: str) -> Tuple[float, float]:
    """Best-effort stake/total-odds extraction from an old generated betslip image OCR."""
    raw = clean_ocr_text(str(text or ""))
    stake = None
    total_odds = None
    joined = " ".join(raw.split())

    m = re.search(r"(?i)(?:stake|bet amount|amount)\s*[:\-]?\s*[$£€]?\s*(\d+(?:\.\d+)?)", joined)
    if m:
        try:
            stake = parse_float(m.group(1))
        except Exception:
            stake = None

    if stake is None:
        m = re.search(r"(?i)[$£€]\s*(\d+(?:\.\d+)?)\s*(?:stake|bet amount|amount)\b", joined)
        if m:
            try:
                stake = parse_float(m.group(1))
            except Exception:
                stake = None

    m = re.search(r"(?i)(?:total odds|combined odds|odds total|total)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*x?", joined)
    if m:
        try:
            value = parse_float(m.group(1))
            if value > 1:
                total_odds = value
        except Exception:
            total_odds = None

    if total_odds is None:
        m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*x\s*(?:total odds|combined odds|odds total)?", joined)
        if m:
            try:
                value = parse_float(m.group(1))
                if value > 1:
                    total_odds = value
            except Exception:
                total_odds = None

    return float(stake if stake is not None else DEFAULT_STAKE), float(total_odds if total_odds is not None else 1.0)


def recovery_parse_image_bet(img_path: str) -> Tuple[Optional[Dict], str]:
    """OCR an old ticket image, then use AI first and regex parser as fallback."""
    extracted = clean_ocr_text(ocr_space(img_path))
    stake, total_odds = recovery_extract_stake_total_from_text(extracted)

    parsed = parse_bet_text_with_ai(extracted, stake=stake, total_odds=total_odds)
    if parsed:
        return parsed, extracted

    parsed = parse_bet_text(extracted)
    if parsed:
        parsed["stake"] = stake
        if total_odds > 1:
            parsed["total_odds"] = total_odds
        return parsed, extracted

    return None, extracted


def recovered_bet_exists(old_ticket_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT id FROM bets WHERE id = ?", (old_ticket_id,)).fetchone()
        return bool(row)


def create_recovered_bet(
    old_ticket_id: int,
    chat_id: int,
    admin_user_id: int,
    admin_username: str,
    bettor: str,
    stake: float,
    total_odds: float,
    legs: List[Dict],
    source_photo_message_id: int,
    accept_message_id: int,
    slip_message_id: int,
    created_at: str,
    conditions_list: Optional[List[str]] = None,
    note: Optional[str] = None,
    bet_tag: str = "",
) -> int:
    payout = round(float(stake) * float(total_odds), 2)
    profit = round(payout - float(stake), 2)
    conditions = "\n".join(conditions_list or DEFAULT_CONDITIONS)

    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO bets
            (
                id, chat_id, user_id, username, bettor,
                stake, total_odds, payout, profit,
                status, result, pnl,
                conditions, note, market_id, bet_tag, is_test,
                source_photo_message_id, accept_message_id, slip_message_id,
                summary_message_id, settled_by, settled_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?, ?, NULL, ?, 0, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                old_ticket_id,
                chat_id,
                admin_user_id,
                admin_username,
                bettor,
                float(stake),
                float(total_odds),
                payout,
                profit,
                conditions,
                note or (f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"),
                clean_bet_tag(bet_tag),
                source_photo_message_id,
                accept_message_id,
                slip_message_id,
                created_at,
            ),
        )

        for leg in legs:
            conn.execute(
                """
                INSERT INTO bet_legs (bet_id, event, selection, market, sport, odds, result)
                SELECT ?, ?, ?, ?, ?, ?, 'open'
                WHERE NOT EXISTS (SELECT 1 FROM bet_legs WHERE bet_id = ?)
                """,
                (
                    old_ticket_id,
                    leg.get("event", ""),
                    leg.get("selection", "Recovered Selection"),
                    leg.get("market", "Winner"),
                    normalize_sport_slug(leg.get("sport") or infer_sport_from_leg_text(leg.get("event", ""), leg.get("selection", ""), leg.get("market", "Winner"))),
                    float(leg.get("odds", 1)),
                    old_ticket_id,
                ),
            )

    return old_ticket_id



# -------------------------
# AI BOOK RECOVERY
# -------------------------

RECOVERY_BOOK_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "message_kind": {
            "type": "string",
            "enum": ["accepted_betslip", "settlement_receipt", "ignore"],
        },
        "ticket_id": {"type": ["integer", "string", "null"]},
        "bettor": {"type": ["string", "null"]},
        "result": {
            "type": ["string", "null"],
            "enum": ["win", "loss", "void", "", None],
        },
        "stake": {"type": ["number", "string", "null"]},
        "top_odds": {"type": ["number", "string", "null"]},
        "payout": {"type": ["number", "string", "null"]},
        "profit": {"type": ["number", "string", "null"]},
        "created_at": {"type": ["string", "null"]},
        "settled_at": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]},
        "conditions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "legs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "selection": {"type": "string"},
                    "market": {"type": "string"},
                    "sport": {
                        "type": "string",
                        "enum": [
                            "football",
                            "tennis",
                            "cricket",
                            "basketball",
                            "baseball",
                            "volleyball",
                            "boxing",
                            "mma",
                            "badminton",
                            "american_football",
                            "other",
                        ],
                    },
                    "odds": {"type": ["number", "string"]},
                },
                "required": ["event", "selection", "market", "sport", "odds"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "ok",
        "message_kind",
        "ticket_id",
        "bettor",
        "result",
        "stake",
        "top_odds",
        "payout",
        "profit",
        "created_at",
        "settled_at",
        "note",
        "conditions",
        "legs",
    ],
    "additionalProperties": False,
}


def recovery_safe_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        raw = str(value).strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "")
        raw = raw.replace("x", "").replace("X", "").replace("×", "")
        if not raw:
            return default
        return float(raw)
    except Exception:
        return default


def recovery_safe_int(value, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        m = re.search(r"\d+", str(value))
        if not m:
            return default
        return int(m.group(0))
    except Exception:
        return default


def recovery_normalize_datetime_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("T", " ").replace("Z", "").strip()
    raw = re.sub(r"\s+", " ", raw)

    # Already close enough: 2026-07-01 22:11:14
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}(?::\d{2})?)", raw)
    if m:
        date_part = m.group(1)
        time_part = m.group(2)
        if len(time_part.split(":")) == 2:
            time_part += ":00"
        return f"{date_part} {time_part}"

    return raw[:19]


def recovery_is_likely_image(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return True
    return str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def build_recovery_book_prompt(message_text: str, message_id: int, reply_to_message_id: int, message_date: str, ocr_text: str = "") -> str:
    source_bits = [
        f"Telegram message id: {message_id}",
        f"Telegram reply_to_message_id: {reply_to_message_id}",
        f"Telegram message date: {message_date}",
        "",
        "Telegram message text/caption:",
        str(message_text or "")[:4000],
    ]
    if ocr_text:
        source_bits += ["", "OCR fallback text:", str(ocr_text or "")[:8000]]

    source_text = "\n".join(source_bits)

    return f"""
You are recovering old Lenny Book betting ledger messages from Telegram history.
You will receive a Telegram bot message text/caption and possibly an image of the bot card.

Classify the message as one of:
- accepted_betslip: a Lenny Book private ticket / betting slip / bet accepted image.
- settlement_receipt: a Lenny Book ticket won/lost/void settlement receipt image or bot settlement message.
- ignore: anything else, including leaderboards, profiles, market cards, random chat, or unreadable images.

CRITICAL ODDS RULE:
- ONLY the large/top summary odds matter.
- For accepted tickets, use the ODDS value in the top stake/odds/return summary box.
- For settlement receipts, use the ODDS value in the stake/odds/payout summary box.
- If individual leg odds conflict with the top summary odds, IGNORE the discrepancy.
- NEVER multiply leg odds to create total odds. NEVER replace top_odds with leg odds.
- Example: if top odds says 1.52x but the leg says 1.45, return top_odds 1.52.
- Example: if top odds says 3.75x but listed legs multiply differently, return top_odds 3.75.

Extract:
- ticket_id: Ticket #270, #00270, #00241 -> 270 or 241.
- bettor: @username or USER box value; omit @. If caption has @id1ss, bettor is id1ss.
- result: win/loss/void only for settlement_receipt, otherwise empty.
- stake: top summary stake only.
- top_odds: top summary odds only, as decimal number.
- payout: return/payout from top summary or settlement card.
- profit: profit from settlement card if visible.
- created_at and settled_at: use card values if visible in YYYY-MM-DD HH:MM:SS; otherwise empty.
- legs: actual legs only. Include individual leg odds as displayed, but those do NOT control top_odds.
- sport: infer from the card icon/text/names.
- conditions: use visible void rules if present; otherwise use default rules.
- note: Single Bet, 2 Leg Multi, etc.

Return ONLY strict JSON matching this shape:
{{
  "ok": true,
  "message_kind": "accepted_betslip",
  "ticket_id": 270,
  "bettor": "sandrcw",
  "result": "",
  "stake": 65.00,
  "top_odds": 1.52,
  "payout": 98.80,
  "profit": null,
  "created_at": "2026-07-02 13:24:00",
  "settled_at": "",
  "note": "Single Bet",
  "conditions": ["Any retirement or walkover = leg void."],
  "legs": [
    {{"event":"Brandon Nakashima vs Jan-Lennard Struff", "selection":"Brandon Nakashima", "market":"Winner", "sport":"tennis", "odds":1.45}}
  ]
}}

If this is not a recoverable Lenny Book accepted ticket or settlement receipt, return:
{{"ok": false, "message_kind": "ignore", "ticket_id": null, "bettor": "", "result": "", "stake": null, "top_odds": null, "payout": null, "profit": null, "created_at": "", "settled_at": "", "note": "", "conditions": [], "legs": []}}

SOURCE:
{source_text}
""".strip()


def call_venice_recovery_book_ai(prompt: str, image_path: str = "") -> Dict:
    """
    TEXT-ONLY Venice call for /recoverbetsbook.
    Important: image_path is intentionally ignored. This function must NEVER send
    image content to Venice, because the selected Venice model is text-only.
    OCR must happen before this function and only OCR text/caption is sent here.
    """
    if not VENICE_API_KEY:
        raise RuntimeError("VENICE_API_KEY missing.")

    url = "https://api.venice.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {VENICE_API_KEY}",
        "Content-Type": "application/json",
    }

    base_payload = {
        "model": VENICE_RECOVERY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You recover Lenny Book betting tickets from OCR/plain Telegram text. "
                    "Use the top summary odds only. Return only strict JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_completion_tokens": 2200,
        "venice_parameters": {
            "enable_web_search": "off",
            "include_venice_system_prompt": False,
            "disable_thinking": True,
            "strip_thinking_response": True,
        },
    }

    payloads = []
    structured_payload = dict(base_payload)
    structured_payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "lenny_book_recovery_message",
            "strict": True,
            "schema": RECOVERY_BOOK_JSON_SCHEMA,
        },
    }
    payloads.append(structured_payload)
    payloads.append(base_payload)

    last_error = None
    for payload in payloads:
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=AI_BETSLIP_TIMEOUT)
            if response.status_code >= 400:
                body = response.text[:700]
                last_error = RuntimeError(f"Venice HTTP {response.status_code}: {body}")
                if AI_BETSLIP_DEBUG:
                    print(last_error)
                continue

            data = response.json()
            try:
                message = data["choices"][0]["message"]
                text = message.get("content") or message.get("reasoning_content") or ""
            except Exception:
                raise RuntimeError(f"Bad Venice response: {str(data)[:500]}")

            return extract_json_object(text)
        except Exception as e:
            last_error = e
            if AI_BETSLIP_DEBUG:
                print(f"Venice recovery parser failed: {e}")

    raise RuntimeError(str(last_error) if last_error else "Venice recovery request failed.")


def validate_recovery_book_record(data: Dict, fallback_text: str = "") -> Optional[Dict]:
    if not isinstance(data, dict) or not data.get("ok"):
        return None

    kind = str(data.get("message_kind") or "").strip().lower()
    if kind not in {"accepted_betslip", "settlement_receipt"}:
        return None

    ticket_id = recovery_safe_int(data.get("ticket_id"))
    if not ticket_id:
        ticket_id = recovery_extract_ticket_id(fallback_text)
    if not ticket_id:
        return None

    stake = recovery_safe_float(data.get("stake"))
    top_odds = recovery_safe_float(data.get("top_odds"))
    payout = recovery_safe_float(data.get("payout"))

    # Only use the top odds from the card. If that was unreadable, the only safe fallback is payout / stake.
    # Do NOT multiply leg odds here.
    if (top_odds is None or top_odds <= 1) and payout and stake and stake > 0:
        top_odds = round(float(payout) / float(stake), 4)

    if stake is None or stake <= 0 or top_odds is None or top_odds <= 1:
        return None

    profit = recovery_safe_float(data.get("profit"))
    if payout is None or payout <= 0:
        payout = round(float(stake) * float(top_odds), 2)
    if profit is None and kind == "settlement_receipt":
        profit = round(float(payout) - float(stake), 2)

    bettor = str(data.get("bettor") or "").strip()
    if not bettor or bettor.lower() in {"unknown", "none", "null"}:
        bettor = recovery_extract_bettor_from_accept_caption(fallback_text)
    bettor = canonicalize_bettor(bettor) or "unknown"

    result = str(data.get("result") or "").strip().lower()
    if kind == "settlement_receipt":
        if result not in {"win", "loss", "void"}:
            extracted_result, _ = recovery_extract_settlement(fallback_text)
            result = extracted_result or ""
        if result not in {"win", "loss", "void"}:
            return None
    else:
        result = ""

    legs = []
    seen = set()
    raw_legs = data.get("legs") or []
    if isinstance(raw_legs, list):
        for raw_leg in raw_legs:
            if not isinstance(raw_leg, dict):
                continue
            leg = normalize_ai_leg(raw_leg)
            if not leg:
                continue
            key = (
                leg["event"].lower(),
                leg["selection"].lower(),
                leg["market"].lower(),
                str(leg.get("sport", "other")).lower(),
                float(leg["odds"]),
            )
            if key in seen:
                continue
            seen.add(key)
            legs.append(leg)

    if not legs:
        legs = [
            {
                "event": "Recovered Lenny Book Ticket",
                "selection": "Recovered Selection",
                "market": "Recovered",
                "sport": "other",
                "odds": round(float(top_odds), 4),
            }
        ]

    conditions = data.get("conditions") or []
    if not isinstance(conditions, list):
        conditions = []
    conditions = [str(x).strip() for x in conditions if str(x).strip()]
    if not conditions:
        conditions = DEFAULT_CONDITIONS

    note = str(data.get("note") or "").strip()
    if not note:
        note = f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet"

    return {
        "kind": kind,
        "ticket_id": int(ticket_id),
        "bettor": bettor,
        "result": result,
        "stake": round(float(stake), 2),
        "total_odds": round(float(top_odds), 4),
        "payout": round(float(payout), 2),
        "profit": round(float(profit), 2) if profit is not None else round(float(payout) - float(stake), 2),
        "created_at": recovery_normalize_datetime_text(data.get("created_at") or ""),
        "settled_at": recovery_normalize_datetime_text(data.get("settled_at") or ""),
        "note": note[:120],
        "conditions": conditions,
        "legs": legs,
    }


def parse_recovery_book_message_with_ai(
    message_text: str,
    image_path: str,
    message_id: int,
    reply_to_message_id: int,
    message_date: str,
) -> Optional[Dict]:
    """
    OCR-FIRST recovery parser.
    This never sends the image to Venice. It downloads the old bot card, OCRs it,
    then sends only text/caption + OCR text to Venice.
    """
    ocr_text = ""

    if image_path and recovery_is_likely_image(image_path):
        try:
            ocr_text = clean_ocr_text(ocr_space(image_path))
        except Exception as e:
            if AI_BETSLIP_DEBUG:
                print(f"AI book recovery OCR failed for message {message_id}: {str(e)[:500]}")
            ocr_text = ""

    combined_fallback = f"{message_text}\n{ocr_text}".strip()

    # Do not waste Venice calls on blank media where both caption and OCR are empty.
    if not combined_fallback.strip():
        return None

    prompt = build_recovery_book_prompt(
        message_text=message_text,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
        message_date=message_date,
        ocr_text=ocr_text,
    )

    try:
        # image_path intentionally blank: TEXT ONLY.
        data = call_venice_recovery_book_ai(prompt, image_path="")
        return validate_recovery_book_record(data, fallback_text=combined_fallback)
    except Exception as e:
        if AI_BETSLIP_DEBUG:
            print(f"AI book recovery text pass failed for message {message_id}: {str(e)[:700]}")
        return None


def recovery_book_import_or_settle(
    record: Dict,
    bot_chat_id: int,
    admin_user_id: int,
    admin_username: str,
    old_message_id: int,
    reply_message_id: int,
    message_date: str,
) -> Dict[str, int]:
    stats = {"imported": 0, "already": 0, "settled": 0, "already_settled": 0, "failed": 0}

    ticket_id = int(record["ticket_id"])
    existing = get_bet_by_ticket_id(ticket_id)

    if not existing:
        bettor = record.get("bettor") or "unknown"
        if bettor != "unknown" and not user_exists(bettor):
            add_user_to_json(bettor)

        created_at = record.get("created_at") or message_date or now_str()
        create_recovered_bet(
            old_ticket_id=ticket_id,
            chat_id=bot_chat_id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            bettor=bettor,
            stake=float(record["stake"]),
            total_odds=float(record["total_odds"]),
            legs=record.get("legs") or [],
            source_photo_message_id=reply_message_id or old_message_id,
            accept_message_id=reply_message_id or old_message_id,
            slip_message_id=old_message_id,
            created_at=created_at,
            conditions_list=record.get("conditions") or DEFAULT_CONDITIONS,
            note=record.get("note"),
            bet_tag="",
        )
        stats["imported"] += 1
    else:
        stats["already"] += 1

    if record.get("kind") == "settlement_receipt" and record.get("result"):
        bet = get_bet_by_ticket_id(ticket_id)
        if not bet:
            stats["failed"] += 1
            return stats

        if str(bet["status"] or "").lower() == "settled":
            stats["already_settled"] += 1
            return stats

        adjusted_odds = float(record["total_odds"]) if record.get("result") == "win" else None
        ok, msg_text, _ = settle_bet(
            int(bet["id"]),
            str(record["result"]),
            admin_user_id,
            adjusted_odds=adjusted_odds,
        )
        if ok:
            stats["settled"] += 1
            # Preserve settled_at from the old card if Venice could read it.
            if record.get("settled_at"):
                with db() as conn:
                    conn.execute(
                        "UPDATE bets SET settled_at = ? WHERE id = ?",
                        (record["settled_at"], int(bet["id"])),
                    )
        elif "already settled" in str(msg_text).lower():
            stats["already_settled"] += 1
        else:
            stats["failed"] += 1

    return stats


def recovery_book_is_interesting_message(text: str, has_media: bool) -> bool:
    low = str(text or "").lower()
    if any(k in low for k in ["lenny book", "ticket", "bet has been accepted", "bet settled", "settlement", "regenerated"]):
        return True
    return bool(has_media)


async def recover_betsbook_from_telethon_history(
    bot_chat_id: int,
    admin_user_id: int,
    admin_username: str,
    status_message=None,
    bot_user_id: Optional[int] = None,
) -> Dict[str, int]:
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    if not VENICE_API_KEY:
        raise RuntimeError("VENICE_API_KEY missing. /recoverbetsbook needs Venice AI.")

    os.makedirs(TELEGRAM_RECOVERY_MEDIA_DIR, exist_ok=True)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_current_chat(client, bot_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. "
            "Make sure the Telegram account used for the Telethon session is a member of this exact group. "
            "Details: " + safe_text(e)
        )

    stats = {
        "scanned": 0,
        "bot_messages": 0,
        "media_downloaded": 0,
        "ai_checked": 0,
        "detected": 0,
        "accepted_detected": 0,
        "settlement_detected": 0,
        "imported": 0,
        "already": 0,
        "settled": 0,
        "already_settled": 0,
        "failed": 0,
        "skipped": 0,
        "not_bot": 0,
    }

    bot_user_id = int(bot_user_id or RECOVERY_BOOK_BOT_USER_ID or 0) or None

    try:
        async for old_msg in client.iter_messages(entity, reverse=True, limit=(TELEGRAM_RECOVERY_LIMIT or None)):
            stats["scanned"] += 1

            if bot_user_id is not None:
                sender_id = int(getattr(old_msg, "sender_id", 0) or 0)
                if sender_id and sender_id != int(bot_user_id):
                    stats["not_bot"] += 1
                    continue

            stats["bot_messages"] += 1
            text = telethon_message_text(old_msg)
            has_media = bool(getattr(old_msg, "media", None))

            if not recovery_book_is_interesting_message(text, has_media):
                stats["skipped"] += 1
                continue

            old_message_id = int(getattr(old_msg, "id", 0) or 0)
            reply_message_id = telethon_reply_message_id(old_msg)
            message_date = telethon_message_datetime(old_msg)

            img_path = ""
            if has_media:
                try:
                    img_path = await old_msg.download_media(file=TELEGRAM_RECOVERY_MEDIA_DIR)
                    if img_path and os.path.exists(img_path):
                        stats["media_downloaded"] += 1
                    else:
                        img_path = ""
                except Exception as e:
                    img_path = ""
                    if AI_BETSLIP_DEBUG:
                        print(f"Could not download recovery media for message {old_message_id}: {e}")

            if img_path and not recovery_is_likely_image(img_path):
                # Non-image documents are ignored. Images are OCRed only, never sent to Venice.
                img_path = ""

            stats["ai_checked"] += 1
            record = parse_recovery_book_message_with_ai(
                message_text=text,
                image_path=img_path,
                message_id=old_message_id,
                reply_to_message_id=reply_message_id,
                message_date=message_date,
            )

            if not record:
                stats["skipped"] += 1
                if stats["ai_checked"] % 25 == 0 and status_message:
                    try:
                        await status_message.edit_text(
                            "♻️ /recoverbetsbook AI scan running...\n"
                            f"Scanned: <b>{stats['scanned']}</b> | Bot messages: <b>{stats['bot_messages']}</b>\n"
                            f"AI checked: <b>{stats['ai_checked']}</b> | Detected: <b>{stats['detected']}</b>\n"
                            f"Imported: <b>{stats['imported']}</b> | Settled: <b>{stats['settled']}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                continue

            stats["detected"] += 1
            if record.get("kind") == "accepted_betslip":
                stats["accepted_detected"] += 1
            elif record.get("kind") == "settlement_receipt":
                stats["settlement_detected"] += 1

            apply_stats = recovery_book_import_or_settle(
                record=record,
                bot_chat_id=bot_chat_id,
                admin_user_id=admin_user_id,
                admin_username=admin_username,
                old_message_id=old_message_id,
                reply_message_id=reply_message_id,
                message_date=message_date,
            )
            for key, value in apply_stats.items():
                if key in stats:
                    stats[key] += int(value or 0)

            if stats["detected"] % 10 == 0 and status_message:
                try:
                    await status_message.edit_text(
                        "♻️ /recoverbetsbook AI scan running...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Bot messages: <b>{stats['bot_messages']}</b>\n"
                        f"Detected: <b>{stats['detected']}</b> "
                        f"(accepted <b>{stats['accepted_detected']}</b>, settled <b>{stats['settlement_detected']}</b>)\n"
                        f"Imported: <b>{stats['imported']}</b> | Already: <b>{stats['already']}</b>\n"
                        f"Settled now: <b>{stats['settled']}</b> | Already settled: <b>{stats['already_settled']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    finally:
        await client.disconnect()

    return stats


async def recoverbetsbook_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    user = update.effective_user
    admin_user_id = user.id if user else 0
    admin_username = user.username or user.full_name if user else "recovery"

    bot_user_id = RECOVERY_BOOK_BOT_USER_ID or int(context.bot.id)

    status = await update.message.reply_text(
        "♻️ /recoverbetsbook started. Venice AI is scanning old bot messages/cards and using TOP odds only...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await recover_betsbook_from_telethon_history(
            bot_chat_id=update.effective_chat.id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            status_message=status,
            bot_user_id=bot_user_id,
        )
        await status.edit_text(
            "✅ /recoverbetsbook complete\n"
            f"Scanned: <b>{stats['scanned']}</b>\n"
            f"Bot messages checked: <b>{stats['bot_messages']}</b>\n"
            f"Media downloaded: <b>{stats['media_downloaded']}</b>\n"
            f"AI checked: <b>{stats['ai_checked']}</b>\n"
            f"Detected tickets: <b>{stats['detected']}</b> "
            f"(accepted <b>{stats['accepted_detected']}</b>, settlement <b>{stats['settlement_detected']}</b>)\n"
            f"Imported: <b>{stats['imported']}</b>\n"
            f"Already existed: <b>{stats['already']}</b>\n"
            f"Settled now: <b>{stats['settled']}</b>\n"
            f"Already settled: <b>{stats['already_settled']}</b>\n"
            f"Failed: <b>{stats['failed']}</b>\n"
            f"Skipped/ignored: <b>{stats['skipped']}</b>\n\n"
            "Top odds rule was used. Leg-odds discrepancies were ignored.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        err = safe_text(str(e))[:3500]
        await status.edit_text(
            "❌ /recoverbetsbook failed\n"
            f"<code>{err}</code>",
            parse_mode=ParseMode.HTML,
        )



# -------------------------
# /recoverbet LB PLACEHOLDER FIXER
# -------------------------

def recoverbet_is_lb_ticket_row(bet_id: int) -> bool:
    """True when an existing ticket still has placeholder LB data in its legs/note."""
    with db() as conn:
        row = conn.execute("SELECT id, note FROM bets WHERE id = ?", (int(bet_id),)).fetchone()
        if not row:
            return False
        note = str(row_value(row, "note", "") or "").strip().lower()
        if note == "lb" or " lb " in f" {note} ":
            return True
        leg = conn.execute(
            """
            SELECT id FROM bet_legs
            WHERE bet_id = ?
            AND (
                LOWER(COALESCE(event, '')) LIKE '%lb%'
                OR LOWER(COALESCE(selection, '')) LIKE '%lb%'
                OR LOWER(COALESCE(market, '')) LIKE '%lb%'
            )
            LIMIT 1
            """,
            (int(bet_id),),
        ).fetchone()
        return bool(leg)


def recoverbet_lb_ticket_ids(requested_ids: Optional[List[int]] = None) -> List[int]:
    """Return ticket ids that still contain LB placeholder legs."""
    ids = []
    with db() as conn:
        if requested_ids:
            candidates = [int(x) for x in requested_ids]
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT b.id
                FROM bets b
                LEFT JOIN bet_legs bl ON bl.bet_id = b.id
                WHERE LOWER(COALESCE(b.note, '')) LIKE '%lb%'
                   OR LOWER(COALESCE(bl.event, '')) LIKE '%lb%'
                   OR LOWER(COALESCE(bl.selection, '')) LIKE '%lb%'
                   OR LOWER(COALESCE(bl.market, '')) LIKE '%lb%'
                ORDER BY b.id
                """
            ).fetchall()
            candidates = [int(r["id"]) for r in rows]

    for tid in candidates:
        if recoverbet_is_lb_ticket_row(int(tid)):
            ids.append(int(tid))
    return sorted(set(ids))


def recoverbet_update_existing_bet(ticket_id: int, parsed: Dict, message_id: int = 0, message_date: str = "") -> None:
    """Replace LB placeholder leg data with parsed OCR/AI betslip data while preserving settlement result."""
    with db() as conn:
        bet = conn.execute("SELECT * FROM bets WHERE id = ?", (int(ticket_id),)).fetchone()
        if not bet:
            raise RuntimeError(f"Ticket #{ticket_id} not found in database.")

        stake = float(parsed.get("stake") or row_value(bet, "stake", 0) or 0)
        total_odds = float(parsed.get("total_odds") or row_value(bet, "total_odds", 1) or 1)
        payout = round(stake * total_odds, 2)
        profit = round(payout - stake, 2)

        status = str(row_value(bet, "status", "open") or "open").strip().lower()
        result = str(row_value(bet, "result", "") or "").strip().lower()
        if status == "settled" and result == "win":
            pnl = profit
            leg_result = "win"
        elif status == "settled" and result == "loss":
            pnl = -stake
            leg_result = "loss"
        elif status == "settled" and result == "void":
            pnl = 0.0
            leg_result = "void"
        else:
            pnl = float(row_value(bet, "pnl", 0) or 0)
            leg_result = "open"

        legs = parsed.get("legs") or []
        note = parsed.get("note") or (f"{len(legs)} Leg Multi" if len(legs) > 1 else "Single Bet")
        conditions = "\n".join(parsed.get("conditions") or DEFAULT_CONDITIONS)

        conn.execute(
            """
            UPDATE bets
            SET stake = ?, total_odds = ?, payout = ?, profit = ?, pnl = ?,
                conditions = ?, note = ?,
                source_photo_message_id = COALESCE(NULLIF(source_photo_message_id, 0), ?),
                accept_message_id = COALESCE(NULLIF(accept_message_id, 0), ?),
                slip_message_id = COALESCE(NULLIF(slip_message_id, 0), ?),
                created_at = COALESCE(NULLIF(created_at, ''), ?)
            WHERE id = ?
            """,
            (
                stake,
                total_odds,
                payout,
                profit,
                pnl,
                conditions,
                note,
                int(message_id or 0),
                int(message_id or 0),
                int(message_id or 0),
                message_date or now_str(),
                int(ticket_id),
            ),
        )

        conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (int(ticket_id),))
        for leg in legs:
            event = str(leg.get("event") or "").strip()
            selection = str(leg.get("selection") or "Recovered Selection").strip()
            market = str(leg.get("market") or "Winner").strip()
            sport = normalize_sport_slug(
                leg.get("sport") or infer_sport_from_leg_text(event, selection, market)
            )
            odds = float(leg.get("odds") or 1)
            conn.execute(
                """
                INSERT INTO bet_legs (bet_id, event, selection, market, sport, odds, result)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (int(ticket_id), event, selection, market, sport, odds, leg_result),
            )


def recoverbet_accept_text_matches_ticket(text: str, ticket_id: int) -> bool:
    low = str(text or "").lower()
    return "bet has been accepted" in low and recovery_extract_ticket_id(text) == int(ticket_id)


async def recoverbet_find_accept_message(client, entity, ticket_id: int, scan_limit: Optional[int] = None):
    """Search Telegram history for: bet has been accepted. Ticket #N"""
    search_text = f"bet has been accepted. Ticket #{int(ticket_id)}"
    try:
        async for msg in client.iter_messages(entity, search=search_text, limit=20):
            if recoverbet_accept_text_matches_ticket(telethon_message_text(msg), ticket_id):
                return msg
    except Exception:
        pass

    # Fallback search without punctuation because Telegram search can be picky.
    try:
        async for msg in client.iter_messages(entity, search=f"Ticket #{int(ticket_id)}", limit=50):
            if recoverbet_accept_text_matches_ticket(telethon_message_text(msg), ticket_id):
                return msg
    except Exception:
        pass

    # Last resort: scan recent history.
    async for msg in client.iter_messages(entity, reverse=False, limit=(scan_limit or TELEGRAM_RECOVERY_LIMIT or 2000)):
        if recoverbet_accept_text_matches_ticket(telethon_message_text(msg), ticket_id):
            return msg
    return None


async def recoverbet_from_telethon_history(
    bot_chat_id: int,
    ticket_ids: Optional[List[int]],
    status_message=None,
    scan_limit: Optional[int] = None,
) -> Dict[str, int]:
    """
    Fix existing LB placeholder tickets.

    Flow for each LB ticket:
    1) Look in the database for tickets whose legs/note contain LB.
    2) Search Telegram for the accepted message: "bet has been accepted. Ticket #X".
    3) Download that accepted betslip image/card.
    4) OCR it, send OCR text to Venice through the existing parser, and update stake/odds/legs.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)
    if not VENICE_API_KEY and AI_BETSLIP_PROVIDER == "venice":
        raise RuntimeError("VENICE_API_KEY missing. /recoverbet needs Venice AI for LB cards.")

    targets = recoverbet_lb_ticket_ids(ticket_ids)
    stats = {
        "targets": len(targets),
        "searched": 0,
        "found_message": 0,
        "downloaded": 0,
        "ocr_checked": 0,
        "updated": 0,
        "not_found": 0,
        "no_media": 0,
        "parse_failed": 0,
        "failed": 0,
        "last_error": "",
    }
    if not targets:
        return stats

    os.makedirs(TELEGRAM_RECOVERY_MEDIA_DIR, exist_ok=True)
    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_current_chat(client, bot_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. Make sure the Telethon account is in this group. "
            "Details: " + safe_text(e)
        )

    try:
        for idx, ticket_id in enumerate(targets, start=1):
            stats["searched"] += 1
            img_path = ""
            try:
                msg = await recoverbet_find_accept_message(client, entity, int(ticket_id), scan_limit=scan_limit)
                if not msg:
                    stats["not_found"] += 1
                    continue
                stats["found_message"] += 1

                if not getattr(msg, "media", None):
                    stats["no_media"] += 1
                    continue

                img_path = await msg.download_media(file=TELEGRAM_RECOVERY_MEDIA_DIR)
                if not img_path or not os.path.exists(img_path) or not recovery_is_likely_image(img_path):
                    stats["no_media"] += 1
                    continue
                stats["downloaded"] += 1

                stats["ocr_checked"] += 1
                parsed, extracted = recovery_parse_image_bet(img_path)
                if not parsed:
                    stats["parse_failed"] += 1
                    if AI_BETSLIP_DEBUG:
                        print(f"/recoverbet parse failed for #{ticket_id}: {str(extracted)[:800]}")
                    continue

                recoverbet_update_existing_bet(
                    ticket_id=int(ticket_id),
                    parsed=parsed,
                    message_id=int(getattr(msg, "id", 0) or 0),
                    message_date=telethon_message_datetime(msg),
                )
                stats["updated"] += 1

            except Exception as e:
                stats["failed"] += 1
                stats["last_error"] = str(e)[:500]
                if AI_BETSLIP_DEBUG:
                    print(f"/recoverbet failed for #{ticket_id}: {e}")
            finally:
                if img_path:
                    try:
                        os.remove(img_path)
                    except Exception:
                        pass

            if status_message and (idx % 5 == 0 or stats["updated"] % 5 == 0):
                try:
                    await status_message.edit_text(
                        "♻️ /recoverbet LB fixer running...\n"
                        f"Targets: <b>{stats['targets']}</b> | Searched: <b>{stats['searched']}</b>\n"
                        f"Found messages: <b>{stats['found_message']}</b> | OCR checked: <b>{stats['ocr_checked']}</b>\n"
                        f"Updated: <b>{stats['updated']}</b> | Parse failed: <b>{stats['parse_failed']}</b> | Not found: <b>{stats['not_found']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    finally:
        await client.disconnect()

    return stats


async def recoverbet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return
    if not update.message:
        return

    # No args = fix every DB ticket that still contains LB.
    # Args supported: /recoverbet 206 OR /recoverbet 206-220 OR /recoverbet 206 210 211
    requested_ids = parse_textbet_ticket_ids(context.args) if context.args else None
    scan_limit = 0

    user = update.effective_user
    admin_username = user.username or user.full_name if user else "recoverbet"
    _ = admin_username  # kept for consistency with other recovery commands

    preview_targets = recoverbet_lb_ticket_ids(requested_ids)
    if not preview_targets:
        await update.message.reply_text(
            "No LB placeholder tickets found" + (" in that range." if requested_ids else ".")
        )
        return

    status = await update.message.reply_text(
        "♻️ /recoverbet started. Fixing LB placeholder tickets by finding their accepted betslip messages, OCRing the card, sending OCR to Venice AI, and updating the DB.",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await recoverbet_from_telethon_history(
            bot_chat_id=update.effective_chat.id,
            ticket_ids=requested_ids,
            status_message=status,
            scan_limit=scan_limit,
        )
        msg = (
            "✅ /recoverbet complete\n"
            f"LB targets: <b>{stats['targets']}</b>\n"
            f"Searched: <b>{stats['searched']}</b>\n"
            f"Accepted messages found: <b>{stats['found_message']}</b>\n"
            f"Images downloaded: <b>{stats['downloaded']}</b>\n"
            f"OCR/Venice checked: <b>{stats['ocr_checked']}</b>\n"
            f"Updated: <b>{stats['updated']}</b>\n"
            f"Not found in Telegram search: <b>{stats['not_found']}</b>\n"
            f"No image/media: <b>{stats['no_media']}</b>\n"
            f"Parse failed: <b>{stats['parse_failed']}</b>\n"
            f"Failed: <b>{stats['failed']}</b>"
        )
        if stats.get("last_error"):
            msg += f"\nLast error: <code>{safe_text(stats['last_error'])}</code>"
        await status.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status.edit_text(
            "❌ /recoverbet failed\n"
            f"<code>{safe_text(str(e))[:3500]}</code>",
            parse_mode=ParseMode.HTML,
        )

# -------------------------
# /betsrecover IMAGE-ONLY GROUP RECOVERY
# -------------------------

def betsrecover_caption_has_ticket(text: str) -> bool:
    """Only captions/text that include a Ticket # are eligible for /betsrecover."""
    return recovery_extract_ticket_id(str(text or "")) is not None


async def betsrecover_from_telethon_history(
    bot_chat_id: int,
    admin_user_id: int,
    admin_username: str,
    status_message=None,
    limit: int = 0,
) -> Dict[str, int]:
    """
    Recover accepted betslip PHOTOS from this group.

    Important behavior requested:
    - scan image/media messages only;
    - use the Telegram caption only for Ticket # and bettor name;
    - OCR/parse the image itself for stake, top odds, payout and bet legs;
    - import every recovered ticket as OPEN/UNSETTLED only;
    - do not read normal text-only messages and do not settle anything.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    os.makedirs(TELEGRAM_RECOVERY_MEDIA_DIR, exist_ok=True)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_current_chat(client, bot_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. "
            "Make sure the Telegram account used for the Telethon session is a member of this exact group. "
            "Details: " + safe_text(e)
        )

    scan_limit = int(limit or TELEGRAM_RECOVERY_LIMIT or 0) or None
    stats = {
        "scanned": 0,
        "media_messages": 0,
        "caption_ticket": 0,
        "media_downloaded": 0,
        "ocr_checked": 0,
        "imported": 0,
        "already": 0,
        "failed": 0,
        "skipped_no_media": 0,
        "skipped_no_ticket_caption": 0,
        "skipped_non_image": 0,
        "skipped_parse": 0,
        "last_error": "",
    }

    try:
        async for old_msg in client.iter_messages(entity, reverse=True, limit=scan_limit):
            stats["scanned"] += 1

            # IMAGE ONLY: ignore text-only Telegram messages completely.
            if not getattr(old_msg, "media", None):
                stats["skipped_no_media"] += 1
                continue

            stats["media_messages"] += 1
            caption = telethon_message_text(old_msg)
            ticket_id = recovery_extract_ticket_id(caption)
            if not ticket_id:
                stats["skipped_no_ticket_caption"] += 1
                continue

            stats["caption_ticket"] += 1

            if recovered_bet_exists(int(ticket_id)):
                stats["already"] += 1
                continue

            old_message_id = int(getattr(old_msg, "id", 0) or 0)
            reply_message_id = telethon_reply_message_id(old_msg)
            message_date = telethon_message_datetime(old_msg)

            img_path = ""
            try:
                img_path = await old_msg.download_media(file=TELEGRAM_RECOVERY_MEDIA_DIR)
                if not img_path or not os.path.exists(img_path):
                    stats["failed"] += 1
                    continue
                stats["media_downloaded"] += 1

                if not recovery_is_likely_image(img_path):
                    stats["skipped_non_image"] += 1
                    continue

                # Parse from OCR of the IMAGE ONLY. Caption is not passed into the parser.
                stats["ocr_checked"] += 1
                parsed, extracted = recovery_parse_image_bet(img_path)
                if not parsed:
                    stats["skipped_parse"] += 1
                    if AI_BETSLIP_DEBUG:
                        print(f"/betsrecover parse failed for ticket {ticket_id}: {str(extracted)[:500]}")
                    continue

                bettor = recovery_extract_bettor_from_accept_caption(caption)
                if bettor and bettor != "unknown" and not user_exists(bettor):
                    add_user_to_json(bettor)

                create_recovered_bet(
                    old_ticket_id=int(ticket_id),
                    chat_id=bot_chat_id,
                    admin_user_id=admin_user_id,
                    admin_username=admin_username,
                    bettor=bettor or "unknown",
                    stake=float(parsed["stake"]),
                    total_odds=float(parsed["total_odds"]),
                    legs=parsed.get("legs") or [],
                    source_photo_message_id=reply_message_id or old_message_id,
                    accept_message_id=old_message_id,
                    slip_message_id=old_message_id,
                    created_at=message_date,
                    conditions_list=parsed.get("conditions") or DEFAULT_CONDITIONS,
                    note=parsed.get("note"),
                    bet_tag="",
                )

                # Force open/unsettled, even if an old helper default ever changes later.
                with db() as conn:
                    conn.execute(
                        "UPDATE bets SET status = 'open', result = NULL, pnl = 0, settled_by = NULL, settled_at = NULL WHERE id = ?",
                        (int(ticket_id),),
                    )
                    conn.execute(
                        "UPDATE bet_legs SET result = 'open' WHERE bet_id = ?",
                        (int(ticket_id),),
                    )

                stats["imported"] += 1

            except Exception as e:
                stats["failed"] += 1
                stats["last_error"] = str(e)[:500]
                if AI_BETSLIP_DEBUG:
                    print(f"/betsrecover failed for message {old_message_id}: {e}")
            finally:
                # Keep the recovery folder clean while scanning thousands of messages.
                if img_path:
                    try:
                        os.remove(img_path)
                    except Exception:
                        pass

            if status_message and (stats["ocr_checked"] % 10 == 0 or stats["imported"] % 10 == 0):
                try:
                    await status_message.edit_text(
                        "♻️ /betsrecover image-only scan running...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Images with ticket captions: <b>{stats['caption_ticket']}</b>\n"
                        f"OCR checked: <b>{stats['ocr_checked']}</b> | Imported open bets: <b>{stats['imported']}</b>\n"
                        f"Already existed: <b>{stats['already']}</b> | Parse skipped: <b>{stats['skipped_parse']}</b> | Failed: <b>{stats['failed']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    finally:
        await client.disconnect()

    return stats


async def betsrecover_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    limit = 0
    if context.args:
        try:
            limit = max(0, int(context.args[0]))
        except Exception:
            await update.message.reply_text("Use: <code>/betsrecover</code> or <code>/betsrecover 500</code>", parse_mode=ParseMode.HTML)
            return

    user = update.effective_user
    admin_user_id = user.id if user else 0
    admin_username = user.username or user.full_name if user else "betsrecover"

    status = await update.message.reply_text(
        "♻️ /betsrecover started. Scanning image messages only. Ticket number comes from caption; bet data comes from OCR of the image.",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await betsrecover_from_telethon_history(
            bot_chat_id=update.effective_chat.id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            status_message=status,
            limit=limit,
        )
        msg = (
            "✅ /betsrecover complete\n"
            f"Scanned: <b>{stats['scanned']}</b>\n"
            f"Media messages: <b>{stats['media_messages']}</b>\n"
            f"Images with ticket captions: <b>{stats['caption_ticket']}</b>\n"
            f"OCR checked: <b>{stats['ocr_checked']}</b>\n"
            f"Imported as unsettled/open: <b>{stats['imported']}</b>\n"
            f"Already existed: <b>{stats['already']}</b>\n"
            f"Skipped no image/media: <b>{stats['skipped_no_media']}</b>\n"
            f"Skipped no ticket caption: <b>{stats['skipped_no_ticket_caption']}</b>\n"
            f"Skipped non-image media: <b>{stats['skipped_non_image']}</b>\n"
            f"OCR/parse skipped: <b>{stats['skipped_parse']}</b>\n"
            f"Failed: <b>{stats['failed']}</b>"
        )
        if stats.get("last_error"):
            msg += f"\nLast error: <code>{safe_text(stats['last_error'])}</code>"
        msg += "\n\nAll recovered tickets were left OPEN/UNSETTLED."
        await status.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status.edit_text(
            "❌ /betsrecover failed\n"
            f"<code>{safe_text(str(e))[:3500]}</code>",
            parse_mode=ParseMode.HTML,
        )

def recover_from_telegram_export(json_path: str, media_base_dir: str, chat_id: int, admin_user_id: int, admin_username: str) -> Dict[str, int]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        raise ValueError("Telegram export JSON must contain a messages list.")

    id_to_msg = {int(m.get("id")): m for m in messages if isinstance(m, dict) and str(m.get("id", "")).isdigit()}
    imported = skipped = failed = settled = already = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = recovery_message_text(msg)
        if not recovery_is_accept_slip(text):
            continue

        old_ticket_id = recovery_extract_ticket_id(text)
        slip_message_id = int(msg.get("id") or 0)
        if not old_ticket_id:
            skipped += 1
            continue
        if recovered_bet_exists(old_ticket_id):
            already += 1
            continue

        img_path = recovery_photo_path(msg, media_base_dir)
        if not img_path:
            failed += 1
            continue

        try:
            parsed, extracted = recovery_parse_image_bet(img_path)
        except Exception as e:
            if AI_BETSLIP_DEBUG:
                print(f"Recovery OCR/AI parse failed for ticket {old_ticket_id}: {e}")
            parsed = None

        if not parsed:
            failed += 1
            continue

        bettor = recovery_extract_bettor_from_accept_caption(text)
        if bettor != "unknown" and not user_exists(bettor):
            add_user_to_json(bettor)

        create_recovered_bet(
            old_ticket_id=old_ticket_id,
            chat_id=chat_id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            bettor=bettor,
            stake=float(parsed["stake"]),
            total_odds=float(parsed["total_odds"]),
            legs=parsed.get("legs") or [],
            source_photo_message_id=int(msg.get("reply_to_message_id") or slip_message_id),
            accept_message_id=int(msg.get("reply_to_message_id") or slip_message_id),
            slip_message_id=slip_message_id,
            created_at=recovery_parse_datetime(msg),
            conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
            note=parsed.get("note"),
            bet_tag="",
        )
        imported += 1

    # Second pass: apply settlements after all tickets exist.
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = recovery_message_text(msg)
        if not recovery_is_settlement(text):
            continue
        result, adjusted_odds = recovery_extract_settlement(text)
        if not result:
            continue
        reply_id = int(msg.get("reply_to_message_id") or 0)
        bet = get_bet_by_message(chat_id, reply_id) if reply_id else None
        if not bet:
            # Some exports preserve the replied ticket inside another message relation poorly.
            # As a fallback, only settle if the settlement text itself contains a Ticket #.
            tid = recovery_extract_ticket_id(text)
            bet = get_bet_by_ticket_id(tid) if tid else None
        if not bet:
            continue
        ok, _, _ = settle_bet(int(bet["id"]), result, admin_user_id, adjusted_odds=adjusted_odds)
        if ok:
            settled += 1

    return {
        "imported": imported,
        "already": already,
        "settled": settled,
        "failed": failed,
        "skipped": skipped,
    }


def telethon_recovery_ready() -> Tuple[bool, str]:
    if TelegramClient is None:
        return False, "Telethon is not installed. Run: pip install telethon"
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        return False, "Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env."
    try:
        int(TELEGRAM_API_ID)
    except Exception:
        return False, "TELEGRAM_API_ID must be a number."
    return True, ""


def telethon_message_text(message) -> str:
    return str(getattr(message, "message", None) or "").strip()


def telethon_message_datetime(message) -> str:
    dt = getattr(message, "date", None)
    if not dt:
        return now_str()
    try:
        return dt.astimezone(ZoneInfo(TICKET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def telethon_reply_message_id(message) -> int:
    try:
        reply = getattr(message, "reply_to", None)
        return int(getattr(reply, "reply_to_msg_id", 0) or 0)
    except Exception:
        return 0


async def telethon_login_check() -> bool:
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)
    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start(phone=TELEGRAM_PHONE or None)
    authed = await client.is_user_authorized()
    await client.disconnect()
    return bool(authed)


async def telethon_get_entity_for_current_chat(client, bot_chat_id: int):
    """Resolve the Telegram chat where /recoverbets was run.

    Bot API chat IDs and Telethon peer IDs are not always cached the same way,
    so this first tries direct resolution, then scans the user's dialog list.
    No TELEGRAM_RECOVERY_CHAT env var is required.
    """
    chat_id = int(bot_chat_id)

    # Fast path: works for many groups/supergroups when the self account has it cached.
    for ref in (chat_id, str(chat_id)):
        try:
            return await client.get_entity(ref)
        except Exception:
            pass

    # Reliable path: find the exact dialog whose Telethon peer id equals the Bot API chat id.
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not entity:
            continue

        try:
            if int(getattr(dialog, "id", 0) or 0) == chat_id:
                return entity
        except Exception:
            pass

        if telethon_utils is not None:
            try:
                if int(telethon_utils.get_peer_id(entity)) == chat_id:
                    return entity
            except Exception:
                pass

    raise RuntimeError(f"chat id {chat_id} was not found in the self account dialog list")


def telethon_candidate_chat_ids(chat_id: int) -> List[int]:
    """Return common Telethon/Bot-API variants for group/channel ids."""
    chat_id = int(chat_id)
    candidates = [chat_id]

    # If someone gives -5361332507 for a supergroup/channel, Telethon may expose
    # it as -1005361332507. Try both forms.
    if chat_id < 0 and not str(abs(chat_id)).startswith("100"):
        candidates.append(int(f"-100{abs(chat_id)}"))

    # If someone gives -1005361332507, also try -5361332507.
    raw = str(abs(chat_id))
    if chat_id < 0 and raw.startswith("100") and len(raw) > 3:
        try:
            candidates.append(-int(raw[3:]))
        except Exception:
            pass

    out = []
    for value in candidates:
        if value not in out:
            out.append(value)
    return out


async def telethon_get_entity_for_any_chat(client, chat_id: int):
    """Resolve a chat id for Telethon, trying exact and -100-prefixed variants."""
    candidates = telethon_candidate_chat_ids(chat_id)

    for cid in candidates:
        for ref in (cid, str(cid)):
            try:
                return await client.get_entity(ref)
            except Exception:
                pass

    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not entity:
            continue

        dialog_ids = set()
        try:
            dialog_ids.add(int(getattr(dialog, "id", 0) or 0))
        except Exception:
            pass

        if telethon_utils is not None:
            try:
                dialog_ids.add(int(telethon_utils.get_peer_id(entity)))
            except Exception:
                pass

        if any(cid in dialog_ids for cid in candidates):
            return entity

    raise RuntimeError(f"chat id {chat_id} was not found in the self account dialog list")



def send_accept_text_matches(text: str) -> bool:
    return bool(SEND_ACCEPTED_PHRASE and SEND_ACCEPTED_PHRASE in str(text or "").lower())


def send_regen_text_matches(text: str) -> bool:
    """Match messages like: 🎟 Ticket #255 regenerated for @username."""
    value = str(text or "").strip().lower()
    if not value:
        return False

    if re.search(r"(?i)(?:🎟\s*)?ticket\s*#?\s*\d*\s*regenerated\b", value):
        return True

    # Safe fallback in case the emoji or ticket number formatting changes.
    return "ticket" in value and "regenerated" in value


def telethon_first_sent_message(sent):
    """Telethon can return a single Message or a list; normalize it."""
    if isinstance(sent, (list, tuple)):
        return sent[0] if sent else None
    return sent


def telethon_sent_message_id(sent) -> int:
    """Return the id of a sent Telethon message/list, or 0."""
    sent_msg = telethon_first_sent_message(sent)
    try:
        return int(getattr(sent_msg, "id", 0) or 0)
    except Exception:
        return 0


def telethon_note_send_error(stats: Dict[str, int], label: str, exc: Exception) -> None:
    """
    Save the real Telethon error without spamming Telegram status messages.
    Check telethon_send_failures.log if anything still fails.
    """
    try:
        reason = f"{type(exc).__name__}: {exc}"
    except Exception:
        reason = str(exc)

    stats["last_error"] = reason[:500]

    try:
        failures = stats.setdefault("failure_reasons", [])
        if isinstance(failures, list) and len(failures) < 25:
            failures.append(f"{label}: {reason[:500]}")
    except Exception:
        pass

    try:
        with open("telethon_send_failures.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {label}: {reason}\n")
    except Exception:
        pass


async def telethon_run_with_retries(make_call, stats: Dict[str, int], label: str, max_retries: int = 3):
    """
    Run a Telethon send/download call and retry normal temporary failures.
    If Telegram returns FloodWaitError, this waits and tries again.
    """
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            return await make_call()
        except Exception as e:
            last_exc = e
            telethon_note_send_error(stats, f"{label} attempt {attempt}", e)

            wait_seconds = 0
            try:
                wait_seconds = int(getattr(e, "seconds", 0) or 0)
            except Exception:
                wait_seconds = 0

            # FloodWaitError normally has .seconds.
            # Keep it safe: wait only if it is not insanely long.
            if wait_seconds and wait_seconds <= 300 and attempt < max_retries:
                await asyncio.sleep(wait_seconds + 1)
                continue

            if attempt < max_retries:
                await asyncio.sleep(1.25 * attempt)
                continue

    if last_exc:
        telethon_note_send_error(stats, label, last_exc)
    return None


async def telethon_download_media_to_temp(msg, stats: Dict[str, int], temp_dir: str) -> str:
    """
    Download a message's media to a temp folder and return its local path.
    This is more reliable than passing msg.media directly to send_file().
    """
    downloaded = await telethon_run_with_retries(
        lambda: msg.download_media(file=temp_dir),
        stats,
        "download_media",
    )
    return str(downloaded or "")


async def telethon_send_text_message(
    client,
    target_entity,
    text: str,
    stats: Dict[str, int],
    reply_to_msg_id: int = 0,
    empty_placeholder: str = "[empty message]",
):
    message_text = str(text or "").strip() or empty_placeholder
    return await telethon_run_with_retries(
        lambda: client.send_message(
            target_entity,
            message_text,
            reply_to=(int(reply_to_msg_id) if int(reply_to_msg_id or 0) else None),
        ),
        stats,
        "send_text_reply" if reply_to_msg_id else "send_text",
    )


async def telethon_copy_message_return(
    client,
    target_entity,
    msg,
    stats: Dict[str, int],
    reply_to_msg_id: int = 0,
):
    """
    Copy a message to target_entity.
    If reply_to_msg_id is provided, it sends it as a REAL reply in the target chat.

    Important fix:
    The old code tried send_file(file=msg.media). That fails for many Telegram
    media objects. This version downloads the media first, sends the local file,
    then falls back to text/placeholder so reply chains do not break.
    """
    text = telethon_message_text(msg)
    media = getattr(msg, "media", None)
    reply_to_value = int(reply_to_msg_id or 0) or None

    if media is not None:
        temp_dir = tempfile.mkdtemp(prefix="telethon_copy_")
        try:
            media_path = await telethon_download_media_to_temp(msg, stats, temp_dir)

            if media_path and os.path.exists(media_path):
                sent = await telethon_run_with_retries(
                    lambda: client.send_file(
                        target_entity,
                        file=media_path,
                        caption=(text or None),
                        reply_to=reply_to_value,
                    ),
                    stats,
                    "send_file_downloaded_reply" if reply_to_value else "send_file_downloaded",
                )
                sent_msg = telethon_first_sent_message(sent)
                if sent_msg:
                    return sent_msg

            # Last media fallback: try Telethon's original media object directly.
            sent = await telethon_run_with_retries(
                lambda: client.send_file(
                    target_entity,
                    file=media,
                    caption=(text or None),
                    reply_to=reply_to_value,
                ),
                stats,
                "send_file_direct_reply" if reply_to_value else "send_file_direct",
            )
            sent_msg = telethon_first_sent_message(sent)
            if sent_msg:
                return sent_msg

        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    # Text fallback.
    # If the source message was only media and media copy failed, still send a
    # placeholder as a reply so the conversation structure stays intact.
    placeholder = "[media message could not be copied]" if media is not None else "[empty message]"
    sent = await telethon_send_text_message(
        client,
        target_entity,
        text,
        stats,
        reply_to_msg_id=int(reply_to_msg_id or 0),
        empty_placeholder=placeholder,
    )
    sent_msg = telethon_first_sent_message(sent)
    if sent_msg:
        return sent_msg

    return None


async def telethon_forward_or_copy_return(client, target_entity, msg, stats: Dict[str, int]):
    """
    Send the main/parent message and return the sent target message object.
    Forward first so the original ticket/caption is preserved.
    If forwarding is blocked, copy the media/text instead.
    """
    sent = await telethon_run_with_retries(
        lambda: client.forward_messages(target_entity, msg),
        stats,
        "forward_parent",
    )
    sent_msg = telethon_first_sent_message(sent)
    if sent_msg:
        stats["forwarded"] += 1
        return sent_msg

    copied = await telethon_copy_message_return(client, target_entity, msg, stats)
    if copied:
        stats["fallback_sent"] += 1
        return copied

    stats["failed"] += 1
    return None


async def telethon_forward_or_send_text(client, target_entity, msg, stats: Dict[str, int]) -> None:
    """Forward/copy one message without reply mapping."""
    await telethon_forward_or_copy_return(client, target_entity, msg, stats)


async def telethon_copy_as_reply(client, target_entity, msg, reply_to_msg_id: int, stats: Dict[str, int]):
    """
    Copy a reply message into the target chat as an actual reply to reply_to_msg_id.
    This is what makes it look like the screenshot: the reply is attached to the ticket.
    """
    target_parent_id = int(reply_to_msg_id or 0)
    if not target_parent_id:
        stats["failed"] += 1
        stats["last_error"] = "Missing target parent message id for reply."
        return None

    sent_msg = await telethon_copy_message_return(
        client,
        target_entity,
        msg,
        stats,
        reply_to_msg_id=target_parent_id,
    )
    if sent_msg:
        stats["reply_sent"] = int(stats.get("reply_sent", 0) or 0) + 1
        return sent_msg

    stats["failed"] += 1
    return None


async def send_accepted_bets_from_telethon_history(
    source_chat_id: int,
    target_chat_id: int,
    status_message=None,
    limit: int = 0,
) -> Dict[str, int]:
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        source_entity = await telethon_get_entity_for_any_chat(client, source_chat_id)
        target_entity = await telethon_get_entity_for_any_chat(client, target_chat_id)
    except Exception:
        await client.disconnect()
        raise

    stats = {
        "scanned": 0,
        "matched": 0,
        "forwarded": 0,
        "fallback_sent": 0,
        "failed": 0,
        "skipped_sender": 0,
        "last_error": "",
        "failure_reasons": [],
    }

    sender_filter = int(SEND_ACCEPTED_BOT_USER_ID or 0) or None

    try:
        async for msg in client.iter_messages(source_entity, reverse=True, limit=(int(limit or 0) or None)):
            stats["scanned"] += 1

            if sender_filter is not None:
                sender_id = int(getattr(msg, "sender_id", 0) or 0)
                if sender_id and sender_id != sender_filter:
                    stats["skipped_sender"] += 1
                    continue

            text = telethon_message_text(msg)
            if not send_accept_text_matches(text):
                continue

            stats["matched"] += 1
            await telethon_forward_or_copy_return(client, target_entity, msg, stats)

            if status_message and stats["matched"] % 25 == 0:
                try:
                    await status_message.edit_text(
                        "📤 /send running...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Matched: <b>{stats['matched']}</b>\n"
                        f"Forwarded: <b>{stats['forwarded']}</b> | Copied: <b>{stats['fallback_sent']}</b> | Failed: <b>{stats['failed']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    finally:
        await client.disconnect()

    return stats


async def send_accepted_bets_with_replies_from_telethon_history(
    source_chat_id: int,
    target_chat_id: int,
    status_message=None,
    limit: int = 0,
) -> Dict[str, int]:
    """
    /sendbes helper:
    1) Find messages/captions containing SEND_ACCEPTED_PHRASE.
    2) Find direct replies to those matched messages in the scanned history.
    3) Send the matched message to the target chat.
    4) Copy each direct reply as an ACTUAL reply to that sent message in the target chat.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        source_entity = await telethon_get_entity_for_any_chat(client, source_chat_id)
        target_entity = await telethon_get_entity_for_any_chat(client, target_chat_id)
    except Exception:
        await client.disconnect()
        raise

    stats = {
        "scanned": 0,
        "reply_scanned": 0,
        "matched": 0,
        "replies_found": 0,
        "forwarded": 0,
        "fallback_sent": 0,
        "reply_sent": 0,
        "failed": 0,
        "skipped_sender": 0,
        "last_error": "",
        "failure_reasons": [],
    }

    sender_filter = int(SEND_ACCEPTED_BOT_USER_ID or 0) or None
    matched_messages = []
    matched_ids = set()
    replies_by_parent: Dict[int, List] = {}

    try:
        # Pass 1: find accepted-bet messages.
        async for msg in client.iter_messages(source_entity, reverse=True, limit=(int(limit or 0) or None)):
            stats["scanned"] += 1

            if sender_filter is not None:
                sender_id = int(getattr(msg, "sender_id", 0) or 0)
                if sender_id and sender_id != sender_filter:
                    stats["skipped_sender"] += 1
                    continue

            text = telethon_message_text(msg)
            if not send_accept_text_matches(text):
                continue

            msg_id = int(getattr(msg, "id", 0) or 0)
            if not msg_id or msg_id in matched_ids:
                continue

            matched_ids.add(msg_id)
            matched_messages.append(msg)
            stats["matched"] += 1

            if status_message and stats["matched"] % 25 == 0:
                try:
                    await status_message.edit_text(
                        "📤 /sendbes finding accepted bets...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Matched: <b>{stats['matched']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        if matched_ids and status_message:
            try:
                await status_message.edit_text(
                    "📤 /sendbes finding replies...\n"
                    f"Accepted bet messages found: <b>{stats['matched']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # Pass 2: find direct replies to the matched messages.
        if matched_ids:
            async for msg in client.iter_messages(source_entity, reverse=True, limit=(int(limit or 0) or None)):
                stats["reply_scanned"] += 1
                parent_id = telethon_reply_message_id(msg)
                if parent_id not in matched_ids:
                    continue

                msg_id = int(getattr(msg, "id", 0) or 0)
                if not msg_id or msg_id == parent_id:
                    continue

                replies_by_parent.setdefault(parent_id, []).append(msg)
                stats["replies_found"] += 1

                if status_message and stats["replies_found"] % 50 == 0:
                    try:
                        await status_message.edit_text(
                            "📤 /sendbes finding replies...\n"
                            f"Reply scan: <b>{stats['reply_scanned']}</b> | Replies found: <b>{stats['replies_found']}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

        if status_message:
            try:
                await status_message.edit_text(
                    "📤 /sendbes sending accepted bets + real replies...\n"
                    f"Accepted bets: <b>{stats['matched']}</b> | Replies: <b>{stats['replies_found']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        sent_source_ids = set()

        # Send each accepted-bet message first, then send its replies as real replies.
        for match in matched_messages:
            match_id = int(getattr(match, "id", 0) or 0)
            if not match_id or match_id in sent_source_ids:
                continue

            sent_parent = await telethon_forward_or_copy_return(client, target_entity, match, stats)
            sent_source_ids.add(match_id)

            target_parent_id = int(getattr(sent_parent, "id", 0) or 0) if sent_parent else 0
            if not target_parent_id:
                # Parent could not be sent, so real replies cannot be attached.
                continue

            for reply in replies_by_parent.get(match_id, []):
                reply_id = int(getattr(reply, "id", 0) or 0)
                if reply_id and reply_id in sent_source_ids:
                    continue

                await telethon_copy_as_reply(client, target_entity, reply, target_parent_id, stats)
                if reply_id:
                    sent_source_ids.add(reply_id)

    finally:
        await client.disconnect()

    return stats


async def send_regenerated_tickets_with_replies_from_telethon_history(
    source_chat_id: int,
    target_chat_id: int,
    status_message=None,
    limit: int = 0,
) -> Dict[str, int]:
    """
    /sendregen helper:
    1) Find regenerated ticket messages like: 🎟 Ticket #255 regenerated for @username.
    2) Find direct replies to those messages, for example: Void.
    3) Send the regenerated ticket to the target group.
    4) Copy each direct reply as an ACTUAL reply to that sent ticket in the target group.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        source_entity = await telethon_get_entity_for_any_chat(client, source_chat_id)
        target_entity = await telethon_get_entity_for_any_chat(client, target_chat_id)
    except Exception:
        await client.disconnect()
        raise

    stats = {
        "scanned": 0,
        "reply_scanned": 0,
        "matched": 0,
        "replies_found": 0,
        "forwarded": 0,
        "fallback_sent": 0,
        "reply_sent": 0,
        "failed": 0,
        "skipped_sender": 0,
        "last_error": "",
        "failure_reasons": [],
    }

    sender_filter = int(SEND_REGEN_BOT_USER_ID or 0) or None
    matched_messages = []
    matched_ids = set()
    replies_by_parent: Dict[int, List] = {}

    try:
        # Pass 1: find regenerated-ticket messages.
        async for msg in client.iter_messages(source_entity, reverse=True, limit=(int(limit or 0) or None)):
            stats["scanned"] += 1

            if sender_filter is not None:
                sender_id = int(getattr(msg, "sender_id", 0) or 0)
                if sender_id and sender_id != sender_filter:
                    stats["skipped_sender"] += 1
                    continue

            text = telethon_message_text(msg)
            if not send_regen_text_matches(text):
                continue

            msg_id = int(getattr(msg, "id", 0) or 0)
            if not msg_id or msg_id in matched_ids:
                continue

            matched_ids.add(msg_id)
            matched_messages.append(msg)
            stats["matched"] += 1

            if status_message and stats["matched"] % 25 == 0:
                try:
                    await status_message.edit_text(
                        "📤 /sendregen finding regenerated tickets...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Matched: <b>{stats['matched']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        if matched_ids and status_message:
            try:
                await status_message.edit_text(
                    "📤 /sendregen finding direct replies...\n"
                    f"Regenerated tickets found: <b>{stats['matched']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # Pass 2: find direct replies to those regenerated-ticket messages.
        if matched_ids:
            async for msg in client.iter_messages(source_entity, reverse=True, limit=(int(limit or 0) or None)):
                stats["reply_scanned"] += 1
                parent_id = telethon_reply_message_id(msg)
                if parent_id not in matched_ids:
                    continue

                msg_id = int(getattr(msg, "id", 0) or 0)
                if not msg_id or msg_id == parent_id:
                    continue

                replies_by_parent.setdefault(parent_id, []).append(msg)
                stats["replies_found"] += 1

                if status_message and stats["replies_found"] % 50 == 0:
                    try:
                        await status_message.edit_text(
                            "📤 /sendregen finding direct replies...\n"
                            f"Reply scan: <b>{stats['reply_scanned']}</b> | Replies found: <b>{stats['replies_found']}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

        if status_message:
            try:
                await status_message.edit_text(
                    "📤 /sendregen sending tickets + replied messages...\n"
                    f"Regenerated tickets: <b>{stats['matched']}</b> | Replies: <b>{stats['replies_found']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        sent_source_ids = set()

        for match in matched_messages:
            match_id = int(getattr(match, "id", 0) or 0)
            if not match_id or match_id in sent_source_ids:
                continue

            sent_parent = await telethon_forward_or_copy_return(client, target_entity, match, stats)
            sent_source_ids.add(match_id)

            target_parent_id = int(getattr(sent_parent, "id", 0) or 0) if sent_parent else 0
            if not target_parent_id:
                # Without a target parent id, Telegram cannot create a real reply.
                continue

            # Send replies as actual Telegram replies to the newly sent parent ticket.
            for reply in replies_by_parent.get(match_id, []):
                reply_id = int(getattr(reply, "id", 0) or 0)
                if reply_id and reply_id in sent_source_ids:
                    continue

                await telethon_copy_as_reply(client, target_entity, reply, target_parent_id, stats)
                if reply_id:
                    sent_source_ids.add(reply_id)

    finally:
        await client.disconnect()

    return stats


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    limit = SEND_ACCEPTED_SCAN_LIMIT
    if context.args:
        raw = str(context.args[0]).strip().lower()
        if raw in {"all", "full"}:
            limit = 0
        else:
            try:
                limit = max(0, int(raw))
            except Exception:
                await update.message.reply_text("Use /send, /send all, or /send 500")
                return

    status = await update.message.reply_text(
        "📤 /send started. Scanning this chat with Telethon and forwarding accepted bet messages...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await send_accepted_bets_from_telethon_history(
            source_chat_id=update.effective_chat.id,
            target_chat_id=SEND_ACCEPTED_TARGET_CHAT_ID,
            status_message=status,
            limit=limit,
        )
        await status.edit_text(
            "✅ /send complete\n"
            f"Target group: <code>{SEND_ACCEPTED_TARGET_CHAT_ID}</code>\n"
            f"Phrase: <code>{safe_text(SEND_ACCEPTED_PHRASE)}</code>\n"
            f"Scanned: <b>{stats['scanned']}</b>\n"
            f"Matched: <b>{stats['matched']}</b>\n"
            f"Forwarded/copied parents: <b>{stats['forwarded'] + stats['fallback_sent']}</b>\n"
            f"Replies sent as real replies: <b>{stats.get('reply_sent', 0)}</b>\n"
            f"Failed: <b>{stats['failed']}</b>"
            + (f"\nLast error: <code>{safe_text(stats.get('last_error', ''))}</code>" if stats.get("last_error") and stats.get("failed") else ""),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ /send failed\n"
            f"<code>{safe_text(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def sendbes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    limit = SEND_ACCEPTED_SCAN_LIMIT
    if context.args:
        raw = str(context.args[0]).strip().lower()
        if raw in {"all", "full"}:
            limit = 0
        else:
            try:
                limit = max(0, int(raw))
            except Exception:
                await update.message.reply_text("Use /sendbes, /sendbes all, or /sendbes 500")
                return

    status = await update.message.reply_text(
        "📤 /sendbes started. Scanning this chat for accepted bets and their direct replies...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await send_accepted_bets_with_replies_from_telethon_history(
            source_chat_id=update.effective_chat.id,
            target_chat_id=SEND_ACCEPTED_TARGET_CHAT_ID,
            status_message=status,
            limit=limit,
        )
        await status.edit_text(
            "✅ /sendbes complete\n"
            f"Target group: <code>{SEND_ACCEPTED_TARGET_CHAT_ID}</code>\n"
            f"Phrase: <code>{safe_text(SEND_ACCEPTED_PHRASE)}</code>\n"
            f"Scanned for bets: <b>{stats['scanned']}</b>\n"
            f"Accepted bet messages: <b>{stats['matched']}</b>\n"
            f"Scanned for replies: <b>{stats['reply_scanned']}</b>\n"
            f"Replies found: <b>{stats['replies_found']}</b>\n"
            f"Forwarded: <b>{stats['forwarded']}</b>\n"
            f"Text fallback sent: <b>{stats['fallback_sent']}</b>\n"
            f"Failed: <b>{stats['failed']}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ /sendbes failed\n"
            f"<code>{safe_text(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def sendregen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    limit = SEND_REGEN_SCAN_LIMIT
    if context.args:
        raw = str(context.args[0]).strip().lower()
        if raw in {"all", "full"}:
            limit = 0
        else:
            try:
                limit = max(0, int(raw))
            except Exception:
                await update.message.reply_text("Use /sendregen, /sendregen all, or /sendregen 500")
                return

    status = await update.message.reply_text(
        "📤 /sendregen started. Scanning this chat for regenerated tickets and their direct replies...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await send_regenerated_tickets_with_replies_from_telethon_history(
            source_chat_id=update.effective_chat.id,
            target_chat_id=SEND_REGEN_TARGET_CHAT_ID,
            status_message=status,
            limit=limit,
        )
        await status.edit_text(
            "✅ /sendregen complete\n"
            f"Target group: <code>{SEND_REGEN_TARGET_CHAT_ID}</code>\n"
            "Match: <code>Ticket #... regenerated</code>\n"
            f"Scanned for tickets: <b>{stats['scanned']}</b>\n"
            f"Regenerated ticket messages: <b>{stats['matched']}</b>\n"
            f"Scanned for replies: <b>{stats['reply_scanned']}</b>\n"
            f"Direct replies found: <b>{stats['replies_found']}</b>\n"
            f"Ticket messages forwarded/copied: <b>{stats['forwarded'] + stats['fallback_sent']}</b>\n"
            f"Replies sent as real replies: <b>{stats['reply_sent']}</b>\n"
            f"Failed: <b>{stats['failed']}</b>"
            + (f"\nLast error: <code>{safe_text(stats.get('last_error', ''))}</code>" if stats.get("last_error") and stats.get("failed") else ""),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ /sendregen failed\n"
            f"<code>{safe_text(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def recover_from_telethon_history(
    bot_chat_id: int,
    admin_user_id: int,
    admin_username: str,
    status_message=None,
) -> Dict[str, int]:
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    os.makedirs(TELEGRAM_RECOVERY_MEDIA_DIR, exist_ok=True)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_current_chat(client, bot_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. "
            "Make sure the Telegram account used for the Telethon session is a member of this exact group, "
            "then run /recoverbets inside that group. Details: " + safe_text(e)
        )

    imported = skipped = failed = settled = already = scanned = media_downloaded = 0
    settlement_candidates: List[Tuple[int, str, int]] = []

    try:
        async for msg in client.iter_messages(entity, reverse=True, limit=(TELEGRAM_RECOVERY_LIMIT or None)):
            scanned += 1
            text = telethon_message_text(msg)

            if recovery_is_settlement(text):
                settlement_candidates.append((telethon_reply_message_id(msg), text, int(getattr(msg, "id", 0) or 0)))

            if not recovery_is_accept_slip(text):
                if scanned % 1000 == 0 and status_message:
                    try:
                        await status_message.edit_text(
                            "♻️ Telethon recovery running...\n"
                            f"Scanned: <b>{scanned}</b>\n"
                            f"Imported: <b>{imported}</b> | Already: <b>{already}</b> | Failed: <b>{failed}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                continue

            old_ticket_id = recovery_extract_ticket_id(text)
            slip_message_id = int(getattr(msg, "id", 0) or 0)
            if not old_ticket_id:
                skipped += 1
                continue

            if recovered_bet_exists(old_ticket_id):
                already += 1
                continue

            if not getattr(msg, "media", None):
                failed += 1
                continue

            img_path = ""
            try:
                img_path = await msg.download_media(file=TELEGRAM_RECOVERY_MEDIA_DIR)
                media_downloaded += 1
            except Exception:
                img_path = ""

            if not img_path or not os.path.exists(img_path):
                failed += 1
                continue

            try:
                parsed, extracted = recovery_parse_image_bet(img_path)
            except Exception as e:
                if AI_BETSLIP_DEBUG:
                    print(f"Recovery OCR/AI parse failed for ticket {old_ticket_id}: {e}")
                parsed = None

            if not parsed:
                failed += 1
                continue

            bettor = recovery_extract_bettor_from_accept_caption(text)
            if bettor != "unknown" and not user_exists(bettor):
                add_user_to_json(bettor)

            create_recovered_bet(
                old_ticket_id=old_ticket_id,
                chat_id=bot_chat_id,
                admin_user_id=admin_user_id,
                admin_username=admin_username,
                bettor=bettor,
                stake=float(parsed["stake"]),
                total_odds=float(parsed["total_odds"]),
                legs=parsed.get("legs") or [],
                source_photo_message_id=telethon_reply_message_id(msg) or slip_message_id,
                accept_message_id=telethon_reply_message_id(msg) or slip_message_id,
                slip_message_id=slip_message_id,
                created_at=telethon_message_datetime(msg),
                conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
                note=parsed.get("note"),
                bet_tag="",
            )
            imported += 1

            if imported % 25 == 0 and status_message:
                try:
                    await status_message.edit_text(
                        "♻️ Telethon recovery running...\n"
                        f"Scanned: <b>{scanned}</b>\n"
                        f"Imported: <b>{imported}</b> | Already: <b>{already}</b> | Failed: <b>{failed}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        for reply_id, text, _message_id in settlement_candidates:
            result, adjusted_odds = recovery_extract_settlement(text)
            if not result:
                continue
            bet = get_bet_by_message(bot_chat_id, reply_id) if reply_id else None
            if not bet:
                tid = recovery_extract_ticket_id(text)
                bet = get_bet_by_ticket_id(tid) if tid else None
            if not bet:
                continue
            ok, _, _ = settle_bet(int(bet["id"]), result, admin_user_id, adjusted_odds=adjusted_odds)
            if ok:
                settled += 1

    finally:
        await client.disconnect()

    return {
        "scanned": scanned,
        "imported": imported,
        "already": already,
        "settled": settled,
        "failed": failed,
        "skipped": skipped,
        "media_downloaded": media_downloaded,
    }



# -------------------------
# /addbetsall - import accepted bet tickets from the current chat
# -------------------------


def addbetsall_extract_bettor_and_telegram(text: str) -> Tuple[str, str]:
    """
    Pull the real bettor from captions like:
      ✅ @Swaxxie bet has been accepted. Ticket #211
      ✅ Swaxxie, bet has been accepted. Ticket #211

    Returns (normalized_user_key, telegram_username).
    """
    raw = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    raw = raw.replace("✅", " ").strip()
    before = re.split(r"(?i)\bbet\s+has\s+been\s+accepted\b", raw, maxsplit=1)[0].strip()

    # Prefer @username because that is what the forwarded tickets use.
    mentions = re.findall(r"@([A-Za-z0-9_]{2,32})", before)
    if mentions:
        tg = normalize_telegram_username(mentions[-1])
        key = canonicalize_bettor(mentions[-1])
        return key or "unknown", tg

    # Fallback for old captions that stored display names instead of @users.
    fallback = recovery_extract_bettor_from_accept_caption(raw)
    if fallback and fallback != "unknown":
        return fallback, ""

    m = re.search(r"([A-Za-z0-9_]{2,32})\s*,?\s*$", before)
    if m:
        key = normalize_user(m.group(1))
        return key or "unknown", ""

    return "unknown", ""


def addbetsall_extract_bet_tag(text: str) -> str:
    """Best-effort tag extraction from accepted captions."""
    raw = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    raw = raw.replace("✅", " ").strip()
    m = re.search(
        r"(?i)(?:@[A-Za-z0-9_]{2,32}|[A-Za-z0-9_]{2,32})\s*,?\s+(.+?)\s+bet\s+has\s+been\s+accepted",
        raw,
    )
    if not m:
        return ""
    return clean_bet_tag(m.group(1))


def addbetsall_log_failure(ticket_id: int, message_id: int, reason: str, extra: str = "") -> None:
    try:
        with open("addbetsall_failures.log", "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"ticket=#{ticket_id or 0} message_id={message_id or 0} {reason}\n"
            )
            if extra:
                f.write(str(extra)[:2000].replace("\r", "") + "\n---\n")
    except Exception:
        pass


def ensure_bets_sequence_after_import() -> None:
    """Make sure the next new ticket continues after the highest imported ticket."""
    try:
        with db() as conn:
            max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM bets").fetchone()[0]
            try:
                conn.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES('bets', ?) "
                    "ON CONFLICT(name) DO UPDATE SET seq = MAX(seq, excluded.seq)",
                    (int(max_id or 0),),
                )
            except Exception:
                # sqlite_sequence might not exist until AUTOINCREMENT has been used.
                pass
    except Exception:
        pass


def addbetsall_placeholder_parsed(ticket_id: int, extracted_text: str) -> Optional[Dict]:
    """
    Last fallback: if OCR can read stake + top odds but cannot recover legs,
    still save the ticket with one placeholder leg so the ledger/order/user is not lost.
    """
    stake, total_odds = recovery_extract_stake_total_from_text(extracted_text)
    if float(total_odds or 0) <= 1:
        return None

    return {
        "stake": float(stake or DEFAULT_STAKE),
        "total_odds": float(total_odds),
        "legs": [
            {
                "event": f"Recovered Ticket #{ticket_id}",
                "selection": "Recovered Selection",
                "market": "Recovered",
                "sport": "other",
                "odds": float(total_odds),
            }
        ],
        "conditions": DEFAULT_CONDITIONS,
        "note": "Recovered from old betslip OCR - selection text not fully readable",
        "_placeholder": True,
    }


async def addbetsall_from_telethon_history(
    source_chat_id: int,
    admin_user_id: int,
    admin_username: str,
    status_message=None,
    limit: int = 0,
) -> Dict[str, int]:
    """
    Scan the current Telegram chat using the Telethon self account and import every
    accepted betting slip into bets.db using its original Ticket # as bets.id.

    It processes tickets in Ticket # order, stores the @username from the caption,
    and does not settle anything. Use /recoversettles separately for results.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    os.makedirs(TELEGRAM_RECOVERY_MEDIA_DIR, exist_ok=True)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_any_chat(client, source_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. "
            "Make sure the Telegram account used for Telethon is a member of this chat. "
            "Details: " + safe_text(e)
        )

    stats = {
        "scanned": 0,
        "matched": 0,
        "media_downloaded": 0,
        "imported": 0,
        "already": 0,
        "failed": 0,
        "skipped": 0,
        "users_added_or_updated": 0,
        "placeholder": 0,
        "first_ticket": 0,
        "last_ticket": 0,
    }

    matches = []

    try:
        async for msg in client.iter_messages(entity, reverse=True, limit=(int(limit) or None)):
            stats["scanned"] += 1
            text = telethon_message_text(msg)

            if not recovery_is_accept_slip(text):
                continue

            ticket_id = recovery_extract_ticket_id(text)
            if not ticket_id:
                stats["skipped"] += 1
                continue

            matches.append((int(ticket_id), int(getattr(msg, "id", 0) or 0), msg, text))

            if stats["scanned"] % 1000 == 0 and status_message:
                try:
                    await status_message.edit_text(
                        "♻️ /addbetsall scanning...\n"
                        f"Scanned: <b>{stats['scanned']}</b> | Found accepted tickets: <b>{len(matches)}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        matches.sort(key=lambda item: (item[0], item[1]))
        stats["matched"] = len(matches)
        if matches:
            stats["first_ticket"] = int(matches[0][0])
            stats["last_ticket"] = int(matches[-1][0])

        if status_message:
            try:
                await status_message.edit_text(
                    "♻️ /addbetsall importing...\n"
                    f"Accepted tickets found: <b>{stats['matched']}</b>\n"
                    f"Range: <b>#{stats['first_ticket']}</b> to <b>#{stats['last_ticket']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        for ticket_id, message_id, msg, text in matches:
            if recovered_bet_exists(ticket_id):
                stats["already"] += 1
                continue

            if not getattr(msg, "media", None):
                stats["failed"] += 1
                addbetsall_log_failure(ticket_id, message_id, "no media/image on accepted ticket", text)
                continue

            bettor, telegram_username = addbetsall_extract_bettor_and_telegram(text)
            if bettor != "unknown":
                try:
                    ok_user, _ = add_user_to_json(bettor, telegram_username=telegram_username)
                    if ok_user:
                        stats["users_added_or_updated"] += 1
                except Exception:
                    pass

            img_path = ""
            extracted = ""
            parsed = None

            try:
                img_path = await msg.download_media(file=TELEGRAM_RECOVERY_MEDIA_DIR)
                if img_path and os.path.exists(img_path):
                    stats["media_downloaded"] += 1
                else:
                    img_path = ""

                if not img_path or not recovery_is_likely_image(img_path):
                    raise RuntimeError("media was not a readable image")

                try:
                    parsed, extracted = recovery_parse_image_bet(img_path)
                except Exception as e:
                    addbetsall_log_failure(ticket_id, message_id, f"OCR/AI exception: {type(e).__name__}: {e}")
                    parsed = None

                if not parsed:
                    # Try one manual OCR pass so we can still save stake/top odds when leg parsing fails.
                    if not extracted:
                        try:
                            extracted = clean_ocr_text(ocr_space(img_path))
                        except Exception:
                            extracted = ""
                    parsed = addbetsall_placeholder_parsed(ticket_id, extracted)
                    if parsed:
                        stats["placeholder"] += 1

                if not parsed:
                    stats["failed"] += 1
                    addbetsall_log_failure(ticket_id, message_id, "could not parse betslip", extracted or text)
                    continue

                legs = parsed.get("legs") or []
                if not legs:
                    stats["failed"] += 1
                    addbetsall_log_failure(ticket_id, message_id, "parsed with no legs", extracted or text)
                    continue

                create_recovered_bet(
                    old_ticket_id=int(ticket_id),
                    chat_id=int(source_chat_id),
                    admin_user_id=int(admin_user_id or 0),
                    admin_username=str(admin_username or "addbetsall"),
                    bettor=bettor or "unknown",
                    stake=float(parsed["stake"]),
                    total_odds=float(parsed["total_odds"]),
                    legs=legs,
                    source_photo_message_id=int(message_id),
                    accept_message_id=int(message_id),
                    slip_message_id=int(message_id),
                    created_at=telethon_message_datetime(msg),
                    conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
                    note=parsed.get("note"),
                    bet_tag=addbetsall_extract_bet_tag(text),
                )
                stats["imported"] += 1

            except Exception as e:
                stats["failed"] += 1
                addbetsall_log_failure(ticket_id, message_id, f"import exception: {type(e).__name__}: {e}", extracted or text)

            if (stats["imported"] + stats["failed"] + stats["already"]) % 20 == 0 and status_message:
                try:
                    await status_message.edit_text(
                        "♻️ /addbetsall importing...\n"
                        f"Found: <b>{stats['matched']}</b> | Imported: <b>{stats['imported']}</b>\n"
                        f"Already: <b>{stats['already']}</b> | Failed: <b>{stats['failed']}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        ensure_bets_sequence_after_import()

    finally:
        await client.disconnect()

    return stats


async def addbetsall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if not is_admin(update):
        if msg:
            await msg.reply_text("Admin only.")
        return

    if not msg:
        return

    limit = 0
    if context.args:
        raw = str(context.args[0]).strip().lower()
        if raw in {"all", "full"}:
            limit = 0
        else:
            try:
                limit = max(0, int(raw))
            except Exception:
                await msg.reply_text("Use /addbetsall, /addbetsall all, or /addbetsall 500")
                return

    user = update.effective_user
    admin_user_id = user.id if user else 0
    admin_username = user.username or user.full_name if user else "addbetsall"

    status = await msg.reply_text(
        "♻️ /addbetsall started. Scanning this chat for accepted tickets and adding them to bets.db in Ticket # order...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await addbetsall_from_telethon_history(
            source_chat_id=update.effective_chat.id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            status_message=status,
            limit=limit,
        )
        await status.edit_text(
            "✅ /addbetsall complete\n"
            f"Scanned messages: <b>{stats['scanned']}</b>\n"
            f"Accepted tickets found: <b>{stats['matched']}</b>\n"
            f"Ticket range: <b>#{stats['first_ticket']}</b> to <b>#{stats['last_ticket']}</b>\n"
            f"Media downloaded: <b>{stats['media_downloaded']}</b>\n"
            f"Imported to bets.db: <b>{stats['imported']}</b>\n"
            f"Already existed: <b>{stats['already']}</b>\n"
            f"Users added/updated: <b>{stats['users_added_or_updated']}</b>\n"
            f"Placeholder tickets: <b>{stats['placeholder']}</b>\n"
            f"Failed OCR/parse/import: <b>{stats['failed']}</b>\n\n"
            "Failures are logged in <code>addbetsall_failures.log</code>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ /addbetsall failed\n"
            f"<code>{safe_text(e)}</code>\n\n"
            "Run it inside the chat that contains the accepted tickets. The Telethon self account must be in that chat.",
            parse_mode=ParseMode.HTML,
        )

async def recover_settles_from_telethon_history(
    bot_chat_id: int,
    admin_user_id: int,
    status_message=None,
    bot_user_id: Optional[int] = None,
) -> Dict[str, int]:
    """
    SAFE settlement recovery.

    This does NOT trust random old messages like "w", "l", "won", "loss" by themselves,
    because normal members may have replied and the real bot may have answered "Admin only."

    It only recovers settlements from confirmed old bot settlement receipts/cards:
        old bot settlement card/message
        -> replied to the old admin won/loss/void command
        -> that command replied to the accepted betslip
        -> original betslip contains Ticket # / stored message id

    If there is no confirmed bot settlement receipt, it skips the command.
    """
    ok, reason = telethon_recovery_ready()
    if not ok:
        raise RuntimeError(reason)

    client = TelegramClient(TELEGRAM_SESSION_NAME, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not logged in yet. Run this once in terminal: python main_recovery.py telethon-login"
        )

    try:
        entity = await telethon_get_entity_for_current_chat(client, bot_chat_id)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(
            "Could not open this Telegram chat with the self account. Details: " + safe_text(e)
        )

    scanned = settled = already = no_bet = no_result = not_confirmed = 0

    try:
        async for old_msg in client.iter_messages(entity, reverse=True, limit=(TELEGRAM_RECOVERY_LIMIT or None)):
            scanned += 1
            text = telethon_message_text(old_msg)

            # Important safety check: only trust the bot's actual settlement receipt/card.
            # Do NOT settle from a member/admin command alone, because failed attempts also exist.
            if not recovery_is_settlement(text):
                no_result += 1
                continue

            if bot_user_id is not None:
                sender_id = int(getattr(old_msg, "sender_id", 0) or 0)
                if sender_id and sender_id != int(bot_user_id):
                    not_confirmed += 1
                    continue

            result, adjusted_odds = recovery_extract_settlement(text)
            if not result:
                no_result += 1
                continue

            # Confirmed settlement receipt -> admin command -> original accepted betslip.
            bet = await recovery_get_replied_bet_from_telethon(client, old_msg, bot_chat_id)

            # Final fallback: if the receipt itself contains Ticket #123.
            if not bet:
                tid = recovery_extract_ticket_id(text)
                bet = get_bet_by_ticket_id(tid) if tid else None

            if not bet:
                no_bet += 1
                continue

            ok, msg_text, _ = settle_bet(int(bet["id"]), result, admin_user_id, adjusted_odds=adjusted_odds)
            if ok:
                settled += 1
            elif "already settled" in str(msg_text).lower():
                already += 1
            else:
                no_bet += 1

            if scanned % 1000 == 0 and status_message:
                try:
                    await status_message.edit_text(
                        "♻️ Confirmed settlement recovery running...\n"
                        f"Scanned: <b>{scanned}</b>\n"
                        f"Settled: <b>{settled}</b> | Already: <b>{already}</b> | No match: <b>{no_bet}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
    finally:
        await client.disconnect()

    return {
        "scanned": scanned,
        "settled": settled,
        "already": already,
        "no_bet": no_bet,
        "no_result": no_result,
        "not_confirmed": not_confirmed,
    }


async def recoversettles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    status = await update.message.reply_text(
        "♻️ Confirmed settlement recovery started. Only using old bot settlement receipts/cards..."
    )

    try:
        stats = await recover_settles_from_telethon_history(
            bot_chat_id=update.effective_chat.id,
            admin_user_id=update.effective_user.id if update.effective_user else 0,
            status_message=status,
            bot_user_id=context.bot.id,
        )
        await status.edit_text(
            "✅ Settlement recovery complete\n"
            f"Scanned: <b>{stats['scanned']}</b>\n"
            f"Settled now: <b>{stats['settled']}</b>\n"
            f"Already settled: <b>{stats['already']}</b>\n"
            f"Confirmed receipts with no matching ticket: <b>{stats['no_bet']}</b>\n"
            f"Skipped unconfirmed/non-bot settlement-looking messages: <b>{stats.get('not_confirmed', 0)}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ Settlement recovery failed\n" + safe_text(str(e)),
            parse_mode=ParseMode.HTML,
        )


async def recoverbets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not update.message:
        return

    user = update.effective_user
    admin_user_id = user.id if user else 0
    admin_username = user.username or user.full_name if user else "recovery"

    # If user replies to export JSON/ZIP, keep the old export importer working.
    reply = update.message.reply_to_message
    doc = getattr(reply, "document", None) if reply else None
    if doc:
        status = await update.message.reply_text("♻️ Export recovery started...")
        temp_dir = tempfile.mkdtemp(prefix="bet_recovery_")
        try:
            json_path = ""
            media_base = temp_dir
            tg_file = await context.bot.get_file(doc.file_id)
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", doc.file_name or "telegram_export")
            local_path = os.path.join(temp_dir, safe_name)
            await tg_file.download_to_drive(local_path)

            if local_path.lower().endswith(".zip"):
                with zipfile.ZipFile(local_path, "r") as z:
                    z.extractall(temp_dir)
                for root, _, files in os.walk(temp_dir):
                    for name in files:
                        if name.lower() == "result.json":
                            json_path = os.path.join(root, name)
                            media_base = root
                            break
                    if json_path:
                        break
            elif local_path.lower().endswith(".json"):
                json_path = local_path
                media_base = os.path.dirname(local_path)

            if not json_path or not os.path.exists(json_path):
                await status.edit_text("Could not find result.json in that upload.")
                return

            stats = recover_from_telegram_export(
                json_path=json_path,
                media_base_dir=media_base,
                chat_id=update.effective_chat.id,
                admin_user_id=admin_user_id,
                admin_username=admin_username,
            )
            await status.edit_text(
                "✅ Export recovery complete\n"
                f"Imported: <b>{stats['imported']}</b>\n"
                f"Already existed: <b>{stats['already']}</b>\n"
                f"Settled: <b>{stats['settled']}</b>\n"
                f"Failed OCR/parse: <b>{stats['failed']}</b>\n"
                f"Skipped: <b>{stats['skipped']}</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            await status.edit_text(f"Recovery failed: {safe_text(e)}", parse_mode=ParseMode.HTML)
            return
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    status = await update.message.reply_text(
        "♻️ Telethon recovery started. Scanning group history from your self account...",
        parse_mode=ParseMode.HTML,
    )

    try:
        stats = await recover_from_telethon_history(
            bot_chat_id=update.effective_chat.id,
            admin_user_id=admin_user_id,
            admin_username=admin_username,
            status_message=status,
        )
        await status.edit_text(
            "✅ Telethon recovery complete\n"
            f"Scanned: <b>{stats['scanned']}</b>\n"
            f"Media downloaded: <b>{stats['media_downloaded']}</b>\n"
            f"Imported: <b>{stats['imported']}</b>\n"
            f"Already existed: <b>{stats['already']}</b>\n"
            f"Settled: <b>{stats['settled']}</b>\n"
            f"Failed OCR/parse: <b>{stats['failed']}</b>\n"
            f"Skipped: <b>{stats['skipped']}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await status.edit_text(
            "❌ Telethon recovery failed\n"
            f"<code>{safe_text(e)}</code>\n\n"
            "First-time setup: create <code>recovery_session.session</code> once, then run <code>/recoverbets</code> inside the group you want to scan.",
            parse_mode=ParseMode.HTML,
        )


def get_bet_with_legs(bet_id: int):
    with db() as conn:
        bet = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()

        if not bet:
            return None, []

        legs = conn.execute(
            "SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY id ASC",
            (bet_id,),
        ).fetchall()

    return bet, legs


def extract_ticket_id_from_message(message: Optional[Message]) -> Optional[int]:
    if not message:
        return None

    text_parts = []

    if getattr(message, "text", None):
        text_parts.append(message.text)

    if getattr(message, "caption", None):
        text_parts.append(message.caption)

    combined = "\n".join(text_parts)

    if not combined:
        return None

    patterns = [
        r"(?i)ticket\s*#?\s*(\d+)",
        r"(?i)#\s*(\d+)",
    ]

    for pattern in patterns:
        m = re.search(pattern, combined)

        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    return None


def get_bet_by_ticket_id(ticket_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bets WHERE id = ? ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()


def get_bet_by_message(chat_id: int, message_id: int):
    with db() as conn:
        bet = conn.execute(
            """
            SELECT * FROM bets
            WHERE chat_id = ?
            AND (
                slip_message_id = ?
                OR summary_message_id = ?
                OR source_photo_message_id = ?
                OR accept_message_id = ?
            )
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, message_id, message_id, message_id, message_id),
        ).fetchone()

        if bet:
            return bet

        bet = conn.execute(
            """
            SELECT * FROM bets
            WHERE (
                slip_message_id = ?
                OR summary_message_id = ?
                OR source_photo_message_id = ?
                OR accept_message_id = ?
            )
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, message_id, message_id, message_id),
        ).fetchone()

        if bet:
            return bet


    return None


# -------------------------
# TEST BET DB ACTIONS
# -------------------------

TEST_BET_TAG = "TEST BET"


def create_test_bet(
    chat_id: int,
    admin_user_id: int,
    admin_username: str,
    bettor: str,
    stake: float,
    total_odds: float,
    legs: List[Dict],
    source_photo_message_id: int,
    accept_message_id: int,
    conditions_list: Optional[List[str]] = None,
    note: Optional[str] = None,
    bet_tag: str = TEST_BET_TAG,
) -> int:
    """
    Create a test ticket in a separate table.
    This never touches the real bets table, so it never uses or increments real ticket IDs.
    """
    payout = round(stake * total_odds, 2)
    profit = round(payout - stake, 2)
    conditions_list = conditions_list or (["TEST BET - Not counted in the real ledger."] + DEFAULT_CONDITIONS)
    conditions = "\n".join(conditions_list)

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO test_bets
            (
                chat_id, user_id, username, bettor,
                stake, total_odds, payout, profit,
                conditions, note, bet_tag,
                source_photo_message_id, accept_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                admin_user_id,
                admin_username,
                bettor,
                stake,
                total_odds,
                payout,
                profit,
                conditions,
                note or "Test Bet",
                clean_bet_tag(bet_tag),
                source_photo_message_id,
                accept_message_id,
                now_str(),
            ),
        )

        test_bet_id = cur.lastrowid

        for leg in legs:
            conn.execute(
                """
                INSERT INTO test_bet_legs
                (test_bet_id, event, selection, market, sport, odds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    test_bet_id,
                    leg.get("event", ""),
                    leg.get("selection", ""),
                    leg.get("market", "Winner"),
                    normalize_sport_slug(leg.get("sport") or infer_sport_from_leg_text(leg.get("event", ""), leg.get("selection", ""), leg.get("market", "Winner"))),
                    float(leg.get("odds", 1)),
                ),
            )

    return test_bet_id


def update_test_ticket_message_ids(
    test_bet_id: int,
    slip_message_id: int = None,
    summary_message_id: int = None,
):
    with db() as conn:
        if slip_message_id is not None:
            conn.execute(
                "UPDATE test_bets SET slip_message_id = ? WHERE id = ?",
                (slip_message_id, test_bet_id),
            )

        if summary_message_id is not None:
            conn.execute(
                "UPDATE test_bets SET summary_message_id = ? WHERE id = ?",
                (summary_message_id, test_bet_id),
            )


def get_test_bet_with_legs(test_bet_id: int):
    with db() as conn:
        bet = conn.execute("SELECT * FROM test_bets WHERE id = ?", (test_bet_id,)).fetchone()

        if not bet:
            return None, []

        legs = conn.execute(
            "SELECT * FROM test_bet_legs WHERE test_bet_id = ? ORDER BY id ASC",
            (test_bet_id,),
        ).fetchall()

    return bet, legs


def extract_test_ticket_id_from_message(message: Optional[Message]) -> Optional[int]:
    if not message:
        return None

    text_parts = []
    if getattr(message, "text", None):
        text_parts.append(message.text)
    if getattr(message, "caption", None):
        text_parts.append(message.caption)

    combined = "\n".join(text_parts)
    if not combined:
        return None

    patterns = [
        r"(?i)test\s*ticket\s*T?#?\s*(\d+)",
        r"(?i)T#\s*(\d+)",
    ]

    for pattern in patterns:
        m = re.search(pattern, combined)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    return None


def get_test_bet_by_ticket_id(test_bet_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM test_bets WHERE id = ? ORDER BY id DESC LIMIT 1",
            (test_bet_id,),
        ).fetchone()


def get_test_bet_by_message(chat_id: int, message_id: int):
    with db() as conn:
        bet = conn.execute(
            """
            SELECT * FROM test_bets
            WHERE chat_id = ?
            AND (
                slip_message_id = ?
                OR summary_message_id = ?
                OR source_photo_message_id = ?
                OR accept_message_id = ?
            )
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, message_id, message_id, message_id, message_id),
        ).fetchone()

        if bet:
            return bet

        bet = conn.execute(
            """
            SELECT * FROM test_bets
            WHERE (
                slip_message_id = ?
                OR summary_message_id = ?
                OR source_photo_message_id = ?
                OR accept_message_id = ?
            )
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, message_id, message_id, message_id),
        ).fetchone()

        if bet:
            return bet

    return None


def settle_test_bet(
    test_bet_id: int,
    result: str,
    settled_by: int,
    adjusted_odds: Optional[float] = None,
) -> Tuple[bool, str, float]:
    bet, legs = get_test_bet_with_legs(test_bet_id)

    if not bet:
        return False, "Test ticket not found.", 0.0

    if str(bet["status"] or "").lower() == "settled":
        return False, f"Test ticket T#{test_bet_id:05d} is already settled as {bet['result']}.", float(bet["pnl"] or 0)

    final_total_odds = float(bet["total_odds"] or 0)
    final_payout = float(bet["payout"] or 0)
    final_profit = float(bet["profit"] or 0)

    if result == "win" and adjusted_odds is not None:
        final_total_odds = round(float(adjusted_odds), 4)
        final_payout = round(float(bet["stake"]) * final_total_odds, 2)
        final_profit = round(final_payout - float(bet["stake"]), 2)
        pnl = final_profit
    elif result == "win":
        pnl = final_profit
    elif result == "loss":
        pnl = -float(bet["stake"])
    else:
        pnl = 0.0

    settled_time = now_str()

    with db() as conn:
        conn.execute(
            """
            UPDATE test_bets
            SET status = 'settled',
                result = ?,
                total_odds = ?,
                payout = ?,
                profit = ?,
                pnl = ?,
                settled_by = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (
                result,
                final_total_odds,
                final_payout,
                final_profit,
                pnl,
                settled_by,
                settled_time,
                test_bet_id,
            ),
        )

        conn.execute(
            """
            UPDATE test_bet_legs
            SET result = ?
            WHERE test_bet_id = ?
            """,
            (result, test_bet_id),
        )

    if result == "win" and adjusted_odds is not None:
        return True, f"Test ticket T#{test_bet_id:05d} settled as WIN at adjusted odds {final_total_odds:.2f}x", pnl

    return True, f"Test ticket T#{test_bet_id:05d} settled as {result.upper()}", pnl


# -------------------------
# MARKET DB ACTIONS
# -------------------------


def create_market_db(
    chat_id: int,
    title: str,
    description: str,
    odds: float,
    rules: str,
    created_by: int,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO markets
            (chat_id, title, description, odds, rules, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                title,
                description,
                odds,
                rules,
                created_by,
                now_str(),
            ),
        )

        return cur.lastrowid


def update_market_message_id(market_id: int, message_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE markets SET market_message_id = ? WHERE id = ?",
            (message_id, market_id),
        )


def get_market_by_id(market_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM markets WHERE id = ?",
            (market_id,),
        ).fetchone()


def get_market_by_message(chat_id: int, message_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM markets
            WHERE chat_id = ?
            AND market_message_id = ?
            AND LOWER(COALESCE(status, 'open')) = 'open'
            """,
            (chat_id, message_id),
        ).fetchone()


def split_market_title(stored_title: str) -> Tuple[str, str]:
    stored_title = str(stored_title or "")

    if "|||" in stored_title:
        title, match_info = stored_title.split("|||", 1)
        return title.strip(), match_info.strip()

    return stored_title.strip(), ""


def settle_market_bets(market_id: int, result: str, settled_by: int):
    market = get_market_by_id(market_id)

    if not market:
        return False, "Market not found.", None, {}

    if str(market["status"] or "").lower() == "settled":
        return False, f"Market #{market_id} is already settled.", market, {}

    settled_at = now_str()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE market_id = ?
            AND LOWER(COALESCE(status, 'open')) = 'open'
            """,
            (market_id,),
        ).fetchall()

        for bet in rows:
            if result == "win":
                pnl = float(bet["profit"])
            elif result == "loss":
                pnl = -float(bet["stake"])
            else:
                pnl = 0.0

            conn.execute(
                """
                UPDATE bets
                SET status = 'settled',
                    result = ?,
                    pnl = ?,
                    settled_by = ?,
                    settled_at = ?
                WHERE id = ?
                """,
                (result, pnl, settled_by, settled_at, bet["id"]),
            )

            conn.execute(
                """
                UPDATE bet_legs
                SET result = ?
                WHERE bet_id = ?
                """,
                (result, bet["id"]),
            )

            apply_bet_to_bookie_balance(bet, pnl)

        conn.execute(
            """
            UPDATE markets
            SET status = 'settled',
                result = ?,
                settled_by = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (result, settled_by, settled_at, market_id),
        )

    with db() as conn:
        all_rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE market_id = ?
            """,
            (market_id,),
        ).fetchall()

    # Do not count voided bets in market wager totals.
    active_rows = active_bet_rows(all_rows)

    total_bets = len(active_rows)
    total_staked = sum(float(r["stake"] or 0) for r in active_rows)
    total_payout = sum(float(r["payout"] or 0) for r in active_rows) if result == "win" else 0.0
    real_user_pnl = sum(float(r["pnl"] or 0) for r in active_rows)
    user_pnl = real_user_pnl + total_manual_pnl_offset(include_hidden=False)
    book_pnl = -user_pnl

    totals = {
        "total_bets": total_bets,
        "total_staked": total_staked,
        "total_payout": total_payout,
        "book_pnl": book_pnl,
    }

    return True, "Market settled.", get_market_by_id(market_id), totals


# -------------------------
# SETTLEMENT
# -------------------------


def normalize_settlement(text: str) -> Optional[str]:
    low = str(text or "").strip().lower()

    if low in ["won", "win", "w", "winner"]:
        return "win"

    if low in ["loss", "lost", "lose", "l"]:
        return "loss"

    if low in ["void", "push", "cancel", "cancelled", "canceled"]:
        return "void"

    return None


def parse_adjusted_win_text(text: str) -> Optional[float]:
    """
    Reply settlement shortcut: won 2.7x / win 2.7 / winner @ 2.7x

    This settles the ticket as a win, but first adjusts the final odds,
    payout, and profit to the odds written in the reply.
    """
    raw = str(text or "").strip().lower().replace("×", "x")

    m = re.fullmatch(
        r"(?:won|win|winner|w)\s*(?:at|@)?\s*(\d+(?:\.\d+)?)\s*x?",
        raw,
    )

    if not m:
        return None

    adjusted_odds = parse_float(m.group(1))

    if adjusted_odds <= 1:
        return None

    return adjusted_odds


def settle_bet(
    bet_id: int,
    result: str,
    settled_by: int,
    adjusted_odds: Optional[float] = None,
) -> Tuple[bool, str, float]:
    bet, legs = get_bet_with_legs(bet_id)

    if not bet:
        return False, "Ticket not found.", 0.0

    if str(bet["status"] or "").lower() == "settled":
        return False, f"Ticket #{bet_id} is already settled as {bet['result']}.", float(bet["pnl"] or 0)

    final_total_odds = float(bet["total_odds"] or 0)
    final_payout = float(bet["payout"] or 0)
    final_profit = float(bet["profit"] or 0)

    if result == "win" and adjusted_odds is not None:
        final_total_odds = round(float(adjusted_odds), 4)
        final_payout = round(float(bet["stake"]) * final_total_odds, 2)
        final_profit = round(final_payout - float(bet["stake"]), 2)
        pnl = final_profit
    elif result == "win":
        pnl = final_profit
    elif result == "loss":
        pnl = -float(bet["stake"])
    else:
        pnl = 0.0

    settled_time = now_str()

    with db() as conn:
        conn.execute(
            """
            UPDATE bets
            SET status = 'settled',
                result = ?,
                total_odds = ?,
                payout = ?,
                profit = ?,
                pnl = ?,
                settled_by = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (
                result,
                final_total_odds,
                final_payout,
                final_profit,
                pnl,
                settled_by,
                settled_time,
                bet_id,
            ),
        )

        conn.execute(
            """
            UPDATE bet_legs
            SET result = ?
            WHERE bet_id = ?
            """,
            (result, bet_id),
        )

    apply_bet_to_bookie_balance(bet, pnl)

    if result == "win" and adjusted_odds is not None:
        return True, f"Ticket #{bet_id} settled as WIN at adjusted odds {final_total_odds:.2f}x", pnl

    return True, f"Ticket #{bet_id} settled as {result.upper()}", pnl




def unsettle_bet(bet_id: int) -> Tuple[bool, str]:
    bet, legs = get_bet_with_legs(bet_id)

    if not bet:
        return False, "Ticket not found."

    if str(bet["status"] or "open").lower() != "settled":
        return False, f"Ticket #{bet_id} is already open."

    reverse_bet_from_bookie_balance(bet)

    with db() as conn:
        conn.execute(
            """
            UPDATE bets
            SET status = 'open',
                result = NULL,
                pnl = 0,
                settled_by = NULL,
                settled_at = NULL
            WHERE id = ?
            """,
            (bet_id,),
        )

        conn.execute(
            """
            UPDATE bet_legs
            SET result = 'open'
            WHERE bet_id = ?
            """,
            (bet_id,),
        )

    return True, f"Ticket #{bet_id} has been unsettled and moved back to OPEN."


def void_all_bets_db(settled_by: int = 0) -> Dict:
    """
    Force-void every real bet in bets.db.

    Safe ledger handling:
    - settled win/loss bets have their old ledger effect reversed first
    - already void/open bets do not change ledger
    - every bet ends as status='settled', result='void', pnl=0
    """
    settled_time = now_str()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, bettor, status, result, pnl
            FROM bets
            WHERE COALESCE(is_test, 0) = 0
            ORDER BY id
            """
        ).fetchall()

        total = len(rows)
        open_bets = 0
        settled_bets = 0
        already_void = 0
        ledger_reversed = 0.0
        ledger_reversed_count = 0

        for bet in rows:
            status = str(bet["status"] or "open").lower()
            result = str(bet["result"] or "").lower()
            pnl = float(bet["pnl"] or 0)

            if status == "settled":
                settled_bets += 1
            else:
                open_bets += 1

            if result == "void":
                already_void += 1

            # If a bet was previously settled win/loss, remove its old ledger impact.
            # Original settlement applied delta = -pnl, so reversal applies +pnl.
            if status == "settled" and result != "void" and abs(pnl) >= 0.005:
                bettor = str(bet["bettor"] or "").strip()
                if bettor:
                    adjust_bookie_balance(bettor, pnl)
                    ledger_reversed += pnl
                    ledger_reversed_count += 1

        conn.execute(
            """
            UPDATE bets
            SET status = 'settled',
                result = 'void',
                pnl = 0,
                settled_by = ?,
                settled_at = ?
            WHERE COALESCE(is_test, 0) = 0
            """,
            (settled_by, settled_time),
        )

        conn.execute(
            """
            UPDATE bet_legs
            SET result = 'void'
            WHERE bet_id IN (SELECT id FROM bets WHERE COALESCE(is_test, 0) = 0)
            """
        )

        # If special-market bets exist, close the market rows too so they do not stay open.
        market_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='markets'"
        ).fetchone()
        market_rows = 0
        if market_exists:
            market_rows = conn.execute("SELECT COUNT(*) AS c FROM markets").fetchone()["c"]
            conn.execute(
                """
                UPDATE markets
                SET status = 'settled',
                    result = 'void',
                    settled_by = ?,
                    settled_at = ?
                """,
                (settled_by, settled_time),
            )

    return {
        "total": total,
        "open_bets": open_bets,
        "settled_bets": settled_bets,
        "already_void": already_void,
        "changed_to_void": total - already_void,
        "ledger_reversed_count": ledger_reversed_count,
        "ledger_reversed": ledger_reversed,
        "markets_voided": market_rows,
        "settled_at": settled_time,
    }



def resequence_bet_numbers(conn) -> Dict:
    """
    Rebuild ticket IDs so remaining bets become 1, 2, 3... in old chronological order.
    Also rewrites bet_legs.bet_id to match the new ticket IDs.
    """
    bet_rows = conn.execute("SELECT * FROM bets ORDER BY id ASC").fetchall()
    leg_rows = conn.execute("SELECT * FROM bet_legs ORDER BY bet_id ASC, id ASC").fetchall()

    old_to_new = {int(row["id"]): i + 1 for i, row in enumerate(bet_rows)}

    bet_columns = [row["name"] for row in conn.execute("PRAGMA table_info(bets)").fetchall()]
    leg_columns = [row["name"] for row in conn.execute("PRAGMA table_info(bet_legs)").fetchall()]

    conn.execute("DELETE FROM bet_legs")
    conn.execute("DELETE FROM bets")

    bet_placeholders = ", ".join(["?"] * len(bet_columns))
    bet_column_sql = ", ".join(bet_columns)

    for row in bet_rows:
        values = []
        for col in bet_columns:
            if col == "id":
                values.append(old_to_new[int(row["id"])])
            else:
                values.append(row[col])

        conn.execute(
            f"INSERT INTO bets ({bet_column_sql}) VALUES ({bet_placeholders})",
            values,
        )

    leg_placeholders = ", ".join(["?"] * len(leg_columns))
    leg_column_sql = ", ".join(leg_columns)
    next_leg_id = 1
    kept_legs = 0

    for row in leg_rows:
        old_bet_id = int(row["bet_id"])

        if old_bet_id not in old_to_new:
            continue

        values = []
        for col in leg_columns:
            if col == "id":
                values.append(next_leg_id)
            elif col == "bet_id":
                values.append(old_to_new[old_bet_id])
            else:
                values.append(row[col])

        conn.execute(
            f"INSERT INTO bet_legs ({leg_column_sql}) VALUES ({leg_placeholders})",
            values,
        )
        next_leg_id += 1
        kept_legs += 1

    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('bets', 'bet_legs')")
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('bets', ?)", (len(bet_rows),))
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('bet_legs', ?)", (kept_legs,))

    return {
        "remaining_bets": len(bet_rows),
        "remaining_legs": kept_legs,
        "old_to_new": old_to_new,
    }


def purge_user_history_db(bettor: str) -> Dict:
    bettor = normalize_user(bettor)

    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM bets WHERE bettor = ? ORDER BY id ASC",
            (bettor,),
        ).fetchall()

        bet_ids = [int(r["id"]) for r in rows]

        if not bet_ids:
            return {"deleted": 0, "remaining_bets": None}

        placeholders = ",".join(["?"] * len(bet_ids))
        conn.execute(f"DELETE FROM bet_legs WHERE bet_id IN ({placeholders})", bet_ids)
        conn.execute(f"DELETE FROM bets WHERE id IN ({placeholders})", bet_ids)
        resequenced = resequence_bet_numbers(conn)

    return {
        "deleted": len(bet_ids),
        "remaining_bets": resequenced["remaining_bets"],
    }


def purge_void_bets_db() -> Dict:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM bets
            WHERE LOWER(COALESCE(result, '')) = 'void'
            ORDER BY id ASC
            """
        ).fetchall()

        bet_ids = [int(r["id"]) for r in rows]

        if not bet_ids:
            return {"deleted": 0, "remaining_bets": None}

        placeholders = ",".join(["?"] * len(bet_ids))
        conn.execute(f"DELETE FROM bet_legs WHERE bet_id IN ({placeholders})", bet_ids)
        conn.execute(f"DELETE FROM bets WHERE id IN ({placeholders})", bet_ids)
        resequenced = resequence_bet_numbers(conn)

    return {
        "deleted": len(bet_ids),
        "remaining_bets": resequenced["remaining_bets"],
    }



def purge_old_test_bets_from_real_ledger_db() -> Dict:
    """
    Cleans up test bets created by the old test system that stored them in the real bets table.
    New test bets use test_bets/test_bet_legs and do not touch real ticket numbering.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM bets
            WHERE COALESCE(is_test, 0) = 1
            ORDER BY id ASC
            """
        ).fetchall()

        bet_ids = [int(r["id"]) for r in rows]

        if not bet_ids:
            return {"deleted": 0, "remaining_bets": None}

        placeholders = ",".join(["?"] * len(bet_ids))
        conn.execute(f"DELETE FROM bet_legs WHERE bet_id IN ({placeholders})", bet_ids)
        conn.execute(f"DELETE FROM bets WHERE id IN ({placeholders})", bet_ids)
        resequenced = resequence_bet_numbers(conn)

    return {
        "deleted": len(bet_ids),
        "remaining_bets": resequenced["remaining_bets"],
    }


def clear_separate_test_ledger_db() -> Dict:
    """Clear the separate test ledger only. This never touches real bets or ticket numbers."""
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM test_bets").fetchone()[0]
        conn.execute("DELETE FROM test_bet_legs")
        conn.execute("DELETE FROM test_bets")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('test_bets', 'test_bet_legs')")

    return {"deleted": int(count)}



def find_duplicate_ticket_numbers_db() -> List[Dict]:
    """
    SQLite PRIMARY KEY normally prevents duplicate bet IDs.
    This command exists as a safety check and also repairs sqlite_sequence after purges.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, COUNT(*) AS c
            FROM bets
            GROUP BY id
            HAVING c > 1
            ORDER BY id ASC
            """
        ).fetchall()

        return [dict(r) for r in rows]


def repair_ticket_numbers_db() -> Dict:
    """
    Force rebuild ticket numbers and reset sqlite_sequence.
    Use this after deleting history if Telegram shows confusing ticket gaps.
    """
    with db() as conn:
        before_count = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        before_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM bets").fetchone()[0]
        duplicates = conn.execute(
            """
            SELECT id, COUNT(*) AS c
            FROM bets
            GROUP BY id
            HAVING c > 1
            """
        ).fetchall()

        resequenced = resequence_bet_numbers(conn)

    return {
        "before_count": int(before_count),
        "before_max": int(before_max),
        "duplicates_found": len(duplicates),
        "remaining_bets": resequenced["remaining_bets"],
    }


def first_leg_summary(bet_id: int) -> str:
    """
    Short readable summary for /bets and /settled.
    - Multi: "4 Leg Multi • first selection • first market"
    - Single: "Single Bet • selection • market"
    """
    with db() as conn:
        legs = conn.execute(
            """
            SELECT event, selection, market, odds
            FROM bet_legs
            WHERE bet_id = ?
            ORDER BY id ASC
            """,
            (bet_id,),
        ).fetchall()

    if not legs:
        return "No leg details saved"

    first = legs[0]
    event = str(row_value(first, "event", "") or "").strip()
    selection = str(row_value(first, "selection", "") or "").strip()
    market = str(row_value(first, "market", "") or "").strip()

    market_clean = market if market and market.lower() not in ["winner", "odds", ""] else ""
    selection_clean = selection or event or "Selection"

    if len(legs) > 1:
        return f"{len(legs)} Leg Multi"

    bits = ["Single Bet"]
    if selection_clean:
        bits.append(selection_clean)
    if market_clean:
        bits.append(market_clean)
    return " • ".join(bits)


def settlement_display_from_legs(legs) -> Dict[str, str]:
    """
    Build match/bet/market display text for the new settlement receipt image.
    Uses saved bet legs from bet_legs or test_bet_legs.
    """
    try:
        legs_list = list(legs or [])
    except Exception:
        legs_list = []

    if not legs_list:
        return {
            "match_name": "Match not provided",
            "bet_on": "Selection not provided",
            "market_name": "Market",
            "sport": "",
        }

    first = legs_list[0]

    event = str(row_value(first, "event", "") or "").strip()
    selection = str(row_value(first, "selection", "") or "").strip()
    market = str(row_value(first, "market", "") or "").strip()

    if len(legs_list) == 1:
        return {
            "match_name": event or "Match not provided",
            "bet_on": selection or "Selection not provided",
            "market_name": market or "Market",
            "sport": "",
        }

    return {
        "match_name": event or f"{len(legs_list)} Leg Multi",
        "bet_on": selection or "Multi Selection",
        "market_name": f"{len(legs_list)} Leg Multi" + (f" • {market}" if market else ""),
        "sport": "",
    }

# -------------------------
# COMMANDS
# -------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏦 <b>Lenny Book Bot Online</b>\n\n"
        "Normal bet:\n"
        "<code>$5 at 2.5x melon bet approved</code>\n\n"
        "Big bet:\n"
        "<code>$100 at 2.7x melon bet accepted pages 2</code>\n\n"
        "Special market:\n"
        "<code>/market Mega Goals Combo, Portugal v Uzbekistan; England v Ghana; France v Brazil, Combined Over 8 Goals, 3.20, Void if exactly 8, 90 mins only</code>\n\n"
        "Commands:\n"
        "• <code>/add melon @stylo</code>\n"
        "• <code>/add melon @stylo melon.png</code> = add/update avatar\n"
        "• <code>/alias andrew sand @sandrcw</code> = make aliases point to same ledger\n"
        "• <code>/userlist</code> / <code>/userlist all</code>\n"
        "• <code>/hideuser melon</code> / <code>/showuser melon</code>\n"
        "• <code>/deleteuser melon YES</code>\n"
        "• <code>/profile melon</code>\n"
        "• <code>/stats</code>\n"
        "• <code>/ledger</code> = bookie balances\n"
        "• <code>/freebets</code> = all available free bets\n"
        "• <code>/freebets wipeall YES</code> = expire all available free bets\n"
        "• <code>/monthly</code> = monthly wager leaderboard\n"
        "• <code>/monthly 2026-06</code> = leaderboard for a month\n"
        "• <code>/bets</code> = all open bets\n"
        "• <code>/settled</code> = recent settled bets\n"
        "• <code>/recent melon</code> = one user's recent settled bets\n"
        "• <code>/fixnumbers YES</code> = repair ticket numbers\n"
        "• <code>/bets melon</code> = one user's open bets\n"
        "• <code>/bet 1</code>\n"
        "• <code>/market Title, Match 1; Match 2; Match 3, Description, Odds, Condition 1...</code>\n"
        "• <code>/msettle 1 win/loss/void</code>",
        parse_mode=ParseMode.HTML,
    )


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/add melon @stylo</code>\n"
            "<code>/add melon @stylo melon.png</code>\n"
            "<code>/add melon melon.png</code>\n"
            "<code>/add arc w-$200</code> = monthly starting wager\n"
            "<code>/add arc b-$200</code> = bookie ledger balance",
            parse_mode=ParseMode.HTML,
        )
        return

    keyword = context.args[0]
    telegram_username = ""
    avatar = ""
    starting_wager = None
    bookie_balance = None

    # Supported:
    # /add melon @stylo
    # /add melon @stylo melon.png
    # /add melon melon.png
    # /add arc w-$200
    # /add arc b-$200
    # /add arc @arc arc.png w-$200 b-$50
    for item in context.args[1:]:
        parsed_wager = parse_starting_wager_token(item)
        parsed_balance = parse_bookie_balance_token(item)

        if parsed_wager is not None:
            starting_wager = parsed_wager
        elif parsed_balance is not None:
            bookie_balance = parsed_balance
        elif item.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            avatar = item
        elif not telegram_username:
            telegram_username = item

    ok, msg = add_user_to_json(keyword, telegram_username, avatar, starting_wager, bookie_balance)

    if ok:
        await update.message.reply_text(f"✅ <b>{safe_text(msg)}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ {safe_text(msg)}", parse_mode=ParseMode.HTML)


async def alias_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/alias andrew sand @sandrcw</code>\n"
            "<code>/alias anchor anchorthis @anchorthis</code>\n\n"
            "All aliases will point to the same ledger.",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, msg = add_aliases_to_user(context.args[0], context.args[1:])
    await update.message.reply_text(
        f"✅ <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
        parse_mode=ParseMode.HTML,
    )


def get_bookie_balance(user_key: str) -> float:
    record = get_user_record(user_key)
    try:
        return float(record.get("bookie_balance") or 0)
    except Exception:
        return 0.0


def format_ledger_balance(amount: float) -> str:
    amount = float(amount or 0)
    if amount > 0:
        return f"{money(amount)} owed to bookie"
    if amount < 0:
        return f"{money(abs(amount))} owed to player"
    return f"{money(0)} settled"


def set_bookie_balance(user_key: str, amount: float) -> Tuple[bool, str]:
    key = canonicalize_bettor(user_key)
    if not key:
        return False, "Invalid user."

    data = load_users()

    if key not in data["users"]:
        data["users"][key] = {
            "display": display_name_from_key(key),
            "telegram": "",
            "avatar": "",
            "starting_wager": 0.0,
            "bookie_balance": 0.0,
            "added_at": now_str(),
        }

    data["users"][key]["bookie_balance"] = round(float(amount), 2)
    save_users(data)

    return True, f"{display_name_from_key(key)} bookie balance set to {money(amount)}."


def adjust_bookie_balance(user_key: str, delta: float) -> Tuple[bool, str]:
    key = canonicalize_bettor(user_key)
    if not key:
        return False, "Invalid user."

    current = get_bookie_balance(key)
    new_amount = round(current + float(delta), 2)
    ok, _ = set_bookie_balance(key, new_amount)

    if not ok:
        return False, "Could not update balance."

    sign = "+" if float(delta) >= 0 else "-"
    return True, (
        f"{display_name_from_key(key)} bookie balance {sign}{money(abs(float(delta)))} "
        f"→ {money(new_amount)}."
    )





MANUAL_STAT_FIELDS = {
    "pnl": {"aliases": ["pnl", "profit", "lifetimepnl", "lifetime_pnl"], "type": "money"},
    "wagered": {"aliases": ["wager", "wagered", "totalwager", "total_wager", "volume", "stake", "staked"], "type": "money"},
    "settled": {"aliases": ["settled", "settledbets", "settled_bets", "closed", "closedbets"], "type": "count"},
    "won": {"aliases": ["won", "wins", "win"], "type": "count"},
    "lost": {"aliases": ["lost", "loss", "losses", "lose", "loses"], "type": "count"},
    "void": {"aliases": ["void", "voids", "push", "pushes", "refunded"], "type": "count"},
    "total_bets": {"aliases": ["total", "bets", "totalbets", "total_bets"], "type": "count"},
}

MANUAL_STAT_ALIAS = {}
for _field, _meta in MANUAL_STAT_FIELDS.items():
    for _alias in _meta["aliases"]:
        MANUAL_STAT_ALIAS[_alias] = _field


def normalize_manual_stat_field(field: str) -> Optional[str]:
    f = str(field or "").strip().lower().replace("-", "_")
    return MANUAL_STAT_ALIAS.get(f)


def manual_stat_type(field: str) -> str:
    return MANUAL_STAT_FIELDS.get(field, {}).get("type", "money")


def _ensure_user_for_manual_stats(data: dict, key: str):
    if key not in data["users"]:
        data["users"][key] = {
            "display": display_name_from_key(key),
            "telegram": "",
            "avatar": "",
            "starting_wager": 0.0,
            "bookie_balance": 0.0,
            "hidden": False,
            "added_at": now_str(),
        }
    data["users"][key].setdefault("manual_stat_offsets", {})


def get_manual_stat_offset(user_key: str, field: str) -> float:
    """Manual stat offset. Displayed stat = real DB stat + saved offset."""
    key = normalize_user(user_key)
    field = normalize_manual_stat_field(field) or field
    record = get_user_record(key)
    try:
        offsets = record.get("manual_stat_offsets") or {}
        if field in offsets:
            return float(offsets.get(field) or 0)
    except Exception:
        pass

    # Backwards compatibility with the earlier /setpnl implementation.
    if field == "pnl":
        try:
            return float(record.get("manual_pnl_offset") or 0)
        except Exception:
            return 0.0
    return 0.0


def set_manual_stat_offset(user_key: str, field: str, offset: float) -> Tuple[bool, str]:
    key = normalize_user(user_key)
    field = normalize_manual_stat_field(field) or field
    if not key:
        return False, "Invalid user."
    if field not in MANUAL_STAT_FIELDS:
        return False, "Invalid stat field."

    data = load_users()
    _ensure_user_for_manual_stats(data, key)
    data["users"][key]["manual_stat_offsets"][field] = round(float(offset), 2)
    data["users"][key][f"manual_{field}_set_at"] = now_str()

    # Keep old field synced for older code paths/cards.
    if field == "pnl":
        data["users"][key]["manual_pnl_offset"] = round(float(offset), 2)
        data["users"][key]["manual_pnl_set_at"] = now_str()

    save_users(data)
    shown = money(offset) if manual_stat_type(field) == "money" else str(int(round(float(offset))))
    return True, f"{display_name_from_key(key)} manual {field} offset set to {shown}."


def get_manual_pnl_offset(user_key: str) -> float:
    return get_manual_stat_offset(user_key, "pnl")


def set_manual_pnl_offset(user_key: str, offset: float) -> Tuple[bool, str]:
    return set_manual_stat_offset(user_key, "pnl", offset)



def _ensure_user_for_profile_metadata(data: dict, key: str):
    data.setdefault("users", {})
    if key not in data["users"]:
        data["users"][key] = {
            "display": display_name_from_key(key),
            "telegram": "",
            "avatar": "",
            "starting_wager": 0.0,
            "bookie_balance": 0.0,
            "hidden": False,
            "added_at": now_str(),
        }
    data["users"][key].setdefault("free_bets_available", 0.0)
    data["users"][key].setdefault("rank", "")


def get_free_bets_available(user_key: str) -> float:
    record = get_user_record(user_key)
    try:
        return max(0.0, float(record.get("free_bets_available") or 0.0))
    except Exception:
        return 0.0


def set_free_bets_available(user_key: str, amount: float) -> Tuple[bool, str]:
    data = load_users()
    key = resolve_user_key(user_key, data)
    if not key:
        return False, "Invalid user."
    _ensure_user_for_profile_metadata(data, key)
    value = max(0.0, round(float(amount), 2))
    data["users"][key]["free_bets_available"] = value
    data["users"][key]["free_bets_updated_at"] = now_str()
    save_users(data)
    return True, f"{display_name_from_key(key)} free bets available set to {money(value)}."


def adjust_free_bets_available(user_key: str, delta: float) -> Tuple[bool, str]:
    key = canonicalize_bettor(user_key)
    if not key:
        return False, "Invalid user."
    new_value = max(0.0, round(get_free_bets_available(key) + float(delta), 2))
    return set_free_bets_available(key, new_value)


def is_free_bet_tag(tag: str) -> bool:
    return clean_bet_tag(tag) == "FREE BET"


def consume_free_bet_on_placement(user_key: str, stake: float, bet_tag: str) -> Optional[Dict]:
    """
    Free bets expire from the visible available balance when the ticket is placed.
    Settlement later should not touch free_bets_available.
    """
    if not is_free_bet_tag(bet_tag):
        return None

    try:
        stake_value = max(0.0, round(float(stake), 2))
    except Exception:
        stake_value = 0.0

    if stake_value <= 0:
        return None

    data = load_users()
    key = resolve_user_key(user_key, data)
    if not key:
        return None

    _ensure_user_for_profile_metadata(data, key)
    record = data["users"][key]

    try:
        old_value = max(0.0, float(record.get("free_bets_available") or 0.0))
    except Exception:
        old_value = 0.0

    new_value = max(0.0, round(old_value - stake_value, 2))
    consumed = round(old_value - new_value, 2)

    if abs(new_value - old_value) < 0.005:
        return {
            "user_key": key,
            "old_value": round(old_value, 2),
            "new_value": round(new_value, 2),
            "consumed": 0.0,
        }

    record["free_bets_available"] = new_value
    record["free_bets_updated_at"] = now_str()
    record["free_bets_last_consumed_at"] = now_str()
    record["free_bets_last_consumed"] = consumed
    record["free_bets_last_placed_stake"] = stake_value
    save_users(data)

    return {
        "user_key": key,
        "old_value": round(old_value, 2),
        "new_value": round(new_value, 2),
        "consumed": consumed,
    }


def free_bets_available_rows(include_zero: bool = False) -> List[Dict]:
    data = load_users()
    rows = []

    for key, record in data.get("users", {}).items():
        try:
            amount = max(0.0, float(record.get("free_bets_available") or 0.0))
        except Exception:
            amount = 0.0

        if amount <= 0 and not include_zero:
            continue

        label = str(record.get("telegram") or record.get("display") or display_name_from_key(key)).strip()
        rows.append({
            "key": key,
            "label": label,
            "amount": round(amount, 2),
            "hidden": bool(record.get("hidden")),
        })

    rows.sort(key=lambda item: (-item["amount"], item["label"].lower()))
    return rows


def wipe_all_free_bets_available() -> Dict:
    data = load_users()
    users = data.get("users", {})
    touched = 0
    total = 0.0
    wiped_at = now_str()

    for record in users.values():
        try:
            amount = max(0.0, float(record.get("free_bets_available") or 0.0))
        except Exception:
            amount = 0.0

        if amount <= 0:
            continue

        total += amount
        touched += 1
        record["free_bets_available"] = 0.0
        record["free_bets_updated_at"] = wiped_at
        record["free_bets_wiped_at"] = wiped_at

    if touched:
        save_users(data)

    return {"users": touched, "total": round(total, 2), "wiped_at": wiped_at}


def get_user_rank(user_key: str) -> str:
    record = get_user_record(user_key)
    return str(record.get("rank") or "").strip()


def set_user_rank(user_key: str, rank: str) -> Tuple[bool, str]:
    data = load_users()
    key = resolve_user_key(user_key, data)
    if not key:
        return False, "Invalid user."
    _ensure_user_for_profile_metadata(data, key)
    clean_rank = re.sub(r"[\r\n\t]+", " ", str(rank or "")).strip()[:60]
    data["users"][key]["rank"] = clean_rank
    data["users"][key]["rank_updated_at"] = now_str()
    save_users(data)
    if clean_rank:
        return True, f"{display_name_from_key(key)} rank set to {clean_rank}."
    return True, f"{display_name_from_key(key)} rank cleared."


def is_free_bet_row(row) -> bool:
    """Recognize free bets from the saved tag or written bet text."""
    text = " ".join(
        str(row_value(row, field, "") or "")
        for field in ("bet_tag", "note", "conditions")
    ).upper()
    normalized = re.sub(r"[^A-Z0-9]+", " ", text)
    return "FREE BET" in normalized or "FREEBET" in text.replace(" ", "")


def total_free_bet_value(rows) -> float:
    """Total face value of all genuine free bets ever given to the player."""
    return round(sum(
        float(row_value(r, "stake", 0) or 0)
        for r in rows
        if not is_test_bet(r) and is_free_bet_row(r)
    ), 2)

def get_real_stats_for_user(user_key: str) -> dict:
    key = normalize_user(user_key)
    empty = {"pnl": 0.0, "wagered": 0.0, "settled": 0, "won": 0, "lost": 0, "void": 0, "total_bets": 0}
    if not key:
        return empty

    with db() as conn:
        rows = conn.execute("SELECT * FROM bets WHERE bettor = ?", (key,)).fetchall()

    real_rows = [r for r in rows if not is_test_bet(r)]
    active_rows = active_bet_rows(real_rows)
    void_rows = [r for r in real_rows if is_void_bet(r)]
    won = sum(1 for r in active_rows if str(r["result"] or "").lower() in {"win", "won"})
    lost = sum(1 for r in active_rows if str(r["result"] or "").lower() == "loss")

    return {
        "pnl": round(sum(float(r["pnl"] or 0) for r in active_rows), 2),
        "wagered": round(sum(float(r["stake"] or 0) for r in active_rows), 2),
        "settled": won + lost,
        "won": won,
        "lost": lost,
        "void": len(void_rows),
        "total_bets": len(active_rows) + len(void_rows),
    }


def get_real_active_pnl_for_user(user_key: str) -> float:
    return float(get_real_stats_for_user(user_key).get("pnl") or 0)


def set_manual_stat_target(user_key: str, field: str, target_value: float) -> Tuple[bool, str]:
    """
    Makes displayed stat equal target_value now.
    Future bets keep updating because real DB stats continue changing while this offset stays fixed.
    """
    key = normalize_user(user_key)
    field = normalize_manual_stat_field(field) or field
    if not key:
        return False, "Invalid user."
    if field not in MANUAL_STAT_FIELDS:
        return False, "Invalid stat field."

    real_stats = get_real_stats_for_user(key)
    real_value = float(real_stats.get(field) or 0)
    target_value = float(target_value)

    if manual_stat_type(field) == "count":
        target_value = int(round(target_value))
        real_value = int(round(real_value))

    offset = round(float(target_value) - float(real_value), 2)
    ok, msg = set_manual_stat_offset(key, field, offset)
    if not ok:
        return False, msg

    shown_target = money(target_value) if manual_stat_type(field) == "money" else str(int(target_value))
    shown_real = money(real_value) if manual_stat_type(field) == "money" else str(int(real_value))
    shown_offset = money(offset) if manual_stat_type(field) == "money" else str(int(round(offset)))
    return True, (
        f"{display_name_from_key(key)} displayed {field} set to {shown_target}. "
        f"Real DB {field} is {shown_real}, saved offset is {shown_offset}."
    )


def set_manual_pnl_target(user_key: str, target_pnl: float) -> Tuple[bool, str]:
    return set_manual_stat_target(user_key, "pnl", target_pnl)


def total_manual_stat_offset(field: str, include_hidden: bool = False) -> float:
    field = normalize_manual_stat_field(field) or field
    data = load_users()
    total = 0.0
    for record in data.get("users", {}).values():
        if record.get("hidden") and not include_hidden:
            continue
        try:
            offsets = record.get("manual_stat_offsets") or {}
            if field in offsets:
                total += float(offsets.get(field) or 0)
            elif field == "pnl":
                total += float(record.get("manual_pnl_offset") or 0)
        except Exception:
            pass
    return round(total, 2)


def total_manual_pnl_offset(include_hidden: bool = False) -> float:
    return total_manual_stat_offset("pnl", include_hidden=include_hidden)

def apply_bet_to_bookie_balance(bet, pnl: float) -> Optional[float]:
    """
    Positive bookie_balance means the player owes the bookie.
    Negative bookie_balance means the bookie owes the player.

    Settlement effect:
    - user loss: pnl = -stake, balance increases by stake
    - user win: pnl = +profit, balance decreases by profit
    - void: pnl = 0, no change
    """
    if not bet or is_test_bet(bet):
        return None

    bettor = str(row_value(bet, "bettor", "") or "").strip()
    if not bettor:
        return None

    delta = -float(pnl or 0)
    adjust_bookie_balance(bettor, delta)
    return get_bookie_balance(bettor)


def reverse_bet_from_bookie_balance(bet) -> Optional[float]:
    """
    Reverse the ledger effect when /unsettle is used.
    Original settlement applied -pnl, so unsetting applies +pnl.
    """
    if not bet or is_test_bet(bet):
        return None

    bettor = str(row_value(bet, "bettor", "") or "").strip()
    if not bettor:
        return None

    old_pnl = float(row_value(bet, "pnl", 0) or 0)
    adjust_bookie_balance(bettor, old_pnl)
    return get_bookie_balance(bettor)



async def under_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg:
        await msg.reply_text("Under maintenance.")
    elif update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Under maintenance.")

async def ledger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = [str(a).strip() for a in context.args if str(a).strip()]
    include_hidden = bool(args and args[0].lower() in ["all", "hidden", "showhidden"])
    if include_hidden:
        args = args[1:]

    usage = (
        "Usage:\n"
        "<code>/ledger</code> = show visible bookie balances\n"
        "<code>/ledger all</code> = include hidden users\n"
        "<code>/ledger melon</code> = show one user\n"
        "<code>/ledger set melon 200</code>\n"
        "<code>/ledger add melon 50</code>\n"
        "<code>/ledger reduce melon 25</code>\n"
        "<code>/ledger clear melon</code>"
    )

    if args:
        action = args[0].lower()

        if action in ["set", "add", "plus", "increase", "inc", "reduce", "remove", "minus", "decrease", "dec", "sub", "clear", "reset"]:
            if len(args) < 2:
                await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
                return

            user_key = canonicalize_bettor(args[1])

            if not user_key:
                await update.message.reply_text("Invalid user.")
                return

            if action in ["clear", "reset"]:
                ok, msg = set_bookie_balance(user_key, 0)
                await update.message.reply_text(
                    f"✅ <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
                    parse_mode=ParseMode.HTML,
                )
                return

            if len(args) < 3:
                await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
                return

            try:
                amount = parse_money_amount(args[2])
            except Exception:
                await update.message.reply_text(
                    "Invalid amount. Example: <code>/ledger add melon 50</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            if action == "set":
                ok, msg = set_bookie_balance(user_key, amount)
            elif action in ["add", "plus", "increase", "inc"]:
                ok, msg = adjust_bookie_balance(user_key, abs(amount))
            else:
                ok, msg = adjust_bookie_balance(user_key, -abs(amount))

            await update.message.reply_text(
                f"✅ <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
                parse_mode=ParseMode.HTML,
            )
            return

        # /ledger melon = one user's balance
        user_key = canonicalize_bettor(args[0])
        label = preferred_ledger_label(user_key)
        balance = get_bookie_balance(user_key)

        await update.message.reply_text(
            f"📒 <b>BOOKIE LEDGER</b>\n"
            f"👤 <b>{safe_text(label)}</b>\n"
            f"💼 Balance: <b>{safe_text(format_ledger_balance(balance))}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    data = load_users()
    users = data.get("users", {})

    if not users:
        await update.message.reply_text("📭 No users added yet.")
        return

    rows = []
    total = 0.0

    for key, record in users.items():
        if record.get("hidden") and not include_hidden:
            continue

        balance = get_bookie_balance(key)
        total += balance
        label = preferred_ledger_label(key)
        rows.append((label, balance))

    rows.sort(key=lambda item: abs(item[1]), reverse=True)

    msg = "📒 <b>BOOKIE LEDGER</b>\n━━━━━━━━━━━━━━━━━━\n"
    if include_hidden:
        msg += "👁 Showing hidden users too\n"
    msg += f"💼 Net: <b>{safe_text(format_ledger_balance(total))}</b>\n\n"

    if not rows:
        msg += "📭 No visible ledger users. Use <code>/ledger all</code> to include hidden users.\n"

    for label, balance in rows:
        if abs(balance) < 0.005:
            emoji = "⚪"
        elif balance > 0:
            emoji = "🟢"
        else:
            emoji = "🔴"

        msg += f"{emoji} <code>{safe_text(label)}</code> — <b>{safe_text(format_ledger_balance(balance))}</b>\n"

    await update.message.reply_text(msg[:3900], parse_mode=ParseMode.HTML)




async def payin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record money received from a player. This reduces what the player owes the bookie."""
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = [str(a).strip() for a in context.args if str(a).strip()]
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: <code>/payin player amount</code>\n"
            "Example: <code>/payin zenn 17.6</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    user_key = canonicalize_bettor(args[0])
    if not user_key:
        await update.message.reply_text("Invalid user.")
        return

    try:
        amount = abs(float(parse_money_amount(args[1])))
    except Exception:
        await update.message.reply_text(
            "Invalid amount. Example: <code>/payin zenn 17.6</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be greater than zero.")
        return

    old_balance = get_bookie_balance(user_key)
    ok, _ = adjust_bookie_balance(user_key, -amount)
    new_balance = get_bookie_balance(user_key)
    label = display_name_from_key(user_key)

    if not ok:
        await update.message.reply_text("⚠️ Could not update the ledger.")
        return

    await update.message.reply_text(
        f"✅ <b>PAYMENT RECEIVED</b>\n"
        f"👤 {safe_text(label)}\n"
        f"💵 Received: <b>{safe_text(money(amount))}</b>\n"
        f"📒 Before: <b>{safe_text(format_ledger_balance(old_balance))}</b>\n"
        f"📒 Now: <b>{safe_text(format_ledger_balance(new_balance))}</b>",
        parse_mode=ParseMode.HTML,
    )


async def payout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record money paid by the bookie to a player."""
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = [str(a).strip() for a in context.args if str(a).strip()]
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: <code>/payout player amount</code>\n"
            "Example: <code>/payout muck 10</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    user_key = canonicalize_bettor(args[0])
    if not user_key:
        await update.message.reply_text("Invalid user.")
        return

    try:
        amount = abs(float(parse_money_amount(args[1])))
    except Exception:
        await update.message.reply_text(
            "Invalid amount. Example: <code>/payout muck 10</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be greater than zero.")
        return

    old_balance = get_bookie_balance(user_key)
    ok, _ = adjust_bookie_balance(user_key, amount)
    new_balance = get_bookie_balance(user_key)
    label = display_name_from_key(user_key)

    if not ok:
        await update.message.reply_text("⚠️ Could not update the ledger.")
        return

    await update.message.reply_text(
        f"✅ <b>PAYMENT SENT</b>\n"
        f"👤 {safe_text(label)}\n"
        f"💵 Sent: <b>{safe_text(money(amount))}</b>\n"
        f"📒 Before: <b>{safe_text(format_ledger_balance(old_balance))}</b>\n"
        f"📒 Now: <b>{safe_text(format_ledger_balance(new_balance))}</b>",
        parse_mode=ParseMode.HTML,
    )




async def setstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = [str(a).strip() for a in context.args if str(a).strip()]
    usage = (
        "Usage:\n"
        "<code>/setpnl player -290</code>\n"
        "<code>/setstats player pnl -290</code>\n"
        "<code>/setstats player wagered 1850</code>\n"
        "<code>/setstats player settled 96</code>\n"
        "<code>/setstats player won 54</code>\n"
        "<code>/setstats player lost 39</code>\n"
        "<code>/setstats player void 3</code>\n\n"
        "All at once:\n"
        "<code>/setstats player pnl=-290 wagered=1850 settled=96 won=54 lost=39 void=3</code>\n\n"
        "These set the displayed history baseline now. Future settled bets update from that baseline."
    )

    if not args:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    user_key = canonicalize_bettor(args[0])
    if not user_key:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    updates = []

    # Backwards compatible: /setpnl player -290
    if len(args) == 2 and not ("=" in args[1]):
        updates.append(("pnl", args[1]))

    # Single field: /setstats player pnl -290
    elif len(args) >= 3 and "=" not in args[1]:
        field = normalize_manual_stat_field(args[1])
        if not field:
            await update.message.reply_text(
                "Invalid field. Use pnl, wagered, settled, won, lost, void, or total.",
                parse_mode=ParseMode.HTML,
            )
            return
        updates.append((field, args[2]))

    # Multiple key=value: /setstats player pnl=-290 wagered=1850 won=54
    else:
        for token in args[1:]:
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            field = normalize_manual_stat_field(k)
            if not field:
                await update.message.reply_text(f"Invalid field: <code>{safe_text(k)}</code>", parse_mode=ParseMode.HTML)
                return
            updates.append((field, v))

    if not updates:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    messages = []
    for field, value_text in updates:
        try:
            if manual_stat_type(field) == "money":
                target = parse_money_amount(value_text)
            else:
                target = int(round(float(str(value_text).replace(",", ""))))
        except Exception:
            await update.message.reply_text(
                f"Invalid value for {safe_text(field)}: <code>{safe_text(value_text)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        ok, msg = set_manual_stat_target(user_key, field, target)
        if not ok:
            await update.message.reply_text(f"⚠️ {safe_text(msg)}", parse_mode=ParseMode.HTML)
            return
        messages.append(msg)

    await update.message.reply_text(
        "✅ <b>Manual stats updated</b>\n" + "\n".join(f"• {safe_text(m)}" for m in messages),
        parse_mode=ParseMode.HTML,
    )

async def userlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_users()
    users = data.get("users", {})

    include_hidden = bool(context.args and str(context.args[0]).lower() in ["all", "hidden", "showhidden"])

    if not users:
        await update.message.reply_text("📭 No users added yet. Use /add melon @stylo")
        return

    msg = "👥 <b>Lenny Book Users</b>\n━━━━━━━━━━━━━━━━━━\n"
    if include_hidden:
        msg += "👁 Showing hidden users too\n"

    visible_count = 0

    for key, value in sorted(users.items()):
        if value.get("hidden") and not include_hidden:
            continue

        visible_count += 1
        display = value.get("display", display_name_from_key(key))
        tg = value.get("telegram", "")

        avatar = value.get("avatar", "")
        avatar_text = f" | 🖼 <code>{safe_text(avatar)}</code>" if avatar else ""
        try:
            start_wager_value = float(value.get("starting_wager") or 0)
        except Exception:
            start_wager_value = 0.0
        start_wager_text = f" | 🏁 <b>{money(start_wager_value)}</b> start" if start_wager_value > 0 else ""
        try:
            bookie_balance_value = float(value.get("bookie_balance") or 0)
        except Exception:
            bookie_balance_value = 0.0
        ledger_text = f" | 📒 <b>{safe_text(format_ledger_balance(bookie_balance_value))}</b>" if abs(bookie_balance_value) >= 0.005 else ""
        try:
            manual_pnl_value = float(value.get("manual_pnl_offset") or 0)
        except Exception:
            manual_pnl_value = 0.0
        manual_pnl_text = f" | 📊 PNL offset <b>{safe_text(money(manual_pnl_value))}</b>" if abs(manual_pnl_value) >= 0.005 else ""
        hidden_text = " | 🙈 <b>hidden</b>" if value.get("hidden") else ""
        aliases = [a for a in user_aliases_for_record(key, value) if a != key and a != alias_key(tg)]
        aliases_text = f" | 🏷 <code>{safe_text(', '.join(aliases))}</code>" if aliases else ""

        if tg:
            msg += f"• <code>{safe_text(display)}</code> → <b>{safe_text(tg)}</b>{aliases_text}{avatar_text}{start_wager_text}{ledger_text}{manual_pnl_text}{hidden_text}\n"
        else:
            msg += f"• <code>{safe_text(display)}</code> → <i>No username saved</i>{aliases_text}{avatar_text}{start_wager_text}{ledger_text}{manual_pnl_text}{hidden_text}\n"

    if visible_count == 0:
        msg += "📭 No visible users. Use <code>/userlist all</code> to include hidden users.\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def hide_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/hideuser melon</code>", parse_mode=ParseMode.HTML)
        return

    ok, msg = set_user_hidden(context.args[0], True)
    await update.message.reply_text(
        f"✅ <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
        parse_mode=ParseMode.HTML,
    )


async def show_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/showuser melon</code>", parse_mode=ParseMode.HTML)
        return

    ok, msg = set_user_hidden(context.args[0], False)
    await update.message.reply_text(
        f"✅ <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
        parse_mode=ParseMode.HTML,
    )


async def delete_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if len(context.args) < 2 or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This deletes the user only from <b>users.json</b>. It does not delete bet history.\\n\\n"
            "Use: <code>/deleteuser melon YES</code>\\n"
            "To delete bet history too, use: <code>/purgeuser melon YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, msg = delete_user_from_json(context.args[0])
    await update.message.reply_text(
        f"🗑 <b>{safe_text(msg)}</b>" if ok else f"⚠️ {safe_text(msg)}",
        parse_mode=ParseMode.HTML,
    )



async def freebets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual free-bet credit balance.
      /freebets
      /freebets all
      /freebets melon
      /freebets set melon 35
      /freebets add melon 10
      /freebets reduce melon 5
      /freebets clear melon
      /freebets wipeall YES
    """
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    command_text = str(getattr(update.message, "text", "") or "")
    command_match = re.match(r"^\s*/([A-Za-z0-9_]+)", command_text)
    command_name = command_match.group(1).lower() if command_match else "freebets"

    args = list(context.args or [])
    if command_name in {"allfreebets", "freebetsall"} and not args:
        args = ["all"]
    elif command_name in {"clearfreebets", "wipefreebets", "expirefreebets"}:
        args = ["wipeall"] + args

    usage = (
        "Commands:\n"
        "<code>/freebets</code> or <code>/freebets all</code> = list all available free bets\n"
        "<code>/freebets melon</code> = check one user\n"
        "<code>/freebets set melon 35</code>\n"
        "<code>/freebets add melon 10</code>\n"
        "<code>/freebets reduce melon 5</code>\n"
        "<code>/freebets clear melon</code>\n"
        "<code>/freebets wipeall YES</code> = expire every user's available free bets"
    )

    if not args:
        rows = free_bets_available_rows()
        total = sum(float(r["amount"]) for r in rows)
        header = (
            "🎟 <b>FREE BETS AVAILABLE</b>\n"
            f"Total available: <b>{safe_text(money(total))}</b>\n"
            f"Users with balance: <b>{len(rows)}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

        if not rows:
            await update.message.reply_text(
                header + "\n📭 No available free bets right now.\n\n" + usage,
                parse_mode=ParseMode.HTML,
            )
            return

        blocks = []
        for row in rows:
            hidden_text = " | hidden" if row["hidden"] else ""
            blocks.append(
                f"• <code>{safe_text(row['label'])}</code>: "
                f"<b>{safe_text(money(row['amount']))}</b>{safe_text(hidden_text)}\n"
            )

        await reply_html_in_chunks(update.message, header + "\n", blocks)
        return

    action = args[0].lower()

    if action in {"all", "list", "view", "balances", "available"}:
        rows = free_bets_available_rows()
        total = sum(float(r["amount"]) for r in rows)
        header = (
            "🎟 <b>FREE BETS AVAILABLE</b>\n"
            f"Total available: <b>{safe_text(money(total))}</b>\n"
            f"Users with balance: <b>{len(rows)}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
        )

        if not rows:
            await update.message.reply_text(header + "📭 No available free bets right now.", parse_mode=ParseMode.HTML)
            return

        blocks = []
        for row in rows:
            hidden_text = " | hidden" if row["hidden"] else ""
            blocks.append(
                f"• <code>{safe_text(row['label'])}</code>: "
                f"<b>{safe_text(money(row['amount']))}</b>{safe_text(hidden_text)}\n"
            )

        await reply_html_in_chunks(update.message, header, blocks)
        return

    if action in {"wipeall", "clearall", "expireall", "resetall"}:
        confirmed = len(args) >= 2 and str(args[1]).upper() == "YES"
        if not confirmed:
            await update.message.reply_text(
                f"⚠️ This will set every user's available free-bet balance to <b>{safe_text(money(0))}</b>.\n\n"
                "Use: <code>/freebets wipeall YES</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        result = wipe_all_free_bets_available()
        await update.message.reply_text(
            "✅ <b>All available free bets wiped.</b>\n"
            f"Users updated: <b>{result['users']}</b>\n"
            f"Expired total: <b>{safe_text(money(result['total']))}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action in {"help", "usage"}:
        await update.message.reply_text(
            "🎟 <b>Free Bets Available</b>\n\n"
            + usage,
            parse_mode=ParseMode.HTML,
        )
        return

    if action in {"set", "add", "reduce", "remove", "subtract", "clear"}:
        if len(args) < 2:
            await update.message.reply_text("Usage: <code>/freebets set melon 35</code>", parse_mode=ParseMode.HTML)
            return
        user_key = args[1]
        if action == "clear":
            ok, msg = set_free_bets_available(user_key, 0.0)
        else:
            if len(args) < 3:
                await update.message.reply_text("Enter an amount. Example: <code>/freebets add melon 10</code>", parse_mode=ParseMode.HTML)
                return
            try:
                amount = abs(float(parse_money_amount(args[2])))
            except Exception:
                await update.message.reply_text("Invalid amount.")
                return
            if action == "set":
                ok, msg = set_free_bets_available(user_key, amount)
            elif action == "add":
                ok, msg = adjust_free_bets_available(user_key, amount)
            else:
                ok, msg = adjust_free_bets_available(user_key, -amount)
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + safe_text(msg), parse_mode=ParseMode.HTML)
        return

    user_key = canonicalize_bettor(args[0])
    value = get_free_bets_available(user_key)
    await update.message.reply_text(
        f"🎟 <b>{safe_text(display_name_from_key(user_key))}</b>\n"
        f"Free bets available: <b>{safe_text(money(value))}</b>",
        parse_mode=ParseMode.HTML,
    )


async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual profile/group rank.
      /rank melon Gold Member
      /rank melon            (check)
      /rank clear melon
    """
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = list(context.args or [])
    if not args:
        await update.message.reply_text(
            "🏷 <b>Player Rank</b>\n\n"
            "<code>/rank melon Gold Member</code> = set rank\n"
            "<code>/rank melon</code> = check rank\n"
            "<code>/rank clear melon</code> = clear rank",
            parse_mode=ParseMode.HTML,
        )
        return

    if args[0].lower() in {"clear", "remove", "delete"}:
        if len(args) < 2:
            await update.message.reply_text("Usage: <code>/rank clear melon</code>", parse_mode=ParseMode.HTML)
            return
        ok, msg = set_user_rank(args[1], "")
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + safe_text(msg), parse_mode=ParseMode.HTML)
        return

    user_key = args[0]
    if len(args) == 1:
        key = canonicalize_bettor(user_key)
        rank = get_user_rank(key) or "Not set"
        await update.message.reply_text(
            f"🏷 <b>{safe_text(display_name_from_key(key))}</b>\nRank: <b>{safe_text(rank)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    rank_text = " ".join(args[1:]).strip()
    ok, msg = set_user_rank(user_key, rank_text)
    await update.message.reply_text(("✅ " if ok else "⚠️ ") + safe_text(msg), parse_mode=ParseMode.HTML)



async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /profile melon")
        return

    raw_lookup = " ".join(context.args).strip()
    users_data = load_users()
    bettor = resolve_user_key(raw_lookup, users_data)
    record = users_data.get("users", {}).get(bettor, {})
    tg = str(record.get("telegram", "") or "").strip()

    # Match real names, aliases, Telegram usernames, @usernames, capitals,
    # spaces and punctuation. Imported spreadsheet rows may use a different
    # identity form from the canonical users.json key.
    lookup_tokens = {
        alias_key(raw_lookup),
        alias_key(bettor),
        alias_key(record.get("display", "")),
        alias_key(record.get("telegram", "")),
    }
    for alias in record.get("aliases", []) or []:
        lookup_tokens.add(alias_key(alias))
    lookup_tokens.discard("")

    visibility_clause, visibility_params = ticket_visibility_sql()

    with db() as conn:
        candidate_rows = conn.execute(
            f"""
            SELECT * FROM bets
            WHERE 1 = 1
            {visibility_clause}
            """,
            visibility_params,
        ).fetchall()

    rows = []
    for row in candidate_rows:
        row_tokens = {
            alias_key(row_value(row, "bettor", "")),
            alias_key(row_value(row, "username", "")),
        }
        row_tokens.discard("")
        if lookup_tokens.intersection(row_tokens):
            rows.append(row)

    if not rows:
        requested_name = record.get("display") or raw_lookup or bettor
        await update.message.reply_text(
            f"📭 No profile data found for <b>{safe_text(requested_name)}</b> in <code>{safe_text(DB_FILE)}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Voided bets are excluded from profile stats because they return stake and
    # should not count toward wager volume, win rate, average stake, or totals.
    active_rows = active_bet_rows(rows)

    total_bets = len(active_rows) + int(round(get_manual_stat_offset(bettor, "total_bets")))
    open_bets = sum(1 for r in active_rows if str(r["status"] or "open").lower() == "open")
    won = sum(1 for r in active_rows if str(r["result"] or "").lower() in {"win", "won"}) + int(round(get_manual_stat_offset(bettor, "won")))
    lost = sum(1 for r in active_rows if str(r["result"] or "").lower() == "loss") + int(round(get_manual_stat_offset(bettor, "lost")))
    settled = won + lost + int(round(get_manual_stat_offset(bettor, "settled")))
    total_wager = sum(float(r["stake"] or 0) for r in active_rows) + get_manual_stat_offset(bettor, "wagered")
    real_lifetime_pnl = sum(float(r["pnl"] or 0) for r in active_rows)
    manual_pnl_offset = get_manual_pnl_offset(bettor)
    lifetime_pnl = real_lifetime_pnl + manual_pnl_offset
    avg_stake = total_wager / total_bets if total_bets else 0
    win_rate = (won / settled * 100) if settled > 0 else 0
    roi = (lifetime_pnl / total_wager * 100) if total_wager > 0 else 0

    image = create_profile_image(
        {
            "name": tg if tg else display_name_from_key(bettor),
            "telegram": tg,
            "total_bets": total_bets,
            "open_bets": open_bets,
            "settled": settled,
            "total_wager": total_wager,
            "lifetime_pnl": lifetime_pnl,
            "won": won,
            "lost": lost,
            "avg_stake": avg_stake,
            "win_rate": win_rate,
            "roi": roi,
            "bookie_balance": get_bookie_balance(bettor),
            "total_free_bet_value": total_free_bet_value(rows),
            "free_bets_available": get_free_bets_available(bettor),
            "rank": get_user_rank(bettor),
        }
    )

    try:
        with open(image, "rb") as f:
            await update.message.reply_photo(photo=f)
    finally:
        try:
            os.remove(image)
        except Exception:
            pass


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    visibility_clause, visibility_params = ticket_visibility_sql()

    with db() as conn:
        rows = conn.execute(f"SELECT * FROM bets WHERE 1=1 {visibility_clause}", visibility_params).fetchall()

    if not rows:
        await update.message.reply_text("📭 No Lenny Book stats yet.")
        return

    # Voided bets are excluded from book stats because they return stake and
    # should not count toward volume, totals, average ticket, or win rate.
    active_rows = active_bet_rows(rows)

    total = len(active_rows)
    open_count = sum(1 for r in active_rows if str(r["status"] or "open").lower() == "open")
    won = sum(1 for r in active_rows if str(r["result"] or "").lower() in {"win", "won"})
    lost = sum(1 for r in active_rows if str(r["result"] or "").lower() == "loss")
    settled = won + lost
    total_wager = sum(float(r["stake"] or 0) for r in active_rows) + total_manual_stat_offset("wagered", include_hidden=False)
    user_pnl = sum(float(r["pnl"] or 0) for r in active_rows) + total_manual_pnl_offset(include_hidden=False)
    book_pnl = -user_pnl
    open_liability = sum(float(r["profit"] or 0) for r in active_rows if str(r["status"] or "open").lower() == "open")
    avg_ticket = total_wager / total if total else 0
    win_rate = (won / settled * 100) if settled > 0 else 0
    roi = (book_pnl / total_wager * 100) if total_wager > 0 else 0

    users_data = load_users()
    total_ledger_balance = 0.0
    for record in users_data.get("users", {}).values():
        if record.get("hidden"):
            continue
        try:
            total_ledger_balance += float(record.get("bookie_balance") or 0)
        except Exception:
            pass

    image = create_stats_image(
        {
            "total": total,
            "open_count": open_count,
            "settled": settled,
            "total_wager": total_wager,
            "user_pnl": user_pnl,
            "book_pnl": book_pnl,
            "open_liability": open_liability,
            "won": won,
            "lost": lost,
            "avg_ticket": avg_ticket,
            "win_rate": win_rate,
            "roi": roi,
            "total_ledger_balance": total_ledger_balance,
        }
    )

    try:
        with open(image, "rb") as f:
            await update.message.reply_photo(photo=f)
    finally:
        try:
            os.remove(image)
        except Exception:
            pass


def monthly_image_date_label(start_str: str, end_str: str) -> str:
    """Display inclusive date range for the image while SQL keeps end exclusive."""
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S") - timedelta(days=1)
        return f"{start_dt.strftime('%b.')} {start_dt.day}, {start_dt.year} - {end_dt.strftime('%b.')} {end_dt.day}, {end_dt.year}"
    except Exception:
        return f"{start_str[:10]} - {end_str[:10]}"


async def monthly_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):

    month_arg = context.args[0] if context.args else ""

    try:
        start_str, end_str, label = month_bounds_from_arg(month_arg)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {safe_text(str(e))}", parse_mode=ParseMode.HTML)
        return

    users_data = load_users()

    requester_username = ""
    if update.effective_user and update.effective_user.username:
        requester_username = f"@{update.effective_user.username}".lower()

    requester_bettor = ""
    if requester_username:
        for key, record in users_data.get("users", {}).items():
            tg = str(record.get("telegram", "") or "").strip().lower()
            if tg == requester_username:
                requester_bettor = key
                break

    visibility_clause, visibility_params = ticket_visibility_sql()

    query = f"""
        SELECT
            bettor,
            COUNT(*) AS total_bets,
            SUM(COALESCE(stake, 0)) AS total_wager,
            SUM(CASE WHEN LOWER(COALESCE(status, 'open')) = 'open' THEN 1 ELSE 0 END) AS open_bets,
            SUM(CASE WHEN LOWER(COALESCE(result, '')) = 'win' THEN 1 ELSE 0 END) AS won,
            SUM(CASE WHEN LOWER(COALESCE(result, '')) = 'loss' THEN 1 ELSE 0 END) AS lost,
            SUM(COALESCE(pnl, 0)) AS user_pnl
        FROM bets
        WHERE COALESCE(created_at, '') >= ?
          AND COALESCE(created_at, '') < ?
          AND LOWER(COALESCE(result, '')) != 'void'
          AND COALESCE(is_test, 0) = 0
          {visibility_clause}
        GROUP BY bettor
        HAVING total_wager > 0
    """

    with db() as conn:
        # Pull all ranked rows first, so the "You" card can show the user's true current rank.
        all_rows_raw = conn.execute(
            query + """
            ORDER BY total_wager DESC, total_bets DESC, bettor ASC
            """,
            [start_str, end_str] + visibility_params,
        ).fetchall()

    # Merge manual monthly starting wager from users.json.
    # Example: /add arc w-$200 adds $200 to Arc's monthly wager rank without creating fake bets.
    row_map = {}

    for row in all_rows_raw:
        item = dict(row)
        bettor_key = str(item.get("bettor") or "").strip().lower()
        if bettor_key:
            row_map[bettor_key] = item

    # Add ORBET wager from re.db into the monthly leaderboard only.
    # FRBET is saved in re.db but is intentionally excluded here.
    for re_item in fetch_re_monthly_rows(start_str, end_str):
        bettor_key = str(re_item.get("bettor") or "").strip().lower()
        if not bettor_key:
            continue
        if bettor_key not in row_map:
            row_map[bettor_key] = {
                "bettor": bettor_key,
                "total_bets": 0,
                "total_wager": 0.0,
                "open_bets": 0,
                "won": 0,
                "lost": 0,
                "user_pnl": 0.0,
            }
        for field in ["total_bets", "open_bets", "won", "lost"]:
            row_map[bettor_key][field] = int(row_map[bettor_key].get(field) or 0) + int(re_item.get(field) or 0)
        for field in ["total_wager", "user_pnl"]:
            row_map[bettor_key][field] = float(row_map[bettor_key].get(field) or 0) + float(re_item.get(field) or 0)

    for user_key, record in users_data.get("users", {}).items():
        try:
            starting_wager = max(0.0, float(record.get("starting_wager") or 0))
        except Exception:
            starting_wager = 0.0

        if starting_wager <= 0:
            continue

        bettor_key = str(user_key or "").strip().lower()

        if not bettor_key:
            continue

        if bettor_key not in row_map:
            row_map[bettor_key] = {
                "bettor": bettor_key,
                "total_bets": 0,
                "total_wager": 0.0,
                "open_bets": 0,
                "won": 0,
                "lost": 0,
                "user_pnl": 0.0,
            }

        row_map[bettor_key]["total_wager"] = float(row_map[bettor_key].get("total_wager") or 0) + starting_wager
        row_map[bettor_key]["starting_wager"] = starting_wager

    sorted_rows = sorted(
        row_map.values(),
        key=lambda r: (
            -float(r.get("total_wager") or 0),
            -int(r.get("total_bets") or 0),
            str(r.get("bettor") or ""),
        ),
    )

    ranked_rows = []
    for i, item in enumerate(sorted_rows, start=1):
        item = dict(item)
        item["rank"] = i
        ranked_rows.append(item)

    rows = ranked_rows[:20]

    requester_row = None
    if requester_bettor:
        requester_row = next(
            (
                r for r in ranked_rows
                if str(r.get("bettor", "") or "").strip().lower() == requester_bettor
            ),
            None,
        )

    if not rows:
        await update.message.reply_text(
            f"📭 No non-void wager found for <b>{safe_text(label)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    date_range_label = monthly_image_date_label(start_str, end_str)

    image = create_monthly_leaderboard_image(
        leaderboard_rows=rows,
        users_data=users_data,
        title="Top Bettors",
        date_range_label=date_range_label,
        requester_row=requester_row,
    )

    try:
        with open(image, "rb") as f:
            await update.message.reply_photo(photo=f)
    finally:
        try:
            os.remove(image)
        except Exception:
            pass

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    path = "lenny_book_export.csv"

    visibility_clause, visibility_params = ticket_visibility_sql()

    with db() as conn:
        bets_data = conn.execute(
            f"SELECT * FROM bets WHERE 1=1 {visibility_clause} ORDER BY id ASC",
            visibility_params,
        ).fetchall()

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticket_id",
                "chat_id",
                "bettor",
                "stake",
                "total_odds",
                "payout",
                "profit",
                "status",
                "result",
                "pnl",
                "market_id",
                "source_photo_message_id",
                "slip_message_id",
                "summary_message_id",
                "is_test",
                "created_at",
                "settled_at",
            ]
        )

        for b in bets_data:
            writer.writerow(
                [
                    b["id"],
                    b["chat_id"],
                    b["bettor"],
                    b["stake"],
                    b["total_odds"],
                    b["payout"],
                    b["profit"],
                    b["status"],
                    b["result"],
                    b["pnl"],
                    b["market_id"],
                    b["source_photo_message_id"],
                    b["slip_message_id"],
                    b["summary_message_id"],
                    row_value(b, "is_test", 0),
                    b["created_at"],
                    b["settled_at"],
                ]
            )

    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename=path)

    try:
        os.remove(path)
    except Exception:
        pass


def textbet_money(x) -> str:
    """Compact stake display for /textbet: 15$ instead of $15.00."""
    try:
        value = float(x or 0)
    except Exception:
        value = 0.0
    if abs(value - int(value)) < 0.005:
        amount = str(int(value))
    else:
        amount = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{amount}{CURRENCY}"


def textbet_result_label(row) -> str:
    result = str(row_value(row, "result", "") or "").strip().lower()
    status = str(row_value(row, "status", "") or "").strip().lower()

    if result in ["win", "won"]:
        return "Win"
    if result in ["loss", "lost", "lose"]:
        return "Loss"
    if result in ["void", "push", "refund"]:
        return "Void"
    if status == "open" or not result:
        return "Open"
    return result.title()


def clean_textbet_piece(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def textbet_join_match_name(event: str) -> str:
    event = clean_textbet_piece(event)
    event = re.sub(r"\s+[-–—]\s+", " v ", event)
    event = re.sub(r"\s+vs\.?\s+", " v ", event, flags=re.I)
    return event


def textbet_is_placeholder(value: str) -> bool:
    value = clean_textbet_piece(value).lower()
    return value in {"", "lb", "lenny book", "selection", "unknown", "n/a", "na", "none"}


def textbet_single_leg_summary(leg) -> str:
    """Readable one-line summary for a single-leg bet.

    Examples:
    - selection='Over 1.5', market='cards', event='England - Mexico'
      -> 'Over 1.5 cards England v Mexico'
    - selection='France', market='Same Game Multi', event='France v Iraq'
      -> 'France v Iraq Same Game Multi'
    """
    event = textbet_join_match_name(row_value(leg, "event", ""))
    selection = clean_textbet_piece(row_value(leg, "selection", ""))
    market = clean_textbet_piece(row_value(leg, "market", ""))

    if textbet_is_placeholder(event) and textbet_is_placeholder(selection) and textbet_is_placeholder(market):
        return "LB"

    generic_markets = {"", "winner", "odds", "outright", "moneyline", "match winner"}
    market_text = "" if market.lower() in generic_markets else market

    # Same Game Multi is normally a match-level description, not a picked runner.
    if market_text and re.search(r"\bsame\s+game\s+multi\b|\bsgm\b", market_text, re.I):
        if not textbet_is_placeholder(event):
            return f"{event} Same Game Multi"
        return "Same Game Multi"

    pieces = []
    if not textbet_is_placeholder(selection):
        pieces.append(selection)
    if market_text and (not pieces or market_text.lower() not in " ".join(pieces).lower()):
        pieces.append(market_text)
    if not textbet_is_placeholder(event) and event.lower() not in " ".join(pieces).lower():
        pieces.append(event)

    return clean_textbet_piece(" ".join(pieces)) or "Selection"


def textbet_multi_leg_summary(legs, row=None) -> str:
    """Short human label for parlays: Zverev/Germany/Bublik Parlay."""
    names = []
    for leg in list(legs or []):
        selection = clean_textbet_piece(row_value(leg, "selection", ""))
        event = textbet_join_match_name(row_value(leg, "event", ""))
        market = clean_textbet_piece(row_value(leg, "market", ""))

        name = ""
        if not textbet_is_placeholder(selection):
            name = selection
        elif not textbet_is_placeholder(event):
            name = event
        elif not textbet_is_placeholder(market) and market.lower() not in {"winner", "odds"}:
            name = market

        name = clean_textbet_piece(name)
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)

    if names:
        # Keep it compact but recognizable.
        if len(names) > 4:
            names = names[:4]
        return "/".join(names) + " Parlay"

    note = clean_textbet_piece(row_value(row, "note", "") if row is not None else "")
    if re.search(r"stake\s*shield", note, re.I):
        return "Stake Shield Parlay"
    if re.search(r"parlay|multi", note, re.I):
        return "Parlay"
    return "Parlay"


def textbet_special_summary(row, legs) -> Optional[str]:
    """Detect special labels so they print exactly like the book messages."""
    note = clean_textbet_piece(row_value(row, "note", ""))
    conditions = clean_textbet_piece(row_value(row, "conditions", ""))
    combined = f"{note} {conditions}"

    if re.search(r"stake\s*shield", combined, re.I):
        return "Stake Shield Parlay"

    # Some recovered cards save SGM in market/note.
    for leg in list(legs or []):
        market = clean_textbet_piece(row_value(leg, "market", ""))
        event = textbet_join_match_name(row_value(leg, "event", ""))
        if re.search(r"\bsame\s+game\s+multi\b|\bsgm\b", market, re.I):
            return f"{event} Same Game Multi" if not textbet_is_placeholder(event) else "Same Game Multi"
    if re.search(r"\bsame\s+game\s+multi\b|\bsgm\b", note, re.I):
        return note

    return None


def textbet_leg_summary(legs, row=None) -> str:
    """
    Build a compact plain-text summary from saved bet_legs rows.
    Single: whole match/market name, e.g. "Over 1.5 cards England v Mexico".
    Multi: short names joined with /, e.g. "Zverev/Germany/Bublik Parlay".
    """
    legs = list(legs or [])
    special = textbet_special_summary(row, legs) if row is not None else None
    if special:
        return special
    if not legs:
        return clean_textbet_piece(row_value(row, "note", "") if row is not None else "") or "No leg details saved"
    if len(legs) == 1:
        one = textbet_single_leg_summary(legs[0])
        # If a one-leg placeholder has a parlay/special note, use that note instead.
        note = clean_textbet_piece(row_value(row, "note", "") if row is not None else "")
        if one.upper() == "LB" and re.search(r"parlay|multi|stake\s*shield", note, re.I):
            return note
        return one
    return textbet_multi_leg_summary(legs, row=row)



def textbet_bettor_display_name(bettor_key: str) -> str:
    """For /textbet, show alias when one exists; otherwise show real/main name.

    Example:
    - main user Professional Watermelon Eater with alias Swaxxie -> Swaxxie
    - user with no real alias -> the saved real/main name
    """
    raw = str(bettor_key or "").strip()
    key = normalize_user(raw)

    try:
        data = load_users()
        users = data.get("users", {}) or {}
        resolved = resolve_user_key(key, data)
        record = users.get(resolved, {}) or {}

        aliases = record.get("aliases", []) or []
        display_token = alias_key(record.get("display", ""))
        telegram_token = alias_key(record.get("telegram", ""))
        main_tokens = {alias_key(resolved), display_token, telegram_token, ""}

        # Prefer a real alias only if one exists. Do not output the canonical
        # ledger/display/telegram name as an alias.
        for item in aliases:
            token = alias_key(item)
            if token not in main_tokens:
                return display_name_from_key(token)

        # No alias found: fall back to real/main display name.
        display = clean_textbet_piece(record.get("display", ""))
        if display:
            return display
        if resolved:
            return display_name_from_key(resolved)
        if raw:
            return display_name_from_key(raw)
    except Exception:
        pass

    return display_name_from_key(raw or key or "unknown")

def parse_textbet_ticket_ids(args: List[str]) -> List[int]:
    raw = " ".join(str(a).strip() for a in args if str(a).strip())
    if not raw:
        return []

    ids = []
    seen = set()
    tokens = re.split(r"[\s,]+", raw)

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        m = re.fullmatch(r"#?(\d+)\s*-\s*#?(\d+)", token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            # Safety cap so Telegram does not get spammed by a huge accidental range.
            if end - start > 250:
                end = start + 250
            for ticket_id in range(start, end + 1):
                if ticket_id not in seen:
                    ids.append(ticket_id)
                    seen.add(ticket_id)
            continue

        m = re.fullmatch(r"#?(\d+)", token)
        if m:
            ticket_id = int(m.group(1))
            if ticket_id not in seen:
                ids.append(ticket_id)
                seen.add(ticket_id)

    return ids


async def textbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /textbet 55-58
    /textbet 55 56 57
    Sends compact plain-text betslip data from the database.
    """
    ticket_ids = parse_textbet_ticket_ids(context.args)
    if not ticket_ids:
        await update.message.reply_text(
            "Usage: /textbet 55-58\nExample: /textbet 95-96"
        )
        return

    hidden_ids = [ticket_id for ticket_id in ticket_ids if is_ticket_hidden_by_config(ticket_id)]
    visible_ticket_ids = [ticket_id for ticket_id in ticket_ids if ticket_id not in set(hidden_ids)]

    bet_rows = []
    with db() as conn:
        if visible_ticket_ids:
            placeholders = ",".join("?" for _ in visible_ticket_ids)
            bet_rows = conn.execute(
                f"SELECT * FROM bets WHERE id IN ({placeholders}) ORDER BY id ASC",
                visible_ticket_ids,
            ).fetchall()

        legs_by_bet = {}
        if bet_rows:
            found_ids = [int(row_value(r, "id", 0)) for r in bet_rows]
            leg_placeholders = ",".join("?" for _ in found_ids)
            leg_rows = conn.execute(
                f"""
                SELECT * FROM bet_legs
                WHERE bet_id IN ({leg_placeholders})
                ORDER BY bet_id ASC, id ASC
                """,
                found_ids,
            ).fetchall()
            for leg in leg_rows:
                legs_by_bet.setdefault(int(row_value(leg, "bet_id", 0)), []).append(leg)

    rows_by_id = {int(row_value(r, "id", 0)): r for r in bet_rows}
    missing = []
    lines = []

    for ticket_id in visible_ticket_ids:
        row = rows_by_id.get(ticket_id)
        if not row:
            missing.append(ticket_id)
            continue

        bettor_key = str(row_value(row, "bettor", "unknown") or "unknown")
        bettor = textbet_bettor_display_name(bettor_key)
        stake = textbet_money(row_value(row, "stake", 0))
        odds = float(row_value(row, "total_odds", 0) or 0)
        odds_text = f"{odds:.2f}".rstrip("0").rstrip(".")
        if "." not in odds_text:
            odds_text = f"{odds_text}.00"
        summary = textbet_leg_summary(legs_by_bet.get(ticket_id, []), row=row)
        result = textbet_result_label(row)
        bet_tag = clean_bet_tag(row_value(row, "bet_tag", ""))
        tag_text = f" {bet_tag}" if bet_tag else ""

        lines.append(f"(#{ticket_id}) {bettor} {stake}{tag_text} - {summary} {odds_text} — {result}")

    if not lines and not missing and not hidden_ids:
        await update.message.reply_text("No matching tickets found.")
        return

    # Telegram hard limit is 4096 chars. Keep a safety buffer and split only
    # between bets so one bet never gets cut in the middle.
    max_len = 3600
    chunks = []
    current = ""
    for line in lines:
        block = line.strip()
        if not block:
            continue
        candidate = block if not current else current + "\n\n" + block
        if current and len(candidate) > max_len:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    for chunk in chunks:
        await update.message.reply_text(chunk)
        await asyncio.sleep(0.08)

    if missing:
        await update.message.reply_text("Missing tickets: " + ", ".join(f"#{x}" for x in missing))

    if hidden_ids:
        enabled, first_visible = hidden_ticket_filter()
        await update.message.reply_text(
            "Hidden by /hidebefore: " + ", ".join(f"#{x}" for x in hidden_ids) + f"\nShowing #{first_visible} and later only."
        )



async def bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bets = all open/unsettled bets, split into Telegram-safe pages
    /bets 50 = latest 50 open bets
    /bets all = all open bets
    /bets melon = one user's open bets
    /bets melon all = all open bets for one user
    """
    current_chat_id = update.effective_chat.id

    args = [str(a).strip() for a in context.args if str(a).strip()]
    target_bettor = None
    target_label = None
    limit = None

    # Plain /bets = all open bets.
    # /bets username = one user's open bets.
    # /bets 50 = latest 50 open bets.
    # Replying with /bets will NOT filter, because that was causing only one bet to show.
    for a in args:
        low = a.lower()
        if low in ["all", "full"]:
            limit = None
            continue
        if low in ["latest", "last"]:
            continue
        if a.isdigit():
            limit = max(1, min(int(a), 500))
            continue
        if not target_bettor:
            target_bettor = canonicalize_bettor(a)

    if target_bettor:
        tg = get_user_telegram(target_bettor)
        target_label = tg if tg else display_name_from_key(target_bettor)

        if not user_exists(target_bettor):
            await update.message.reply_text(
                f"⚠️ User <b>{safe_text(display_name_from_key(target_bettor))}</b> is not added yet.",
                parse_mode=ParseMode.HTML,
            )
            return

    sql_limit = ""
    params = []
    visibility_clause, visibility_params = ticket_visibility_sql()

    with db() as conn:
        if target_bettor:
            where = f"""
                WHERE bettor = ?
                AND LOWER(COALESCE(status, 'open')) = 'open'
                AND COALESCE(is_test, 0) = 0
                {visibility_clause}
            """
            params = [target_bettor] + visibility_params
        else:
            # Open bets are global to the book. Older/imported tickets may have
            # chat_id = 0 or a legacy group-ID format, so filtering by the
            # current Telegram chat hides valid open tickets in the group.
            where = f"""
                WHERE LOWER(COALESCE(status, 'open')) = 'open'
                AND COALESCE(is_test, 0) = 0
                {visibility_clause}
            """
            params = list(visibility_params)

        if limit is not None:
            sql_limit = "LIMIT ?"
            params.append(limit)

        rows = conn.execute(
            f"""
            SELECT * FROM bets
            {where}
            ORDER BY id DESC
            {sql_limit}
            """,
            params,
        ).fetchall()

        # Fallback for old bets saved with another chat_id.
        # This prevents /bets from showing only one bet when many open bets exist in DB.
        if not target_bettor and len(rows) < 2:
            fallback_params = list(visibility_params)
            fallback_limit = ""

            if limit is not None:
                fallback_limit = "LIMIT ?"
                fallback_params.append(limit)

            fallback_rows = conn.execute(
                f"""
                SELECT * FROM bets
                WHERE LOWER(COALESCE(status, 'open')) = 'open'
                AND COALESCE(is_test, 0) = 0
                {visibility_clause}
                ORDER BY id DESC
                {fallback_limit}
                """,
                fallback_params,
            ).fetchall()

            if len(fallback_rows) > len(rows):
                rows = fallback_rows

    if not rows:
        if target_bettor:
            await update.message.reply_text(
                f"📭 No open bets for <b>{safe_text(target_label)}</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("📭 No open bets.")
        return

    total_stake = sum(float(row_value(r, "stake", 0) or 0) for r in rows)
    title_count = len(rows)

    if target_bettor:
        header = (
            f"🟡 <b>OPEN / UNSETTLED BETS</b> ({title_count})\n"
            f"👤 <b>{safe_text(target_label)}</b>\n"
            f"💰 Total stake: <b>{money(total_stake)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )
    else:
        header = (
            f"🟡 <b>OPEN / UNSETTLED BETS</b> ({title_count})\n"
            f"💰 Total stake: <b>{money(total_stake)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )

    blocks = []

    for r in rows:
        bettor = r["bettor"] or "unknown"
        tg = get_user_telegram(bettor)
        label = tg if tg else display_name_from_key(bettor)
        market_tag = f" | M#{r['market_id']}" if r["market_id"] else ""
        bet_summary = first_leg_summary(int(r["id"]))

        blocks.append(
            f"🎟 <b>#{r['id']}</b>{safe_text(market_tag)} | <code>{safe_text(label)}</code>\n"
            f"🧾 {safe_text(bet_summary)}\n"
            f"💰 <b>{money(r['stake'])}</b> | 📈 <b>{float(r['total_odds']):.2f}x</b> | 🏆 <b>{money(r['payout'])}</b>\n\n"
        )

    await reply_html_in_chunks(update.message, header, blocks)


async def settled_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /settled = recent settled win/loss bets
    /settled all = include voids too
    /settled melon = one user's settled win/loss bets
    /settled melon all = one user's settled bets including voids

    User lookup supports alias, canonical name, display name,
    Telegram username, and @username.
    """
    current_chat_id = update.effective_chat.id
    args = [str(a).strip() for a in context.args if str(a).strip()]

    include_voids = any(a.lower() in ["all", "void", "voids"] for a in args)
    limit = 25
    target_bettor = None
    target_label = None

    for a in args:
        if a.lower() in ["all", "void", "voids"]:
            continue
        if a.isdigit():
            limit = max(1, min(int(a), 50))
            continue
        if not target_bettor:
            target_bettor = canonicalize_bettor(a)

    params = []
    where = [
        "LOWER(COALESCE(status, 'open')) = 'settled'",
        "COALESCE(is_test, 0) = 0",
    ]

    visibility_clause, visibility_params = ticket_visibility_sql()
    if visibility_clause:
        where.append("id >= ?")
        params.extend(visibility_params)

    if not include_voids:
        where.append("LOWER(COALESCE(result, '')) != 'void'")

    if target_bettor:
        target_label = preferred_ledger_label(target_bettor)

        # Imported bets can contain names such as "Melon" while users.json
        # stores "melon", and older rows may only contain a Telegram username.
        # Fetch settled rows and resolve each one through users.json.
        sql = f"""
            SELECT * FROM bets
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(settled_at, created_at) DESC, id DESC
        """

        with db() as conn:
            all_rows = conn.execute(sql, params).fetchall()

        rows = [r for r in all_rows if bettor_matches_user(r, target_bettor)][:limit]

    else:
        group_where = list(where)
        group_params = list(params)
        group_where.append("chat_id = ?")
        group_params.append(current_chat_id)
        group_params.append(limit)

        sql = f"""
            SELECT * FROM bets
            WHERE {' AND '.join(group_where)}
            ORDER BY COALESCE(settled_at, created_at) DESC, id DESC
            LIMIT ?
        """

        with db() as conn:
            rows = conn.execute(sql, group_params).fetchall()

            if not rows:
                # Fallback for imported/old rows saved with chat_id 0
                # or a previous group ID.
                fallback_sql = f"""
                    SELECT * FROM bets
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(settled_at, created_at) DESC, id DESC
                    LIMIT ?
                """
                rows = conn.execute(fallback_sql, params + [limit]).fetchall()

    if not rows:
        label = f" for <b>{safe_text(target_label)}</b>" if target_bettor else ""
        await update.message.reply_text(
            f"📭 No settled bets found{label}.",
            parse_mode=ParseMode.HTML,
        )
        return

    title_bits = ["📜 <b>PAST SETTLED BETS</b>"]
    if target_bettor:
        title_bits.append(f"👤 <b>{safe_text(target_label)}</b>")
    if include_voids:
        title_bits.append("↩️ Including voids")

    msg = "\n".join(title_bits) + "\n━━━━━━━━━━━━━━━━━━\n\n"

    for r in rows:
        bettor = r["bettor"] or r["username"] or "unknown"
        label = preferred_ledger_label(bettor)

        result = str(r["result"] or "settled").lower()
        emoji = "✅" if result in ["win", "won"] else "❌" if result in ["loss", "lost"] else "↩️"

        pnl_value = float(r["pnl"] or 0)
        pnl_text = money(pnl_value) if pnl_value >= 0 else f"-{money(abs(pnl_value))}"
        event_text = first_leg_summary(int(r["id"]))
        market_tag = f" | M#{r['market_id']}" if r["market_id"] else ""

        msg += (
            f"🎟 <b>#{int(r['id']):05d}</b>{safe_text(market_tag)} | "
            f"<code>{safe_text(label)}</code>\n"
            f"{emoji} <b>{safe_text(result.upper())}</b> | "
            f"💰 {money(r['stake'])} @ <b>{float(r['total_odds']):.2f}x</b> | "
            f"P/L: <b>{safe_text(pnl_text)}</b>\n"
            f"🏟 <i>{safe_text(event_text[:140])}</i>\n"
            f"🕒 {safe_text(str(r['settled_at'] or r['created_at'] or ''))}\n\n"
        )

    await update.message.reply_text(msg[:3900], parse_mode=ParseMode.HTML)


async def recent_settled_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin shortcut for recently settled bets by individual user.
      /recent melon
      /recent melon 40
      /recent melon all
    """
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    has_user_arg = any(
        str(a).strip().lower() not in {"all", "void", "voids"}
        and not str(a).strip().isdigit()
        for a in (context.args or [])
    )

    if not has_user_arg:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/recent melon</code>\n"
            "<code>/recent melon 40</code>\n"
            "<code>/recent melon all</code> = include voids",
            parse_mode=ParseMode.HTML,
        )
        return

    await settled_bets(update, context)


async def hidebefore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Hide old tickets from user-facing commands without deleting them.

    /hidebefore                 -> show status
    /hidebefore 280             -> turn on, show #280 and later only
    /hidebefore on              -> turn on using saved threshold
    /hidebefore off             -> turn off
    /hidebefore 280 off         -> save threshold but turn off
    """
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args = [str(a).strip().lower() for a in context.args if str(a).strip()]
    cfg = load_ticket_config()

    if not args:
        await update.message.reply_text(
            "🙈 /hidebefore status: " + hidden_ticket_status_text() + "\n\n"
            "Use:\n"
            "/hidebefore 280  = hide tickets before #280\n"
            "/hidebefore on   = turn it on\n"
            "/hidebefore off  = turn it off"
        )
        return

    # Find a ticket number anywhere in the command.
    threshold = None
    for token in args:
        token = token.replace("#", "")
        if token.isdigit():
            threshold = int(token)
            break

    enabled = bool(cfg.get("hide_before_enabled", False))

    if any(a in {"off", "disable", "disabled", "false", "0"} for a in args):
        enabled = False
    elif any(a in {"on", "enable", "enabled", "true", "1"} for a in args) or threshold is not None:
        enabled = True

    if threshold is None:
        threshold = int(cfg.get("hide_before") or 0)

    if enabled and threshold <= 0:
        await update.message.reply_text("Usage: /hidebefore 280")
        return

    save_ticket_config({
        "hide_before_enabled": enabled,
        "hide_before": int(threshold or 0),
    })

    if enabled:
        await update.message.reply_text(
            f"✅ Old tickets hidden. Bot will show/count tickets #{int(threshold)} and later only.\n"
            f"Tickets before #{int(threshold)} stay in bets.db and can be shown again with /hidebefore off."
        )
    else:
        saved = int(threshold or 0)
        extra = f" Saved threshold is still #{saved}." if saved else ""
        await update.message.reply_text(f"✅ Old-ticket hiding turned OFF.{extra}")


async def setticketstart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        cfg = load_ticket_config()
        with db() as conn:
            next_id = allocate_next_real_ticket_id(conn)
        await update.message.reply_text(
            f"🎟 Current ticket start_after: #{int(cfg.get('start_after') or 0)}\n"
            f"Next new ticket will be: #{next_id}\n\n"
            f"Use: /setticketstart 250"
        )
        return

    try:
        start_after = int(str(context.args[0]).strip().replace('#', ''))
    except Exception:
        await update.message.reply_text("Usage: /setticketstart 250")
        return

    if start_after < 0:
        await update.message.reply_text("Ticket start number cannot be negative.")
        return

    save_ticket_config({"start_after": start_after})

    with db() as conn:
        next_id = allocate_next_real_ticket_id(conn)

    await update.message.reply_text(
        f"✅ Ticket start point set after #{start_after}.\n"
        f"Next new ticket will be #{next_id}."
    )


async def ticketconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    cfg = load_ticket_config()
    with db() as conn:
        next_id = allocate_next_real_ticket_id(conn)

    await update.message.reply_text(
        f"🎟 Ticket config\n"
        f"start_after: #{int(cfg.get('start_after') or 0)}\n"
        f"ignore_above: #{int(cfg.get('ignore_above') or 0)}\n"
        f"next_ticket: #{next_id}\n"
        f"hide_before: {safe_text(hidden_ticket_status_text())}"
    )


async def fixnumbers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This rebuilds ticket numbers from the database and resets the next ticket ID.\n\n"
            "Use: <code>/fixnumbers YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    result = repair_ticket_numbers_db()

    await update.message.reply_text(
        f"🔢 <b>Lenny repaired ticket numbers.</b>\n"
        f"Before max ticket: <b>#{result['before_max']}</b>\n"
        f"Tickets kept: <b>{result['remaining_bets']}</b>\n"
        f"Duplicates found in DB: <b>{result['duplicates_found']}</b>\n"
        f"Next new ticket will continue after <b>#{result['remaining_bets']}</b>.\n\n"
        f"Note: old Telegram photos/captions cannot be edited, so they may still show old numbers.",
        parse_mode=ParseMode.HTML,
    )


async def bet_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /bet 1")
        return

    try:
        bet_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /bet 1")
        return

    if is_ticket_hidden_by_config(bet_id):
        enabled, first_visible = hidden_ticket_filter()
        await update.message.reply_text(
            f"Ticket #{bet_id} is hidden by /hidebefore. Showing #{first_visible} and later only. Use /hidebefore off to show old tickets."
        )
        return

    bet, legs = get_bet_with_legs(bet_id)

    if not bet:
        await update.message.reply_text("Ticket not found.")
        return

    conditions = str(bet["conditions"] or "").splitlines() or DEFAULT_CONDITIONS

    image = create_betslip_image(
        ticket_id=bet_id,
        stake=float(bet["stake"]),
        total_odds=float(bet["total_odds"]),
        legs=[dict(x) for x in legs],
        conditions=conditions,
        created_at=str(bet["created_at"] or ""),
        bet_tag=str(bet["bet_tag"] or "") if "bet_tag" in bet.keys() else "",
    )

    sent_photo = None
    bettor = bet["bettor"] or "unknown"
    tg = get_user_telegram(bettor)
    label = tg if tg else display_name_from_key(bettor)

    try:
        with open(image, "rb") as f:
            sent_photo = await update.message.reply_photo(
                photo=f,
                caption=f"🎟 Ticket #{bet_id} regenerated for {safe_text(label)}.",
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass

    if sent_photo:
        update_ticket_message_ids(
            bet_id,
            slip_message_id=sent_photo.message_id,
            summary_message_id=None,
        )





async def unsettle_all_bets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    confirm = " ".join(context.args or []).strip().lower()
    if confirm not in {"confirm", "yes", "sure"}:
        await update.message.reply_text(
            "⚠️ This will make <b>ALL bets</b> open/unsettled again.\n\n"
            "Use: <code>/unsettleall confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        with db() as conn:
            settled_rows = conn.execute(
                "SELECT * FROM bets WHERE LOWER(COALESCE(status, '')) = 'settled'"
            ).fetchall()

        reverted_balance = 0
        for bet in settled_rows:
            try:
                reverse_bet_from_bookie_balance(bet)
                reverted_balance += 1
            except Exception:
                pass

        with db() as conn:
            cur = conn.execute(
                """
                UPDATE bets
                SET status = 'open',
                    result = NULL,
                    pnl = 0,
                    settled_by = NULL,
                    settled_at = NULL
                """
            )
            opened_bets = int(cur.rowcount or 0)

            try:
                cur2 = conn.execute("UPDATE bet_legs SET result = 'open'")
                opened_legs = int(cur2.rowcount or 0)
            except Exception:
                opened_legs = 0

        await update.message.reply_text(
            "✅ <b>All bets are now open/unsettled.</b>\n"
            f"Bets updated: <b>{opened_bets}</b>\n"
            f"Legs updated: <b>{opened_legs}</b>\n"
            f"Balances reversed for settled bets: <b>{reverted_balance}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ /unsettleall failed: <code>{safe_text(str(e)[:800])}</code>",
            parse_mode=ParseMode.HTML,
        )




# -------------------------
# DELETE BETSLIP / TICKET
# -------------------------

def delete_bet_ticket_from_db(ticket_id: int) -> Tuple[bool, str, Dict]:
    """
    Permanently remove one real ticket from bets.db.
    If the ticket was already settled, reverse its ledger effect first.
    """
    ticket_id = int(ticket_id)
    details = {"ticket_id": ticket_id, "ledger_reversed": False, "messages": []}

    with db() as conn:
        bet = conn.execute("SELECT * FROM bets WHERE id = ?", (ticket_id,)).fetchone()
        if not bet:
            return False, f"Ticket #{ticket_id} not found.", details

        details.update({
            "slip_message_id": row_value(bet, "slip_message_id", None),
            "summary_message_id": row_value(bet, "summary_message_id", None),
            "source_photo_message_id": row_value(bet, "source_photo_message_id", None),
            "accept_message_id": row_value(bet, "accept_message_id", None),
            "chat_id": row_value(bet, "chat_id", None),
            "bettor": row_value(bet, "bettor", ""),
            "status": row_value(bet, "status", ""),
            "result": row_value(bet, "result", ""),
            "pnl": row_value(bet, "pnl", 0),
        })

        # If this ticket had already affected the ledger, undo it before deleting.
        status = str(row_value(bet, "status", "") or "").lower()
        result = str(row_value(bet, "result", "") or "").lower()
        pnl = float(row_value(bet, "pnl", 0) or 0)
        if status == "settled" and result in {"win", "loss"} and abs(pnl) >= 0.005:
            reverse_bet_from_bookie_balance(bet)
            details["ledger_reversed"] = True

        conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (ticket_id,))
        conn.execute("DELETE FROM bets WHERE id = ?", (ticket_id,))

    return True, f"Ticket #{ticket_id} deleted from database.", details


async def delete_bet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reply to a betslip with /delete to remove the Telegram slip and delete its DB ticket.
    Also supports /delete 123.
    """
    if not update.message:
        return

    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    ticket_id = None
    replied = update.message.reply_to_message

    if context.args:
        try:
            ticket_id = int(str(context.args[0]).replace("#", "").strip())
        except Exception:
            ticket_id = None

    bet = None
    if ticket_id is None and replied:
        bet = get_bet_by_message(update.effective_chat.id, replied.message_id)

        if not bet and replied.reply_to_message:
            bet = get_bet_by_message(update.effective_chat.id, replied.reply_to_message.message_id)

        if not bet:
            extracted = extract_ticket_id_from_message(replied)
            if extracted:
                bet = get_bet_by_ticket_id(extracted)

        if bet:
            ticket_id = int(bet["id"])

    if ticket_id is None:
        await update.message.reply_text(
            "Usage: reply to a betslip with <code>/delete</code> or use <code>/delete 123</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, msg, details = delete_bet_ticket_from_db(ticket_id)
    if not ok:
        await update.message.reply_text(f"⚠️ {safe_text(msg)}", parse_mode=ParseMode.HTML)
        return

    # Delete the replied betslip first.
    if replied:
        await try_delete_message(replied)

    # Delete any stored bot messages for the ticket too, if Telegram allows it.
    chat_id = update.effective_chat.id
    for key in ("slip_message_id", "summary_message_id", "source_photo_message_id", "accept_message_id"):
        mid = details.get(key)
        try:
            if mid and int(mid) != int(getattr(replied, "message_id", 0) or 0):
                await context.bot.delete_message(chat_id=chat_id, message_id=int(mid))
        except Exception:
            pass

    note = "✅ Deleted betslip + removed ticket from DB."
    if details.get("ledger_reversed"):
        note += " Ledger effect was reversed."

    # Send a short confirmation, then remove the /delete command to keep chat clean.
    try:
        confirm = await update.message.reply_text(f"{note}\nTicket #{ticket_id} is gone.")
    except Exception:
        confirm = None

    await try_delete_message(update.message)


async def unsettle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except Exception:
            bet_id = None

    if bet_id is None and update.message.reply_to_message:
        bet = get_bet_by_message(update.effective_chat.id, update.message.reply_to_message.message_id)

        if not bet and update.message.reply_to_message.reply_to_message:
            bet = get_bet_by_message(
                update.effective_chat.id,
                update.message.reply_to_message.reply_to_message.message_id,
            )

        if not bet:
            ticket_id = extract_ticket_id_from_message(update.message.reply_to_message)
            if ticket_id:
                bet = get_bet_by_ticket_id(ticket_id)

        if bet:
            bet_id = int(bet["id"])

    if bet_id is None:
        await update.message.reply_text(
            "Usage: <code>/unsettle 123</code> or reply to a ticket with <code>/unsettle</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, msg = unsettle_bet(bet_id)

    if ok:
        await update.message.reply_text(f"↩️ <b>{safe_text(msg)}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ {safe_text(msg)}", parse_mode=ParseMode.HTML)


async def void_all_bets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This will VOID every real bet in the database, including open bets and already-settled win/loss bets.\n\n"
            "It also reverses old win/loss ledger effects first, then sets every bet to VOID.\n\n"
            "Use: <code>/voidallbets YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    result = void_all_bets_db(settled_by=update.effective_user.id if update.effective_user else 0)

    await update.message.reply_text(
        f"↩️ <b>All real bets voided.</b>\n"
        f"🎟 Total bets touched: <b>{result['total']}</b>\n"
        f"🟢 Were open: <b>{result['open_bets']}</b>\n"
        f"✅ Were already settled: <b>{result['settled_bets']}</b>\n"
        f"↩️ Already void before: <b>{result['already_void']}</b>\n"
        f"🔁 Changed to void now: <b>{result['changed_to_void']}</b>\n"
        f"📒 Ledger reversals: <b>{result['ledger_reversed_count']}</b> bets / net <b>{safe_text(money(result['ledger_reversed']))}</b>\n"
        f"📢 Markets closed as void: <b>{result['markets_voided']}</b>",
        parse_mode=ParseMode.HTML,
    )


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    question = update.message.text.replace("/ai", "", 1).strip() if update.message.text else ""

    status_msg = await update.message.reply_text("🍉 Lenny is sniffing the form...")

    try:
        source_text, error = await get_replied_ai_source_text(update, context)

        if error:
            await status_msg.edit_text(error, parse_mode=ParseMode.HTML)
            return

        answer = call_venice_sports_ai(question, source_text or "")
        await status_msg.edit_text(answer, parse_mode=ParseMode.HTML)
    except Exception as e:
        if AI_BETSLIP_DEBUG:
            print(f"/ai failed: {e}")
        await status_msg.edit_text(f"Lenny AI failed: {safe_text(str(e)[:300])}", parse_mode=ParseMode.HTML)


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    raw = update.message.text.replace("/market", "", 1).strip()

    parts = [p.strip() for p in raw.split(",") if p.strip()]

    if len(parts) < 5:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/market Title, Match 1; Match 2; Match 3, Description, Odds, Condition 1, Condition 2...</code>\n\n"
            "Example:\n"
            "<code>/market Mega Goals Combo, Portugal v Uzbekistan; England v Ghana; France v Brazil, Combined Over 8 Goals, 3.20, Void if exactly 8, 90 mins only, Admin decision final</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    title = parts[0]
    match_info = parts[1]
    description = parts[2]

    try:
        odds = parse_float(parts[3])
    except Exception:
        await update.message.reply_text("Invalid odds. Example: 2.09")
        return

    if odds <= 1:
        await update.message.reply_text("Odds must be above 1.00")
        return

    conditions = parts[4:]
    rules = "\n".join(f"• {condition}" for condition in conditions)

    stored_title = f"{title}|||{match_info}"

    market_id = create_market_db(
        chat_id=update.effective_chat.id,
        title=stored_title,
        description=description,
        odds=odds,
        rules=rules,
        created_by=update.effective_user.id if update.effective_user else 0,
    )

    market = get_market_by_id(market_id)

    image = create_market_image(
        market_id=market_id,
        title=stored_title,
        description=description,
        odds=odds,
        rules=rules,
        created_at=market["created_at"],
    )

    sent = None

    try:
        with open(image, "rb") as f:
            sent = await update.message.reply_photo(
                photo=f,
                caption=(
                    f"📢 <b>Special Market Open</b>\n"
                    f"🎟 Market: <b>#{market_id}</b>\n"
                    f"📈 Odds: <b>{odds:.2f}x</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass

    if sent:
        update_market_message_id(market_id, sent.message_id)


async def msettle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /msettle 1 win/loss/void")
        return

    try:
        market_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid market ID.")
        return

    result = normalize_settlement(context.args[1])

    if not result:
        await update.message.reply_text("Use: win, loss, or void")
        return

    ok, msg, market, totals = settle_market_bets(
        market_id=market_id,
        result=result,
        settled_by=update.effective_user.id if update.effective_user else 0,
    )

    if not ok:
        await update.message.reply_text(msg)
        return

    clean_title, _ = split_market_title(market["title"])

    image = create_market_settlement_image(
        market_id=market_id,
        title=clean_title,
        result=result,
        total_bets=totals["total_bets"],
        total_staked=totals["total_staked"],
        total_payout=totals["total_payout"],
        book_pnl=totals["book_pnl"],
        settled_at=market["settled_at"],
    )

    try:
        with open(image, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=(
                    f"✅ <b>Special Market Settled</b>\n"
                    f"🎟 Market: <b>#{market_id}</b>\n"
                    f"📌 Result: <b>{result.upper()}</b>\n"
                    f"👥 Bets Settled: <b>{totals['total_bets']}</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass




async def purge_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if len(context.args) < 2 or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This permanently deletes one user's full bet history and renumbers tickets.\n\n"
            "Use: <code>/purgeuser melon YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    bettor = canonicalize_bettor(context.args[0])

    if not bettor:
        await update.message.reply_text("Usage: /purgeuser melon YES")
        return

    result = purge_user_history_db(bettor)

    if result["deleted"] == 0:
        await update.message.reply_text(
            f"📭 No bet history found for <b>{safe_text(display_name_from_key(bettor))}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"🧹 <b>Lenny wiped the slate.</b>\n"
        f"👤 User: <b>{safe_text(display_name_from_key(bettor))}</b>\n"
        f"🗑 Deleted bets: <b>{result['deleted']}</b>\n"
        f"🔢 Ticket numbers resequenced: <b>1 → {result['remaining_bets']}</b>",
        parse_mode=ParseMode.HTML,
    )


async def purge_voids_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This permanently deletes all past VOID bets and renumbers tickets.\n\n"
            "Use: <code>/purgevoids YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    result = purge_void_bets_db()

    if result["deleted"] == 0:
        await update.message.reply_text("📭 No void bets found to delete.")
        return

    await update.message.reply_text(
        f"🧹 <b>Void history deleted.</b>\n"
        f"🗑 Deleted void bets: <b>{result['deleted']}</b>\n"
        f"🔢 Ticket numbers resequenced: <b>1 → {result['remaining_bets']}</b>",
        parse_mode=ParseMode.HTML,
    )


async def purge_test_ledger_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "⚠️ This cleans test data. It will remove old test bets accidentally saved in the real ledger, "
            "renumber real tickets after that cleanup, and clear the separate test ledger.\n\n"
            "Use: <code>/purgetestledger YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    old_result = purge_old_test_bets_from_real_ledger_db()
    separate_result = clear_separate_test_ledger_db()

    if old_result["deleted"] == 0 and separate_result["deleted"] == 0:
        await update.message.reply_text("📭 No test ledger data found to delete.")
        return

    remaining_text = (
        f"1 → {old_result['remaining_bets']}"
        if old_result.get("remaining_bets") is not None
        else "unchanged"
    )

    await update.message.reply_text(
        f"🧪 <b>Test ledger cleaned.</b>\n"
        f"🗑 Old real-ledger test bets removed: <b>{old_result['deleted']}</b>\n"
        f"🗑 Separate test bets removed: <b>{separate_result['deleted']}</b>\n"
        f"🔢 Real ticket numbers: <b>{remaining_text}</b>\n\n"
        f"New test bets will use <b>T#00001</b>, <b>T#00002</b>, etc. and will not touch real ticket numbers.",
        parse_mode=ParseMode.HTML,
    )


# -------------------------
# TEST BETS
# -------------------------


def parse_test_bet_args(args_text: str) -> Optional[Dict]:
    """
    Parse /testbet arguments.
    Supported:
    - /testbet melon $43 at 1.58x
    - /testbet melon 43 1.58
    - /testbet $43 at 1.58x melon
    """
    raw = str(args_text or "").strip()
    if not raw:
        return None

    amount = r"\$?\s*(\d+(?:\.\d+)?)\s*\$?"
    odds = r"(\d+(?:\.\d+)?)\s*x?"
    user = r"([a-zA-Z0-9_]+)"

    patterns = [
        rf"(?i)^\s*{user}\s+{amount}\s*(?:at|@)?\s*{odds}\s*$",
        rf"(?i)^\s*{user}\s+(?:stake\s*)?{amount}\s*(?:odds\s*)?{odds}\s*$",
        rf"(?i)^\s*{amount}\s*(?:at|@)\s*{odds}\s+{user}\s*$",
    ]

    for pattern in patterns:
        m = re.search(pattern, raw)
        if not m:
            continue

        groups = m.groups()
        if len(groups) == 3:
            if re.match(r"^[a-zA-Z0-9_]+$", groups[0]):
                bettor_raw, stake_raw, odds_raw = groups[0], groups[1], groups[2]
            else:
                stake_raw, odds_raw, bettor_raw = groups[0], groups[1], groups[2]

            bettor_key = canonicalize_bettor(bettor_raw)
            stake = parse_float(stake_raw)
            total_odds = parse_float(odds_raw)

            if bettor_key and stake > 0 and total_odds > 1:
                return {
                    "bettor": bettor_key,
                    "stake": stake,
                    "total_odds": total_odds,
                    "pages": 1,
                    "bet_tag": TEST_BET_TAG,
                }

    return None


async def test_bet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    args_text = " ".join(context.args or []).strip()
    accepted = parse_test_bet_args(args_text)

    if not accepted:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/testbet melon $43 at 1.58x</code>\n"
            "or reply to a bet image/text with the same command.",
            parse_mode=ParseMode.HTML,
        )
        return

    bettor = accepted["bettor"]

    if not user_exists(bettor):
        await update.message.reply_text(
            f"⚠️ User <b>{safe_text(display_name_from_key(bettor))}</b> is not added yet.\n"
            f"Use: <code>/add {safe_text(bettor)} @username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    reply = update.message.reply_to_message
    parsed = None
    source_message_id = update.message.message_id

    if reply:
        source_message_id = reply.message_id
        text_bits = []
        if getattr(reply, "text", None):
            text_bits.append(reply.text)
        if getattr(reply, "caption", None):
            text_bits.append(reply.caption)

        if text_bits:
            source_text = "\n".join(text_bits).strip()
        elif getattr(reply, "photo", None):
            status_msg = await update.message.reply_text("🧪 Reading test bet image...")
            try:
                source_text = clean_ocr_text(await ocr_acceptance_photos(reply, context, accepted.get("pages", 1)))
            finally:
                await try_delete_message(status_msg)
        else:
            source_text = ""

        if source_text:
            parsed = parse_bet_text_with_ai(source_text, accepted["stake"], accepted["total_odds"])
            if not parsed:
                parsed = parse_bet_text(source_text)

    if not parsed:
        parsed = {
            "stake": accepted["stake"],
            "total_odds": accepted["total_odds"],
            "legs": [
                {
                    "event": "Test Event",
                    "selection": "Test Selection",
                    "market": "Testing Only",
                    "odds": accepted["total_odds"],
                }
            ],
            "conditions": ["TEST BET - Not counted in the real ledger."] + DEFAULT_CONDITIONS,
            "note": "Test Bet",
        }
    else:
        parsed["stake"] = accepted["stake"]
        parsed["total_odds"] = accepted["total_odds"]
        parsed["conditions"] = ["TEST BET - Not counted in the real ledger."] + list(parsed.get("conditions", DEFAULT_CONDITIONS))
        parsed["note"] = "Test Bet"

    user = update.effective_user
    test_bet_id = create_test_bet(
        chat_id=update.effective_chat.id,
        admin_user_id=user.id if user else 0,
        admin_username=user.username or user.full_name if user else "unknown",
        bettor=bettor,
        stake=parsed["stake"],
        total_odds=parsed["total_odds"],
        legs=parsed["legs"],
        source_photo_message_id=source_message_id,
        accept_message_id=update.message.message_id,
        conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
        note=parsed.get("note"),
        bet_tag=TEST_BET_TAG,
    )

    bet, legs = get_test_bet_with_legs(test_bet_id)
    conditions = str(bet["conditions"] or "").splitlines() or DEFAULT_CONDITIONS

    image = create_betslip_image(
        ticket_id=test_bet_id,
        stake=float(bet["stake"]),
        total_odds=float(bet["total_odds"]),
        legs=[dict(x) for x in legs],
        conditions=conditions,
        created_at=str(bet["created_at"] or ""),
        bet_tag=TEST_BET_TAG,
    )

    tg = get_user_telegram(bettor)
    label = tg if tg else display_name_from_key(bettor)
    sent_photo = None

    try:
        with open(image, "rb") as f:
            sent_photo = await update.message.reply_photo(
                photo=f,
                caption=f"🧪 TEST bet created for {safe_text(label)}. Test Ticket T#{test_bet_id:05d}. Not counted in real ledger/stats/leaderboard.",
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass

    if sent_photo:
        update_test_ticket_message_ids(test_bet_id, slip_message_id=sent_photo.message_id, summary_message_id=None)


async def test_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM test_bets
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 No test bets found.")
        return

    msg = "🧪 <b>TEST BETS</b>\n━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        bettor = r["bettor"] or "unknown"
        tg = get_user_telegram(bettor)
        label = tg if tg else display_name_from_key(bettor)
        status = str(r["status"] or "open").upper()
        result = str(r["result"] or "").upper()
        result_part = f" | {safe_text(result)}" if result else ""
        msg += (
            f"🎟 <b>#{r['id']}</b> | <code>{safe_text(label)}</code>\n"
            f"💰 Stake: <b>{money(r['stake'])}</b> | 📈 Odds: <b>{float(r['total_odds']):.2f}x</b>\n"
            f"📌 Status: <b>{safe_text(status)}</b>{result_part}\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# -------------------------
# GROUP HANDLERS
# -------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    lowered_text = text.lower().strip()
    test_prefix_match = re.match(r"^(?:test|testing)\s+(.+)$", lowered_text)

    if test_prefix_match and update.message.reply_to_message:
        test_settle_text = test_prefix_match.group(1).strip()
        test_adjusted_odds = parse_adjusted_win_text(test_settle_text)
        if test_adjusted_odds is not None:
            await handle_settlement_reply(update, context, "win", adjusted_odds=test_adjusted_odds, require_test=True)
            return

        test_settlement = normalize_settlement(test_settle_text)
        if test_settlement:
            await handle_settlement_reply(update, context, test_settlement, require_test=True)
            return

    adjusted_win_odds = parse_adjusted_win_text(text)

    if adjusted_win_odds is not None and update.message.reply_to_message:
        await handle_settlement_reply(update, context, "win", adjusted_odds=adjusted_win_odds)
        return

    settlement = normalize_settlement(text)

    if settlement and update.message.reply_to_message:
        await handle_settlement_reply(update, context, settlement)
        return

    accepted = parse_acceptance_text(text)

    if accepted and update.message.reply_to_message:
        await handle_acceptance_reply(update, context, accepted)
        return


async def handle_acceptance_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    accepted: Dict,
):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    reply = update.message.reply_to_message

    if not reply:
        await update.message.reply_text(
            "Reply to the original bet image or offer text with:\n"
            "<code>$5 at 2.5x melon bet accepted</code>\n"
            "or for big image bets:\n"
            "<code>$100 at 2.7x melon bet accepted pages 2</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    replied_text_bits = []

    if getattr(reply, "text", None):
        replied_text_bits.append(reply.text)

    if getattr(reply, "caption", None):
        replied_text_bits.append(reply.caption)

    reply_has_text = bool("\n".join(replied_text_bits).strip())
    reply_has_photo = bool(getattr(reply, "photo", None))

    if not reply_has_photo and not reply_has_text:
        await update.message.reply_text(
            "Reply to the original bet image or offer text with:\n"
            "<code>$5 at 2.5x melon bet accepted</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    bettor = accepted["bettor"]

    if not user_exists(bettor):
        await update.message.reply_text(
            f"⚠️ User <b>{safe_text(display_name_from_key(bettor))}</b> is not added yet.\n"
            f"Use: <code>/add {safe_text(bettor)} @username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    market = get_market_by_message(update.effective_chat.id, reply.message_id)

    if market:
        user = update.effective_user

        market_conditions = [
            str(market["rules"] or "Special market rules apply."),
            "Market result is settled by admin.",
        ]

        market_title = str(market["title"] or "")
        market_match_info = ""

        if "|||" in market_title:
            market_title, market_match_info = market_title.split("|||", 1)
            market_title = market_title.strip()
            market_match_info = market_match_info.strip()

        legs = [
            {
                "event": market_match_info or market_title,
                "selection": str(market["description"]),
                "market": "Special Market",
                "odds": float(accepted["total_odds"]),
            }
        ]

        bet_id = create_bet(
            chat_id=update.effective_chat.id,
            admin_user_id=user.id if user else 0,
            admin_username=user.username or user.full_name if user else "unknown",
            bettor=bettor,
            stake=accepted["stake"],
            total_odds=accepted["total_odds"],
            legs=legs,
            source_photo_message_id=reply.message_id,
            accept_message_id=update.message.message_id,
            conditions_list=market_conditions,
            note=f"Special Market #{market['id']}",
            market_id=int(market["id"]),
            bet_tag=accepted.get("bet_tag", ""),
        )

        bet, saved_legs = get_bet_with_legs(bet_id)
        consume_free_bet_on_placement(
            bettor,
            float(row_value(bet, "stake", accepted["stake"])),
            row_value(bet, "bet_tag", accepted.get("bet_tag", "")),
        )

        image = create_betslip_image(
            ticket_id=bet_id,
            stake=float(bet["stake"]),
            total_odds=float(bet["total_odds"]),
            legs=[dict(x) for x in saved_legs],
            conditions=market_conditions,
            created_at=str(bet["created_at"] or ""),
            bet_tag=str(bet["bet_tag"] or "") if "bet_tag" in bet.keys() else "",
        )

        tg = get_user_telegram(bettor)

        if tg:
            caption = f"✅ {safe_text(tg)} special market bet has been accepted. Ticket #{bet_id}"
        else:
            caption = f"✅ {safe_text(display_name_from_key(bettor))}, special market bet has been accepted. Ticket #{bet_id}"

        sent_photo = None

        try:
            with open(image, "rb") as f:
                sent_photo = await update.message.reply_photo(
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
        finally:
            try:
                os.remove(image)
            except Exception:
                pass

        if sent_photo:
            update_ticket_message_ids(
                bet_id,
                slip_message_id=sent_photo.message_id,
                summary_message_id=None,
            )

        await try_delete_message(update.message)
        return

    status_msg = await update.message.reply_text("🧾 Creating betslip...")

    try:
        if reply_has_text:
            extracted = "\n".join(replied_text_bits).strip()
        else:
            pages = int(accepted.get("pages", 1))
            extracted = await ocr_acceptance_photos(reply, context, pages)
    except Exception as e:
        await status_msg.edit_text(f"Could not read bet source: {e}")
        return

    extracted = clean_ocr_text(extracted)

    parsed = parse_bet_text_with_ai(
        extracted,
        stake=accepted["stake"],
        total_odds=accepted["total_odds"],
    )

    # Fallback keeps the bot alive if Gemini/Venice is down or returns unusable JSON.
    if not parsed:
        parsed = parse_bet_text(extracted)

        if parsed:
            parsed["stake"] = accepted["stake"]
            parsed["total_odds"] = accepted["total_odds"]

    if not parsed:
        await status_msg.edit_text(
            "OCR worked, but AI/parser could not build a ticket.\n\n"
            f"OCR text:\n{extracted[:1500]}"
        )
        return

    user = update.effective_user

    accepted_bet_tag = accepted.get("bet_tag", "")
    use_re_db = is_re_bet_tag(accepted_bet_tag)

    if use_re_db:
        bet_id = create_re_bet(
            chat_id=update.effective_chat.id,
            admin_user_id=user.id if user else 0,
            admin_username=user.username or user.full_name if user else "unknown",
            bettor=bettor,
            stake=parsed["stake"],
            total_odds=parsed["total_odds"],
            legs=parsed["legs"],
            source_photo_message_id=reply.message_id,
            accept_message_id=update.message.message_id,
            conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
            note=parsed.get("note"),
            bet_tag=accepted_bet_tag,
        )
        bet, legs = get_re_bet_with_legs(bet_id)
    else:
        bet_id = create_bet(
            chat_id=update.effective_chat.id,
            admin_user_id=user.id if user else 0,
            admin_username=user.username or user.full_name if user else "unknown",
            bettor=bettor,
            stake=parsed["stake"],
            total_odds=parsed["total_odds"],
            legs=parsed["legs"],
            source_photo_message_id=reply.message_id,
            accept_message_id=update.message.message_id,
            conditions_list=parsed.get("conditions", DEFAULT_CONDITIONS),
            note=parsed.get("note"),
            bet_tag=accepted_bet_tag,
        )
        bet, legs = get_bet_with_legs(bet_id)
        consume_free_bet_on_placement(
            bettor,
            float(row_value(bet, "stake", parsed["stake"])),
            row_value(bet, "bet_tag", accepted_bet_tag),
        )
    conditions = str(bet["conditions"] or "").splitlines() or DEFAULT_CONDITIONS

    image = create_betslip_image(
        ticket_id=bet_id,
        stake=bet["stake"],
        total_odds=bet["total_odds"],
        legs=[dict(x) for x in legs],
        conditions=conditions,
        created_at=bet["created_at"],
        bet_tag=str(bet["bet_tag"] or "") if "bet_tag" in bet.keys() else "",
    )

    sent_photo = None
    tg = get_user_telegram(bettor)

    bet_tag_text = accepted.get("bet_tag") or ""
    bet_tag_prefix = f"{bet_tag_text} " if bet_tag_text else ""

    if tg:
        caption = (
            f"✅ {safe_text(tg)} "
            f"{safe_text(bet_tag_prefix)}bet has been accepted. Ticket #{bet_id}"
        )
    else:
        caption = (
            f"✅ {safe_text(display_name_from_key(bettor))}, "
            f"{safe_text(bet_tag_prefix)}bet has been accepted. Ticket #{bet_id}"
        )

    try:
        with open(image, "rb") as f:
            sent_photo = await update.message.reply_photo(
                photo=f,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass

    if sent_photo:
        if use_re_db:
            update_re_ticket_message_ids(
                bet_id,
                slip_message_id=sent_photo.message_id,
                summary_message_id=None,
            )
        else:
            update_ticket_message_ids(
                bet_id,
                slip_message_id=sent_photo.message_id,
                summary_message_id=None,
            )

    await try_delete_message(status_msg)
    await try_delete_message(update.message)


async def handle_settlement_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: str,
    adjusted_odds: Optional[float] = None,
    require_test: bool = False,
):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    replied = update.message.reply_to_message

    if not replied:
        return

    if require_test:
        bet = get_test_bet_by_message(update.effective_chat.id, replied.message_id)

        if not bet and replied.reply_to_message:
            bet = get_test_bet_by_message(update.effective_chat.id, replied.reply_to_message.message_id)

        if not bet:
            test_ticket_id = extract_test_ticket_id_from_message(replied)

            if not test_ticket_id and replied.reply_to_message:
                test_ticket_id = extract_test_ticket_id_from_message(replied.reply_to_message)

            if test_ticket_id:
                bet = get_test_bet_by_ticket_id(test_ticket_id)

        if not bet:
            await update.message.reply_text(
                "Could not find this test ticket. Reply directly to the generated test ticket image."
            )
            return

        ok, msg, pnl = settle_test_bet(
            test_bet_id=bet["id"],
            result=result,
            settled_by=update.effective_user.id if update.effective_user else 0,
            adjusted_odds=adjusted_odds,
        )

        if not ok:
            await update.message.reply_text(msg)
            return

        bet, saved_legs = get_test_bet_with_legs(int(bet["id"]))
        display_info = settlement_display_from_legs(saved_legs)

        bettor = bet["bettor"] or "unknown"
        tg = get_user_telegram(bettor)
        label = tg if tg else display_name_from_key(bettor)
        settled_at = str(bet["settled_at"] or now_str())

        image = create_settlement_image(
            ticket_id=bet["id"],
            bettor=f"TEST • {label}",
            result=result,
            stake=float(bet["stake"]),
            total_odds=float(bet["total_odds"]),
            payout=float(bet["payout"]),
            pnl=float(pnl),
            created_at=str(bet["created_at"] or ""),
            settled_at=settled_at,
            match_name=display_info["match_name"],
            bet_on=display_info["bet_on"],
            market_name=display_info["market_name"],
            sport=display_info["sport"],
            legs=saved_legs,
        )

        result_word = {
            "win": "won ✅",
            "loss": "lost ❌",
            "void": "voided ↩️",
        }.get(result, f"{result} ✅")

        if adjusted_odds is not None:
            caption = (
                f"🧪 TEST {safe_text(label)} bet settled: <b>{safe_text(result_word)}</b> "
                f"at adjusted odds <b>{float(adjusted_odds):.2f}x</b>"
            )
        else:
            caption = f"🧪 TEST {safe_text(label)} bet settled: <b>{safe_text(result_word)}</b>"

        try:
            with open(image, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
        finally:
            try:
                os.remove(image)
            except Exception:
                pass

        return

    bet = get_bet_by_message(update.effective_chat.id, replied.message_id)

    if not bet and replied.reply_to_message:
        bet = get_bet_by_message(update.effective_chat.id, replied.reply_to_message.message_id)

    if not bet:
        ticket_id = extract_ticket_id_from_message(replied)

        if not ticket_id and replied.reply_to_message:
            ticket_id = extract_ticket_id_from_message(replied.reply_to_message)

        if ticket_id:
            bet = get_bet_by_ticket_id(ticket_id)

    if not bet:
        await update.message.reply_text(
            "Could not find this ticket. Use /bets or reply directly to the generated ticket image."
        )
        return

    ok, msg, pnl = settle_bet(
        bet_id=bet["id"],
        result=result,
        settled_by=update.effective_user.id if update.effective_user else 0,
        adjusted_odds=adjusted_odds,
    )

    if not ok:
        await update.message.reply_text(msg)
        return

    bet, saved_legs = get_bet_with_legs(int(bet["id"]))
    display_info = settlement_display_from_legs(saved_legs)

    ticket_is_test = is_test_bet(bet)
    bettor = bet["bettor"] or "unknown"
    tg = get_user_telegram(bettor)
    label = tg if tg else display_name_from_key(bettor)
    settled_at = str(bet["settled_at"] or now_str())

    image = create_settlement_image(
        ticket_id=bet["id"],
        bettor=label,
        result=result,
        stake=float(bet["stake"]),
        total_odds=float(bet["total_odds"]),
        payout=float(bet["payout"]),
        pnl=float(pnl),
        created_at=str(bet["created_at"] or ""),
        settled_at=settled_at,
        match_name=display_info["match_name"],
        bet_on=display_info["bet_on"],
        market_name=display_info["market_name"],
        sport=display_info["sport"],
        legs=saved_legs,
    )

    result_word = {
        "win": "won ✅",
        "loss": "lost ❌",
        "void": "voided ↩️",
    }.get(result, f"{result} ✅")

    if adjusted_odds is not None:
        caption = (
            f"{'🧪 TEST ' if ticket_is_test else ''}{safe_text(label)} bet settled: <b>{safe_text(result_word)}</b> "
            f"at adjusted odds <b>{float(adjusted_odds):.2f}x</b>"
        )
    else:
        caption = f"{'🧪 TEST ' if ticket_is_test else ''}{safe_text(label)} bet settled: <b>{safe_text(result_word)}</b>"

    try:
        with open(image, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(image)
        except Exception:
            pass




# -------------------------
# DIRECT RANGE SETTLEMENT (/settle 270-277 open won loss space void)
# -------------------------


def parse_direct_settle_tokens(tokens):
    """
    Converts words after the range into actions.
    open  = set ticket back to open/unsettled
    won   = settle win
    loss  = settle loss
    void  = settle void
    space = if ticket exists, void it; if no ticket exists, skip it
    empty/skip = skip only
    won1.16 / win1.16 = win with adjusted odds
    """
    parsed = []

    for token in tokens:
        raw = str(token or "").strip().lower().replace("×", "x")
        if not raw:
            continue

        if raw in {"open", "unsettled", "unsettle", "pending"}:
            parsed.append(("open", None))
            continue

        if raw in {"space"}:
            parsed.append(("space", None))
            continue

        if raw in {"empty", "skip", "none", "-", "_", "na", "n/a"}:
            parsed.append(("skip", None))
            continue

        if raw in {"void", "push", "cancel", "cancelled", "canceled"}:
            parsed.append(("void", None))
            continue

        if raw in {"won", "win", "w", "winner"}:
            parsed.append(("win", None))
            continue

        if raw in {"loss", "lost", "lose", "l"}:
            parsed.append(("loss", None))
            continue

        m = re.fullmatch(r"(?:won|win|winner|w)\s*(\d+(?:\.\d+)?)x?", raw)
        if m:
            odds = parse_float(m.group(1))
            if odds > 1:
                parsed.append(("win", odds))
                continue

        return None, f"Unknown result word: {token}"

    return parsed, None


def get_bet_row_only(bet_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM bets WHERE id = ?", (int(bet_id),)).fetchone()


def force_open_bet(bet_id: int) -> tuple:
    bet = get_bet_row_only(int(bet_id))
    if not bet:
        return True, f"#{bet_id}: missing, skipped"

    if str(bet["status"] or "open").lower() == "settled":
        reverse_bet_from_bookie_balance(bet)

    with db() as conn:
        conn.execute(
            """
            UPDATE bets
            SET status = 'open', result = NULL, pnl = 0, settled_by = NULL, settled_at = NULL
            WHERE id = ?
            """,
            (int(bet_id),),
        )
        conn.execute("UPDATE bet_legs SET result = 'open' WHERE bet_id = ?", (int(bet_id),))

    return True, f"#{bet_id}: open"


def force_settle_bet(bet_id: int, result: str, settled_by: int, adjusted_odds=None) -> tuple:
    bet, legs = get_bet_with_legs(int(bet_id))
    if not bet:
        return False, f"#{bet_id}: missing, skipped", 0.0

    if str(bet["status"] or "open").lower() == "settled":
        reverse_bet_from_bookie_balance(bet)

    final_total_odds = float(bet["total_odds"] or 0)
    final_payout = float(bet["payout"] or 0)
    final_profit = float(bet["profit"] or 0)

    if result == "win" and adjusted_odds is not None:
        final_total_odds = round(float(adjusted_odds), 4)
        final_payout = round(float(bet["stake"] or 0) * final_total_odds, 2)
        final_profit = round(final_payout - float(bet["stake"] or 0), 2)
        pnl = final_profit
    elif result == "win":
        pnl = final_profit
    elif result == "loss":
        pnl = -float(bet["stake"] or 0)
    else:
        pnl = 0.0

    settled_time = now_str()

    with db() as conn:
        conn.execute(
            """
            UPDATE bets
            SET status = 'settled',
                result = ?,
                total_odds = ?,
                payout = ?,
                profit = ?,
                pnl = ?,
                settled_by = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (result, final_total_odds, final_payout, final_profit, pnl, settled_by, settled_time, int(bet_id)),
        )
        conn.execute("UPDATE bet_legs SET result = ? WHERE bet_id = ?", (result, int(bet_id)))

    fresh_bet = get_bet_row_only(int(bet_id)) or bet
    apply_bet_to_bookie_balance(fresh_bet, pnl)

    if result == "win" and adjusted_odds is not None:
        return True, f"#{bet_id}: WIN {float(adjusted_odds):.2f}x", pnl
    return True, f"#{bet_id}: {result.upper()}", pnl


async def settle_range_command_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await settle_range_command(update, context)
    except Exception as e:
        try:
            await update.effective_message.reply_text(f"❌ /settle crashed: {type(e).__name__}: {e}")
        except Exception:
            pass


async def settle_range_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("Admin only.")
        return

    msg = update.effective_message
    text = str(msg.text or msg.caption or "").strip()

    # Supports both command text and replying to a message containing the result line.
    args_text = re.sub(r"^\s*/\s*(?:settle|rangesettle|rsettle)(?:@\w+)?\s*", "", text, flags=re.I).strip()
    if not args_text and msg.reply_to_message:
        args_text = str(msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()

    m = re.match(r"^(\d+)\s*-\s*(\d+)\s+(.+)$", args_text, flags=re.I | re.S)
    if not m:
        await msg.reply_text(
            "Use like:\n"
            "/settle 270-277 open won loss space void\n\n"
            "open = unsettled\n"
            "won/win = won\n"
            "loss/lost = lost\n"
            "void = void\n"
            "space = void if ticket exists, skip if missing\n"
            "empty/skip = skip"
        )
        return

    start = int(m.group(1))
    end = int(m.group(2))
    results_raw = m.group(3).strip()

    step = 1 if end >= start else -1
    ticket_ids = list(range(start, end + step, step))
    result_tokens = re.split(r"\s+", results_raw)

    parsed, err = parse_direct_settle_tokens(result_tokens)
    if err:
        await msg.reply_text(f"❌ {err}")
        return

    if len(parsed) != len(ticket_ids):
        await msg.reply_text(
            f"❌ Count mismatch. Range {start}-{end} has {len(ticket_ids)} tickets, "
            f"but you typed {len(parsed)} result words."
        )
        return

    settled_by = update.effective_user.id if update.effective_user else 0
    lines = []
    changed = 0
    skipped = 0

    for bet_id, (action, adjusted_odds) in zip(ticket_ids, parsed):
        if action == "skip":
            lines.append(f"#{bet_id}: skipped")
            skipped += 1
            continue

        if action == "space":
            if not get_bet_row_only(bet_id):
                lines.append(f"#{bet_id}: no bet, skipped")
                skipped += 1
                continue
            ok, out, pnl = force_settle_bet(bet_id, "void", settled_by)
            lines.append(out)
            changed += 1 if ok else 0
            continue

        if action == "open":
            ok, out = force_open_bet(bet_id)
            lines.append(out)
            changed += 1 if ok and "missing" not in out else 0
            skipped += 1 if "missing" in out else 0
            continue

        ok, out, pnl = force_settle_bet(bet_id, action, settled_by, adjusted_odds)
        lines.append(out)
        changed += 1 if ok else 0
        skipped += 0 if ok else 1

    reply = f"✅ Range settle done. Changed: {changed}. Skipped: {skipped}.\n\n" + "\n".join(lines)
    if len(reply) > 3900:
        reply = reply[:3900] + "\n..."
    await msg.reply_text(reply)


async def settleresults_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same engine, just alias command name.
    await settle_range_command_safe(update, context)


# -------------------------
# RUN
# -------------------------




async def fixaliases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or str(context.args[-1]).upper() != "YES":
        await update.message.reply_text(
            "This merges old duplicate bettor names in <b>bets.db</b> using users.json aliases.\n\n"
            "Use: <code>/fixaliases YES</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    data = load_users()
    ensure_user_aliases(data)

    changed = 0
    details = []

    with db() as conn:
        rows = conn.execute("SELECT DISTINCT bettor FROM bets WHERE bettor IS NOT NULL AND bettor != ''").fetchall()
        for row in rows:
            old = str(row["bettor"] or "").strip()
            new = resolve_user_key(old, data)
            if new and old != new:
                cur = conn.execute("UPDATE bets SET bettor = ? WHERE bettor = ?", (new, old))
                count = cur.rowcount or 0
                if count:
                    changed += count
                    details.append(f"{old} → {new} ({count})")

    msg = f"✅ Fixed aliases in bets.db. Updated rows: <b>{changed}</b>"
    if details:
        msg += "\n\n" + "\n".join(f"• <code>{safe_text(x)}</code>" for x in details[:30])
        if len(details) > 30:
            msg += f"\n• ...and {len(details)-30} more"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing. Add it to .env or Startup Variables.")

    ensure_users_file()
    init_db()
    init_re_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("alias", alias_user))
    app.add_handler(CommandHandler("fixaliases", fixaliases_command))
    app.add_handler(CommandHandler("userlist", userlist))
    app.add_handler(CommandHandler("ledger", ledger))
    app.add_handler(CommandHandler("payin", payin_command))
    app.add_handler(CommandHandler("payout", payout_command))
    app.add_handler(CommandHandler("hideuser", hide_user_command))
    app.add_handler(CommandHandler("showuser", show_user_command))
    app.add_handler(CommandHandler(["deleteuser", "removeuser"], delete_user_command))
    app.add_handler(CommandHandler(["freebets", "freebet", "fbalance", "allfreebets", "freebetsall", "clearfreebets", "wipefreebets", "expirefreebets"], freebets_command))
    app.add_handler(CommandHandler(["rank", "setrank"], rank_command))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler(["setstats", "setstat", "setsummary", "setpnl"], setstats_command))
    app.add_handler(CommandHandler("monthly", monthly_leaderboard))
    app.add_handler(CommandHandler(["leaderboard", "lb"], monthly_leaderboard))
    app.add_handler(CommandHandler(["testbet", "test", "tb"], test_bet_command))
    app.add_handler(CommandHandler(["testbets", "testingbets"], test_bets))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler(["recoverbet", "recoverlb", "fixlb"], recoverbet_command))
    app.add_handler(CommandHandler(["recoverbets", "rebuildbets", "importbets"], recoverbets_command))
    app.add_handler(CommandHandler("recoverbetsbook", recoverbetsbook_command))
    app.add_handler(CommandHandler(["betsrecover", "recoveropenbets", "recoverunsettled"], betsrecover_command))
    app.add_handler(CommandHandler(["addbetsall", "importbetsall"], addbetsall_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler(["sendbes", "sendbets"], sendbes_command))
    app.add_handler(CommandHandler(["sendregen", "regen"], sendregen_command))
    app.add_handler(CommandHandler(["recoversettles", "settlesrecover", "recoverresults"], recoversettles_command))
    app.add_handler(CommandHandler(["settle", "rangesettle", "rsettle"], settle_range_command_safe))
    app.add_handler(CommandHandler(["settleresults", "settlebulk", "bulksettle", "settlerange"], settleresults_command))
    app.add_handler(CommandHandler(["delete", "deletebet", "removebet"], delete_bet_command))
    app.add_handler(CommandHandler(["textbet", "textbets"], textbet))
    app.add_handler(CommandHandler(["bets", "openbets", "unsettled"], bets))
    app.add_handler(CommandHandler(["settled", "history", "pastbets"], settled_bets))
    app.add_handler(CommandHandler("recent", recent_settled_command))
    app.add_handler(CommandHandler(["hidebefore", "hideold", "oldbets"], hidebefore_command))
    app.add_handler(CommandHandler(["setticketstart", "ticketstart"], setticketstart_command))
    app.add_handler(CommandHandler(["ticketconfig", "ticketcfg"], ticketconfig_command))
    app.add_handler(CommandHandler("fixnumbers", fixnumbers_command))
    app.add_handler(CommandHandler("bet", bet_info))
    app.add_handler(CommandHandler("unsettle", unsettle_command))
    app.add_handler(CommandHandler(["unsettleall", "openallbets", "forceunsettleall"], unsettle_all_bets_command))
    app.add_handler(CommandHandler(["voidallbets", "forcevoidall", "voidall"], void_all_bets_command))
    app.add_handler(CommandHandler(["purgeuser", "clearuser", "deleteuserhistory"], purge_user_command))
    app.add_handler(CommandHandler(["purgevoids", "clearvoids", "deletevoids"], purge_voids_command))
    app.add_handler(CommandHandler(["purgetestledger", "cleartestledger"], purge_test_ledger_command))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("market", market_command))
    app.add_handler(CommandHandler("msettle", msettle_command))

    app.add_handler(MessageHandler(filters.PHOTO, remember_photo))
    app.add_handler(MessageHandler(filters.Regex(r"^\s*/\s*(?:settle|rangesettle|rsettle)(?:@\w+)?(?:\s|$)") & filters.TEXT, settle_range_command_safe), group=-99)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Lenny Book group ledger bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
