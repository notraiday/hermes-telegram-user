"""Digest watermarks: monotonic marks and gap-aware advancement.

The invariant under test is the one that matters: a mark may only move forward,
and a fetch cut short may record a hole but must never advance past it — an
over-eager advance silently skips messages forever. Marks are keyed by scope
(the chat, or one topic of a forum), so the same invariants are checked per
topic and against the whole-chat mark they live under.
"""

import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import isolated_state, plugin_module  # noqa: E402

PEER = "123456789"
OTHER = "987654321"
THREAD = 7
SIBLING = 8


def _marks():
    return plugin_module("core.state.watermarks")


def test_unknown_peer_has_no_mark():
    with isolated_state():
        marks = _marks()
        assert marks.get_mark(PEER) is None
        assert marks.list_marks() == []
        bounds = marks.resume_bounds(PEER)
        assert bounds["min_id"] == 0
        assert bounds["max_id"] is None
        assert bounds["has_hole"] is False


def test_a_completed_walk_advances_the_mark():
    with isolated_state():
        marks = _marks()
        record = marks.advance(PEER, contiguous=100)
        assert record["contiguous"] == 100
        assert record["has_hole"] is False
        assert marks.get_mark(PEER)["contiguous"] == 100
        assert marks.resume_bounds(PEER)["min_id"] == 100


def test_a_lower_report_cannot_lower_the_mark():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        lowered = marks.advance(PEER, contiguous=50)
        assert lowered["contiguous"] == 100
        assert marks.get_mark(PEER)["contiguous"] == 100


def test_a_truncated_walk_records_a_hole_without_advancing():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        record = marks.advance(PEER, contiguous=200, top=500)

        assert record["contiguous"] == 100, "a cut-short walk must not advance the mark"
        assert record["has_hole"] is True
        assert record["pending_from_id"] == 200
        assert record["pending_top_id"] == 500

        bounds = marks.resume_bounds(PEER)
        assert bounds["min_id"] == 100
        assert bounds["max_id"] == 200, "the next fetch must close the hole first"
        assert bounds["has_hole"] is True


def test_a_smaller_page_cannot_shrink_an_open_hole():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=200, top=500)
        marks.advance(PEER, contiguous=300, top=400)

        record = marks.get_mark(PEER)
        assert record["pending_from_id"] == 200, "a hole only ever grows"
        assert record["pending_top_id"] == 500


def test_closing_the_hole_advances_and_clears_it():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=200, top=500)
        closed = marks.advance(PEER, contiguous=600)

        assert closed["contiguous"] == 600
        assert closed["has_hole"] is False
        assert closed["pending_from_id"] is None
        bounds = marks.resume_bounds(PEER)
        assert bounds["has_hole"] is False
        assert bounds["max_id"] is None


def test_marks_are_isolated_per_peer():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=10)
        marks.advance(OTHER, contiguous=20)

        assert marks.get_mark(PEER)["contiguous"] == 10
        assert marks.get_mark(OTHER)["contiguous"] == 20
        assert {row["peer_id"] for row in marks.list_marks()} == {PEER, OTHER}

        assert marks.forget_mark(PEER) is True
        assert marks.get_mark(PEER) is None
        assert marks.get_mark(OTHER)["contiguous"] == 20


def test_a_thread_mark_never_inherits_the_chat_mark():
    """A thread read must not be floored at a chat-wide mark it never reached.

    If it were, messages that arrived only in that thread would sit below the
    floor and never be digested — silently, and with no way to notice.
    """
    with isolated_state():
        marks = _marks()
        marks.set_mark(PEER, contiguous=900)

        assert marks.get_mark(PEER, "7") is None
        bounds = marks.resume_bounds(PEER, "7")
        assert bounds["min_id"] == 0, "a thread with no mark of its own starts clean"
        assert bounds["contiguous"] == 0
        assert bounds["has_hole"] is False

        marks.set_mark(PEER, contiguous=10, thread_id="7")
        assert marks.get_mark(PEER)["contiguous"] == 900, "the chat mark must not move"
        assert marks.get_mark(PEER, "7")["contiguous"] == 10

        # ...and the reverse direction holds too: advancing the chat leaves the thread alone
        marks.set_mark(PEER, contiguous=1500)
        assert marks.get_mark(PEER, "7")["contiguous"] == 10


def test_forget_all_clears_every_mark():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=1)
        marks.advance(OTHER, contiguous=2)
        assert marks.forget_all() == 2
        assert marks.list_marks() == []
        assert marks.forget_mark(PEER) is False


def test_state_is_persisted_and_reloaded():
    with isolated_state() as root:
        marks = _marks()
        marks.advance(PEER, contiguous=42)

        path = root / "digest_watermarks.json"
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["marks"][PEER]["contiguous"] == 42

        # simulate a fresh process so the file is read back rather than the cache
        importlib.reload(marks)
        assert marks.get_mark(PEER)["contiguous"] == 42


def test_a_corrupt_file_degrades_to_no_marks_instead_of_raising():
    with isolated_state() as root:
        marks = _marks()
        (root / "digest_watermarks.json").write_text("{not json", encoding="utf-8")
        importlib.reload(marks)

        assert marks.get_mark(PEER) is None
        assert marks.get_mark(PEER, THREAD) is None
        assert marks.list_marks() == []
        # and the store still works afterwards
        marks.advance(PEER, contiguous=5)
        assert marks.get_mark(PEER)["contiguous"] == 5
        marks.advance(PEER, contiguous=6, thread_id=THREAD)
        assert marks.get_mark(PEER, THREAD)["contiguous"] == 6
        assert marks.get_mark(PEER)["contiguous"] == 5


def test_unusable_peer_ids_and_bounds_are_refused():
    with isolated_state():
        marks = _marks()
        for bad in ("not-a-name", "", None, 1.5, "@channel"):
            try:
                marks.advance(bad, contiguous=1)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for peer id {bad!r}")

        try:
            marks.advance(PEER, contiguous=10, top=1)
        except ValueError:
            pass
        else:
            raise AssertionError("top below contiguous must be refused")


def test_the_lock_serialises_two_writers():
    with isolated_state():
        marks = _marks()
        state = {"inside": 0, "peak": 0}

        async def worker():
            async with marks.watermark_lock(PEER):
                state["inside"] += 1
                state["peak"] = max(state["peak"], state["inside"])
                await asyncio.sleep(0.02)
                state["inside"] -= 1

        async def run():
            await asyncio.gather(*(worker() for _ in range(4)))

        asyncio.run(run())
        assert state["peak"] == 1, "the per-peer lock must not admit two writers at once"


def test_the_lock_lets_different_scopes_write_concurrently():
    with isolated_state():
        marks = _marks()
        state = {"inside": 0, "peak": 0}

        async def worker(thread_id):
            async with marks.watermark_lock(PEER, thread_id):
                state["inside"] += 1
                state["peak"] = max(state["peak"], state["inside"])
                # both scopes must be able to hold their lock at the same time
                for _ in range(500):
                    if state["inside"] == 2:
                        break
                    await asyncio.sleep(0.001)
                state["inside"] -= 1

        async def run():
            await asyncio.gather(worker(None), worker(THREAD))

        asyncio.run(run())
        assert state["peak"] == 2, "a chat and its topic are separate scopes with separate locks"


def test_a_thread_mark_is_isolated_from_the_whole_chat_mark():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        thread = marks.advance(PEER, contiguous=10, thread_id=THREAD)

        assert thread["peer_id"] == PEER
        assert thread["thread_id"] == "7", "a topic id is canonicalised to a string"
        assert marks.get_mark(PEER, THREAD)["contiguous"] == 10
        assert marks.get_mark(PEER)["contiguous"] == 100, "a topic's mark is not the chat's"
        assert marks.get_mark(PEER)["thread_id"] is None

        # and the other direction: a chat-wide walk must not move a topic
        marks.advance(PEER, contiguous=300)
        assert marks.get_mark(PEER, THREAD)["contiguous"] == 10
        assert marks.get_mark(PEER)["contiguous"] == 300


def test_sibling_threads_do_not_share_a_mark():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=10, thread_id=THREAD)
        marks.advance(PEER, contiguous=20, thread_id=SIBLING)

        assert marks.get_mark(PEER, THREAD)["contiguous"] == 10
        assert marks.get_mark(PEER, SIBLING)["contiguous"] == 20

        assert marks.forget_mark(PEER, THREAD) is True
        assert marks.get_mark(PEER, THREAD) is None
        assert marks.forget_mark(PEER, THREAD) is False
        assert marks.get_mark(PEER, SIBLING)["contiguous"] == 20


def test_a_hole_is_recorded_and_closed_per_thread():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=200, top=500, thread_id=THREAD)

        whole = marks.resume_bounds(PEER)
        assert whole["thread_id"] is None
        assert whole["has_hole"] is False, "a topic's hole must not bound the whole chat"

        bounds = marks.resume_bounds(PEER, THREAD)
        assert bounds["thread_id"] == "7"
        assert bounds["min_id"] == 0, "the topic has no mark of its own yet"
        assert bounds["max_id"] == 200, "the topic's next fetch closes its own hole"
        assert bounds["has_hole"] is True
        assert bounds["pending_top_id"] == 500

        closed = marks.advance(PEER, contiguous=250, thread_id=THREAD)
        assert closed["contiguous"] == 500, "the topic's hole closes at the top it saw"
        assert closed["has_hole"] is False
        assert marks.resume_bounds(PEER, THREAD)["has_hole"] is False
        assert marks.get_mark(PEER)["contiguous"] == 100, "the chat-wide mark is untouched"


def test_list_marks_reports_both_halves_of_every_scope_in_order():
    with isolated_state():
        marks = _marks()
        marks.advance(OTHER, contiguous=5, thread_id=THREAD)
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=11, thread_id=SIBLING)
        marks.advance(PEER, contiguous=10, thread_id=THREAD)

        rows = marks.list_marks()
        assert [(row["peer_id"], row["thread_id"]) for row in rows] == [
            (PEER, None),
            (PEER, "7"),
            (PEER, "8"),
            (OTHER, "7"),
        ]
        assert all("has_hole" in row and "updated_at" in row for row in rows)


def test_forget_all_counts_threads_as_marks_of_their_own():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=1)
        marks.advance(PEER, contiguous=2, thread_id=THREAD)
        marks.advance(PEER, contiguous=3, thread_id=SIBLING)

        assert marks.forget_all() == 3
        assert marks.list_marks() == []
        assert marks.get_mark(PEER) is None
        assert marks.get_mark(PEER, THREAD) is None


def test_thread_marks_are_stored_under_a_composite_key():
    with isolated_state() as root:
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=10, thread_id=THREAD)

        payload = json.loads((root / "digest_watermarks.json").read_text(encoding="utf-8"))
        assert set(payload["marks"]) == {PEER, f"{PEER}:7"}
        assert payload["marks"][f"{PEER}:7"]["contiguous"] == 10
        assert payload["marks"][PEER]["contiguous"] == 100


def test_a_pre_thread_file_still_loads_and_stays_a_whole_chat_mark():
    with isolated_state() as root:
        marks = _marks()
        path = root / "digest_watermarks.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "marks": {
                        PEER: {
                            "contiguous": 42,
                            "pending_from_id": None,
                            "pending_top_id": None,
                            "updated_at": None,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        importlib.reload(marks)

        assert marks.get_mark(PEER)["contiguous"] == 42
        assert marks.get_mark(PEER)["thread_id"] is None
        assert marks.get_mark(PEER, THREAD) is None, "an old mark must not leak into topics"
        assert marks.list_marks()[0]["thread_id"] is None

        marks.advance(PEER, contiguous=50)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["marks"][PEER]["contiguous"] == 50, "a chat mark keeps its pre-thread key"
        assert set(payload["marks"]) == {PEER}


def test_malformed_stored_keys_are_dropped_and_valid_ones_are_kept():
    with isolated_state() as root:
        marks = _marks()
        good = {"contiguous": 42, "pending_from_id": None, "pending_top_id": None, "updated_at": None}
        (root / "digest_watermarks.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "marks": {
                        PEER: good,  # a pre-thread whole-chat mark
                        f"{PEER}:7": good,  # one topic
                        f"{PEER}:0": good,  # zero is the whole chat, never a topic of its own
                        f"{PEER}:-1": good,  # a negative topic is nonsense
                        f"{PEER}:abc": good,  # not an id
                        f"{PEER}:7:8": good,  # two separators
                        f"{PEER}:8": "not a mark",  # a valid key with an unusable value
                        "not-a-peer": good,
                        "": good,
                    },
                }
            ),
            encoding="utf-8",
        )
        importlib.reload(marks)

        rows = marks.list_marks()
        assert [(row["peer_id"], row["thread_id"]) for row in rows] == [(PEER, None), (PEER, "7")]
        assert marks.get_mark(PEER, 0)["contiguous"] == 42, "0 still means the whole chat"
        assert marks.get_mark(PEER, SIBLING) is None
        assert marks.get_mark(PEER, 7)["contiguous"] == 42


def test_a_composite_key_can_never_be_read_as_a_peer():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=10, thread_id=THREAD)
        for bad in (f"{PEER}:{THREAD}", f"{PEER}:-1", f"{THREAD}:{PEER}"):
            try:
                marks.get_mark(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"a composite key must not be a peer id: {bad!r}")


def test_thread_ids_are_canonicalised_and_refused_when_unusable():
    with isolated_state():
        marks = _marks()
        for empty in (None, 0, "0", "", "   "):
            marks.forget_all()
            marks.advance(PEER, contiguous=5, thread_id=empty)
            assert marks.get_mark(PEER)["contiguous"] == 5, f"{empty!r} means the whole chat"
            assert marks.get_mark(PEER)["thread_id"] is None
            assert len(marks.list_marks()) == 1

        for same in (THREAD, "7", "007", " 7 "):
            marks.forget_all()
            marks.advance(PEER, contiguous=10, thread_id=same)
            assert marks.get_mark(PEER, THREAD)["contiguous"] == 10, f"{same!r} is a topic id"
            assert marks.get_mark(PEER) is None

        for bad in (-1, "junk", 1.5, True, 2**31):
            try:
                marks.advance(PEER, contiguous=1, thread_id=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for thread id {bad!r}")


def test_set_mark_asserts_coverage_and_cannot_lower_it():
    with isolated_state():
        marks = _marks()
        assert marks.get_mark(PEER) is None

        record = marks.set_mark(PEER, contiguous=50)
        assert record["peer_id"] == PEER
        assert record["thread_id"] is None
        assert record["contiguous"] == 50
        assert record["has_hole"] is False
        assert marks.get_mark(PEER)["contiguous"] == 50
        assert marks.resume_bounds(PEER)["min_id"] == 50

        lowered = marks.set_mark(PEER, contiguous=40)
        assert lowered["contiguous"] == 50, "an assertion can never move the mark back"
        assert marks.get_mark(PEER)["contiguous"] == 50


def test_set_mark_clears_a_hole_and_never_jumps_to_its_top():
    with isolated_state():
        marks = _marks()
        marks.advance(PEER, contiguous=100)
        marks.advance(PEER, contiguous=200, top=500)
        # the whole chat has a hole open at 200..500; the assertion covers 150
        record = marks.set_mark(PEER, contiguous=150)

        assert record["contiguous"] == 150, "the asserted id, never the hole's top"
        assert record["has_hole"] is False
        assert record["pending_top_id"] is None
        bounds = marks.resume_bounds(PEER)
        assert bounds["min_id"] == 150
        assert bounds["max_id"] is None
        assert bounds["has_hole"] is False

        # a topic's hole is cleared by an assertion about that topic only
        marks.advance(PEER, contiguous=400, top=900, thread_id=THREAD)
        topic = marks.set_mark(PEER, contiguous=450, thread_id=THREAD)
        assert topic["contiguous"] == 450
        assert marks.resume_bounds(PEER, THREAD)["has_hole"] is False
        assert marks.get_mark(PEER)["contiguous"] == 150, "the chat-wide mark is untouched"


def test_set_mark_is_per_thread():
    with isolated_state():
        marks = _marks()
        marks.set_mark(PEER, contiguous=30, thread_id=THREAD)
        marks.set_mark(PEER, contiguous=70)

        assert marks.get_mark(PEER, THREAD)["contiguous"] == 30
        assert marks.get_mark(PEER, THREAD)["thread_id"] == "7"
        assert marks.get_mark(PEER)["contiguous"] == 70
        assert marks.get_mark(PEER, SIBLING) is None

        marks.set_mark(PEER, contiguous=80, thread_id=SIBLING)
        assert marks.get_mark(PEER, SIBLING)["contiguous"] == 80
        assert marks.get_mark(PEER, THREAD)["contiguous"] == 30
        assert marks.get_mark(PEER)["contiguous"] == 70


def test_set_mark_refuses_values_that_assert_nothing():
    with isolated_state():
        marks = _marks()
        for bad in (0, -1, "junk", 1.5, -0.5, float("inf"), float("nan"), True, None, ""):
            try:
                marks.set_mark(PEER, contiguous=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for contiguous={bad!r}")
        assert marks.get_mark(PEER) is None, "a refused assertion must not write a mark"

        # a whole number is a whole number, however it is spelled
        assert marks.set_mark(PEER, contiguous=2.0)["contiguous"] == 2
        assert marks.set_mark(PEER, contiguous="3")["contiguous"] == 3

        for bad_peer in ("not-a-name", None):
            try:
                marks.set_mark(bad_peer, contiguous=1)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for peer id {bad_peer!r}")

        try:
            marks.set_mark(PEER, contiguous=1, thread_id="junk")
        except ValueError:
            pass
        else:
            raise AssertionError("a bad thread id must be refused")


def test_a_repeated_assertion_writes_nothing():
    with isolated_state() as root:
        marks = _marks()
        marks.set_mark(PEER, contiguous=50)
        path = root / "digest_watermarks.json"
        before = path.read_text(encoding="utf-8")

        marks.set_mark(PEER, contiguous=50)
        marks.set_mark(PEER, contiguous=10)
        assert path.read_text(encoding="utf-8") == before, "a no-op must not touch the file"


def test_a_write_by_another_process_is_neither_missed_nor_erased():
    """The gateway, the dashboard and the CLI share the file: none may save a stale copy."""
    import json as _json

    from _plugin_support import isolated_state, plugin_module

    with isolated_state():
        marks = plugin_module("core.state.watermarks")
        marks.set_mark("100", contiguous=5)  # this process now holds a cached copy
        path = marks._path()
        data = _json.loads(path.read_text(encoding="utf-8"))
        data["marks"]["200"] = {"contiguous": 9, "pending_from_id": None,
                                "pending_top_id": None, "updated_at": None}
        path.write_text(_json.dumps(data, indent=4), encoding="utf-8")  # "another process"

        assert marks.get_mark("200")["contiguous"] == 9  # seen, not served from the stale copy
        marks.set_mark("300", contiguous=3)
        stored = _json.loads(path.read_text(encoding="utf-8"))["marks"]
        assert set(stored) == {"100", "200", "300"}  # and not erased by this process's save
        assert path.with_name(path.name + ".lock").exists()


def test_marks_of_groups_channels_and_new_user_ids_survive_a_restart():
    """Peer ids are not message ids: negative and > 2**31 ids must load back."""
    from _plugin_support import isolated_state, plugin_module

    with isolated_state():
        marks = plugin_module("core.state.watermarks")
        peers = ("-50", "-1001234567890", "6123456789", "777000")
        for peer in peers:
            marks.set_mark(peer, contiguous=42)
        marks.set_mark("-1001234567890", contiguous=7, thread_id=3)
        marks._cache = None  # what a fresh process sees: only the file
        marks._CACHE_STAMP = None
        for peer in peers:
            assert (marks.get_mark(peer) or {}).get("contiguous") == 42, peer
        assert marks.get_mark("-1001234567890", 3)["contiguous"] == 7
