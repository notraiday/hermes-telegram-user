from __future__ import annotations

import json
import os
import threading
import unicodedata
from pathlib import Path
from typing import Any, Optional

from ..sanitize import sanitize_name
from .paths import StateLock, file_stamp, private_file, state_dir

_LOCK = StateLock(lambda: _path())
_CACHE: Optional[dict[str, dict[str, Any]]] = None


def _path() -> Path:
    return state_dir() / "aliases.json"


def _key(alias: str) -> str:
    return unicodedata.normalize("NFKC", str(alias or "")).strip().casefold()


_CACHE_PATH: Optional[str] = None
_CACHE_STAMP: Optional[tuple[int, int, int]] = None


def _follow_account() -> None:
    """Drop the in-memory copy when the account changed or another process wrote the file."""
    global _CACHE, _CACHE_PATH, _CACHE_STAMP
    path = _path()
    current, stamp = str(path), file_stamp(path)
    if _CACHE_PATH != current or _CACHE_STAMP != stamp:
        _CACHE = None
        _CACHE_PATH, _CACHE_STAMP = current, stamp


def _load_unlocked() -> dict[str, dict[str, Any]]:
    _follow_account()
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _path()
    if not path.exists():
        _CACHE = {}
        return _CACHE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE = {}
        return _CACHE
    aliases = raw.get("aliases", {}) if isinstance(raw, dict) else {}
    _CACHE = aliases if isinstance(aliases, dict) else {}
    return _CACHE


def _save_unlocked(aliases: dict[str, dict[str, Any]]) -> None:
    _follow_account()
    global _CACHE, _CACHE_STAMP
    path = _path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps({"version": 1, "aliases": aliases}, ensure_ascii=False, indent=2)
    tmp.write_text(payload + "\n", encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)
    private_file(path)
    _CACHE, _CACHE_STAMP = aliases, file_stamp(path)


def get_alias(alias: str) -> Optional[dict[str, Any]]:
    key = _key(alias)
    if not key:
        return None
    with _LOCK:
        value = _load_unlocked().get(key)
        return dict(value) if isinstance(value, dict) else None


def list_aliases() -> list[dict[str, Any]]:
    with _LOCK:
        rows = [dict(value) for value in _load_unlocked().values() if isinstance(value, dict)]
    rows.sort(key=lambda row: str(row.get("alias") or "").casefold())
    return rows


def aliases_for_peer(peer_id: str | int) -> list[str]:
    wanted = str(peer_id)
    with _LOCK:
        values = list(_load_unlocked().values())
    out = [
        sanitize_name(value.get("alias"), limit=128)
        for value in values
        if isinstance(value, dict) and str(value.get("peer_id")) == wanted and value.get("alias")
    ]
    return sorted(set(filter(None, out)), key=str.casefold)


def set_alias(
    alias: str,
    *,
    peer_id: str | int,
    name: str,
    username: Optional[str] = None,
) -> dict[str, Any]:
    display = sanitize_name(alias, limit=128)
    key = _key(display)
    if not key:
        raise ValueError("alias is required")
    row = {
        "alias": display,
        "peer_id": str(peer_id),
        "name": sanitize_name(name, limit=256),
        "username": sanitize_name(username, limit=128) if username else None,
    }
    with _LOCK:
        aliases = dict(_load_unlocked())
        aliases[key] = row
        _save_unlocked(aliases)
    return dict(row)


def delete_alias(alias: str) -> bool:
    key = _key(alias)
    if not key:
        return False
    with _LOCK:
        aliases = dict(_load_unlocked())
        existed = key in aliases
        if existed:
            aliases.pop(key, None)
            _save_unlocked(aliases)
    return existed
