"""Local archive storage: write, edit, search, window, forget."""

import asyncio
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import isolated_state, plugin_module  # noqa: E402

CHAT = "-1001234567890"


def _row(message_id, text, *, date=None, sender="Иска", outgoing=False):
    return {
        "chat_id": CHAT,
        "message_id": message_id,
        "date": float(date if date is not None else 1700000000 + message_id),
        "sender": sender,
        "text": text,
        "outgoing": outgoing,
    }


def _open():
    return plugin_module("core.state.archive")


def test_archive_round_trip_and_substring_search():
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev", username="dev")
            written = archive.store_messages(
                con,
                [
                    _row(1, "обсуждали P40 и поставки"),
                    _row(2, "не про P40, а про P50"),
                    _row(3, "случайная строка"),
                ],
            )
            assert written == 3
            assert archive.count_messages(con, CHAT) == 3

            hits = archive.search(con, pattern="p40")
            assert sorted(hit["message_id"] for hit in hits) == [1, 2]
            assert all("P40" in hit["text"] for hit in hits)
            assert len(archive.search(con, pattern="P40")) == 2
            assert archive.search(con, pattern="nothing-here") == []
        finally:
            con.close()


def test_edit_replaces_the_row_instead_of_appending():
    """The archive is what Telegram holds now, not a log of every version."""
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            archive.store_messages(con, [_row(7, "первая версия")])
            archive.store_messages(con, [_row(7, "правка: тоже про P40")])

            assert archive.count_messages(con, CHAT) == 1
            hits = archive.search(con, pattern="P40")
            assert [hit["message_id"] for hit in hits] == [7]
            assert "правка" in hits[0]["text"]
            assert archive.search(con, pattern="первая версия") == []
        finally:
            con.close()


def test_reindex_preserves_stored_sender_labels():
    """A bare re-fetch that cannot resolve the sender must not erase the stored name."""
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            archive.store_messages(con, [_row(9, "текст", sender="Иска")])
            archive.store_messages(
                con, [{"chat_id": CHAT, "message_id": 9, "date": 1700000009.0, "text": "текст v2"}]
            )
            rows = archive.search(con, chat_id=CHAT)
            assert len(rows) == 1
            assert rows[0]["sender"] == "Иска"
            assert rows[0]["text"] == "текст v2"
        finally:
            con.close()


def test_window_filter_is_inclusive_lower_exclusive_upper():
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            archive.store_messages(
                con, [_row(1, "a", date=100.0), _row(2, "b", date=200.0), _row(3, "c", date=300.0)]
            )
            # newest first, so compare as sets
            assert sorted(r["message_id"] for r in archive.search(con, since=200.0)) == [2, 3]
            assert [r["message_id"] for r in archive.search(con, until=200.0)] == [1]
            assert [r["message_id"] for r in archive.search(con, since=200.0, until=300.0)] == [2]
        finally:
            con.close()


def _ops():
    return plugin_module("core.archive")


def test_the_local_search_layer_normalises_dates_and_refuses_nonsense():
    """Date coercion lives in the model-facing layer, not in the storage layer."""
    with isolated_state():
        archive = _open()
        ops = _ops()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            stamp = 1700000000.0
            archive.store_messages(con, [_row(1, "x", date=stamp)])

            later = datetime.fromtimestamp(stamp + 1, tz=timezone.utc)
            earlier = datetime.fromtimestamp(stamp - 1, tz=timezone.utc)

            # until is exclusive: the row sits below stamp+1 and above stamp-1
            assert asyncio.run(ops.search_archive(con, until=later))["count"] == 1
            assert asyncio.run(ops.search_archive(con, until=earlier))["count"] == 0
            # since is inclusive
            assert asyncio.run(ops.search_archive(con, since=earlier))["count"] == 1
            assert asyncio.run(ops.search_archive(con, since=later))["count"] == 0

            # the same bounds as ISO strings
            assert asyncio.run(ops.search_archive(con, since=earlier.isoformat()))["count"] == 1

            # 'today' is far in the future relative to these fixtures
            assert asyncio.run(ops.search_archive(con, since="today"))["count"] == 0

            for bad in (True, False):
                try:
                    asyncio.run(ops.search_archive(con, since=bad))
                except ValueError:
                    pass
                else:
                    raise AssertionError("a boolean date bound must be refused")

            for bad in ("not a date", "2026-13-45"):
                try:
                    asyncio.run(ops.search_archive(con, since=bad))
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"a nonsense date must be refused: {bad!r}")
        finally:
            con.close()


def test_searching_an_unsynced_chat_refuses():
    """An empty answer would read as 'they never said that' — a different statement."""
    with isolated_state():
        archive = _open()
        ops = _ops()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            try:
                asyncio.run(ops.search_archive(con, chat_id="-1009999999999"))
            except ValueError as exc:
                assert "not in the local archive" in str(exc)
            else:
                raise AssertionError("searching an unarchived chat must refuse")
        finally:
            con.close()


def test_forget_drops_the_chat_and_its_messages():
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            archive.store_messages(con, [_row(1, "x"), _row(2, "y")])
            assert archive.forget_chat(con, CHAT) is True
            assert archive.count_messages(con, CHAT) == 0
            assert archive.forget_chat(con, CHAT) is False
        finally:
            con.close()


def test_stats_and_chat_listing_report_sync_state():
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev", username="dev")
            archive.store_messages(con, [_row(1, "x")])
            archive.set_watermarks(con, CHAT, oldest=1, newest=1, complete=True)

            stats = archive.stats(con)
            assert stats["chats"] == 1
            assert stats["messages"] == 1

            assert [c["chat_id"] for c in archive.list_chats(con)] == [CHAT]
            marks = archive.get_watermarks(con, CHAT)
            assert marks["oldest"] == 1 and marks["newest"] == 1
            assert marks["complete"] is True
        finally:
            con.close()


def test_store_rejects_rows_without_a_usable_message_id():
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            for bad in ({"chat_id": CHAT}, {"chat_id": CHAT, "message_id": 0}, {"message_id": 1}):
                try:
                    archive.store_messages(con, [bad])
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"expected ValueError for {bad!r}")
        finally:
            con.close()


def test_text_is_masked_on_the_way_in():
    """A control character must not be storable, so no later read has to strip it."""
    with isolated_state():
        archive = _open()
        con = archive.open_archive()
        try:
            archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
            archive.store_messages(con, [_row(4, "a\u202eb\u0000c")])
            rows = archive.search(con, chat_id=CHAT)
            assert rows[0]["text"] == "abc"
        finally:
            con.close()


def test_archive_lives_in_the_state_dir_and_survives_reopen():
    with isolated_state() as root:
        archive = _open()
        assert archive.archive_path().parent == root

        con = archive.open_archive()
        archive.register_chat(con, chat_id=CHAT, kind="channel", title="Dev")
        archive.store_messages(con, [_row(1, "x")])
        con.close()

        again = archive.open_archive()
        try:
            assert archive.count_messages(again, CHAT) == 1
        finally:
            again.close()


def test_the_state_directory_stays_enterable_by_its_owner():
    # The directory is narrowed to 0700, not 0600: without the x bit its owner
    # cannot reach the archive inside it (root, which runs these tests, can).
    with isolated_state() as root:
        archive = _open()
        root.chmod(0o755)
        archive.open_archive().close()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(archive.archive_path().stat().st_mode) == 0o600


def test_chat_key_normalises_the_numeric_forms():
    with isolated_state():
        archive = _open()
        assert archive.chat_key("-1001234567890") == "-1001234567890"
        assert archive.chat_key(" -1001234567890 ") == "-1001234567890"
        assert archive.chat_key(-1001234567890) == "-1001234567890"
