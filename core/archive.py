"""Archive operations: copy named chats down to SQLite, and search them offline.

Read-only towards Telegram like everything else in this plugin — the only RPC
here is ``client.get_messages``, which is history retrieval and acknowledges
nothing. No Read* RPC is ever called, so a sync leaves every unread badge in the
account exactly as it found it.

The sync is resumable and gap-aware. A walk pages newest→oldest; when it runs
out of budget before joining up with what is already on disk, the hole is
recorded in ``pending_from_id``/``pending_top_id`` and the ``newest`` watermark
does **not** move — advancing it across a gap would make the messages inside
unreachable forever. The next call closes the hole first and only then advances
the mark, and the marks are written even when an RPC fails mid-walk.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from .client import entity_label
from .helpers import bounded_int, forward_label, parse_dt, peer_id
from .media import media_info
from .sanitize import sanitize_name, sanitize_text
from .state.aliases import aliases_for_peer
from .state.archive import (
    TEXT_LIMIT,
    epoch_of,
    get_watermarks,
    iso_of,
    iter_messages,
    list_chats,
    match_needle,
    register_chat,
    set_watermarks,
    store_messages,
    text_matches,
)
from .state.archive import count_messages as count_stored_messages

#: Messages fetched per Telegram request while archiving. Telethon pages
#: history at 100 internally, so asking for a round multiple of that keeps one
#: request per page rather than one and a fifth.
ARCHIVE_PAGE = 200

#: Ceiling on one Telegram request's page size.
ARCHIVE_MAX_PAGE = 1000

#: How many messages one ``sync_chat`` call may fetch. A ceiling rather than
#: "until it finishes", because a chat with 300 000 messages would otherwise
#: hold the session and its flood budget for as long as it took. The call
#: reports where it stopped, and repeating it continues from there.
ARCHIVE_MAX_SYNC = 5000

#: How many stored rows one search may run its text predicate over. Bounds the
#: cost of a search that matches almost nothing in a very large archive.
ARCHIVE_MAX_SCAN = 50_000

#: A longer query is not something anybody typed.
MAX_PATTERN = 500


@dataclass(slots=True)
class _Walk:
    """What one page-walk achieved: ids, counts, and how it stopped."""

    stored: int = 0
    scanned: int = 0
    lowest: Optional[int] = None
    highest: Optional[int] = None
    reached: bool = False
    #: Telegram had nothing older left to give, which is the only honest basis
    #: for calling a backfill complete: a date floor can be reached without the
    #: beginning of the history being anywhere near.
    exhausted: bool = False


def chat_kind(entity: Any) -> str:
    """A one-word kind for an entity, matching how this plugin reads chat types.

    ``core.folders.dialog_summary`` reports ``is_group``/``is_channel``/``is_user``
    booleans; the archive keeps one word for the same three cases, plus the two
    that matter when reading history: a bot and a broadcast channel.

    The test is on Telethon's marker attributes rather than on ``isinstance``,
    because Telethon is imported lazily everywhere in this plugin and a *forbidden*
    entity (one this account can no longer read) is not a subclass of the type it
    mirrors. ``unknown`` is a label for an entity this build does not recognise —
    never a licence to treat the chat as public.
    """
    if hasattr(entity, "bot"):
        return "bot" if bool(getattr(entity, "bot", False)) else "user"
    if hasattr(entity, "broadcast"):
        return "channel" if bool(getattr(entity, "broadcast", False)) else "supergroup"
    name = type(entity).__name__.lower()
    if name in {"chat", "chatforbidden"}:
        return "group"
    if name in {"channel", "channelforbidden"}:
        return "channel"
    return "unknown"


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_bound(value: Any, *, field: str) -> Optional[float]:
    """A date bound from the model, refusing to guess at anything else.

    An ISO-8601 string goes through ``core.helpers.parse_dt``, so ``today`` and
    ``yesterday`` work and a naive timestamp means local time, exactly as it does
    in every other date argument this plugin takes. Epochs and datetimes are
    accepted as they are for callers that already resolved one.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a date, a datetime or an epoch, not a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.astimezone()).timestamp()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = parse_dt(text)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO-8601 date or 'today'/'yesterday' ({exc})"
        ) from None
    if parsed is None:
        raise ValueError(f"{field} must be an ISO-8601 date")
    return parsed.timestamp()


def _stored(message: Any, chat_id: str) -> dict[str, Any]:
    """One Telethon message, flattened into the columns the archive keeps.

    A subset of ``core.helpers.message_to_dict`` on purpose: views, reactions,
    pin state, buttons and read pointers are all *live* properties that change
    after a message is sent, and storing them would mean an archive that
    confidently reports last week's reaction counts. What is kept is what does
    not move: who said what, when, in reply to what.
    """
    sender = getattr(message, "sender", None)
    reply = getattr(message, "reply_to", None)
    text = getattr(message, "message", None) or None
    media = media_info(message)
    sender_username = getattr(sender, "username", None) if sender is not None else None
    return {
        "chat_id": chat_id,
        "message_id": int(getattr(message, "id", 0) or 0),
        "date": epoch_of(getattr(message, "date", None)),
        "sender_id": peer_id(sender) or (str(getattr(message, "sender_id", "") or "") or None),
        "sender": sanitize_name(entity_label(sender), limit=256) if sender is not None else None,
        "sender_username": sanitize_name(sender_username, limit=128) if sender_username else None,
        "outgoing": bool(getattr(message, "out", False)),
        "text": sanitize_text(text, limit=TEXT_LIMIT) if text else None,
        "text_truncated": bool(text and len(text) > TEXT_LIMIT),
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
        "topic_id": getattr(reply, "reply_to_top_id", None),
        "media_type": (media or {}).get("kind"),
        "edited_at": epoch_of(getattr(message, "edit_date", None)),
        "forwarded_from": forward_label(message),
        "forward_checked": True,
    }


def _in_window(message: Any, floor_epoch: Optional[float]) -> bool:
    """Whether a fetched message is at or above a date-bounded sync's floor."""
    if floor_epoch is None:
        return True
    sent = epoch_of(getattr(message, "date", None))
    return sent is None or sent >= floor_epoch


async def _walk(
    client: Any,
    con: sqlite3.Connection,
    entity: Any,
    chat_id: str,
    *,
    start_at: int,
    floor_id: int,
    floor_epoch: Optional[float],
    page: int,
    budget: int,
    note: Callable[[int, int], None],
) -> _Walk:
    """Page backwards from ``start_at`` down to the floor, storing as it goes.

    Both id bounds are Telegram's own *exclusive* cursors, which is what makes
    this resumable: ``max_id`` walks down from the newest message, ``min_id``
    stops at the watermark already on disk, and neither ever re-requests a
    message that is stored. ``floor_epoch`` is the optional date floor of a
    date-bounded sync: messages older than it are neither stored nor walked past.

    ``note`` is called with the lowest and highest ids stored so far after every
    stored page, which is how a walk that dies on a flood wait still leaves the
    hole recorded instead of losing it with the exception.

    ``reached`` is the value the caller must not ignore. A walk that merely ran
    out of budget has left a gap between what it stored and what was already
    there, and a watermark advanced across that gap would mean those messages
    are never fetched again. ``exhausted`` is set only when Telegram itself ran
    out of older messages, which is what makes a backfill complete rather than
    merely stopped.
    """
    outcome = _Walk()
    cursor = max(_as_int(start_at) or 0, 0)
    while budget > 0:
        want = min(page, budget)
        page_rows = list(await client.get_messages(entity, limit=want, max_id=cursor, min_id=floor_id))
        outcome.scanned += len(page_rows)
        if not page_rows:
            outcome.reached = True
            outcome.exhausted = floor_id == 0
            break

        keep = [row for row in page_rows if _in_window(row, floor_epoch)]
        dropped = len(page_rows) - len(keep)
        rows = [_stored(row, chat_id) for row in keep if getattr(row, "id", None)]
        written = store_messages(con, rows)
        ids = [row["message_id"] for row in rows if row["message_id"]]
        if ids:
            low, high = min(ids), max(ids)
            lowest = low if outcome.lowest is None else min(outcome.lowest, low)
            highest = high if outcome.highest is None else max(outcome.highest, high)
            outcome.lowest, outcome.highest = lowest, highest
            outcome.stored += written
            note(lowest, highest)

        budget -= len(page_rows)
        # Exclusive again: the next page starts strictly below everything just
        # read, including what the date floor kept out of the archive.
        fetched_ids = [int(getattr(row, "id", 0) or 0) for row in page_rows]
        cursor = min(fetched_ids) if fetched_ids else cursor
        if len(page_rows) < want:
            # Telegram had fewer messages than were asked for: there are none
            # left between the cursor and the floor.
            outcome.reached = True
            outcome.exhausted = floor_id == 0
            break
        if dropped:
            # This page crossed the date floor, so the window it bounded is
            # covered. The rows below the floor are somebody else's sync's job,
            # and whether the history ends down there is not knowable from here.
            outcome.reached = True
            break
        if cursor <= floor_id + 1:
            # And nothing can lie in a gap of zero ids.
            outcome.reached = True
            outcome.exhausted = floor_id == 0
            break
    return outcome


async def sync_chat(
    client: Any,
    con: sqlite3.Connection,
    entity: Any,
    *,
    page: int = ARCHIVE_PAGE,
    max_sync: int = ARCHIVE_MAX_SYNC,
    since: Any = None,
) -> dict[str, Any]:
    """Copy one chat's history down, newest messages first, resumably.

    New messages are taken before old ones: a caller that runs out of budget in
    the middle of a long backfill still gets today's messages, which is what it
    almost always wanted. ``since`` bounds the window by date (inclusive, as
    every other date argument here is) and turns the sync into a fill of that
    window: a chat whose history reaches further back is not reported
    ``complete``, because the beginning of the history has not been reached —
    while a chat whose whole history lies inside the window is, because Telegram
    ran out of older messages either way.

    The watermarks are written in a ``finally``: the walk records the hole on
    disk after every page, and the bookkeeping is saved on the way out even when
    the RPC that failed is what is unwinding the stack. Losing the marks is how
    a sync re-fetches the same page forever.
    """
    chat_id = peer_id(entity)
    if not chat_id:
        raise ValueError("archive sync needs a resolved chat entity")
    page = bounded_int(page, ARCHIVE_PAGE, 1, ARCHIVE_MAX_PAGE)
    max_sync = bounded_int(max_sync, ARCHIVE_MAX_SYNC, 1, ARCHIVE_MAX_SYNC)
    floor_epoch = _epoch_bound(since, field="since")
    kind = chat_kind(entity)
    title = sanitize_name(entity_label(entity), limit=256)
    username = getattr(entity, "username", None)
    username = sanitize_name(username, limit=128) if username else None
    register_chat(con, chat_id=chat_id, kind=kind, title=title, username=username)

    marks = get_watermarks(con, chat_id)
    state: dict[str, Any] = {
        "oldest": _as_int(marks["oldest"]),
        "newest": _as_int(marks["newest"]),
        "complete": bool(marks["complete"]),
        "pending_from": _as_int(marks["pending_from_id"]),
        "pending_top": _as_int(marks["pending_top_id"]),
    }

    def save() -> None:
        set_watermarks(
            con,
            chat_id,
            oldest=state["oldest"],
            newest=state["newest"],
            complete=state["complete"],
            pending_from_id=state["pending_from"],
            pending_top_id=state["pending_top"],
        )

    def note_new(low: int, high: int) -> None:
        """Record both ends of an interrupted run for new messages."""
        state["pending_top"] = high if state["pending_top"] is None else max(state["pending_top"], high)
        state["pending_from"] = low if state["pending_from"] is None else min(state["pending_from"], low)
        save()

    def note_old(low: int, high: int) -> None:
        """Move the backfill mark down to what was actually stored."""
        state["oldest"] = low if state["oldest"] is None else min(state["oldest"], low)
        state["newest"] = high if state["newest"] is None else max(state["newest"], high)
        save()

    added = 0
    scanned = 0
    try:
        if state["newest"] is not None:
            walked = await _walk(
                client,
                con,
                entity,
                chat_id,
                start_at=state["pending_from"] or 0,
                floor_id=state["newest"],
                floor_epoch=floor_epoch,
                page=page,
                budget=max_sync,
                note=note_new,
            )
            added += walked.stored
            scanned += walked.scanned
            if walked.reached:
                # The hole is closed: everything from the old watermark up to
                # the highest id seen across the whole run is on disk now, so
                # the mark may move to it.
                if state["pending_top"] is not None:
                    state["newest"] = max(state["newest"], state["pending_top"])
                state["pending_from"] = None
                state["pending_top"] = None
            # Still a hole: `note_new` recorded both ends of it, and the
            # watermark stays where it was.

        remaining = max_sync - scanned
        if remaining > 0 and not state["complete"]:
            walked = await _walk(
                client,
                con,
                entity,
                chat_id,
                start_at=state["oldest"] or 0,
                floor_id=0,
                floor_epoch=floor_epoch,
                page=page,
                budget=remaining,
                note=note_old,
            )
            added += walked.stored
            scanned += walked.scanned
            # Only a walk that saw Telegram run out of older messages has
            # reached the beginning of the history. Reaching a date floor is a
            # different thing, and reporting it as completeness would be a lie
            # the next sync would believe.
            state["complete"] = walked.exhausted
    finally:
        save()

    contiguous = state["pending_from"] is None
    complete = bool(state["complete"])
    return {
        "chat_id": chat_id,
        "kind": kind,
        "title": title,
        "username": username,
        "added": added,
        "scanned": scanned,
        "total": count_stored_messages(con, chat_id),
        # Two different failures, reported separately: `complete` is "back to
        # the first message ever sent", `contiguous` is "nothing missing in the
        # middle". An archive with a gap under today's messages is up to date
        # and incomplete at the same time.
        "complete": complete,
        "contiguous": contiguous,
        "whole": complete and contiguous,
        "oldest": state["oldest"],
        "newest": state["newest"],
        "pending_from_id": state["pending_from"],
        "pending_top_id": state["pending_top"],
        "since": iso_of(floor_epoch),
        "synced_at": get_watermarks(con, chat_id)["synced_at"],
    }


def _archived_row(row: dict[str, Any], chat: dict[str, Any]) -> dict[str, Any]:
    """An archived message in the shape ``core.helpers.message_to_dict`` returns.

    The same keys for the fields the archive keeps, so a caller writes one
    parser for live and archived rows; ``archived_at`` is what tells the two
    apart, because it is never present on a live read. Fields the archive does
    not store — views, reactions, buttons, forwards — are absent rather than
    invented: a fabricated zero would read as a fact.
    """
    entry: dict[str, Any] = {
        "id": row["message_id"],
        "date": iso_of(row["date"]),
        "sender_id": row["sender_id"],
        "sender": row["sender"],
        "text": row["text"] if row["text"] is not None else "",
        "out": row["outgoing"],
        "reply_to_msg_id": row["reply_to_msg_id"],
        "topic_id": row["topic_id"],
        "chat_id": row["chat_id"],
        "archived_at": chat.get("synced_at"),
    }
    if row["sender_username"]:
        entry["sender_username"] = row["sender_username"]
    if row["text_truncated"]:
        entry["text_truncated"] = True
    if row["edited_at"] is not None:
        entry["edited_at"] = iso_of(row["edited_at"])
    if row.get("forwarded_from"):
        entry["forwarded_from"] = row["forwarded_from"]
    if row["media_type"]:
        # The kind only: the archive records that a message had a photo, and
        # leaves fetching the photo to the media tools and their own quota.
        entry["media"] = {"kind": row["media_type"]}
    if chat.get("title"):
        entry["chat"] = chat["title"]
    if chat.get("username"):
        entry["chat_username"] = chat["username"]
    if chat.get("aliases"):
        entry["chat_aliases"] = chat["aliases"]
    return entry


async def search_archive(
    con: sqlite3.Connection,
    *,
    query: Any = None,
    chat_id: Any = None,
    since: Any = None,
    until: Any = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Search the local archive, in Python, without touching Telegram.

    ``query`` is a case-insensitive substring; there is no FTS5 index in this
    project and no SQL matching path either, so one predicate
    (``core.state.archive.text_matches``) decides every match. ``since`` is
    inclusive and ``until`` exclusive, the window convention every other tool
    here follows. ``chat_id`` restricts the search to one archived chat and
    refuses a chat that was never archived: an empty answer for a chat nobody
    synced reads as "they never said that", which is a different statement.

    Rows are streamed and matching stops as soon as one match more than ``limit``
    is found, which is what makes ``truncated`` exact rather than a guess.
    """
    needle = match_needle(query)
    if needle is not None and len(needle) > MAX_PATTERN:
        raise ValueError(f"archive search query is longer than {MAX_PATTERN} characters")
    since_epoch = _epoch_bound(since, field="since")
    until_epoch = _epoch_bound(until, field="until")
    if since_epoch is not None and until_epoch is not None and until_epoch <= since_epoch:
        raise ValueError("archive search needs until to be later than since")
    take = bounded_int(limit, 100, 1, ARCHIVE_MAX_SCAN)
    scope = str(chat_id).strip() if chat_id is not None else ""

    chats = list_chats(con)
    by_id = {chat["chat_id"]: chat for chat in chats}
    if scope:
        if scope not in by_id:
            raise ValueError(
                f"chat {sanitize_name(scope, limit=64)} is not in the local archive; "
                "sync it first"
            )
        chats = [by_id[scope]]

    messages: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    with closing(
        iter_messages(con, chat_id=scope or None, since=since_epoch, until=until_epoch)
    ) as rows:
        for row in rows:
            if scanned >= ARCHIVE_MAX_SCAN:
                truncated = True
                break
            scanned += 1
            if not text_matches(row["text"], needle):
                continue
            if len(messages) >= take:
                # One match more than the caller asked for: the answer is
                # truncated, and saying so is the point.
                truncated = True
                break
            messages.append(_archived_row(row, by_id.get(row["chat_id"], {})))

    stamps = [chat["synced_at"] for chat in chats if chat.get("synced_at")]
    return {
        "count": len(messages),
        "scanned": scanned,
        "truncated": truncated,
        "messages": messages,
        "chat": by_id.get(scope) if scope else None,
        "chats_searched": len(chats),
        # The *oldest* sync among the chats searched, because that is the honest
        # freshness of the answer as a whole: results came from every one of them.
        "synced_at": min(stamps) if stamps else None,
    }


def chat_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every archived chat, with the numbers a status listing needs.

    Read straight from the database: this is the offline surface, and it must
    answer without Telegram — a chat that has since been left still has a row,
    and that is exactly what a status listing is for. Aliases are added here
    because the disk is the only place that knows them, and the model addresses
    chats by the names it saved rather than by their ids.
    """
    rows: list[dict[str, Any]] = []
    for chat in list_chats(con):
        entry = dict(chat)
        entry["aliases"] = aliases_for_peer(chat["chat_id"])
        rows.append(entry)
    return rows
