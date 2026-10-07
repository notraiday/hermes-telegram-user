from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from .accounts import get_account, telethon_proxy_kwargs
from .limits import configured_flood_sleep_threshold, telegram_error_message, tool_gate
from .sanitize import sanitize_name

# Every tool call opens its own short-lived client on its own event loop: current
# Hermes may run async tool handlers on a fresh worker loop, and Telethon clients
# must stay on the loop they were connected on. Which account (session, proxy)
# the client belongs to comes from core.accounts' context variable.


def credentials(account: Optional[str] = None) -> tuple[int, str, str]:
    acc = get_account(account)
    return acc.api_id, acc.api_hash, acc.session


def _new_client(account: Optional[str] = None):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError("Telethon is not installed: pip install telethon") from exc

    acc = get_account(account)
    return TelegramClient(
        StringSession(acc.session),
        acc.api_id,
        acc.api_hash,
        flood_sleep_threshold=configured_flood_sleep_threshold(),
        **telethon_proxy_kwargs(acc.proxy),
    )


async def _connect_authorized(client):
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram StringSession is not authorized")
    return client


@asynccontextmanager
async def tool_client(account: Optional[str] = None) -> AsyncIterator[Any]:
    """Create one paced, loop-local Telethon client for a Hermes tool call.

    The gate bounds concurrent connections and honors any process-wide FloodWait
    backoff learned from previous Telegram RPCs. The client is always disconnected
    before Hermes tears down the worker event loop.
    """
    async with tool_gate():
        client = _new_client(account)
        try:
            await _connect_authorized(client)
            yield client
        except Exception as exc:
            message = telegram_error_message(exc)
            if message != str(exc):
                raise RuntimeError(message) from exc
            raise
        finally:
            with suppress(Exception):
                await client.disconnect()


def utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def entity_label(entity: Any) -> str:
    for attr in ("title", "username", "first_name"):
        value = getattr(entity, attr, None)
        if value:
            return sanitize_name(value, limit=256)
    return sanitize_name(getattr(entity, "id", "unknown"), limit=256)
