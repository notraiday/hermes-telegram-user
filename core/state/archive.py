"""The local archive: a copy of named chats, on this machine, in SQLite.

Two tables and no account column: this plugin follows one Telegram account, so
the peer id alone names a chat. ``chat_id`` is Telethon's signed peer id as a
string — the same key space ``aliases.json`` (``peer_id``) and
``transcripts.sqlite3`` (``chat_id``) use — so one number names the same chat in
every store this plugin keeps.

A *separate* database from ``transcripts.sqlite3`` on purpose: erasing archived
messages must not take the transcription cache and the alias book with it.

No class: the connection is the state, and every function takes it. Statements
are written to be safe to repeat (``CREATE TABLE IF NOT EXISTS``, upserts), so a
schema that ever has to change incompatibly is answered by deleting the file and
re-syncing rather than by a migration framework — the archive is a cache of
something Telegram still holds.

Every function assumes a connection from :func:`open_archive` (it sets
``row_factory = sqlite3.Row`` and the pragmas the rest depends on) and that the
caller keeps it on one thread, as ``sqlite3`` does by default.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from ..sanitize import sanitize_name, sanitize_text
from .paths import private_file, state_dir

#: The same ceiling ``core.helpers.message_to_dict`` gives stored text, so an
#: archived row and a live one truncate in exactly the same place.
TEXT_LIMIT = 12000

_SENDER_LIMIT = 256
_USERNAME_LIMIT = 128
_MEDIA_LIMIT = 32
_KIND_LIMIT = 32
_SENDER_ID_LIMIT = 64

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_chats (
    chat_id           TEXT NOT NULL PRIMARY KEY,
    kind              TEXT NOT NULL,
    username          TEXT,
    title             TEXT,
    -- The watermarks. `newest_message_id` is what a re-sync bounds its request
    -- below with, so stored messages are never fetched twice;
    -- `oldest_message_id` is where backfilling continues from.
    oldest_message_id INTEGER,
    newest_message_id INTEGER,
    -- An interrupted run for *new* messages. When a walk down from the newest
    -- message ran out of budget before joining `newest_message_id`, there is a
    -- hole: `pending_from_id` is where to carry on from and `pending_top_id` is
    -- the id that becomes the watermark once the hole closes. Without them the
    -- next run would restart at the top, re-fetch the same page forever and
    -- never join the two ends.
    pending_from_id   INTEGER,
    pending_top_id    INTEGER,
    -- Whether backfilling reached the beginning of the history. A partial
    -- archive that claimed to be whole would answer "nobody said that" about a
    -- chat it has only the recent end of.
    complete          INTEGER NOT NULL DEFAULT 0,
    first_synced_at   REAL NOT NULL,
    last_synced_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS archived_messages (
    chat_id         TEXT NOT NULL,
    message_id      INTEGER NOT NULL,
    date            REAL,
    -- A peer id as a string, like `chat_id`: `message_to_dict` prints sender
    -- ids the same way, and a live row and an archived row have to be readable
    -- by one parser.
    sender_id       TEXT,
    sender          TEXT,
    sender_username TEXT,
    outgoing        INTEGER NOT NULL DEFAULT 0,
    text            TEXT,
    text_truncated  INTEGER NOT NULL DEFAULT 0,
    reply_to_msg_id INTEGER,
    topic_id        INTEGER,
    -- The attachment *kind* only. Archiving never downloads a byte: the archive
    -- records that a message had a photo, not the photo.
    media_type      TEXT,
    -- Kept because an edit moves it: the row-level evidence that what is
    -- stored is the text as it stands now, not as it was first sent.
    edited_at       REAL,
    PRIMARY KEY (chat_id, message_id),
    FOREIGN KEY (chat_id) REFERENCES archived_chats(chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS archived_messages_date_idx
    ON archived_messages(chat_id, date);
"""

#: One upsert for a whole page. `ON CONFLICT DO UPDATE` rather than
#: ``INSERT OR IGNORE``: a message edited between two syncs has to end up with
#: its current text, and a re-run that overlaps a page boundary must not fail on
#: a primary key. The label columns are the exception — they are kept when the
#: incoming page has nothing for them (a bare fetch can return a sender id that
#: Telethon has not resolved into an entity, and a re-sync must not erase the
#: name the first sync had resolved).
_STORE_SQL = """
INSERT INTO archived_messages (
    chat_id, message_id, date, sender_id, sender, sender_username, outgoing, text,
    text_truncated, reply_to_msg_id, topic_id, media_type, edited_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(chat_id, message_id) DO UPDATE SET
    date = COALESCE(excluded.date, archived_messages.date),
    sender_id = COALESCE(excluded.sender_id, archived_messages.sender_id),
    sender = COALESCE(excluded.sender, archived_messages.sender),
    sender_username = COALESCE(excluded.sender_username, archived_messages.sender_username),
    outgoing = excluded.outgoing,
    text = excluded.text,
    text_truncated = excluded.text_truncated,
    reply_to_msg_id = COALESCE(excluded.reply_to_msg_id, archived_messages.reply_to_msg_id),
    topic_id = COALESCE(excluded.topic_id, archived_messages.topic_id),
    media_type = COALESCE(excluded.media_type, archived_messages.media_type),
    edited_at = COALESCE(excluded.edited_at, archived_messages.edited_at)
"""


def archive_path() -> Path:
    """Where the archive lives, next to the rest of this plugin's state."""
    return state_dir() / "archive.sqlite3"


def chat_key(chat_id: Any) -> str:
    """The canonical key for a chat: ``str(telethon.utils.get_peer_id(...))``.

    The same key space as ``aliases.json``'s ``peer_id`` and
    ``transcripts.sqlite3``'s ``chat_id``. Callers holding an entity must go
    through ``core.helpers.peer_id`` first; this only normalizes the string form
    so a number and its text never become two rows for one chat.
    """
    key = str(chat_id).strip() if chat_id is not None else ""
    if not key:
        raise ValueError("chat_id is required")
    return key


def iso_of(epoch: Optional[float]) -> Optional[str]:
    """A stored timestamp as UTC ISO-8601, the format a live read prints."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def epoch_of(value: Optional[datetime]) -> Optional[float]:
    """A Telegram datetime as epoch seconds. A naive one is read as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        # Telegram always sends an offset; a naive value can only come from a
        # caller that built one by hand, and guessing a local zone would make
        # two machines disagree about when the same message was sent.
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def match_needle(query: Any) -> Optional[str]:
    """The case-folded needle for a query, or ``None`` for "no text filter"."""
    text = str(query or "").strip()
    return text.casefold() or None


def text_matches(text: Optional[str], needle: Optional[str]) -> bool:
    """The archive's text predicate, in one place: case-insensitive substring.

    There is no FTS5 index in this project and no SQL ``LIKE`` path either:
    matching is this function, over rows read in Python. :func:`search` here and
    ``core.archive.search_archive`` share it, so the same query cannot mean two
    slightly different things depending on which one answered — SQLite's
    ``LIKE`` folds ASCII only, and the two would disagree on non-ASCII text.
    """
    if not needle:
        return True
    return needle in (text or "").casefold()


# --- opening it -------------------------------------------------------------


def _narrow(path: Path, mode: int, *, what: str) -> None:
    """Take a path down to ``mode``, or fail closed.

    Like every other state file in this plugin, the ``chmod`` is best effort;
    unlike them, the *result* is then checked on POSIX and a failure refuses to
    go on. A file holding somebody's private messages that stayed group- or
    world-readable while the run reported success is the one outcome worth
    failing over. Windows ACLs cannot express a Unix mode, so there the check
    degrades to the best-effort call alone.

    The chmod uses ``mode`` itself, not ``private_file``'s fixed 0600: a
    directory at 0600 loses its x bit, and its owner can no longer reach the
    archive inside it.
    """
    with suppress(OSError):
        path.chmod(mode)
    if os.name != "posix":
        return
    try:
        actual = path.stat().st_mode & 0o777
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {what} {path}: {exc}") from None
    if actual & ~mode:
        raise RuntimeError(
            f"{what} {path} is still {actual:#o} after chmod {mode:#o}; refusing to "
            "keep private messages in a file others can read"
        )


def _prepare(path: Path, *, own_directory: bool) -> None:
    """Make the archive file exist, private, and a regular file.

    Checked on **every** open, not only on creation: an archive left ``0644`` by
    an earlier run, a restore from a backup or a careless ``chmod -R`` is a
    readable copy of somebody's private messages either way.

    ``O_CREAT|O_EXCL|O_NOFOLLOW`` at ``0600`` rather than letting sqlite3 create
    the file: the mode is then set at creation, so there is no window in which
    the database exists and is world-readable, and a symlink planted at that
    name is an error rather than a write to wherever it points. SQLite gives the
    ``-wal`` and ``-shm`` sidecars the mode of the main file, so narrowing it
    first is what keeps those private too.

    ``own_directory`` narrows the parent as well — true only for this plugin's
    own state directory. A caller that points the archive elsewhere owns that
    directory's permissions (narrowing, say, a shared ``/tmp`` would be a
    different bug than the one this prevents).
    """
    root = path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if own_directory:
        _narrow(root, 0o700, what="the state directory")

    if path.is_symlink():
        raise RuntimeError(f"{path} is a symlink; the archive must be a regular file")
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"{path} exists and is not a regular file")
        _narrow(path, 0o600, what="the archive")
        return
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(path, flags, 0o600))
    except FileExistsError:
        # Another process got there first; judge what is now on disk.
        _narrow(path, 0o600, what="the archive")


def open_archive(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the archive, private before anything is written into it.

    Creates the file and the schema when they are missing, from the same
    idempotent statements every time, so a fresh install and an existing file
    take one path. The connection is in autocommit mode; the writes that must be
    all-or-nothing (a page of messages, an erase) open their own transaction.

    The caller owns the connection and must close it.
    """
    target = Path(path) if path is not None else archive_path()
    _prepare(target, own_directory=path is None)
    con = sqlite3.connect(target, isolation_level=None, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(ARCHIVE_SCHEMA)
    _add_missing_columns(con)
    private_file(target)
    return con


def _add_missing_columns(con: sqlite3.Connection) -> None:
    """Bring a file written by an older build up to the current columns.

    Deliberately **not** a migration framework: the archive is a cache of
    something Telegram still holds, so a schema that changes incompatibly is
    answered by deleting the file and re-syncing. What this covers is the narrow
    case of a column added to a table that ``CREATE TABLE IF NOT EXISTS`` will
    not touch.
    """
    for column in ("pending_from_id INTEGER", "pending_top_id INTEGER"):
        with suppress(sqlite3.OperationalError):  # already there
            con.execute(f"ALTER TABLE archived_chats ADD COLUMN {column}")  # noqa: S608


@contextmanager
def _transaction(con: sqlite3.Connection) -> Iterator[None]:
    """All of these writes or none of them.

    The connection is in autocommit mode, so without this a page of two hundred
    messages would be two hundred transactions and two hundred fsyncs; it also
    means a page that fails half way leaves half a page on disk. Nested use is a
    no-op, so a caller holding its own transaction keeps it.
    """
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


# --- writing ----------------------------------------------------------------


def register_chat(
    con: sqlite3.Connection,
    *,
    chat_id: Any,
    title: Any,
    kind: Any,
    username: Any = None,
) -> None:
    """Create the chat row if it is new, refresh its labels if it is not.

    Called at the start of every sync, so it must never fail on a chat that is
    already registered and must never disturb the watermarks. A label the caller
    cannot supply leaves the stored one alone instead of erasing it.
    """
    key = chat_key(chat_id)
    now = time.time()
    clean_kind = sanitize_name(kind, limit=_KIND_LIMIT) if kind else "unknown"
    clean_title = sanitize_name(title, limit=256) if title else None
    clean_username = sanitize_name(username, limit=_USERNAME_LIMIT) if username else None
    with _transaction(con):
        updated = con.execute(
            "UPDATE archived_chats SET kind = ?, title = COALESCE(?, title), "
            "username = COALESCE(?, username) WHERE chat_id = ?",
            (clean_kind, clean_title, clean_username, key),
        ).rowcount
        if not updated:
            con.execute(
                "INSERT INTO archived_chats "
                "(chat_id, kind, title, username, first_synced_at, last_synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, clean_kind, clean_title, clean_username, now, now),
            )


def _int_of(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_of(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text_of(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    clean = sanitize_name(value, limit=limit)
    return clean or None


def _stored_row(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """One row on its way to disk, validated and masked here rather than trusted.

    This is the single choke point for text that came from Telegram, so the
    masking happens on the way *in* as well as on the way out: a caller cannot
    store a control character that a later read would have to strip anyway.
    """
    message_id = _int_of(item.get("message_id"))
    if message_id is None or message_id <= 0:
        raise ValueError("stored message needs a positive message_id")
    raw_text = item.get("text")
    text = sanitize_text(raw_text, limit=TEXT_LIMIT) if raw_text else None
    truncated = bool(item.get("text_truncated")) or (bool(raw_text) and len(str(raw_text)) > TEXT_LIMIT)
    return (
        chat_key(item.get("chat_id")),
        message_id,
        _float_of(item.get("date")),
        _text_of(item.get("sender_id"), _SENDER_ID_LIMIT),
        _text_of(item.get("sender"), _SENDER_LIMIT),
        _text_of(item.get("sender_username"), _USERNAME_LIMIT),
        int(bool(item.get("outgoing"))),
        text,
        int(truncated),
        _int_of(item.get("reply_to_msg_id")),
        _int_of(item.get("topic_id")),
        _text_of(item.get("media_type"), _MEDIA_LIMIT),
        _float_of(item.get("edited_at")),
    )


def store_messages(con: sqlite3.Connection, messages: Sequence[Mapping[str, Any]]) -> int:
    """Write a page of messages, returning how many rows were written.

    Each item needs ``chat_id`` and ``message_id``; the rest of the columns are
    optional. An edited message replaces the stored row, which is the point of
    the upsert: the table is a copy of what Telegram holds *now*, not a log of
    what it held when the sync first ran.
    """
    if not messages:
        return 0
    rows = [_stored_row(item) for item in messages]
    with _transaction(con):
        con.executemany(_STORE_SQL, rows)
    return len(rows)


def set_watermarks(
    con: sqlite3.Connection,
    chat_id: Any,
    *,
    oldest: Optional[int] = None,
    newest: Optional[int] = None,
    pending_from_id: Optional[int] = None,
    pending_top_id: Optional[int] = None,
    complete: Optional[bool] = None,
) -> None:
    """Record where this chat's archive now stops, in both directions.

    ``pending_from_id``/``pending_top_id`` are written unconditionally, ``NULL``
    included: clearing them is how a closed hole stops being a hole, and an
    update that only ever set them would leave a chat permanently marked as
    interrupted.

    ``complete`` is left alone when it is ``None``. Only the backfill walk knows
    whether it reached the first message, and the sync that records a hole in
    the middle has nothing to say about the far end.
    """
    key = chat_key(chat_id)
    now = time.time()
    with _transaction(con):
        changed = con.execute(
            "UPDATE archived_chats SET oldest_message_id = ?, newest_message_id = ?, "
            "pending_from_id = ?, pending_top_id = ?, complete = COALESCE(?, complete), "
            "last_synced_at = ? WHERE chat_id = ?",
            (
                _int_of(oldest),
                _int_of(newest),
                _int_of(pending_from_id),
                _int_of(pending_top_id),
                None if complete is None else int(bool(complete)),
                now,
                key,
            ),
        ).rowcount
    if not changed:
        raise ValueError(f"archive has no chat {key}; register it before setting watermarks")


def forget_chat(con: sqlite3.Connection, chat_id: Any) -> bool:
    """Erase one chat and its messages, returning whether it was archived.

    Idempotent by design: a cleanup that fails the second time it runs is a
    cleanup nobody automates. The messages are deleted explicitly rather than
    left to ``ON DELETE CASCADE``, so the erase is complete even on a connection
    opened without ``PRAGMA foreign_keys``.
    """
    key = chat_key(chat_id)
    with _transaction(con):
        con.execute("DELETE FROM archived_messages WHERE chat_id = ?", (key,))
        existed = con.execute("DELETE FROM archived_chats WHERE chat_id = ?", (key,)).rowcount
    return bool(existed)


# --- reading ----------------------------------------------------------------


def _message_from(row: sqlite3.Row) -> dict[str, Any]:
    """One stored row, sanitized again on the way out."""
    return {
        "chat_id": str(row["chat_id"]),
        "message_id": int(row["message_id"]),
        "date": row["date"],
        "sender_id": _text_of(row["sender_id"], _SENDER_ID_LIMIT),
        "sender": _text_of(row["sender"], _SENDER_LIMIT),
        "sender_username": _text_of(row["sender_username"], _USERNAME_LIMIT),
        "outgoing": bool(row["outgoing"]),
        "text": sanitize_text(row["text"], limit=TEXT_LIMIT) if row["text"] else None,
        "text_truncated": bool(row["text_truncated"]),
        "reply_to_msg_id": row["reply_to_msg_id"],
        "topic_id": row["topic_id"],
        "media_type": _text_of(row["media_type"], _MEDIA_LIMIT),
        "edited_at": row["edited_at"],
    }


def iter_messages(
    con: sqlite3.Connection,
    *,
    chat_id: Any = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    batch: int = 500,
) -> Generator[dict[str, Any], None, None]:
    """Stored messages, newest first, streamed off the cursor.

    Streamed rather than returned as a list: fifty thousand message bodies is
    hundreds of megabytes, and a search that stops at the first fifty matches
    should not have paid for all of them first. ``since`` is inclusive and
    ``until`` exclusive, the same window convention the live read tools use.
    """
    where: list[str] = []
    args: list[Any] = []
    if chat_id is not None:
        where.append("chat_id = ?")
        args.append(chat_key(chat_id))
    if since is not None:
        where.append("date >= ?")
        args.append(float(since))
    if until is not None:
        where.append("date < ?")
        args.append(float(until))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    # Every fragment of `clause` is a literal written above; caller values only
    # ever travel in `args`, as bound parameters.
    cursor = con.execute(
        f"SELECT * FROM archived_messages{clause} ORDER BY date DESC, message_id DESC",  # noqa: S608
        args,
    )
    try:
        while True:
            rows = cursor.fetchmany(max(1, int(batch)))
            if not rows:
                break
            for row in rows:
                yield _message_from(row)
    finally:
        cursor.close()


def search(
    con: sqlite3.Connection,
    *,
    pattern: Any = None,
    chat_id: Any = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Stored messages whose text matches ``pattern``, newest first.

    The storage-level query: same matching rule as everywhere else
    (:func:`text_matches`, case-insensitive substring), with the SQL doing only
    what it can index — chat and date. ``offset`` skips matches, not rows, so
    paging through results is stable while ``limit`` bounds one page.
    """
    needle = match_needle(pattern)
    take = max(1, int(limit))
    skip = max(0, int(offset))
    out: list[dict[str, Any]] = []
    for row in iter_messages(con, chat_id=chat_id, since=since, until=until):
        if not text_matches(row["text"], needle):
            continue
        if skip:
            skip -= 1
            continue
        out.append(row)
        if len(out) >= take:
            break
    return out


def count_messages(con: sqlite3.Connection, chat_id: Any = None) -> int:
    """How many messages are stored, across the archive or for one chat."""
    if chat_id is None:
        row = con.execute("SELECT COUNT(*) AS n FROM archived_messages").fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM archived_messages WHERE chat_id = ?",
            (chat_key(chat_id),),
        ).fetchone()
    return int(row["n"]) if row else 0


def get_watermarks(con: sqlite3.Connection, chat_id: Any) -> dict[str, Any]:
    """Everything the sync bookkeeping needs about one chat, in one dict.

    A chat that was never synced returns the same keys with empty values rather
    than ``None`` itself: the caller then reads a watermark the same way whether
    or not the row exists, and a missing row can never be mistaken for a chat
    whose watermarks happen to be zero.

    ``complete`` means the backfill reached the first message ever sent;
    ``contiguous`` means there is no hole in the middle of what is stored. They
    are different failures and are reported separately.
    """
    key = chat_key(chat_id)
    row = con.execute("SELECT * FROM archived_chats WHERE chat_id = ?", (key,)).fetchone()
    if row is None:
        return {
            "chat_id": key,
            "kind": None,
            "title": None,
            "username": None,
            "oldest": None,
            "newest": None,
            "pending_from_id": None,
            "pending_top_id": None,
            "complete": False,
            "contiguous": True,
            "whole": False,
            "first_synced_at": None,
            "synced_at": None,
            "messages": 0,
        }
    pending_from = _int_of(row["pending_from_id"])
    complete = bool(row["complete"])
    contiguous = pending_from is None
    return {
        "chat_id": str(row["chat_id"]),
        "kind": sanitize_name(row["kind"], limit=_KIND_LIMIT) if row["kind"] else None,
        "title": sanitize_name(row["title"], limit=256) if row["title"] else None,
        "username": sanitize_name(row["username"], limit=_USERNAME_LIMIT) if row["username"] else None,
        "oldest": _int_of(row["oldest_message_id"]),
        "newest": _int_of(row["newest_message_id"]),
        "pending_from_id": pending_from,
        "pending_top_id": _int_of(row["pending_top_id"]),
        "complete": complete,
        "contiguous": contiguous,
        "whole": complete and contiguous,
        "first_synced_at": iso_of(row["first_synced_at"]),
        "synced_at": iso_of(row["last_synced_at"]),
        "messages": count_messages(con, key),
    }


def _chat_from(row: sqlite3.Row) -> dict[str, Any]:
    """One archived chat with its aggregate, in the shape a listing reports."""
    pending_from = _int_of(row["pending_from_id"])
    complete = bool(row["complete"])
    contiguous = pending_from is None
    return {
        "chat_id": str(row["chat_id"]),
        "kind": sanitize_name(row["kind"], limit=_KIND_LIMIT) if row["kind"] else None,
        "title": sanitize_name(row["title"], limit=256) if row["title"] else None,
        "username": sanitize_name(row["username"], limit=_USERNAME_LIMIT) if row["username"] else None,
        "messages": int(row["messages"]),
        "oldest_message_id": _int_of(row["oldest_message_id"]),
        "newest_message_id": _int_of(row["newest_message_id"]),
        "oldest": iso_of(row["oldest_date"]),
        "newest": iso_of(row["newest_date"]),
        "pending_from_id": pending_from,
        "pending_top_id": _int_of(row["pending_top_id"]),
        "complete": complete,
        "contiguous": contiguous,
        "whole": complete and contiguous,
        "first_synced_at": iso_of(row["first_synced_at"]),
        "synced_at": iso_of(row["last_synced_at"]),
    }


def list_chats(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every archived chat with its message count and both ends, newest sync first."""
    rows = con.execute(
        "SELECT c.*, COUNT(m.message_id) AS messages, MIN(m.date) AS oldest_date, "
        "MAX(m.date) AS newest_date FROM archived_chats c "
        "LEFT JOIN archived_messages m ON m.chat_id = c.chat_id "
        "GROUP BY c.chat_id ORDER BY c.last_synced_at DESC"
    ).fetchall()
    return [_chat_from(row) for row in rows]


def _db_file(con: sqlite3.Connection) -> Optional[str]:
    for row in con.execute("PRAGMA database_list").fetchall():
        if row["name"] == "main":
            return str(row["file"]) if row["file"] else None
    return None


def _file_size(path: Optional[str]) -> Optional[int]:
    if not path:
        return None
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def stats(con: sqlite3.Connection) -> dict[str, Any]:
    """What is on this disk: counts, both ends of every chat, and the file size.

    ``db_bytes`` is the database itself and ``wal_bytes`` the write-ahead log
    beside it, which after a long sync can be the larger of the two — a size
    report that quoted only the first would understate the disk used.
    """
    file_path = _db_file(con)
    return {
        "chats": int(con.execute("SELECT COUNT(*) AS n FROM archived_chats").fetchone()["n"]),
        "messages": count_messages(con),
        "db_path": file_path,
        "db_bytes": _file_size(file_path),
        "wal_bytes": _file_size(f"{file_path}-wal" if file_path else None),
        "per_chat": list_chats(con),
    }
