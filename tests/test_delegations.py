"""Delegated dialogs, offline: one approval, then writes only there, only while active.

Pins what makes it safe to let the agent write without asking: creating or
changing a delegation always goes through the owner, tg_dialog_message reaches
no other chat and nothing after the delegation is closed or expired, the
dialogs job wakes only on the other side's unseen answers, and the archiver's
inbox leaves delegated chats alone.
"""

import asyncio
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import account_env, isolated_state, plugin_module  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _World:
    """A write account with Saved Messages and one manager on a > 2**31 user id."""

    def __init__(self):
        from telethon.tl import types as tl

        self.me = tl.User(id=1, is_self=True, first_name="me")
        self.manager = tl.User(id=7000000001, first_name="Анна", username="manager")
        self.friend = tl.User(id=2, first_name="Петя")
        self.history = {1: [], 7000000001: [(10, False, "Здравствуйте")], 2: [(5, False, "привет")]}
        self.sent = []

    def incoming(self, peer, text):
        last = max([m[0] for m in self.history[peer]] or [0])
        self.history[peer].append((last + 1, False, text))

    def client(self):
        world = self

        class Client:
            async def iter_dialogs(self):
                for e in (world.me, world.manager, world.friend):
                    top = world.history[e.id][-1][0] if world.history[e.id] else 0
                    yield SimpleNamespace(entity=e, dialog=SimpleNamespace(top_message=top))

            async def get_entity(self, raw):
                for e in (world.manager, world.friend):
                    if str(raw).lstrip("@") in (e.username, str(e.id)) or raw == e.id:
                        return e
                raise ValueError(f"no entity {raw!r}")

            async def get_me(self):
                return world.me

            async def iter_messages(self, entity, limit=None, min_id=0, reverse=False, **kw):
                rows = [m for m in world.history[entity.id] if m[0] > (min_id or 0)]
                rows = rows if reverse else list(reversed(rows))
                for mid, out, text in rows[:limit]:
                    yield SimpleNamespace(
                        id=mid, date=datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc), message=text,
                        out=out, sender=None if out else entity, sender_id=entity.id, reply_to=None,
                        media=None, fwd_from=None, entities=None, reply_markup=None, grouped_id=None)

            async def send_message(self, entity, text, reply_to=None, **kw):
                last = max([m[0] for m in world.history[entity.id]] or [0])
                world.history[entity.id].append((last + 1, True, text))
                world.sent.append((entity.id, text))
                return SimpleNamespace(id=last + 1, date=datetime.now(timezone.utc))

        @contextlib.asynccontextmanager
        async def fake_client(*a, **k):
            yield Client()

        return fake_client


@contextlib.contextmanager
def _setup(mode="write"):
    tools = plugin_module("tools")
    world = _World()
    with account_env(mode=mode), isolated_state():
        original = tools.tool_client
        tools.tool_client = world.client()
        try:
            yield tools, world
        finally:
            tools.tool_client = original


def _delegate(tools, **extra):
    args = {"chat": "@manager", "goal": "столик на 4, пятница 19:00", "limits": "18:30–20:00", **extra}
    return json.loads(_run(tools._tg_delegate_dialog(args)))["delegation"]


def test_creating_or_changing_a_delegation_always_asks_the_owner():
    tools = plugin_module("tools")
    with account_env(mode="write"):
        args = {"chat": "@manager", "goal": "столик", "limits": "до 20:00", "hours": 24}
        first = tools.delegation_approval(tool_name="tg_delegate_dialog", args=args, session_id="s")
        assert first["action"] == "approve"
        assert "@manager" in first["message"] and "столик" in first["message"] and "до 20:00" in first["message"]
        wider = tools.delegation_approval(tool_name="tg_delegate_dialog", args={**args, "limits": "любое время"})
        assert wider["rule_key"] != first["rule_key"]  # "always" for one does not approve another
        assert tools.delegation_approval("tg_delegate_dialog", args)["action"] == "approve"  # positional form
        assert tools.delegation_approval(tool_name="tg_dialog_message", args={}) is None
        assert tools.delegation_approval(tool_name="tg_send_message", args={}) is None


def test_the_agent_writes_only_in_the_delegated_chat_and_wakes_only_on_answers():
    with _setup() as (tools, world):
        row = _delegate(tools)
        assert row["active"] and row["peer_id"] == "7000000001" and row["last_seen_id"] == 10
        assert _run(tools.delegations_peek())["waiting"] == []  # history before it is not news

        sent = json.loads(_run(tools._tg_dialog_message({"delegation": row["id"], "text": "Добрый день!"})))
        assert sent["sent"] and world.sent == [(7000000001, "Добрый день!")]

        world.incoming(7000000001, "На 19:00 есть столик у окна")
        peek = _run(tools.delegations_peek())
        assert [w["id"] for w in peek["waiting"]] == [row["id"]] and peek["waiting"][0]["new_messages"] == 1

        text = _run(tools._tg_dialog_read({"delegation": row["id"]}))
        assert "Цель: столик на 4, пятница 19:00" in text and "Рамки: 18:30–20:00" in text
        assert "* [08.10" in text and "столик у окна" in text
        world.incoming(7000000001, "Подтвердите, пожалуйста")  # arrives after the read
        _run(tools._tg_dialog_message({"delegation": row["id"], "text": "Да, бронируем на 19:00"}))
        peek = _run(tools.delegations_peek())
        assert peek["waiting"] and peek["waiting"][0]["new_messages"] == 1  # unread answer still wakes

        _run(tools._tg_dialog_read({"delegation": row["id"], "mark_seen": True}))
        assert _run(tools.delegations_peek())["waiting"] == []

        closed = json.loads(_run(tools._tg_close_delegation({"delegation": row["id"], "outcome": "забронировано"})))
        assert closed["closed"] and closed["delegation"]["status"] == "closed"
        refused = json.loads(_run(tools._tg_dialog_message({"delegation": row["id"], "text": "ещё"})))
        assert "closed" in json.dumps(refused, ensure_ascii=False)
        assert json.loads(_run(tools._tg_dialog_message({"delegation": "nope", "text": "x"})))
        assert len(world.sent) == 2


def test_an_expired_delegation_is_closed_and_reported():
    delegations = plugin_module("core.state.delegations")
    with _setup() as (tools, world):
        row = _delegate(tools, hours=1)
        delegations.update_delegation(row["id"], expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        world.incoming(7000000001, "Вы ещё здесь?")
        peek = _run(tools.delegations_peek())
        assert peek["waiting"] == [] and [e["id"] for e in peek["expired"]] == [row["id"]]
        assert delegations.get_delegation(row["id"])["status"] == "expired"
        refused = _run(tools._tg_dialog_message({"delegation": row["id"], "text": "да"}))
        assert "expired" in refused and world.sent == []
        assert _run(tools.delegations_peek())["expired"] == []  # reported once


def test_a_read_only_account_writes_only_where_a_delegation_allows():
    with _setup(mode="read") as (tools, world):
        warning = tools.delegation_approval(tool_name="tg_delegate_dialog",
                                            args={"chat": "@manager", "goal": "x"})["message"]
        assert "только для чтения" in warning
        row = _delegate(tools)
        sent = json.loads(_run(tools._tg_dialog_message({"delegation": row["id"], "text": "Здравствуйте"})))
        assert sent["sent"] and world.sent == [(7000000001, "Здравствуйте")]
        refused = _run(tools._tg_send_message({"chat": "@manager", "text": "мимо поручения"}))
        assert "read-only" in refused and len(world.sent) == 1


def test_delegating_again_changes_terms():
    with _setup() as (tools, _):
        first = _delegate(tools)
        again = _delegate(tools, limits="18:00–21:00")
        assert again["id"] == first["id"] and again["limits"] == "18:00–21:00"
        assert "Saved Messages" in _run(tools._tg_delegate_dialog({"chat": "me", "goal": "x"}))


def test_the_archiver_inbox_leaves_delegated_chats_to_the_dialogs_job():
    with _setup() as (tools, world):
        row = _delegate(tools)
        world.incoming(7000000001, "ответ менеджера")
        world.incoming(2, "как дела?")
        text = _run(tools._tg_read_inbox({}))
        assert "как дела?" in text and "ответ менеджера" not in text
        _run(tools._tg_close_delegation({"delegation": row["id"], "outcome": "готово"}))
        assert "ответ менеджера" in _run(tools._tg_read_inbox({}))


def test_dialogs_command_prints_waiting_and_expired(capsys, tmp_path):
    import argparse

    cli = plugin_module("cli")
    with _setup() as (tools, world):
        row = _delegate(tools)
        world.incoming(7000000001, "ответ")
        args = argparse.Namespace(telegram_user_action="dialogs", peek=True, account=None,
                                  env=tmp_path / "missing.env")
        assert cli.run_command(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["waiting"] == 1 and out["accounts"][0]["waiting"][0]["id"] == row["id"]
        args.peek = False
        assert cli.run_command(args) == 2


def test_the_hook_is_registered_through_the_entry_point():
    package = plugin_module("core") and sys.modules["hermes_telegram_user_under_test"]
    hooks, tools_seen = [], []
    ctx = SimpleNamespace(register_tool=lambda **k: tools_seen.append(k["name"]),
                          register_hook=lambda name, fn: hooks.append((name, fn)))
    with isolated_state():
        package.register(ctx)
    assert [name for name, _ in hooks] == ["pre_tool_call"]
    assert hooks[0][1] is plugin_module("tools").delegation_approval
    manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    assert "hooks:\n  - pre_tool_call\n" in manifest  # Hermes ignores hooks it was not told about
