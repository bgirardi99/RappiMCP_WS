"""
Per-process RappiClient cache.

SQLite is the source of truth for tokens. This module holds the live
RappiClient instances so we don't re-create them on every message.
Token rotations are persisted back to SQLite via the on_token_refresh callback.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rappi_client import RappiClient
from bot.db import get_user, update_tokens

_clients: dict[str, RappiClient] = {}
_lock = threading.Lock()


def get_client(phone: str) -> RappiClient | None:
    """
    Return the live RappiClient for this phone number.
    Loads from DB if not yet cached. Returns None if user not registered.
    """
    with _lock:
        if phone in _clients:
            return _clients[phone]

        user = get_user(phone)
        if not user:
            return None

        client = RappiClient(
            access_token=user["access_token"],
            refresh_token=user["refresh_token"],
            device_id=user["device_id"],
            user_id=user["user_id"],
            on_token_refresh=lambda a, r: update_tokens(phone, a, r),
        )
        _clients[phone] = client
        return client


def invalidate_client(phone: str) -> None:
    """Force reload from DB on next get_client() call (e.g. after re-registration)."""
    with _lock:
        _clients.pop(phone, None)
