"""The memory export, offline: chats to markdown, attachments to text, nothing redone.

Pins the scope (Saved Messages, private chats and collection chats — delegated
ones too — never bots), one file per chat per month, a second run with nothing
new writing nothing, and the attachment rules: new ones by day, old ones only at
night, the original kept and both linked from the chat.
"""

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import account_env, isolated_state, plugin_module  # noqa: E402

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)  # 15:00 in Moscow: daytime


def _msg(mid, when, text="", out=False, sender=None, photo=False, document=None):
    fields = {k: None for k in ("web_preview", "sticker", "voice", "video_note", "video", "audio", "gif",
                                "contact", "geo", "poll", "media", "reply_to", "edit_date", "fwd_from",
                                "entities", "reply_markup", "grouped_id")}
    fields.update(id=mid, date=when, message=text, out=out, sender=sender,
                  sender_id=getattr(sender, "id", None), photo=object() if photo else None,
                  document=document, file=SimpleNamespace(mime_type="image/jpeg" if photo else
                                                          getattr(document, "mime_type", None),
                                                          size=1000, name=None, duration=None))
    if photo or document is not None:
        fields["media"] = object()
    return SimpleNamespace(**fields)


class _World:
    def __init__(self):
        from telethon.tl import types as tl

        self.me = tl.User(id=1, is_self=True, first_name="me")
        self.friend = tl.User(id=6000000001, first_name="Петя")
        self.bot = tl.User(id=900, bot=True, first_name="бот")
        self.group = tl.Chat(id=50, title="Дача", photo=tl.ChatPhotoEmpty(), participants_count=3,
                             date=NOW, version=1)
        sep = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
        self.history = {
            1: [_msg(1, sep, "купить хлеб", out=True)],
            6000000001: [_msg(10, sep, "привет", sender=self.friend),
                         _msg(11, NOW - timedelta(days=10), "вот паспорт", sender=self.friend, photo=True),
                         _msg(12, NOW - timedelta(hours=3), "чек", sender=self.friend, photo=True),
                         _msg(13, NOW - timedelta(hours=2), "договор",
                              sender=self.friend, document=SimpleNamespace(mime_type="application/zip"))],
            900: [_msg(30, sep, "реклама", sender=self.bot)],
            -50: [_msg(100, sep, "шашлык в субботу", sender=self.friend)],
        }

    def add(self, peer, message):
        self.history[peer].append(message)

    def client(self):
        world = self

        def peer(entity):
            return -entity.id if type(entity).__name__ == "Chat" else entity.id

        class Client:
            async def iter_dialogs(self):
                for e in (world.me, world.friend, world.bot, world.group):
                    yield SimpleNamespace(entity=e, dialog=SimpleNamespace(
                        top_message=max(m.id for m in world.history[peer(e)])))

            async def get_messages(self, entity, limit=None, max_id=0, min_id=0, ids=None):
                rows = world.history[peer(entity)]
                if ids is not None:
                    return next((m for m in rows if m.id == ids), None)
                picked = [m for m in rows if (not max_id or m.id < max_id) and m.id > (min_id or 0)]
                return sorted(picked, key=lambda m: m.id, reverse=True)[:limit]

            async def download_media(self, message, file=None):
                return b"\xff\xd8 image " + str(message.id).encode()

        @contextlib.asynccontextmanager
        async def fake_client(*a, **k):
            yield Client()

        return fake_client


@contextlib.contextmanager
def _setup(tmp_path, **env):
    tools = plugin_module("tools")
    export = plugin_module("export")
    store = plugin_module("core.state.collections")
    world = _World()
    values = {"HERMES_TIMEZONE": "Europe/Moscow", **env}
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    with account_env(mode="read"), isolated_state():
        store.save_collection("dacha", members=[{"peer_id": "-50", "thread": None}])
        original = tools.tool_client
        tools.tool_client = world.client()
        try:
            yield export, world, tmp_path / "out"
        finally:
            tools.tool_client = original
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _run(export, out, **kw):
    return asyncio.run(export.export_account(out, now=kw.pop("now", NOW), **kw))


def test_chats_become_monthly_markdown_and_a_quiet_run_writes_nothing(tmp_path):
    with _setup(tmp_path) as (export, world, out):
        report = _run(export, out)
        assert not report["errors"], report
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))
        assert files == [
            "acct/Дача__-50/2026-09.md",
            "acct/Избранное__1/2026-09.md",
            "acct/Петя__6000000001/2026-09.md",
            "acct/Петя__6000000001/2026-10.md",
        ], files  # the bot's chat is not exported
        october = (out / "acct/Петя__6000000001/2026-10.md").read_text(encoding="utf-8")
        assert 'chat_id: "6000000001"' in october and "month: 2026-10" in october
        assert "# Петя — октябрь 2026" in october
        assert "#13 Петя [файл]: договор" in october and "#12 Петя [фото]: чек" in october
        september = (out / "acct/Петя__6000000001/2026-09.md").read_text(encoding="utf-8")
        assert "[28.09 15:00] #11 Петя [фото]: вот паспорт" in september  # ten days ago, Moscow time
        assert "[20.09 12:00] #1 я: купить хлеб" in (out / "acct/Избранное__1/2026-09.md").read_text(encoding="utf-8")

        assert _run(export, out)["files_written"] == 0  # nothing new: nothing rewritten

        world.add(6000000001, SimpleNamespace(**{**vars(world.history[6000000001][0]), "id": 14,
                                                 "date": NOW, "message": "ещё"}))
        report = _run(export, out)
        assert report["files_written"] == 1 and report["added"] == 1
        assert "#14 Петя: ещё" in (out / "acct/Петя__6000000001/2026-10.md").read_text(encoding="utf-8")


def test_attachments_new_by_day_old_at_night_with_original_and_links(tmp_path, monkeypatch):
    seen = []

    def fake_vision(data, mime="image/jpeg"):
        seen.append(data)
        return "ДОКУМЕНТ: кассовый чек\nПятёрочка\nИтого 512,00"

    with _setup(tmp_path, HERMES_TG_USER_VISION_MODEL="qwen-vision",
                HERMES_TG_USER_VISION_URL="http://10.10.10.2:11434/v1") as (export, world, out):
        monkeypatch.setattr(export, "vision_text", fake_vision)
        report = _run(export, out, ocr=True)
        assert report["media_done"] == 1 and len(seen) == 1  # only the fresh photo by day
        assert report["media_skipped"] == 1  # the zip document: not an image, never retried
        chat = out / "acct/Петя__6000000001"
        text = (chat / "media/12.md").read_text(encoding="utf-8")
        assert "source: telegram-media" in text and 'original: "files/12.jpg"' in text
        assert 'link: "tg://openmessage?user_id=6000000001&message_id=12"' in text
        assert "Итого 512,00" in text and (chat / "files/12.jpg").read_bytes().startswith(b"\xff\xd8")
        month = (chat / "2026-10.md").read_text(encoding="utf-8")
        assert "[фото: Пятёрочка Итого 512,00 → media/12.md]" in month

        night = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)  # 02:30 in Moscow
        report = _run(export, out, ocr=True, now=night)
        assert report["media_done"] == 1 and (chat / "media/11.md").exists()  # the old one, at night
        assert _run(export, out, ocr=True, now=night)["media_done"] == 0  # all done, nothing redone


def test_message_links_open_the_message_in_telegram():
    export = plugin_module("export")
    assert export.message_link("-1001234567890", 5, "supergroup") == "https://t.me/c/1234567890/5"
    assert export.message_link("-50", 5, "group") == "tg://openmessage?chat_id=50&message_id=5"


def test_ocr_is_off_until_a_vision_model_is_named(tmp_path, monkeypatch):
    with _setup(tmp_path) as (export, world, out):
        monkeypatch.setattr(export, "_hermes_config", lambda: {})
        monkeypatch.setattr(export, "vision_text",
                            lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")))
        report = _run(export, out, ocr=True)
        assert report["media_done"] == 0 and not (out / "acct/Петя__6000000001/media").exists()


def test_the_vision_model_comes_from_the_hermes_config(monkeypatch):
    export = plugin_module("export")
    for name in ("HERMES_TG_USER_VISION_MODEL", "HERMES_TG_USER_VISION_URL"):
        monkeypatch.delenv(name, raising=False)
    config = {"model": {"provider": "custom", "default": "qwen3.8:27b-mtp-q4_K_M",
                        "base_url": "http://10.10.10.2:11434/v1"}}
    monkeypatch.setattr(export, "_hermes_config", lambda: config)
    assert export.vision_endpoint() == ("http://10.10.10.2:11434/v1", "qwen3.8:27b-mtp-q4_K_M")

    config["auxiliary"] = {"vision": {"model": "qwen-vl", "base_url": "http://ollama.lan:11434/v1"}}
    assert export.vision_endpoint() == ("http://ollama.lan:11434/v1", "qwen-vl")

    assert export.vision_endpoint.__doc__  # auxiliary.vision first, then the main model


def test_vision_uses_ollamas_own_api_with_thinking_off(monkeypatch):
    export = plugin_module("export")
    calls = []

    def fake_post(url, payload):
        calls.append((url, payload))
        return {"message": {"content": "<think>hm</think>ФОТО: дача, мангал"}}

    monkeypatch.setattr(export, "_post", fake_post)
    monkeypatch.setattr(export, "vision_endpoint", lambda: ("http://10.10.10.2:11434/v1", "qwen"))
    assert export.vision_text(b"img") == "ФОТО: дача, мангал"
    url, payload = calls[0]
    assert url == "http://10.10.10.2:11434/api/chat" and payload["think"] is False
    assert payload["messages"][0]["images"] == ["aW1n"]


def test_a_year_of_history_except_the_chats_kept_whole(tmp_path):
    old = datetime(2025, 3, 1, 9, 0, tzinfo=timezone.utc)  # older than a year before NOW
    with _setup(tmp_path) as (export, world, out):
        for peer, mid in ((6000000001, 5), (-50, 90)):
            world.history[peer].insert(0, _msg(mid, old, "давнее", sender=world.friend))
        world.history[1] = [_msg(1, old, "давнее", out=True),
                            _msg(2, datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc), "купить хлеб", out=True)]
        report = _run(export, out)  # no limit yet: everything, as before
        assert (out / "acct/Петя__6000000001/2025-03.md").exists()

        os.environ["HERMES_TG_USER_EXPORT_DAYS"] = "365"
        os.environ["HERMES_TG_USER_EXPORT_FULL"] = "-50, избранное, -1009999"
        try:
            report = _run(export, out)
            files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))
            assert "acct/Петя__6000000001/2025-03.md" not in files  # fell out of the window: deleted
            assert "acct/Петя__6000000001/2026-10.md" in files
            assert "acct/Дача__-50/2025-03.md" in files and "acct/Избранное__1/2025-03.md" in files
            assert report["full_history_missing"] == ["-1009999"]  # not in any collection
        finally:
            for name in ("HERMES_TG_USER_EXPORT_DAYS", "HERMES_TG_USER_EXPORT_FULL"):
                os.environ.pop(name, None)


def test_a_chat_whose_year_is_archived_is_not_asked_again(tmp_path):
    calls = []
    with _setup(tmp_path, HERMES_TG_USER_EXPORT_DAYS="30") as (export, world, out):
        world.history[6000000001].insert(0, _msg(5, datetime(2026, 1, 5, tzinfo=timezone.utc), "зима",
                                                 sender=world.friend))
        tools = plugin_module("tools")
        inner = tools.tool_client

        @contextlib.asynccontextmanager
        async def counting(*a, **k):
            async with inner(*a, **k) as client:
                original = client.get_messages

                async def get_messages(entity, *args, **kwargs):
                    if kwargs.get("ids") is None:
                        calls.append(getattr(entity, "id", None))
                    return await original(entity, *args, **kwargs)

                client.get_messages = get_messages
                yield client

        tools.tool_client = counting
        _run(export, out)
        assert not (out / "acct/Петя__6000000001/2026-01.md").exists()  # older than 30 days
        first = calls.count(6000000001)
        assert first >= 1
        calls.clear()
        report = _run(export, out)
        assert calls.count(6000000001) == 0 and report["synced"] == 0  # its 30 days are on disk
