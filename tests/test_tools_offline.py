"""Tool-level behaviour that can be verified without Telegram and without telethon.

Covers three things worth pinning:
  * the registration contract — 29 tools, the right toolset, usable schemas;
  * every handler that decides *before* touching Telegram returns a structured
    error rather than raising, so a model can always read the failure;
  * the marking contract — the badge and the local mark move together, and a
    failed acknowledgement moves neither.
"""

import asyncio
import contextlib
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import account_env, isolated_state, plugin_module  # noqa: E402

EXPECTED_TOOLSET = "telegram_user"
EXPECTED_TOOL_COUNT = 54

# handler name -> substring the structured error must contain
GUARDED_HANDLERS = {
    "_tg_find_chat": "query is required",
    "_tg_list_topics": "chat is required",
    "_tg_read_messages": "chat is required",
    "_tg_get_message_context": "message_id must be an integer",
    "_tg_search_messages": "chat and query are required",
    "_tg_search_global": "query is required",
    "_tg_read_folder": "folder is required",
    "_tg_download_media": "message_id must be an integer",
    "_tg_transcribe_voice": "message_id must be an integer",
    "_tg_participants": "chat is required",
    "_tg_set_alias": "alias and chat are required",
    "_tg_delete_alias": "alias is required",
    "_tg_get_pinned": "chat is required",
    "_tg_get_scheduled": "chat is required",
    "_tg_get_profile": "target is required",
    "_tg_archive_sync": "chat is required",
    "_tg_archive_forget": "chat is required",
    "_tg_archive_search": "at least one of chat or query is required",
    "_tg_search_media": "global media search requires kind or query",
    "_tg_mark_summarized": "chat is required",
    "_tg_save_collection": "name is required",
    "_tg_set_collection_brief": "name is required",
    "_tg_delete_collection": "name is required",
    "_tg_read_collection": "collection is required",
}

# handlers that answer from local state only
LOCAL_HANDLERS = (
    "_tg_list_aliases",
    "_tg_list_digest_marks",
    "_tg_forget_digest_marks",
    "_tg_archive_status",
    "_tg_list_collections",
)

# tools that read Telegram content, so they must carry the injection warning
CONTENT_TOOLS = (
    "tg_read_messages",
    "tg_get_message_context",
    "tg_search_messages",
    "tg_search_global",
    "tg_get_unread",
    "tg_read_folder",
    "tg_search_media",
    "tg_get_pinned",
    "tg_get_drafts",
    "tg_get_scheduled",
    "tg_get_profile",
    "tg_archive_sync",
    "tg_archive_search",
    "tg_read_collection",
)

PEER = "555"


def _tools():
    return plugin_module("tools")


def _marks():
    return plugin_module("core.state.watermarks")


def _run(coro):
    return asyncio.run(coro)


def test_registration_contract():
    tools = _tools()

    class Ctx:
        def __init__(self):
            self.tools = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

    ctx = Ctx()
    tools.register_tools(ctx)

    names = [entry["name"] for entry in ctx.tools]
    assert len(names) == EXPECTED_TOOL_COUNT
    assert len(set(names)) == EXPECTED_TOOL_COUNT
    assert {entry["toolset"] for entry in ctx.tools} == {EXPECTED_TOOLSET}
    assert all(entry["is_async"] for entry in ctx.tools)
    assert all(entry["requires_env"] for entry in ctx.tools)
    for entry in ctx.tools:
        assert inspect.iscoroutinefunction(entry["handler"]), (
            f"{entry['name']} is registered with a non-coroutine handler"
        )

    for entry in ctx.tools:
        schema = entry["schema"]
        assert schema["name"] == entry["name"]
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"
        assert isinstance(schema["parameters"]["properties"], dict)
        if "required" in schema["parameters"]:
            declared = set(schema["parameters"]["properties"])
            assert set(schema["parameters"]["required"]) <= declared


def test_the_read_only_guard_text_is_attached_where_it_matters():
    tools = _tools()
    by_name = {name: desc for name, desc, _handler, _params in tools._TOOL_DEFS}
    for name in CONTENT_TOOLS:
        assert "untrusted data" in by_name[name], f"{name} is missing the untrusted-data warning"


def test_handlers_refuse_bad_input_without_raising():
    tools = _tools()
    with isolated_state():
        for handler_name, needle in GUARDED_HANDLERS.items():
            raw = _run(getattr(tools, handler_name)({}))
            payload = json.loads(raw)
            assert "error" in payload, f"{handler_name} should refuse an empty call"
            assert needle in payload["error"], f"{handler_name}: {payload['error']!r}"


def test_local_handlers_answer_offline():
    tools = _tools()
    with isolated_state():
        for handler_name in LOCAL_HANDLERS:
            payload = json.loads(_run(getattr(tools, handler_name)({})))
            assert "error" not in payload, f"{handler_name}: {payload}"


def test_ip_network_is_coarsened_and_never_echoed_in_full():
    tools = _tools()
    assert tools._ip_net("203.0.113.77") == "203.0.0.0"
    assert tools._ip_net("10.0.0.1") == "10.0.0.0"
    assert tools._ip_net("2a02:1234:5678:9abc::1") == "2a02:1234:5678::"
    for bad in ("not-an-ip", "", None, 5):
        assert tools._ip_net(bad) is None


@contextlib.contextmanager
def _faked_marking(tools, *, fail_ack=False, topic_id=42):
    """Replace the Telegram boundary so the handler's own ordering is what runs."""
    names = (
        "tool_client",
        "resolve_chat",
        "find_topic_root",
        "acknowledge_read",
        "peer_id",
        "entity_label",
    )
    originals = {name: getattr(tools, name) for name in names}
    seen = {"acks": [], "client": object()}

    @contextlib.asynccontextmanager
    async def fake_tool_client():
        yield seen["client"]

    async def fake_resolve_chat(_client, _chat, **_kwargs):
        return "ENTITY"

    async def fake_find_topic(_client, _entity, _topic):
        return topic_id

    async def fake_acknowledge(_client, _entity, *, topic_id=None, up_to=None):
        seen["acks"].append({"topic_id": topic_id, "up_to": up_to})
        if fail_ack:
            raise RuntimeError("telegram refused")
        return {"scope": "topic" if topic_id else "chat", "topic_id": topic_id, "up_to": up_to}

    tools.tool_client = fake_tool_client
    tools.resolve_chat = fake_resolve_chat
    tools.find_topic_root = fake_find_topic
    tools.acknowledge_read = fake_acknowledge
    tools.peer_id = lambda _entity: PEER
    tools.entity_label = lambda _entity: "TestChat"
    try:
        yield seen
    finally:
        for name, value in originals.items():
            setattr(tools, name, value)


def test_marking_moves_the_badge_and_the_mark_together():
    tools = _tools()
    marks = _marks()
    with account_env(), isolated_state():
        with _faked_marking(tools) as seen:
            payload = json.loads(_run(tools._tg_mark_summarized({"chat": "c", "up_to": 50, "acknowledge": True})))

        assert payload["up_to"] == 50
        assert payload["mark"] == 50
        assert payload["previous_mark"] == 0
        assert payload["acknowledged"]["up_to"] == 50
        assert seen["acks"] == [{"topic_id": None, "up_to": 50}]
        assert marks.get_mark(PEER)["contiguous"] == 50


def test_a_failed_acknowledgement_moves_nothing():
    """The acknowledgement runs first: if it fails, the mark must not have moved.

    Otherwise the next digest would start after these messages while the owner's
    badge still burns — messages lost silently, which is the failure this whole
    design exists to avoid.
    """
    tools = _tools()
    marks = _marks()
    with account_env(), isolated_state():
        marks.set_mark(PEER, contiguous=10)

        with _faked_marking(tools, fail_ack=True):
            payload = json.loads(_run(tools._tg_mark_summarized({"chat": "c", "up_to": 50, "acknowledge": True})))

        assert "error" in payload
        assert marks.get_mark(PEER)["contiguous"] == 10, (
            "the mark moved even though the badge was never cleared"
        )


def test_marking_with_acknowledge_false_leaves_the_badge_alone():
    tools = _tools()
    marks = _marks()
    with isolated_state():
        with _faked_marking(tools) as seen:
            payload = json.loads(
                _run(tools._tg_mark_summarized({"chat": "c", "up_to": 30, "acknowledge": False}))
            )
        assert seen["acks"] == [], "no read acknowledgement may be sent"
        assert payload["acknowledged"] is None
        assert payload["mark"] == 30
        assert marks.get_mark(PEER)["contiguous"] == 30


def test_marking_without_a_position_refuses_to_guess():
    """Falling back to 'the whole chat' would clear a badge for unsummarised text."""
    tools = _tools()
    marks = _marks()
    with isolated_state():
        with _faked_marking(tools) as seen:
            payload = json.loads(_run(tools._tg_mark_summarized({"chat": "c"})))
        assert "no recorded summary position" in payload["error"]
        assert seen["acks"] == []
        assert marks.get_mark(PEER) is None


def test_marking_without_a_position_uses_the_recorded_one():
    tools = _tools()
    marks = _marks()
    with account_env(), isolated_state():
        marks.set_mark(PEER, contiguous=77)
        with _faked_marking(tools) as seen:
            payload = json.loads(_run(tools._tg_mark_summarized({"chat": "c", "acknowledge": True})))
        assert payload["up_to"] == 77
        assert seen["acks"] == [{"topic_id": None, "up_to": 77}]


def test_marking_a_thread_uses_the_thread_scope():
    tools = _tools()
    marks = _marks()
    with account_env(), isolated_state():
        marks.set_mark(PEER, contiguous=100)  # the whole-chat mark
        with _faked_marking(tools, topic_id=42) as seen:
            payload = json.loads(
                _run(tools._tg_mark_summarized({"chat": "c", "topic": "general", "up_to": 20, "acknowledge": True}))
            )

        assert payload["thread_id"] == "42"
        assert seen["acks"] == [{"topic_id": 42, "up_to": 20}]
        assert marks.get_mark(PEER, "42")["contiguous"] == 20
        assert marks.get_mark(PEER)["contiguous"] == 100, (
            "marking one thread must not move the whole-chat mark"
        )


@contextlib.contextmanager
def _faked_unread(tools, scopes):
    """Replace the Telegram boundary for a collection-scoped unread read."""
    names = (
        "tool_client",
        "select_scopes",
        "_read_messages",
        "dialog_summary",
        "dialog_waiting",
        "dialog_is_muted",
    )
    originals = {name: getattr(tools, name) for name in names}
    seen = {"fetch": [], "collection": None}

    @contextlib.asynccontextmanager
    async def fake_tool_client():
        yield object()

    async def fake_select_scopes(_client, name):
        seen["collection"] = name
        stored = plugin_module("core.state.collections").get_collection(name) or {}
        return {
            "name": name,
            "brief": stored.get("brief", ""),
            "members": stored.get("members") or [],
        }, list(scopes)

    async def fake_read(_client, _entity, *, limit, since=None, until=None, **kwargs):
        seen["fetch"].append(dict(kwargs))
        return [{"id": 1, "out": False, "text": "hello"}]

    tools.tool_client = fake_tool_client
    tools.select_scopes = fake_select_scopes
    tools._read_messages = fake_read
    tools.dialog_summary = lambda _dialog: {"id": "111", "name": "Forum"}
    tools.dialog_waiting = lambda _dialog: True
    tools.dialog_is_muted = lambda _dialog: False
    try:
        yield seen
    finally:
        for name, value in originals.items():
            setattr(tools, name, value)


class _FakeDialog:
    """A dialog carrying a chat-wide read pointer, as a real forum dialog does."""

    class _Raw:
        read_inbox_max_id = 5
        top_message = 9
        unread_mark = False

    def __init__(self):
        self.dialog = self._Raw()
        self.entity = "ENTITY"
        self.name = "Forum"
        self.id = 111


def test_unread_of_a_collection_honours_thread_scopes():
    """The same collection must not mean two different things in two tools.

    tg_read_collection reads a thread member through its own topic. If
    tg_get_unread fell back to whole-chat dialogs for the same collection it
    would silently widen the scope, with nothing in the payload to say so.
    """
    tools = _tools()
    with isolated_state():
        dialog = _FakeDialog()
        with _faked_unread(tools, [(dialog, None), (dialog, 7)]) as seen:
            payload = json.loads(_run(tools._tg_get_unread({"collection": "C"})))

        assert seen["collection"] == "C"
        whole_chat, thread = seen["fetch"][0], seen["fetch"][1]

        assert whole_chat.get("reply_to") is None, "a whole-chat member reads openly"
        assert whole_chat.get("min_id") == 5, "a chat member is floored at the chat read pointer"

        assert thread["reply_to"] == 7, "a thread member reads within its topic"
        assert "min_id" not in thread, (
            "a thread must not be floored by the chat-wide read pointer: that pointer can "
            "sit above messages the topic never delivered, and the thread would then report "
            "itself empty while its badge is still lit"
        )

        assert [entry.get("thread_id") for entry in payload["chats"]] == [None, "7"]


def test_unread_of_a_folder_still_reads_whole_dialogs():
    """The folder path must keep its old shape: no scopes, no thread ids."""
    tools = _tools()
    with isolated_state():
        dialog = _FakeDialog()

        @contextlib.asynccontextmanager
        async def fake_tool_client():
            yield object()

        async def fake_select_dialogs(_client, _token):
            return None, [dialog]

        originals = {n: getattr(tools, n) for n in ("tool_client", "_select_dialogs",
                                                    "_read_messages", "dialog_summary",
                                                    "dialog_waiting", "dialog_is_muted")}
        fetched = []
        tools.tool_client = fake_tool_client
        tools._select_dialogs = fake_select_dialogs
        tools.dialog_summary = lambda _d: {"id": "111"}
        tools.dialog_waiting = lambda _d: True
        tools.dialog_is_muted = lambda _d: False

        async def fake_read(_client, _entity, *, limit, since=None, until=None, **kwargs):
            fetched.append(dict(kwargs))
            return [{"id": 1, "out": False}]

        tools._read_messages = fake_read
        try:
            payload = json.loads(_run(tools._tg_get_unread({})))
        finally:
            for name, value in originals.items():
                setattr(tools, name, value)

        assert fetched[0].get("reply_to") is None
        assert "thread_id" not in payload["chats"][0]
    tools = _tools()
    with isolated_state():
        with _faked_marking(tools) as seen:
            for bad in (0, -3, "later"):
                payload = json.loads(_run(tools._tg_mark_summarized({"chat": "c", "up_to": bad})))
                assert "up_to must be" in payload["error"]
        assert seen["acks"] == []


def _digest_payload(chats: int, per_chat: int) -> dict[str, Any]:
    """The shape ``tg_read_folder`` returns, sized like the production one."""
    return {
        "folder": {"id": 3, "title": "it/ai"},
        "chat_count": chats,
        "read_receipts_sent": False,
        "since_last_digest": True,
        "chats": [
            {
                "id": f"-100{index}",
                "name": f"chat {index}",
                "unread": index,
                "messages": [
                    {"id": number, "date": "2026-09-30T00:00:00+00:00", "text": "x" * 700}
                    for number in range(per_chat)
                ],
            }
            for index in range(chats)
        ],
    }


def test_an_oversized_digest_is_trimmed_instead_of_spilled():
    """A folder-wide digest has to fit in one turn.

    41 chats x 50 messages is what ``tg_read_folder`` actually returned: 299 KB,
    past Hermes' spillover threshold, which hands the model a stub instead of the
    data — it then writes code to read the spilled file and spends the turn
    recovering what it had already asked for. Trimming here is the difference
    between one call and that hunt.
    """
    payload = _digest_payload(chats=41, per_chat=50)
    assert len(json.dumps(payload)) > 300_000, "the fixture must reproduce the real size"

    tools = _tools()
    with isolated_state():
        text = tools._json(payload)
        parsed = json.loads(text)
        budget = tools._RESULT_BUDGET_CHARS

    assert len(text) <= budget, f"still {len(text)} chars, over the {budget} budget"
    assert parsed["result_budget"]["trimmed"] is True
    assert parsed["folder"] == {"id": 3, "title": "it/ai"}
    assert parsed["chat_count"] == 41, "the summary is what the caller came for"

    first = parsed["chats"][0]
    assert first["name"] == "chat 0"
    assert first["unread"] == 0
    assert len(first["messages"]) == tools._TRIMMED_MESSAGES_PER_CHAT
    assert first["messages_omitted"] == 50 - tools._TRIMMED_MESSAGES_PER_CHAT
    assert first["messages"][-1]["id"] == 49, "the newest messages are the ones kept"


def test_a_result_that_fits_is_returned_untouched():
    """The budget is a floor on what survives, not a rewrite of every answer."""
    payload = {"chat": "it/ai", "messages": [{"id": 1, "text": "hello"}]}
    tools = _tools()
    with isolated_state():
        assert json.loads(tools._json(payload)) == payload


def test_a_payload_the_trimmer_cannot_shrink_reports_instead_of_mangling():
    """Last resort: a valid error beats invalid JSON or a silent cut."""
    payload = {"odd_shape": ["x" * 300] * 400}
    tools = _tools()
    with isolated_state():
        text = tools._json(payload)
        parsed = json.loads(text)

    assert len(text) <= tools._RESULT_BUDGET_CHARS
    assert "error" in parsed
    assert "hint" in parsed


def test_summary_only_folder_read_never_fetches_messages():
    """``messages_per_chat=0`` answers "what is in this folder" without the chats.

    Without it the only folder-wide call dragged every message along — 41 chats,
    299 KB — and the turn received a spillover stub instead of a chat list.
    """
    tools = _tools()
    with isolated_state():
        # More chats than the message-oriented default of 30: the summary-only form
        # has to report the folder, not the first 30 of it.
        dialogs = [_FakeDialog() for _ in range(41)]
        originals = {
            name: getattr(tools, name)
            for name in ("tool_client", "_select_dialogs", "_read_messages", "dialog_summary")
        }

        @contextlib.asynccontextmanager
        async def fake_tool_client():
            yield object()

        class _Folder:
            id = 3
            title = "it/ai"

        async def fake_select_dialogs(_client, _token):
            return _Folder(), dialogs

        async def explode(*_args, **_kwargs):
            raise AssertionError("messages_per_chat=0 must not read any messages")

        tools.tool_client = fake_tool_client
        tools._select_dialogs = fake_select_dialogs
        tools._read_messages = explode
        tools.dialog_summary = lambda dialog: {"id": str(id(dialog)), "name": "chat"}
        try:
            payload = json.loads(
                _run(tools._tg_read_folder({"folder": "it/ai", "messages_per_chat": 0}))
            )
        finally:
            for name, value in originals.items():
                setattr(tools, name, value)

    assert len(payload["chats"]) == 41, "the whole folder, not the first 30 of it"
    assert all(chat["messages"] == [] for chat in payload["chats"])


def test_an_emptied_telegram_error_still_names_the_failure():
    """An error the model cannot read is one it retries.

    GetForumTopicsRequest on a non-forum chat came back as
    ``" (caused by GetForumTopicsRequest)"`` and the model called the tool seven
    times in a single turn, because there was nothing in the error to act on.
    """
    limits = plugin_module("core.limits")
    with isolated_state():
        blank = limits.telegram_error_message(Exception(""))
        caused = limits.telegram_error_message(Exception(" (caused by GetForumTopicsRequest)"))
        plain = limits.telegram_error_message(Exception("chat not found"))

    assert blank == "Telegram returned no error text for this call (Exception)"
    assert "GetForumTopicsRequest" in caused, "the only useful part must survive"
    assert plain == "chat not found", "a real message is passed through untouched"


def test_list_topics_answers_for_a_chat_that_is_not_a_forum():
    """The tool must not issue an RPC whose failure it cannot explain.

    ``GetForumTopicsRequest`` fails on every chat that is not a forum, and that
    failure carries no usable text. The entity is already resolved here, so the
    answer comes from it instead of from a doomed round trip.
    """
    tools = _tools()
    with isolated_state():

        class _NotAForum:
            first_name = "Al"
            last_name = "st0r"

        class _Client:
            def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("no RPC may be issued for a non-forum chat")

        originals = {
            name: getattr(tools, name) for name in ("tool_client", "resolve_chat", "entity_label")
        }

        @contextlib.asynccontextmanager
        async def fake_tool_client():
            yield _Client()

        async def fake_resolve(_client, _chat):
            return _NotAForum()

        tools.tool_client = fake_tool_client

        def fake_label(_entity):
            return "Al st0r"

        for name, value in (("resolve_chat", fake_resolve), ("entity_label", fake_label)):
            setattr(tools, name, value)
        try:
            payload = json.loads(_run(tools._tg_list_topics({"chat": "Al"})))
        finally:
            for name, value in originals.items():
                setattr(tools, name, value)

    assert payload["topics"] == []
    assert "private chat" in payload["error"]
    assert tools._is_forum(_NotAForum()) is False


def test_an_ambiguous_chat_names_the_matches_it_found():
    """``ambiguous`` alone is an instruction to guess again.

    In production the model asked for a chat by name, got
    ``{"error": "Telegram chat is ambiguous: тест"}`` after a 29-second scan, and
    had nothing to choose between — so it retried with another name. Naming the
    matches is what makes the error actionable.
    """
    helpers = plugin_module("core.helpers")

    class _Entity:
        def __init__(self, ident: int) -> None:
            self.id = ident
            self.username = None

    class _Dialog:
        def __init__(self, name: str, ident: int) -> None:
            self.name = name
            self.entity = _Entity(ident)

    class _Client:
        async def get_entity(self, _raw: Any) -> Any:
            raise ValueError("not a username")

        def iter_dialogs(self) -> Any:
            # Exact matches win over partial ones, so ambiguity needs two of them.
            dialogs = [_Dialog("тест", 111), _Dialog("тест", 222)]

            async def _generate():
                for dialog in dialogs:
                    yield dialog

            return _generate()

    with isolated_state():
        try:
            _run(helpers.resolve_chat(_Client(), "тест", allow_alias=False))
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("two matches must be reported as ambiguous")

    assert "matches 2" in message
    assert "id 111" in message and "id 222" in message
    assert "will not resolve it" in message


def test_a_read_only_account_never_clears_the_badge():
    tools = _tools()
    marks = _marks()
    with account_env(mode="read"), isolated_state():
        with _faked_marking(tools) as seen:
            payload = json.loads(
                _run(tools._tg_mark_summarized({"chat": "c", "up_to": 30, "acknowledge": True}))
            )
        assert seen["acks"] == [], "a read-only account must not write read state"
        assert payload["acknowledged"] is None
        assert marks.get_mark(PEER)["contiguous"] == 30


def test_write_tools_refuse_on_a_read_only_account():
    tools = _tools()
    with account_env(mode="read"), isolated_state():
        for handler, args in (
            (tools._tg_send_message, {"chat": "c", "text": "hi"}),
            (tools._tg_forward_messages, {"from_chat": "a", "to_chat": "b", "message_ids": [1]}),
            (tools._tg_send_reaction, {"chat": "c", "message_id": 1, "reaction": ["👍"]}),
            (tools._tg_edit_message, {"chat": "c", "message_id": 1, "text": "x"}),
            (tools._tg_delete_messages, {"chat": "c", "message_ids": [1]}),
            (tools._tg_pin_message, {"chat": "c", "message_id": 1}),
            (tools._tg_mark_read, {"chat": "c", "up_to": 5}),
            (tools._tg_send_file, {"chat": "c", "path": "/tmp/x"}),
        ):
            payload = json.loads(_run(handler(args)))
            assert "read-only" in payload.get("error", ""), handler.__name__


def test_state_is_separate_per_account():
    tools = _tools()
    marks = _marks()
    accounts = plugin_module("core.accounts")
    env = {
        "HERMES_TG_USER_API_ID": "1", "HERMES_TG_USER_API_HASH": "h",
        "HERMES_TG_USER_ACCOUNTS": "one,two",
        "HERMES_TG_USER_ONE_SESSION": "s1", "HERMES_TG_USER_TWO_SESSION": "s2",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with isolated_state() as root:
            with accounts.use_account("one"):
                marks.set_mark("123", contiguous=10)
            with accounts.use_account("two"):
                assert marks.get_mark("123") is None
                marks.set_mark("123", contiguous=99)
            with accounts.use_account("one"):
                assert marks.get_mark("123")["contiguous"] == 10
            assert (root / "accounts" / "one").is_dir() and (root / "accounts" / "two").is_dir()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_account_parameter_is_added_and_routed():
    tools = _tools()

    class Ctx:
        def __init__(self):
            self.tools = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

    with account_env(name="personal", mode="read"):
        ctx = Ctx()
        tools.register_tools(ctx)
        entry = next(t for t in ctx.tools if t["name"] == "tg_accounts")
        prop = entry["schema"]["parameters"]["properties"]["account"]
        assert prop["enum"] == ["personal"]
        payload = json.loads(_run(entry["handler"]({"account": "personal"})))
        assert payload["accounts"][0]["mode"] == "read"
        bad = json.loads(_run(entry["handler"]({"account": "nope"})))
        assert "unknown Telegram account" in bad["error"]


def test_proxy_urls_become_telethon_arguments():
    accounts = plugin_module("core.accounts")
    kw = accounts.telethon_proxy_kwargs("socks5://u:p@127.0.0.1:1080")
    assert kw["proxy"] == {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080,
                           "rdns": True, "username": "u", "password": "p"}
    assert accounts.telethon_proxy_kwargs("http://h:8080")["proxy"]["proxy_type"] == "http"
    mt = accounts.telethon_proxy_kwargs("mtproxy://ee00ff@h:443")
    assert mt["proxy"] == ("h", 443, "ee00ff")
    assert accounts.telethon_proxy_kwargs(None) == {}
    assert accounts.proxy_label("socks5://u:p@h:1") == "socks5://h:1"


def test_inbox_reads_saved_private_and_collection_chats_and_marks_them():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from telethon.tl import types as tl

    tools = _tools()
    store = plugin_module("core.state.collections")

    me = tl.User(id=1, is_self=True, first_name="me")
    friend = tl.User(id=2, first_name="friend")
    stranger = tl.User(id=3, first_name="stranger")
    bot = tl.User(id=4, bot=True, first_name="bot")
    service = tl.User(id=777000, first_name="Telegram")
    group = tl.Chat(id=50, title="group", photo=tl.ChatPhotoEmpty(), participants_count=3,
                    date=datetime.now(timezone.utc), version=1)
    other_group = tl.Chat(id=60, title="other", photo=tl.ChatPhotoEmpty(), participants_count=3,
                          date=datetime.now(timezone.utc), version=1)
    history = {1: [1, 2, 3], 2: [10, 11], 3: [20], 4: [30], 777000: [40], 50: [100, 101, 102], 60: [200]}

    def dialog(entity):
        top = history[entity.id][-1] if entity.id in history else 0
        return SimpleNamespace(entity=entity, dialog=SimpleNamespace(top_message=top))

    class Client:
        async def iter_dialogs(self):
            for e in (me, friend, stranger, bot, service, group, other_group):
                yield dialog(e)

        async def iter_messages(self, entity, limit=None, min_id=0, reverse=False, **kw):
            ids = [i for i in history[entity.id] if i > (min_id or 0)]
            ids = ids if reverse else list(reversed(ids))
            for i in ids[:limit]:
                yield SimpleNamespace(id=i, date=datetime.now(timezone.utc), message=f"m{i}",
                                      out=entity.id == 1, sender=None, sender_id=None,
                                      reply_to=None, media=None, fwd_from=None, entities=None,
                                      reply_markup=None, grouped_id=None)

    @contextlib.asynccontextmanager
    async def fake_client(*a, **k):
        yield Client()

    with account_env(mode="read"), isolated_state():
        store.save_collection("watch", members=[{"peer_id": "-50", "thread": None}])
        original = tools.tool_client
        tools.tool_client = fake_client
        try:
            first = json.loads(_run(tools._tg_read_inbox({"format": "json"})))
            got = {c["chat_id"]: c for c in first["chats"]}
            assert set(got) == {"1", "2", "3", "-50"}, sorted(got)
            assert got["1"]["sources"] == ["saved"] and got["1"]["first_visit"]
            assert [m["id"] for m in got["-50"]["messages"]] == [100, 101, 102]
            assert got["2"]["up_to"] == 11

            marked = json.loads(_run(tools._tg_mark_inbox({"marks": first["marks"]})))
            assert not marked["errors"]

            history[2].append(12)
            history[50].append(103)
            second = json.loads(_run(tools._tg_read_inbox({"format": "json"})))
            got = {c["chat_id"]: [m["id"] for m in c["messages"]] for c in second["chats"]}
            assert got == {"2": [12], "-50": [103]}, got
        finally:
            tools.tool_client = original


def test_saved_messages_words_never_resolve_to_a_public_username():
    from types import SimpleNamespace

    helpers = plugin_module("core.helpers")
    me = SimpleNamespace(id=1, is_self=True)

    class Client:
        async def get_me(self):
            return me

        async def get_entity(self, raw):
            return SimpleNamespace(id=999, username=str(raw).lstrip("@"))

    with account_env(mode="read"), isolated_state():
        for word in ("saved", "Saved Messages", "избранное", "me", "self"):
            assert _run(helpers.resolve_chat(Client(), word)) is me, word
        assert _run(helpers.resolve_chat(Client(), "@saved")).id == 999


def _peek_world():
    """A fake account: Saved Messages, private chats, a bot, the service chat, groups."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from telethon.tl import types as tl

    me = tl.User(id=1, is_self=True, first_name="me")
    friend = tl.User(id=2, first_name="friend")
    bot = tl.User(id=4, bot=True, first_name="bot")
    service = tl.User(id=777000, first_name="Telegram")
    group = tl.Chat(id=50, title="group", photo=tl.ChatPhotoEmpty(), participants_count=3,
                    date=datetime.now(timezone.utc), version=1)
    forum = tl.Chat(id=60, title="forum", photo=tl.ChatPhotoEmpty(), participants_count=3,
                    date=datetime.now(timezone.utc), version=1)
    history = {1: [1, 2], 2: [10, 11], 4: [30], 777000: [40], 50: [100], 60: [200, 201]}
    thread = {60: [201]}  # forum thread 7; its messages are in the chat's history too
    requests = []

    class Client:
        async def iter_dialogs(self):
            for e in (me, friend, bot, service, group, forum):
                top = history[e.id][-1] if history[e.id] else 0
                yield SimpleNamespace(entity=e, dialog=SimpleNamespace(top_message=top))

        async def iter_messages(self, entity, limit=None, min_id=0, reverse=False, reply_to=None, **kw):
            requests.append((entity.id, reply_to))
            pool = thread.get(entity.id, []) if reply_to is not None else history[entity.id]
            ids = [i for i in pool if i > (min_id or 0)]
            ids = ids if reverse else list(reversed(ids))
            for i in ids[:limit]:
                yield SimpleNamespace(id=i, date=datetime.now(timezone.utc), message=f"m{i}",
                                      out=entity.id == 1, sender=None, sender_id=None,
                                      reply_to=None, media=None, fwd_from=None, entities=None,
                                      reply_markup=None, grouped_id=None)

    @contextlib.asynccontextmanager
    async def fake_client(*a, **k):
        yield Client()

    return history, thread, requests, fake_client


def test_inbox_peek_sees_what_read_inbox_would_return_and_marks_nothing():
    tools = _tools()
    store = plugin_module("core.state.collections")
    history, thread, requests, fake_client = _peek_world()

    with account_env(mode="read"), isolated_state():
        store.save_collection("watch", members=[{"peer_id": "-50", "thread": None},
                                                {"peer_id": "-60", "thread": 7}])
        original = tools.tool_client
        tools.tool_client = fake_client
        try:
            peek = _run(tools.inbox_peek())
            got = {(c["chat_id"], c["thread_id"]): c for c in peek["chats"]}
            assert set(got) == {("1", None), ("2", None), ("-50", None), ("-60", "7")}, sorted(got)
            assert all(c["first_visit"] for c in got.values())
            assert peek["new_chats"] == 4 and peek["account"] == "acct"
            # Whole chats are judged by the dialog's top message: no request per chat.
            assert requests == [(60, 7)], requests

            # Peeking marked nothing: the inbox still returns all of it.
            first = json.loads(_run(tools._tg_read_inbox({"format": "json"})))
            assert {(c["chat_id"], c["thread_id"]) for c in first["chats"]} == set(got)
            assert not json.loads(_run(tools._tg_mark_inbox({"marks": first["marks"]})))["errors"]
            assert _run(tools.inbox_peek())["new_chats"] == 0

            history[2].append(12)
            history[60].append(202)
            thread[60].append(202)
            history[4].append(31)  # bots stay out, as in tg_read_inbox
            peek = _run(tools.inbox_peek())
            assert {(c["chat_id"], c["thread_id"]) for c in peek["chats"]} == {("2", None), ("-60", "7")}
            assert not any(c["first_visit"] for c in peek["chats"])
        finally:
            tools.tool_client = original


def test_inbox_command_prints_every_account_and_flags_failures(capsys, tmp_path):
    import argparse

    tools = _tools()
    cli = plugin_module("cli")
    _, _, _, fake_client = _peek_world()

    with account_env(mode="read"), isolated_state():
        original = tools.tool_client
        tools.tool_client = fake_client
        try:
            args = argparse.Namespace(telegram_user_action="inbox", peek=True, account=None,
                                      env=tmp_path / "missing.env")
            assert cli.run_command(args) == 0
            out = json.loads(capsys.readouterr().out)
            assert out["new_chats"] == 2 and [a["account"] for a in out["accounts"]] == ["acct"]

            args.account = ["acct", "nobody"]
            assert cli.run_command(args) == 1
            out = json.loads(capsys.readouterr().out)
            assert out["accounts"][1]["account"] == "nobody" and "unknown" in out["accounts"][1]["error"]

            args.peek = False
            assert cli.run_command(args) == 2
        finally:
            tools.tool_client = original


def test_inbox_command_takes_plugin_variables_from_the_env_file(tmp_path):
    cli = plugin_module("cli")
    env = tmp_path / ".env"
    env.write_text("HERMES_TG_USER_PEEKTEST_X='from file'\nOTHER_SECRET=nope\n"
                   "export HERMES_TG_USER_PEEKTEST_Y=set\n", encoding="utf-8")
    keys = ("HERMES_TG_USER_PEEKTEST_X", "HERMES_TG_USER_PEEKTEST_Y", "OTHER_SECRET")
    saved = {k: os.environ.pop(k, None) for k in keys}
    os.environ["HERMES_TG_USER_PEEKTEST_Y"] = "already"
    try:
        cli._load_account_env(env)
        assert os.environ["HERMES_TG_USER_PEEKTEST_X"] == "from file"
        assert os.environ["HERMES_TG_USER_PEEKTEST_Y"] == "already"
        assert "OTHER_SECRET" not in os.environ
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_inbox_text_is_one_line_per_message_and_marks_by_batch():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from telethon.tl import types as tl

    tools = _tools()
    me = tl.User(id=1, is_self=True, first_name="me")
    friend = tl.User(id=2, first_name="Петя")
    history = {1: [1], 2: [10, 11]}
    texts = {1: "купить хлеб", 10: "привет", 11: "встреча в пятницу\nв 15:00"}

    class Client:
        async def iter_dialogs(self):
            for e in (me, friend):
                yield SimpleNamespace(entity=e, dialog=SimpleNamespace(top_message=history[e.id][-1]))

        async def iter_messages(self, entity, limit=None, min_id=0, reverse=False, **kw):
            ids = [i for i in history[entity.id] if i > (min_id or 0)]
            ids = ids if reverse else list(reversed(ids))
            for i in ids[:limit]:
                yield SimpleNamespace(
                    id=i, date=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc), message=texts[i],
                    out=entity.id == 1, sender=None if entity.id == 1 else friend, sender_id=entity.id,
                    reply_to=None, media=None, fwd_from=None, entities=None, reply_markup=None,
                    grouped_id=None)

    @contextlib.asynccontextmanager
    async def fake_client(*a, **k):
        yield Client()

    with account_env(mode="read"), isolated_state():
        original = tools.tool_client
        tools.tool_client = fake_client
        previous_tz = os.environ.get("HERMES_TIMEZONE")
        os.environ["HERMES_TIMEZONE"] = "Europe/Moscow"
        try:
            text = _run(tools._tg_read_inbox({}))
            assert text.startswith("Аккаунт acct: новое в 2 чатах.")
            assert "== me · Избранное — заметки владельца · id 1" in text
            assert "[07.10 15:00] #1 я: купить хлеб" in text
            assert "[07.10 15:00] #11 Петя: встреча в пятницу\n  в 15:00" in text
            assert "{" not in text  # no JSON to wade through
            batch = text.split('batch="', 1)[1].split('"', 1)[0]
            assert len(batch) == 6

            assert "unknown batch" in _run(tools._tg_mark_inbox({"batch": "zzz"}))
            marked = json.loads(_run(tools._tg_mark_inbox({"batch": batch})))
            assert marked["marked"] == 2 and not marked["errors"]
            assert _run(tools._tg_read_inbox({})) == "Аккаунт acct: нового нет."
        finally:
            tools.tool_client = original
            if previous_tz is None:
                os.environ.pop("HERMES_TIMEZONE", None)
            else:
                os.environ["HERMES_TIMEZONE"] = previous_tz


def test_mark_all_starts_from_now_without_reading_any_chat(capsys, tmp_path):
    import argparse

    tools = _tools()
    cli = plugin_module("cli")
    store = plugin_module("core.state.collections")
    history, thread, requests, fake_client = _peek_world()

    with account_env(mode="read"), isolated_state():
        store.save_collection("watch", members=[{"peer_id": "-60", "thread": 7}])
        original = tools.tool_client
        tools.tool_client = fake_client
        try:
            args = argparse.Namespace(telegram_user_action="inbox", peek=False, mark_all=True,
                                      account=None, env=tmp_path / "missing.env")
            assert cli.run_command(args) == 0
            out = json.loads(capsys.readouterr().out)
            assert out["marked"] == 3, out  # Saved Messages, the private chat, the thread
            assert requests == []  # only the dialog list was fetched
            assert _run(tools.inbox_peek())["new_chats"] == 0

            history[2].append(12)
            history[60].append(202)
            thread[60].append(202)
            peek = _run(tools.inbox_peek())
            assert {(c["chat_id"], c["thread_id"]) for c in peek["chats"]} == {("2", None), ("-60", "7")}

            args.peek = True
            assert cli.run_command(args) == 2  # one mode at a time
        finally:
            tools.tool_client = original
