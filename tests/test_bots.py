"""Bots, offline: reading keyboards, pressing buttons, inline mode.

Pins the safety rules around acting through a bot: only write accounts (or a
read-only one inside its delegated chat) press anything, buttons that hand over
the phone, a location or a payment are never pressed, link buttons only report
their URL, and labels about money, orders, confirmations or deletions ask the
owner through the plugin's hook.
"""

import asyncio
import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import account_env, isolated_state, plugin_module  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    def __init__(self):
        from telethon.tl import types as tl

        self.tl = tl
        self.entity = tl.User(id=900, bot=True, first_name="Пицца", username="pizza_bot")
        self.clicked = []
        self.sent_inline = []
        self.messages = {50: self._message(50, "Выберите размер", inline=True)}

    def _button(self, text, kind):
        bot = self

        async def click(*a, **k):
            bot.clicked.append(text)
            if kind == "callback":
                bot.messages[51] = bot._message(51, f"Вы выбрали: {text}", inline=False, buttons=[])
                return SimpleNamespace(message=f"Принято: {text}", alert=False, url=None)
            if kind == "text":
                return SimpleNamespace(id=52)
            raise AssertionError(f"must never click a {kind} button")

        raw = {
            "callback": lambda: self.tl.InlineButtonTypeCallback(data=text.encode()),
            "url": lambda: self.tl.InlineButtonTypeUrl(url="https://pizza.example/menu"),
            "phone": lambda: self.tl.ButtonTypeRequestPhone(),
            "buy": lambda: self.tl.InlineButtonTypeBuy(),
            "text": lambda: self.tl.ButtonTypeDefault(),
            "inline": lambda: self.tl.InlineButtonTypeSwitchInline(query="пепперони"),
        }[kind]()
        return SimpleNamespace(text=text, button=SimpleNamespace(text=text, type=raw),
                               url="https://pizza.example/menu" if kind == "url" else None,
                               inline_query="пепперони" if kind == "inline" else None, click=click)

    def _message(self, mid, text, *, inline, buttons=None):
        if buttons is None:
            buttons = [[self._button("Маленькая", "callback"), self._button("Большая", "callback")],
                       [self._button("Меню", "url"), self._button("Поделиться номером", "phone")],
                       [self._button("Оплатить заказ", "callback"), self._button("Купить", "buy")],
                       [self._button("Найти пиццу", "inline")]]
        markup = (self.tl.ReplyInlineMarkup(rows=[]) if inline else self.tl.ReplyKeyboardMarkup(rows=[]))
        return SimpleNamespace(
            id=mid, date=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc), message=text, out=False,
            sender=self.entity, sender_id=900, reply_to=None, media=None, fwd_from=None, entities=None,
            reply_markup=markup if buttons else None, grouped_id=None, buttons=buttons)

    def client(self):
        bot = self

        class Client:
            async def get_entity(self, raw):
                if str(raw).lstrip("@") in ("pizza_bot", "900"):
                    return bot.entity
                raise ValueError(raw)

            async def iter_dialogs(self):
                yield SimpleNamespace(entity=bot.entity, dialog=SimpleNamespace(top_message=max(bot.messages)))

            async def iter_messages(self, entity, limit=None, **kw):
                for mid in sorted(bot.messages, reverse=True)[:limit]:
                    yield bot.messages[mid]

            async def get_messages(self, entity, ids=None):
                return bot.messages.get(int(ids))

            async def inline_query(self, bot_entity, query, entity=None):
                async def click(target):
                    bot.sent_inline.append((query, target.id))
                    return SimpleNamespace(id=77, date=datetime.now(timezone.utc))
                return [SimpleNamespace(result=SimpleNamespace(id=f"r{i}"), type="article",
                                        title=f"{query} #{i}", description="30 см", url=None, click=click)
                        for i in range(3)]

        @contextlib.asynccontextmanager
        async def fake_client(*a, **k):
            yield Client()

        return fake_client


@contextlib.contextmanager
def _setup(mode="write"):
    tools = plugin_module("tools")
    bot = _Bot()
    with account_env(mode=mode), isolated_state():
        original = tools.tool_client
        tools.tool_client = bot.client()
        try:
            yield tools, bot
        finally:
            tools.tool_client = original


def test_messages_show_the_keyboard_with_button_kinds():
    helpers = plugin_module("core.helpers")
    with _setup() as (tools, bot):
        row = helpers.message_to_dict(bot.messages[50], chat=bot.entity)
        kinds = {b["text"]: b["kind"] for r in row["keyboard"]["rows"] for b in r}
        assert row["keyboard"]["type"] == "inline"
        assert kinds == {"Маленькая": "callback", "Большая": "callback", "Меню": "url",
                         "Поделиться номером": "phone", "Оплатить заказ": "callback",
                         "Купить": "payment", "Найти пиццу": "switch_inline"}
        line = tools._inbox_line(row)
        assert "[кнопки: Маленькая | Большая | Меню ↗ |" in line and "Найти пиццу (inline)" in line


def test_pressing_a_button_returns_the_bots_answer_and_the_new_state():
    with _setup() as (tools, bot):
        out = _run(tools._tg_click_button({"chat": "@pizza_bot", "text": "большая", "wait": 0}))
        assert bot.clicked == ["Большая"]
        assert "Нажата «Большая» (сообщение #50)" in out and "Принято: Большая" in out
        assert "Вы выбрали: Большая" in out  # what the bot sent back is read in the same call


def test_buttons_that_hand_over_data_or_money_are_never_pressed():
    with _setup() as (tools, bot):
        for label in ("Поделиться номером", "Купить"):
            out = json.loads(_run(tools._tg_click_button({"chat": "@pizza_bot", "text": label, "wait": 0})))
            assert out["pressed"] is False and "does not press" in out["error"]
        link = json.loads(_run(tools._tg_click_button({"chat": "@pizza_bot", "text": "Меню", "wait": 0})))
        assert link == {"url": "https://pizza.example/menu", "pressed": False,
                        "note": "A link button: not opened. Fetch the page if it is needed."}
        switch = json.loads(_run(tools._tg_click_button({"chat": "@pizza_bot", "text": "Найти пиццу", "wait": 0})))
        assert switch["inline_query"] == "пепперони" and switch["pressed"] is False
        assert "no button" in _run(tools._tg_click_button({"chat": "@pizza_bot", "text": "Нет такой", "wait": 0}))
        assert bot.clicked == []


def test_risky_labels_ask_the_owner_and_plain_ones_do_not():
    tools = plugin_module("tools")
    ask = tools.delegation_approval(tool_name="tg_click_button",
                                    args={"chat": "@pizza_bot", "text": "Оплатить заказ"})
    assert ask["action"] == "approve" and "Оплатить заказ" in ask["message"]
    assert tools.delegation_approval(tool_name="tg_click_button",
                                     args={"chat": "@pizza_bot", "text": "Большая"}) is None
    for label in ("Подтвердить", "Удалить аккаунт", "Confirm order", "Отменить запись"):
        assert tools.delegation_approval(tool_name="tg_click_button", args={"text": label})["action"] == "approve"


def test_read_only_accounts_act_only_inside_a_delegated_chat():
    delegations = plugin_module("core.state.delegations")
    with _setup(mode="read") as (tools, bot):
        refused = _run(tools._tg_click_button({"chat": "@pizza_bot", "text": "Большая", "wait": 0}))
        assert "read-only" in refused and bot.clicked == []
        delegations.save_delegation(peer_id="900", chat="pizza_bot", username="pizza_bot", goal="заказать пиццу")
        _run(tools._tg_click_button({"chat": "@pizza_bot", "text": "Большая", "wait": 0}))
        assert bot.clicked == ["Большая"]


def test_inline_mode_lists_results_and_sends_the_chosen_one():
    with _setup() as (tools, bot):
        listed = json.loads(_run(tools._tg_inline_query({"bot": "@pizza_bot", "query": "маргарита"})))
        assert [r["id"] for r in listed["results"]] == ["r0", "r1", "r2"] and bot.sent_inline == []
        sent = json.loads(_run(tools._tg_send_inline_result(
            {"bot": "@pizza_bot", "query": "маргарита", "chat": "@pizza_bot", "id": "r1"})))
        assert sent["sent"] and bot.sent_inline == [("маргарита", 900)]
