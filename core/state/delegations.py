"""Delegated dialogs: chats the agent may write to on its own, for one goal, until a deadline.

The owner approves a delegation once (``tg_delegate_dialog`` asks through the
plugin's own pre_tool_call hook); after that ``tg_dialog_message`` writes to that
chat without asking again, and nowhere else. A cron job keeps the conversation
going: it wakes when the other side answers after ``last_seen_id``.

The file is per account (it lives under ``accounts/<name>/``) and is read from
disk on every call: it is small, it is shared by the gateway, the dashboard and
the CLI, and a stale copy here would mean writing to a chat whose delegation was
already closed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..sanitize import sanitize_name, sanitize_text
from .paths import StateLock, private_file, state_dir

__all__ = [
    "active_peer_ids",
    "close_delegation",
    "get_delegation",
    "is_active",
    "list_delegations",
    "record_read",
    "save_delegation",
    "update_delegation",
]

_FILENAME = "delegations.json"
_TEXT_LIMIT = 2000


def _path() -> Path:
    return state_dir() / _FILENAME


_LOCK = StateLock(lambda: _path())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_unlocked() -> list[dict[str, Any]]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("delegations") if isinstance(raw, dict) else None
    return [row for row in rows or [] if isinstance(row, dict) and row.get("id")]


def _save_unlocked(rows: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "delegations": rows}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)
    private_file(path)


def is_active(row: dict[str, Any], now: Optional[datetime] = None) -> bool:
    if row.get("status") != "active":
        return False
    try:
        return (now or _now()) < datetime.fromisoformat(str(row.get("expires_at")))
    except ValueError:
        return False


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["active"] = is_active(row)
    return out


def list_delegations(*, include_closed: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _load_unlocked()
    return [_public(r) for r in rows if include_closed or is_active(r)]


def get_delegation(delegation_id: str) -> Optional[dict[str, Any]]:
    wanted = str(delegation_id or "").strip()
    with _LOCK:
        for row in _load_unlocked():
            if row["id"] == wanted:
                return _public(row)
    return None


def active_peer_ids() -> set[str]:
    return {str(r["peer_id"]) for r in list_delegations()}


def save_delegation(*, peer_id: str, chat: str, username: Optional[str], goal: str,
                    limits: str = "", share: str = "", hours: int = 72,
                    last_seen_id: int = 0) -> dict[str, Any]:
    """Create the delegation for this chat, or replace the terms of its active one."""
    now = _now()
    terms = {
        "goal": sanitize_text(goal, limit=_TEXT_LIMIT),
        "limits": sanitize_text(limits or "", limit=_TEXT_LIMIT),
        "share": sanitize_text(share or "", limit=_TEXT_LIMIT),
        "expires_at": (now + timedelta(hours=int(hours))).isoformat(),
        "updated_at": now.isoformat(),
    }
    with _LOCK:
        rows = _load_unlocked()
        for row in rows:
            if str(row.get("peer_id")) == str(peer_id) and is_active(row, now):
                row.update(terms)
                _save_unlocked(rows)
                return _public(row)
        row = {
            "id": os.urandom(3).hex(),
            "peer_id": str(peer_id),
            "chat": sanitize_name(chat, limit=256),
            "username": sanitize_name(username, limit=128) if username else None,
            "status": "active",
            "outcome": None,
            "created_at": now.isoformat(),
            "last_seen_id": int(last_seen_id or 0),
            "read_up_to": int(last_seen_id or 0),
            **terms,
        }
        rows.append(row)
        _save_unlocked(rows)
        return _public(row)


def update_delegation(delegation_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    with _LOCK:
        rows = _load_unlocked()
        for row in rows:
            if row["id"] == str(delegation_id):
                row.update(fields)
                row["updated_at"] = _now().isoformat()
                _save_unlocked(rows)
                return _public(row)
    return None


def record_read(delegation_id: str, up_to: int, *, mark_seen: bool) -> Optional[dict[str, Any]]:
    """Remember how far the agent has read; with mark_seen, stop waking on those messages."""
    with _LOCK:
        rows = _load_unlocked()
        for row in rows:
            if row["id"] == str(delegation_id):
                row["read_up_to"] = max(int(row.get("read_up_to") or 0), int(up_to))
                if mark_seen:
                    row["last_seen_id"] = max(int(row.get("last_seen_id") or 0), row["read_up_to"])
                _save_unlocked(rows)
                return _public(row)
    return None


def close_delegation(delegation_id: str, outcome: str, status: str = "closed") -> Optional[dict[str, Any]]:
    return update_delegation(delegation_id, status=status,
                             outcome=sanitize_text(outcome or "", limit=_TEXT_LIMIT))
