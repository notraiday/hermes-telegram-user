"""Named collections of chats: a reusable set, keyed by canonical scope.

Naming a group of chats once beats re-enumerating them on every request, and
the tempting shortcut -- storing titles or usernames -- is the bug that makes
such a list rot: a chat can be renamed, and a title is neither unique nor
stable. A collection therefore stores the canonical numeric peer id produced by
``telethon.utils.get_peer_id`` -- the same key space as ``aliases.json``'s
``peer_id``, ``digest_watermarks.json``'s keys and ``transcripts.sqlite3``'s
``chat_id`` -- and treats a peer's name as decoration for a human reader,
refreshed by whoever resolves the collection against the live dialog list
(``core.collections``).

A member may name one forum thread of a chat instead of the whole chat: its
scope is the pair ``(peer_id, thread)``, where ``thread`` is the root message id
of a topic and ``None`` means the entire chat. That pair -- not the peer id
alone -- is what merging, deduplication and exclusion compare, so the same chat
can be a member both as a whole and once per topic, and removing one topic never
touches its siblings.

Three things a naive store gets wrong, and this one does not:

* **a peer may not be both a member and excluded.** The two lists would then
  disagree about whether the chat belongs, and whichever branch a reader
  happened to check first would win. Exclusion wins here: on write, and again
  on every read, so the invariant holds even for a hand-edited file. A
  whole-chat exclusion removes every row of that peer, threads included; an
  exclusion naming one thread removes only that scope.
* **a row without a peer id, or with an unusable thread, is not a member.** It
  can never be resolved by id, so it is refused rather than stored, where it
  would masquerade as a chat that has disappeared from the account.
* **the template is a separate file, and is deliberately not cached.** It lives
  at ``<state dir>/templates/<slug>-<digest>.md`` rather than inside the JSON,
  because its owner has to be able to rewrite a markdown skeleton in an editor.
  Membership is cached in memory; the template is not, so every read goes to the
  file and an edit takes effect without restarting the process. A save that does
  not mention it leaves it untouched, so replacing the members of a studied
  collection cannot silently discard the template written for it; only an
  explicit string -- ``""`` included -- replaces it.

The name is a key, not an identifier: it is normalized with NFKC and casefolded
(so ``Work`` and ``ｗｏｒｋ`` are one collection), which also means renaming a
collection is a save under a new name, not an edit of the old one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..sanitize import sanitize_name, sanitize_text
from .paths import private_file, state_dir

__all__ = [
    "collections_path",
    "delete_collection",
    "get_collection",
    "list_collections",
    "save_collection",
    "set_collection_brief",
    "template_path",
]

_FILENAME = "collections.json"
_TEMPLATES_DIRNAME = "templates"
_VERSION = 1
#: Long enough for a human label, short enough that a pasted paragraph is not
#: accepted as one.
_MAX_NAME_LEN = 128
#: Telegram ids are int32 on the wire; anything longer is a caller bug, and a
#: row carrying one could never match a real dialog. A thread is a message id,
#: so it lives in the same key space and obeys the same bound.
_MAX_PEER_ID_LEN = 40
#: A brief is a full output template — headings, their order, what each section
#: holds, the wording of the title — not a one-line hint. It has to be roomy
#: enough for a real markdown skeleton, and still bounded so a pasted log is not
#: accepted as one.
_MAX_BRIEF_LEN = 20000

_LOCK = threading.RLock()
_CACHE: Optional[dict[str, dict[str, Any]]] = None


def collections_path() -> Path:
    """Where collections live, next to the rest of this plugin's state."""
    return state_dir() / _FILENAME


def _slug(key: str) -> str:
    """A readable, filesystem-safe stem for a collection key.

    ``\\w`` keeps letters and digits in any script, so a Cyrillic name stays
    readable on disk instead of collapsing to dashes; everything a path cannot
    carry becomes a dash.
    """
    cleaned = re.sub(r"[^\w.-]+", "-", key, flags=re.UNICODE).strip(".-")
    return cleaned[:_MAX_NAME_LEN] or "collection"


def template_path(name: str) -> Optional[Path]:
    """The markdown template file backing one collection, or ``None`` for a bad name.

    A short digest of the key is appended so two names that slugify identically
    -- ``a/b`` and ``a-b`` -- cannot share one file, which would silently give
    them the same template.
    """
    key = _key(name)
    if not key:
        return None
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return state_dir() / _TEMPLATES_DIRNAME / f"{_slug(key)}-{digest}.md"


def _read_template(name: str) -> str:
    """The stored template, or ``""`` -- a missing or unreadable file is not an error.

    Read on every access rather than cached: the whole point of a file is that
    its owner can edit it between runs.
    """
    path = template_path(name)
    if path is None or not path.exists():
        return ""
    try:
        return sanitize_text(path.read_text(encoding="utf-8"), limit=_MAX_BRIEF_LEN)
    except (OSError, UnicodeDecodeError):
        return ""


def _write_template(name: str, text: str) -> None:
    """Replace the template file; ``""`` removes it. Atomic, and private like the rest."""
    path = template_path(name)
    if path is None:
        raise ValueError("collection name is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if not text.strip():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)
    private_file(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(name: Any) -> str:
    """The storage key of a collection name: NFKC, trimmed, casefolded."""
    return unicodedata.normalize("NFKC", str(name or "")).strip().casefold()


def _peer_key(value: Any) -> Optional[str]:
    """The canonical peer id a row names, or ``None`` when it names none.

    Deliberately strict: only an integer or its decimal text is accepted, so a
    title, a username or a path pasted into the peer field is refused instead
    of becoming a key that can never match a dialog.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            text = str(int(token, 10))
        except ValueError:
            return None
    else:
        return None
    if len(text) > _MAX_PEER_ID_LEN:
        return None
    return text


def _thread_key(value: Any) -> Optional[str]:
    """The canonical thread id a present ``thread`` value names, or ``None``.

    A thread is a forum topic, named by the root message id of that topic, so it
    is validated exactly like a peer id -- decimal text or an int, never a bool,
    canonicalised so ``007`` and `` 7 `` collapse to ``7`` -- and it must be
    positive: message ids start at one, so a zero names no scope. Absence is
    decided by the caller, which is why ``None`` here means "unusable" and
    never "the whole chat".
    """
    if value is None:
        return None
    thread = _peer_key(value)
    if thread is None or int(thread) < 1:
        return None
    return thread


def _row(raw: Any) -> Optional[dict[str, Any]]:
    """One stored row, normalized, or ``None`` when it names no usable scope."""
    if not isinstance(raw, dict):
        return None
    peer = _peer_key(raw.get("peer_id"))
    if peer is None:
        return None
    thread = None
    if raw.get("thread") is not None:
        thread = _thread_key(raw.get("thread"))
        if thread is None:
            return None
    username = raw.get("username")
    return {
        "peer_id": peer,
        "name": sanitize_name(raw.get("name"), limit=256),
        "username": sanitize_name(username, limit=128) if username else None,
        "thread": thread,
    }


def _checked_rows(values: Any, *, field: str) -> list[dict[str, Any]]:
    """Every row a caller supplied, refusing anything unresolvable."""
    if values is None:
        return []
    try:
        items = list(values)
    except TypeError:
        raise ValueError(f"collection {field} must be a list of rows") from None
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f"collection {field}[{index}] must be a mapping with a peer_id")
        row = _row(raw)
        if row is None:
            raise ValueError(f"collection {field}[{index}] has no usable peer_id or thread")
        rows.append(row)
    return rows


def _stored_rows(value: Any) -> list[dict[str, Any]]:
    """Rows read off disk: unusable ones are dropped, never resolved."""
    if not isinstance(value, (list, tuple)):
        return []
    return _dedupe(row for row in (_row(item) for item in value) if row is not None)


def _dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows keyed by scope, the first row seen for a scope winning."""
    out: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
    for row in rows:
        out.setdefault((row["peer_id"], row["thread"]), row)
    return list(out.values())


def _disjoint(members: list[dict[str, Any]], exclude: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Members minus excluded scopes: exclusion wins.

    A whole-chat exclusion (``thread`` is ``None``) covers every row of that
    peer, threads included; an exclusion that names a thread covers only that
    ``(peer, thread)`` scope, leaving the chat's other rows alone.
    """
    across = {row["peer_id"] for row in exclude if row["thread"] is None}
    scoped = {
        (row["peer_id"], row["thread"]) for row in exclude if row["thread"] is not None
    }
    return [
        row
        for row in members
        if row["peer_id"] not in across and (row["peer_id"], row["thread"]) not in scoped
    ]


def _stored_row(key: Any, raw: Any) -> Optional[dict[str, Any]]:
    """One collection read off disk, repaired, or ``None`` when unusable."""
    storage_key = _key(key)
    if not storage_key or not isinstance(raw, dict):
        return None
    name = sanitize_name(raw.get("name"), limit=_MAX_NAME_LEN) or storage_key
    exclude = _stored_rows(raw.get("exclude"))
    members = _disjoint(_stored_rows(raw.get("members")), exclude)
    updated = raw.get("updated_at")
    # Deliberately no template here: the in-memory cache holds membership only,
    # and the markdown file is read on every access so an edit made in an editor
    # takes effect without restarting the process.
    return {
        "name": name,
        "members": members,
        "exclude": exclude,
        "updated_at": sanitize_name(updated, limit=64) if updated else None,
    }


def _public(key: str, row: dict[str, Any]) -> dict[str, Any]:
    """A copy of a collection, safe for a caller to mutate.

    The template is read from its file here rather than taken from the cached
    row, so the file stays the source of truth between runs.
    """
    return {
        "name": row["name"],
        "members": [dict(item) for item in row["members"]],
        "exclude": [dict(item) for item in row["exclude"]],
        "brief": _read_template(key),
        "updated_at": row["updated_at"],
    }


_CACHE_PATH: Optional[str] = None


def _follow_account() -> None:
    """Drop the in-memory copy when the active account (and so the file) changed."""
    global _CACHE, _CACHE_PATH
    current = str(collections_path())
    if _CACHE_PATH != current:
        _CACHE = None
        _CACHE_PATH = current


def _load_unlocked() -> dict[str, dict[str, Any]]:
    """Collections as currently known; a missing or corrupt file means none."""
    _follow_account()
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = collections_path()
    if not path.exists():
        _CACHE = {}
        return _CACHE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE = {}
        return _CACHE
    stored = raw.get("collections", {}) if isinstance(raw, dict) else {}
    collections: dict[str, dict[str, Any]] = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            row = _stored_row(key, value)
            if row is not None:
                collections[_key(key)] = row
    _CACHE = collections
    return _CACHE


def _save_unlocked(collections: dict[str, dict[str, Any]]) -> None:
    _follow_account()
    global _CACHE
    path = collections_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    ordered = {key: collections[key] for key in sorted(collections)}
    payload = json.dumps(
        {"version": _VERSION, "collections": ordered}, ensure_ascii=False, indent=2
    )
    tmp.write_text(payload + "\n", encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)
    private_file(path)
    _CACHE = collections


def list_collections() -> list[dict[str, Any]]:
    """Every collection as a summary row, ordered by name."""
    with _LOCK:
        rows = [
            {
                "name": row["name"],
                "member_count": len(row["members"]),
                "exclude_count": len(row["exclude"]),
                "has_brief": bool(_read_template(key)),
                "updated_at": row["updated_at"],
            }
            for key, row in _load_unlocked().items()
        ]
    rows.sort(key=lambda row: str(row.get("name") or "").casefold())
    return rows


def get_collection(name: str) -> Optional[dict[str, Any]]:
    """One collection with its rows, or ``None`` when it does not exist."""
    key = _key(name)
    if not key:
        return None
    with _LOCK:
        row = _load_unlocked().get(key)
        return _public(key, row) if row is not None else None


def save_collection(
    name: str,
    *,
    members: Any,
    exclude: Any = (),
    replace: bool = True,
    brief: Optional[str] = None,
) -> dict[str, Any]:
    """Store a collection and return it.

    ``replace=True`` overwrites both lists. ``replace=False`` merges: rows are
    unioned by scope -- ``(peer_id, thread)`` -- keeping the first row seen for a
    scope, so a call can add members without restating the ones already stored
    (and an existing name or username is not clobbered by a coarser one). A
    scope named in ``exclude`` is dropped from the members either way: a
    whole-chat exclusion takes every row of that peer, a threaded one only the
    topic it names.

    ``brief`` is the full output template the caller wrote for this collection:
    the skeleton every later summary follows, so two runs look the same. ``None``
    leaves whatever is stored untouched -- including across a ``replace=True``
    member overwrite -- an explicit string replaces it, and ``""`` clears it.
    """
    display = sanitize_name(name, limit=_MAX_NAME_LEN)
    key = _key(display)
    if not key:
        raise ValueError("collection name is required")
    if brief is not None and not isinstance(brief, str):
        raise ValueError("collection brief must be text")
    incoming_brief = None if brief is None else sanitize_text(brief, limit=_MAX_BRIEF_LEN)
    incoming_members = _checked_rows(members, field="members")
    incoming_exclude = _checked_rows(exclude, field="exclude")
    with _LOCK:
        store = dict(_load_unlocked())
        stored = store.get(key)
        previous = stored if not replace and isinstance(stored, dict) else None
        if isinstance(previous, dict):
            member_rows = _dedupe([*previous.get("members", ()), *incoming_members])
            exclude_rows = _dedupe([*previous.get("exclude", ()), *incoming_exclude])
        else:
            member_rows = _dedupe(incoming_members)
            exclude_rows = _dedupe(incoming_exclude)
        row = {
            "name": display,
            "members": _disjoint(member_rows, exclude_rows),
            "exclude": exclude_rows,
            "updated_at": _now(),
        }
        store[key] = row
        _save_unlocked(store)
        if incoming_brief is not None:
            _write_template(key, incoming_brief)
    return _public(key, row)


def set_collection_brief(name: str, brief: Any) -> dict[str, Any]:
    """Replace just the standing brief, leaving members and exclusions alone.

    A dedicated writer rather than a read-modify-write through
    :func:`save_collection`: tool calls can run concurrently, and rebuilding the
    whole record from a snapshot taken outside the lock would drop whatever a
    simultaneous ``save_collection`` had just stored.
    """
    if not isinstance(brief, str):
        raise ValueError("collection brief must be text")
    key = _key(name)
    if not key:
        raise ValueError("collection name is required")
    with _LOCK:
        store = dict(_load_unlocked())
        stored = store.get(key)
        if not isinstance(stored, dict):
            raise ValueError(f"collection not found: {sanitize_name(name, limit=_MAX_NAME_LEN)}")
        text = sanitize_text(brief, limit=_MAX_BRIEF_LEN)
        row = dict(stored)
        row["updated_at"] = _now()
        store[key] = row
        _save_unlocked(store)
        _write_template(key, text)
    return _public(key, row)


def delete_collection(name: str) -> bool:
    """Drop one collection; ``True`` if it existed."""
    key = _key(name)
    if not key:
        return False
    with _LOCK:
        store = dict(_load_unlocked())
        existed = key in store
        if existed:
            store.pop(key, None)
            _save_unlocked(store)
            path = template_path(key)
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    return existed
