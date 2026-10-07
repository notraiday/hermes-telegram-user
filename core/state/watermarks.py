"""Digest watermarks: how far each chat has been digested, and where the gap is.

A digest that re-reads the whole chat every time is useless, and one that
trusts a single number is worse than useless: it silently drops messages.
So a mark here is *two* numbers, not one.

``contiguous``
    The highest message id such that everything at or below it has been
    digested. It is a floor, never a ceiling: a fetch that wants what is new
    asks for ids strictly above it, and never re-reads an id at or below it.

``pending_from_id`` / ``pending_top_id``
    The two ends of a *hole* — the range left over when a run stopped early
    (budget, a truncated page, a crash). ``pending_from_id`` is the lowest id
    that run reached and therefore where the next run carries on *from*;
    ``pending_top_id`` is the highest id ever seen while the hole has been
    open, and is the id that becomes ``contiguous`` once the hole closes. The
    mark does not move while a hole is open, because moving it across a gap
    makes every message inside the gap unreachable forever.

A mark belongs to a *scope*: a whole chat, or one topic of a forum. A single
number per chat cannot describe a forum — digesting one topic would mark every
sibling topic as digested too — so the scope is part of the key. A whole-chat
mark keeps the bare canonical peer id as its key (the shape every file written
before topics existed already has) and one topic is keyed ``<peer id>:<topic
id>``. Both halves of a composite key are canonical integers, so neither can
contain the separator: a peer id can never collide with a composite key, and a
composite can never be read as a peer.

The four failure modes of the design this replaces
(``other/tgai/tgai/storage.py`` + ``commands/aggregate.py``) are each closed
here explicitly:

* **keyed by peer id, not by display name.** A name is mutable and not unique;
  re-keying a mark on rename either loses it or, worse, hands one chat's mark
  to another. Keys are the canonical numeric id produced by
  ``telethon.utils.get_peer_id`` — the same key space as ``aliases.json``'s
  ``peer_id`` and ``transcripts.sqlite3``'s ``chat_id``.
* **the mark advances only on proof of contiguity.** ``advance`` can never move
  ``contiguous`` backwards, and a report that does not reach down to the
  current mark leaves the mark alone instead of jumping it. The one writer that
  is not a walk report is :func:`set_mark`, where the caller *asserts* how far
  the scope has been read: it moves the mark to the id it is given (or leaves
  it, being non-decreasing) and never past it to a stale ``pending_top_id``.
* **a truncated page records a hole instead of being skipped.** A run that
  stopped short leaves ``pending_from_id``/``pending_top_id`` behind, and
  :func:`resume_bounds` hands the next fetch the cursors that close it first.
* **atomic and locked.** The file is written tmp-file → ``0600`` → ``os.replace``,
  every mutation holds a module lock, and :func:`watermark_lock` serialises the
  read-fetch-write cycles of one scope.

None of this touches Telegram: a watermark is written locally and a read
pointer is never acknowledged, so a digest can be repeated at no cost.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..sanitize import sanitize_name
from .paths import private_file, state_dir

__all__ = [
    "advance",
    "forget_all",
    "forget_mark",
    "get_mark",
    "list_marks",
    "resume_bounds",
    "set_mark",
    "watermark_lock",
]

_FILENAME = "digest_watermarks.json"
_VERSION = 1
#: Splits a composite key into peer and topic. Both halves are canonical
#: integers, so neither can contain it and no peer id can ever collide with a
#: composite key.
_THREAD_SEP = ":"
#: Telegram ids are int32 on the wire; anything larger is a caller bug, and a
#: mark that is too large would answer "nothing is new" about everything.
_MAX_MESSAGE_ID = 2**31 - 1
#: Long enough for the longest canonical id, short enough that a path-looking
#: or pasted string is rejected instead of becoming a key.
_MAX_PEER_ID_LEN = 40

_LOCK = threading.RLock()
_SCOPE_LOCKS_LOCK = threading.Lock()
_SCOPE_LOCKS: dict[str, threading.Lock] = {}
_cache: Optional[dict[str, dict[str, Any]]] = None


def _path() -> Path:
    return state_dir() / _FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _peer_key(peer_id: str | int) -> str:
    """Canonical numeric peer id, or ``ValueError``.

    Sanitising first and parsing second is what stops a display name, a t.me
    link or an entity repr from ever becoming a key: it would parse as nothing
    and be refused rather than stored under a key no other module uses.
    """
    raw = sanitize_name(str(peer_id), limit=_MAX_PEER_ID_LEN).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("peer_id must be a numeric Telegram id") from None
    if value == 0:
        raise ValueError("peer_id is required")
    return str(value)


def _thread_key(thread_id: Any) -> Optional[str]:
    """Canonical topic id, ``None`` for a whole chat, or ``ValueError``.

    A topic id is the id of the topic's first message, so it is canonicalised
    and range-checked exactly like a peer id. ``None``, ``0`` and the empty
    string all mean "the whole chat": an absent topic and a zero topic are the
    same scope, and hiding that difference here keeps a caller from having to
    know which shape its own input happens to be in.
    """
    if thread_id is None:
        return None
    if isinstance(thread_id, bool):
        raise ValueError("thread_id must be a Telegram topic id")
    raw = sanitize_name(str(thread_id), limit=_MAX_PEER_ID_LEN).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("thread_id must be a numeric Telegram topic id") from None
    if value == 0:
        return None
    if value < 0 or value > _MAX_MESSAGE_ID:
        raise ValueError("thread_id is out of range")
    return str(value)


def _composite(peer_id: str, thread_id: Optional[str]) -> str:
    """The storage key of one scope."""
    return peer_id if thread_id is None else f"{peer_id}{_THREAD_SEP}{thread_id}"


def _mark_key(peer_id: str | int, thread_id: Any = None) -> tuple[str, Optional[str], str]:
    """``(peer_id, thread_id, storage key)`` for the scope a caller named."""
    peer = _peer_key(peer_id)
    thread = _thread_key(thread_id)
    return peer, thread, _composite(peer, thread)


def _stored_scope(key: Any) -> Optional[tuple[str, Optional[str]]]:
    """``(peer_id, thread_id)`` for a key read off disk, or ``None`` if unusable.

    Two shapes are accepted and nothing else: a bare canonical id is a
    whole-chat mark — every file written before topics existed is made of
    these — and ``peer:thread`` is one topic's mark. Both halves are reparsed
    and re-canonicalised here, so a hand-edited key cannot smuggle a
    non-numeric, negative or zero-scoped id into the store where the two key
    spaces would overlap.
    """
    if not isinstance(key, str):
        return None
    peer_raw, sep, thread_raw = key.partition(_THREAD_SEP)
    peer = _coerce_stored_id(peer_raw)
    if peer is None:
        return None
    if not sep:
        return str(peer), None
    thread = _coerce_stored_id(thread_raw)
    if thread is None or thread < 1:
        return None
    return str(peer), str(thread)


def _message_id(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a message id")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a message id") from None
    if parsed < 0 or parsed > _MAX_MESSAGE_ID:
        raise ValueError(f"{field} is out of range")
    return parsed


def _coerce_stored_id(value: Any) -> Optional[int]:
    """Best-effort read of an id that came off disk; ``None`` if unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or parsed > _MAX_MESSAGE_ID:
        return None
    return parsed


def _normalize_mark(raw: Any) -> Optional[dict[str, Any]]:
    """Repair one stored mark, or drop it.

    A row is only kept when it is internally consistent, and an inconsistent
    hole is dropped rather than guessed at: a dropped hole costs one re-read
    from the top, while a guessed one can jump the mark across messages that
    were never digested.
    """
    if not isinstance(raw, dict):
        return None
    contiguous = _coerce_stored_id(raw.get("contiguous"))
    if contiguous is None:
        return None
    pending_from = _coerce_stored_id(raw.get("pending_from_id"))
    pending_top = _coerce_stored_id(raw.get("pending_top_id"))
    # The hole is the open range (contiguous, pending_from): it exists only when
    # at least one id sits inside it, and only when both ends are known.
    if (
        pending_from is None
        or pending_top is None
        or pending_from < contiguous + 2
        or pending_top < pending_from
    ):
        pending_from = None
        pending_top = None
    updated_at = raw.get("updated_at")
    return {
        "contiguous": contiguous,
        "pending_from_id": pending_from,
        "pending_top_id": pending_top,
        "updated_at": sanitize_name(updated_at, limit=64) if isinstance(updated_at, str) else None,
    }


def _empty_mark() -> dict[str, Any]:
    return {"contiguous": 0, "pending_from_id": None, "pending_top_id": None, "updated_at": None}


_CACHE_PATH: Optional[str] = None


def _follow_account() -> None:
    """Drop the in-memory copy when the active account (and so the file) changed."""
    global _cache, _CACHE_PATH
    current = str(_path())
    if _CACHE_PATH != current:
        _cache = None
        _CACHE_PATH = current


def _load_unlocked() -> dict[str, dict[str, Any]]:
    """Marks as currently known; a missing or corrupt file means "no marks"."""
    _follow_account()
    global _cache
    if _cache is not None:
        return _cache
    loaded: dict[str, dict[str, Any]] = {}
    path = _path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and raw.get("version", _VERSION) == _VERSION:
        stored = raw.get("marks")
        if isinstance(stored, dict):
            for key, value in stored.items():
                scope = _stored_scope(key)
                mark = _normalize_mark(value)
                if scope is None or mark is None:
                    continue
                loaded[_composite(*scope)] = mark
    _cache = loaded
    return _cache


def _save_unlocked(marks: dict[str, dict[str, Any]]) -> None:
    _follow_account()
    global _cache
    path = _path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps({"version": _VERSION, "marks": marks}, ensure_ascii=False, indent=2)
    tmp.write_text(payload + "\n", encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)
    private_file(path)
    _cache = marks


def _row(peer_id: str, thread_id: Optional[str], mark: dict[str, Any]) -> dict[str, Any]:
    return {
        "peer_id": peer_id,
        "thread_id": thread_id,
        "contiguous": mark["contiguous"],
        "pending_from_id": mark["pending_from_id"],
        "pending_top_id": mark["pending_top_id"],
        "has_hole": mark["pending_from_id"] is not None,
        "updated_at": mark["updated_at"],
    }


def _scope_lock(key: str) -> threading.Lock:
    with _SCOPE_LOCKS_LOCK:
        lock = _SCOPE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SCOPE_LOCKS[key] = lock
        return lock


@asynccontextmanager
async def watermark_lock(peer_id: str | int, thread_id: Any = None) -> AsyncIterator[None]:
    """Serialise one scope's read-fetch-advance cycle.

    Prevents two concurrent digests of the same scope from both reading the same
    ``min_id``, both fetching the same window and both advancing the mark: the
    loser would report messages the winner already digested, and a hole it
    opens could be closed by the wrong run. The lock is per scope, so digests of
    different chats — and of different topics of one forum — still run
    concurrently; only writers of the same mark are held apart.
    """
    _, _, key = _mark_key(peer_id, thread_id)
    lock = _scope_lock(key)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()


def get_mark(peer_id: str | int, thread_id: Any = None) -> Optional[dict[str, Any]]:
    """One scope's mark, or ``None`` when that scope has never been digested.

    Prevents a caller from inventing a default mark (typically "everything so
    far") for a scope that has no mark at all: the distinction between "nothing
    digested yet" and "digested up to id N" is the whole reason the file exists,
    so its absence is reported rather than papered over. The row names both
    halves of the scope, so a caller that walks several topics at once can tell
    the rows apart without remembering what it asked for.
    """
    peer, thread, key = _mark_key(peer_id, thread_id)
    with _LOCK:
        mark = _load_unlocked().get(key)
        return _row(peer, thread, mark) if mark is not None else None


def list_marks() -> list[dict[str, Any]]:
    """Every stored mark, ordered by peer id and then by scope.

    Prevents a caller from reporting on "all chats" by walking an unordered
    dict: the order is fixed here so two runs with the same state produce the
    same list, which is what makes a diff of two digests readable. A chat's
    whole-chat mark sorts before its topics (the floor sorts first), so the
    list reads as one chat's states in the order they were created.
    """
    with _LOCK:
        rows = []
        for key, mark in _load_unlocked().items():
            scope = _stored_scope(key)
            if scope is not None:
                rows.append(_row(scope[0], scope[1], mark))
    rows.sort(key=lambda row: (int(row["peer_id"]), -1 if row["thread_id"] is None else int(row["thread_id"])))
    return rows


def advance(
    peer_id: str | int,
    *,
    contiguous: int,
    top: int | None = None,
    thread_id: Any = None,
) -> dict[str, Any]:
    """Record the outcome of one walk over a scope and return the resulting mark.

    ``contiguous`` is the lowest id the walk reached; ``top`` is the highest id
    it saw. With ``top=None`` the walk reached the floor it aimed at and
    ``contiguous`` is the highest id it covered — that is the only way the mark
    moves forward. With ``top`` given the walk stopped short, so the range
    between the mark and ``contiguous`` is a hole: the mark stays put, and both
    ends are recorded for :func:`resume_bounds` (an open hole only ever grows;
    it is cleared by a walk that reaches the mark, or by ``top=None``).

    ``thread_id`` names the topic the walk covered; with it omitted (or ``None``/
    ``0``/``""``) the walk covered the whole chat. A topic's walk touches only
    that topic's mark, and a whole-chat walk only the chat-wide one: the two are
    separate records even though one chat contains the other.

    Prevents the two failures of a single-number mark: moving it backwards
    (a shorter walk being mistaken for an older state) and jumping it across a
    limit-truncated page, which would leave the messages under the jump
    undigested and unreachable. A no-op report writes nothing, so ``updated_at``
    still means "when the mark last changed".
    """
    peer, thread, key = _mark_key(peer_id, thread_id)
    low = _message_id(contiguous, field="contiguous")
    high = None if top is None else _message_id(top, field="top")
    if high is not None and high < low:
        raise ValueError("top must not be below contiguous")
    with _LOCK:
        marks = dict(_load_unlocked())
        mark = dict(marks.get(key) or _empty_mark())
        before = (mark["contiguous"], mark["pending_from_id"], mark["pending_top_id"])
        pending_from = mark["pending_from_id"]
        pending_top = mark["pending_top_id"]
        # Telegram's cursors are exclusive, so a walk that stopped at id
        # `mark + 1` has nothing left between it and the mark: the hole is empty
        # and the walk counts as having reached the floor.
        if high is not None and low >= mark["contiguous"] + 2:
            mark["pending_from_id"] = low if pending_from is None else min(pending_from, low)
            mark["pending_top_id"] = high if pending_top is None else max(pending_top, high)
        else:
            mark["contiguous"] = max(mark["contiguous"], pending_top or 0, high or 0, low)
            mark["pending_from_id"] = None
            mark["pending_top_id"] = None
        after = (mark["contiguous"], mark["pending_from_id"], mark["pending_top_id"])
        if after != before:
            mark["updated_at"] = _now()
            marks[key] = mark
            _save_unlocked(marks)
        return _row(peer, thread, mark)


def set_mark(
    peer_id: str | int,
    *,
    contiguous: int,
    thread_id: Any = None,
) -> dict[str, Any]:
    """Assert how far a scope has been read, and return the resulting mark.

    This is the explicit counterpart to :func:`advance`. A walk report says
    "this is as far as the fetch got", so a completed walk is allowed to jump
    the mark up to a ``pending_top_id`` seen earlier and clear the hole with it.
    An assertion says "everything up to N is done", where that jump would be
    wrong: ``pending_top_id`` is the highest id ever *seen* while a hole was
    open, so lifting the mark to it would claim a middle region the caller
    never vouched for. Here the mark becomes the id the caller names — never
    that id plus anything — and the hole is cleared because the assertion is
    exactly the proof of contiguity the hole was waiting for.

    The mark is non-decreasing, so a lower assertion is a no-op rather than a
    rollback, and a caller cannot lower a mark by asserting a stale value. A
    no-op writes nothing, so ``updated_at`` still means "when the mark last
    changed".

    ``contiguous`` must be a positive message id: unlike a walk report, which
    can legitimately cover nothing (``contiguous=0``), an assertion about an
    empty range asserts nothing and is refused rather than stored as a mark of
    zero that would make every later read look new. A fractional id is refused
    for the same reason — an assertion is a claim about an id, so ``1.5`` is a
    caller bug rather than a silent claim about id ``1``.
    """
    peer, thread, key = _mark_key(peer_id, thread_id)
    value = _message_id(contiguous, field="contiguous")
    if isinstance(contiguous, float) and not contiguous.is_integer():
        raise ValueError("contiguous must be a whole message id")
    if value < 1:
        raise ValueError("contiguous must be a positive message id")
    with _LOCK:
        marks = dict(_load_unlocked())
        mark = dict(marks.get(key) or _empty_mark())
        before = (mark["contiguous"], mark["pending_from_id"], mark["pending_top_id"])
        mark["contiguous"] = max(mark["contiguous"], value)
        mark["pending_from_id"] = None
        mark["pending_top_id"] = None
        after = (mark["contiguous"], mark["pending_from_id"], mark["pending_top_id"])
        if after != before:
            mark["updated_at"] = _now()
            marks[key] = mark
            _save_unlocked(marks)
        return _row(peer, thread, mark)


def resume_bounds(peer_id: str | int, thread_id: Any = None) -> dict[str, Any]:
    """The cursors a fetch should use, closing a pending hole before anything new.

    ``min_id`` is the mark (the exclusive floor: digested ids are never
    re-read), ``max_id`` is where an interrupted run stopped, or ``None`` for a
    walk that should start from the newest message. ``has_hole`` says which of
    the two cases the caller is in. Closing the hole first is what prevents the
    hole from being refilled by newer traffic forever: every run that starts at
    the top without it re-reads the same newest page and the old messages are
    never reached.

    The bounds belong to one scope — a chat, or one topic of a forum — so a
    topic's hole never sends a chat-wide fetch back to an old page, and a
    chat-wide hole never makes a topic re-read its own history.
    """
    peer, thread, key = _mark_key(peer_id, thread_id)
    with _LOCK:
        mark = _load_unlocked().get(key)
        mark = dict(mark) if mark is not None else _empty_mark()
    pending_from = mark["pending_from_id"]
    return {
        "peer_id": peer,
        "thread_id": thread,
        "min_id": mark["contiguous"],
        "max_id": pending_from,
        "has_hole": pending_from is not None,
        "contiguous": mark["contiguous"],
        "pending_from_id": pending_from,
        "pending_top_id": mark["pending_top_id"],
    }


def forget_mark(peer_id: str | int, thread_id: Any = None) -> bool:
    """Drop one scope's mark; ``True`` if it existed.

    Prevents a stale mark from outliving the chat it describes — a mark left
    behind by a mis-keyed or deleted target would otherwise make the next
    digest of a chat that reuses the id answer "nothing is new". Only the named
    scope goes: forgetting one topic leaves the chat-wide mark and the sibling
    topics alone, because a caller that wanted the chat reset can name it
    without a thread.
    """
    _, _, key = _mark_key(peer_id, thread_id)
    with _LOCK:
        marks = dict(_load_unlocked())
        existed = marks.pop(key, None) is not None
        if existed:
            _save_unlocked(marks)
    return existed


def forget_all() -> int:
    """Drop every mark and return how many were dropped.

    Prevents a wholesale reset from being a partial one: the count returned is
    the number of scopes that were actually forgotten — every topic counts, so
    the number is marks, not chats — and a caller can tell an empty store from
    a failed clear.
    """
    with _LOCK:
        marks = dict(_load_unlocked())
        count = len(marks)
        if count:
            _save_unlocked({})
    return count
