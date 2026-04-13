"""
SQLite persistence layer.

Tables:
  users               — Rappi credentials per phone number
  conversation_history — Message turns for Claude context
  session_state       — Active store per user (survives restarts)
"""

import json
import sqlite3
import threading
from pathlib import Path

_DB_PATH = Path("bot.db")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str = "bot.db") -> None:
    global _DB_PATH
    _DB_PATH = Path(path)
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                phone          TEXT PRIMARY KEY,
                user_id        INTEGER NOT NULL,
                device_id      TEXT NOT NULL,
                access_token   TEXT NOT NULL,
                refresh_token  TEXT NOT NULL,
                registered_at  TEXT DEFAULT (datetime('now')),
                last_seen_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                phone      TEXT NOT NULL REFERENCES users(phone),
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_history_phone_id
                ON conversation_history(phone, id);

            CREATE TABLE IF NOT EXISTS session_state (
                phone             TEXT PRIMARY KEY REFERENCES users(phone),
                active_store_id   INTEGER,
                active_store_type TEXT,
                active_store_name TEXT,
                updated_at        TEXT DEFAULT (datetime('now'))
            );
        """)


# ------------------------------------------------------------------ #
#  Users                                                              #
# ------------------------------------------------------------------ #

def upsert_user(
    phone: str,
    user_id: int,
    device_id: str,
    access_token: str,
    refresh_token: str,
) -> None:
    with _lock, _connect() as conn:
        conn.execute("""
            INSERT INTO users (phone, user_id, device_id, access_token, refresh_token)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                user_id       = excluded.user_id,
                device_id     = excluded.device_id,
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                last_seen_at  = datetime('now')
        """, (phone, user_id, device_id, access_token, refresh_token))


def get_user(phone: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def update_tokens(phone: str, access_token: str, refresh_token: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("""
            UPDATE users
            SET access_token = ?, refresh_token = ?, last_seen_at = datetime('now')
            WHERE phone = ?
        """, (access_token, refresh_token, phone))


# ------------------------------------------------------------------ #
#  Conversation history                                               #
# ------------------------------------------------------------------ #

def append_message(phone: str, role: str, content) -> None:
    """Store one message turn. content can be str or list — will be JSON-serialized."""
    serialized = json.dumps(content, ensure_ascii=False)
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO conversation_history (phone, role, content) VALUES (?, ?, ?)",
            (phone, role, serialized),
        )


def get_recent_history(phone: str, limit: int = 40) -> list[dict]:
    """Return the last `limit` message rows, oldest first."""
    with _lock, _connect() as conn:
        rows = conn.execute("""
            SELECT id, role, content FROM conversation_history
            WHERE phone = ?
            ORDER BY id DESC
            LIMIT ?
        """, (phone, limit)).fetchall()
    # Reverse so oldest is first (correct chronological order for Claude)
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def trim_history(phone: str, keep: int = 40) -> None:
    """Delete oldest rows beyond `keep` for this phone."""
    with _lock, _connect() as conn:
        conn.execute("""
            DELETE FROM conversation_history
            WHERE phone = ? AND id NOT IN (
                SELECT id FROM conversation_history
                WHERE phone = ?
                ORDER BY id DESC
                LIMIT ?
            )
        """, (phone, phone, keep))


def clear_history(phone: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM conversation_history WHERE phone = ?", (phone,))


# ------------------------------------------------------------------ #
#  Session state                                                      #
# ------------------------------------------------------------------ #

def get_session_state(phone: str) -> dict:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM session_state WHERE phone = ?", (phone,)
        ).fetchone()
        if row:
            return dict(row)
    return {"active_store_id": None, "active_store_type": None, "active_store_name": None}


def set_session_state(
    phone: str,
    store_id: int | None,
    store_type: str | None,
    store_name: str | None,
) -> None:
    with _lock, _connect() as conn:
        conn.execute("""
            INSERT INTO session_state (phone, active_store_id, active_store_type, active_store_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                active_store_id   = excluded.active_store_id,
                active_store_type = excluded.active_store_type,
                active_store_name = excluded.active_store_name,
                updated_at        = datetime('now')
        """, (phone, store_id, store_type, store_name))
