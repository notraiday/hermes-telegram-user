from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .client import entity_label, utc_iso
from .state.aliases import aliases_for_peer, get_alias
from .media import media_info
from .sanitize import sanitize_name, sanitize_structure, sanitize_text

#: How many matches an ambiguity error names before summarising the rest. Without
#: the list, the caller learns only that its query was wrong — so it guesses again,
#: and again: the turn burns on retries that cannot converge on their own.
_AMBIGUOUS_SHOWN = 8


def _candidates(entities: list[Any]) -> str:
    shown = ", ".join(
        f"{entity_label(entity)} (id {getattr(entity, 'id', '?')})"
        for entity in entities[:_AMBIGUOUS_SHOWN]
    )
    rest = len(entities) - _AMBIGUOUS_SHOWN
    return f"{shown} and {rest} more" if rest > 0 else shown


def _topic_candidates(topics: list[Any]) -> str:
    shown = ", ".join(
        f"{sanitize_name(getattr(topic, 'title', ''), limit=64)} "
        f"(id {int(getattr(topic, 'id', 0) or 0)})"
        for topic in topics[:_AMBIGUOUS_SHOWN]
    )
    rest = len(topics) - _AMBIGUOUS_SHOWN
    return f"{shown} and {rest} more" if rest > 0 else shown


def parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    now = datetime.now().astimezone()
    if s.lower() == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if s.lower() == "yesterday":
        day = now - timedelta(days=1)
        return day.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def clean_text(value: Any, *, limit: int = 12000) -> str:
    return sanitize_text(value, limit=limit)


def person_name(entity: Any) -> str:
    parts = [
        str(x).strip()
        for x in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
        if x
    ]
    return sanitize_name(" ".join(parts) or entity_label(entity), limit=256)


def peer_id(entity: Any) -> str:
    if entity is None:
        return ""
    try:
        from telethon import utils

        return str(utils.get_peer_id(entity))
    except Exception:
        return str(getattr(entity, "id", "") or "")


def _reply_quote(reply: Any) -> Optional[dict[str, Any]]:
    if reply is None:
        return None
    quote = getattr(reply, "quote_text", None)
    if not quote:
        return None
    out: dict[str, Any] = {"text": sanitize_text(quote, limit=2000)}
    offset = getattr(reply, "quote_offset", None)
    if offset is not None:
        try:
            out["offset"] = int(offset)
        except (TypeError, ValueError):
            pass
    return out


def _button_texts(message: Any) -> list[str]:
    out: list[str] = []
    try:
        for row in getattr(message, "buttons", None) or []:
            for button in row:
                text = sanitize_name(getattr(button, "text", None), limit=256)
                if text:
                    out.append(text)
    except Exception:
        return []
    return out[:50]


# Button kinds by Telethon's button type. "callback" and "text" are what an agent
# may press; the rest either only carry data (url, copy, profile), switch to inline
# mode, or hand over something the owner has not offered (phone, location, a
# payment, a login, a web app) and are never pressed by the plugin.
_BUTTON_KINDS = {
    "InlineButtonTypeCallback": "callback",
    "ButtonTypeDefault": "text",
    "InlineButtonTypeUrl": "url",
    "InlineButtonTypeSwitchInline": "switch_inline",
    "InlineButtonTypeCopy": "copy",
    "InlineButtonTypeUserProfile": "profile",
    "InlineButtonTypeGame": "game",
    "InlineButtonTypeBuy": "payment",
    "InlineButtonTypeUrlAuth": "login",
    "InlineButtonTypeWebView": "webapp",
    "ButtonTypeSimpleWebView": "webapp",
    "ButtonTypeRequestPhone": "phone",
    "ButtonTypeRequestGeoLocation": "location",
    "ButtonTypeRequestPeer": "choose_chat",
    "ButtonTypeRequestPoll": "poll",
    "InlineButtonTypeDisabled": "disabled",
}


def button_kind(button: Any) -> str:
    raw = getattr(getattr(button, "button", None), "type", None)
    return _BUTTON_KINDS.get(type(raw).__name__, "other")


def keyboard_of(message: Any) -> Optional[dict[str, Any]]:
    """The message's keyboard: inline or reply, rows of {text, kind[, url]}."""
    try:
        rows = getattr(message, "buttons", None) or []
        out_rows = []
        for row in rows:
            out_row = []
            for button in row:
                text = sanitize_name(getattr(button, "text", None), limit=256)
                if not text:
                    continue
                item: dict[str, Any] = {"text": text, "kind": button_kind(button)}
                url = getattr(button, "url", None)
                if url:
                    item["url"] = sanitize_text(url, limit=2048)
                out_row.append(item)
            if out_row:
                out_rows.append(out_row)
    except Exception:
        return None
    if not out_rows:
        return None
    markup = type(getattr(message, "reply_markup", None)).__name__
    return {"type": "reply" if markup == "ReplyKeyboardMarkup" else "inline", "rows": out_rows[:20]}


def _hidden_urls(message: Any) -> list[str]:
    out: list[str] = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            clean = sanitize_text(url, limit=2048)
            if clean and clean not in out:
                out.append(clean)
    return out[:50]


def _forwarded_info(message: Any) -> Optional[dict[str, Any]]:
    raw = getattr(message, "fwd_from", None)
    if raw is None:
        return None
    out: dict[str, Any] = {}
    date = getattr(raw, "date", None)
    if date is not None:
        out["date"] = utc_iso(date)
    from_name = getattr(raw, "from_name", None)
    if from_name:
        out["from_name"] = sanitize_name(from_name, limit=256)
    post_author = getattr(raw, "post_author", None)
    if post_author:
        out["post_author"] = sanitize_name(post_author, limit=256)
    channel_post = getattr(raw, "channel_post", None)
    if channel_post is not None:
        try:
            out["channel_post"] = int(channel_post)
        except (TypeError, ValueError):
            pass

    wrapper = getattr(message, "forward", None)
    if wrapper is not None:
        origin_chat = getattr(wrapper, "chat", None)
        if origin_chat is not None:
            out["from_chat"] = sanitize_name(entity_label(origin_chat), limit=256)
            origin_username = getattr(origin_chat, "username", None)
            if origin_username:
                out["from_username"] = sanitize_name(origin_username, limit=128)
            origin_id = peer_id(origin_chat)
            if origin_id:
                out["from_chat_id"] = origin_id
        origin_sender = getattr(wrapper, "sender", None)
        if origin_sender is not None:
            out["from_user"] = person_name(origin_sender)
            sender_id = peer_id(origin_sender)
            if sender_id:
                out["from_user_id"] = sender_id
    return out or {"unknown_origin": True}


def message_to_dict(message: Any, *, chat: Any = None) -> dict[str, Any]:
    sender = getattr(message, "sender", None)
    reply = getattr(message, "reply_to", None)
    sender_peer_id = peer_id(sender) or str(getattr(message, "sender_id", "") or "")
    row: dict[str, Any] = {
        "id": int(message.id),
        "date": utc_iso(getattr(message, "date", None)),
        "sender_id": sender_peer_id,
        "sender": sanitize_name(entity_label(sender), limit=256) if sender else None,
        "text": sanitize_text(getattr(message, "message", None) or "", limit=12000),
        "out": bool(getattr(message, "out", False)),
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
        "topic_id": getattr(reply, "reply_to_top_id", None),
    }
    sender_aliases = aliases_for_peer(sender_peer_id) if sender_peer_id else []
    if sender_aliases:
        row["sender_aliases"] = sender_aliases

    username = getattr(sender, "username", None) if sender else None
    if username:
        row["sender_username"] = sanitize_name(username, limit=128)

    quote = _reply_quote(reply)
    if quote:
        row["reply_quote"] = quote

    grouped_id = getattr(message, "grouped_id", None)
    if grouped_id:
        row["grouped_id"] = str(grouped_id)
    edit_date = getattr(message, "edit_date", None)
    if edit_date:
        row["edited_at"] = utc_iso(edit_date)
    if bool(getattr(message, "pinned", False)):
        row["pinned"] = True
    if bool(getattr(message, "noforwards", False)):
        row["protected_content"] = True

    for field in ("views", "forwards"):
        value = getattr(message, field, None)
        if value is not None:
            try:
                row[field] = int(value)
            except (TypeError, ValueError):
                pass

    replies = getattr(message, "replies", None)
    comments = getattr(replies, "replies", None) if replies is not None else None
    if comments is not None:
        try:
            row["comments"] = int(comments)
        except (TypeError, ValueError):
            pass

    via_bot_id = getattr(message, "via_bot_id", None)
    if via_bot_id:
        row["via_bot_id"] = str(via_bot_id)
    post_author = getattr(message, "post_author", None)
    if post_author:
        row["post_author"] = sanitize_name(post_author, limit=256)

    forwarded = _forwarded_info(message)
    if forwarded:
        row["forwarded"] = forwarded

    buttons = _button_texts(message)
    if buttons:
        row["buttons"] = buttons
        keyboard = keyboard_of(message)
        if keyboard:
            row["keyboard"] = keyboard
    urls = _hidden_urls(message)
    if urls:
        row["link_urls"] = urls

    action = getattr(message, "action", None)
    if action is not None:
        row["service_action"] = type(action).__name__

    attachment = media_info(message)
    if attachment:
        row["media"] = sanitize_structure(attachment, string_limit=2048)

    chat_id = ""
    if chat is not None:
        chat_id = peer_id(chat)
        row["chat_id"] = chat_id
        row["chat"] = sanitize_name(entity_label(chat), limit=256)
        chat_username = getattr(chat, "username", None)
        if chat_username:
            row["chat_username"] = sanitize_name(chat_username, limit=128)
        chat_aliases = aliases_for_peer(chat_id) if chat_id else []
        if chat_aliases:
            row["chat_aliases"] = chat_aliases

    if attachment and attachment.get("kind") in {"voice", "audio"} and chat_id:
        try:
            from .state.transcripts import get_cached_transcript

            cached = get_cached_transcript(chat_id, int(message.id))
        except Exception:
            cached = None
        if cached:
            row["transcript"] = sanitize_text(cached.get("transcript"), limit=30000)
            row["transcript_cached"] = True
            if cached.get("provider"):
                row["transcript_provider"] = sanitize_name(cached["provider"], limit=128)
            if cached.get("model"):
                row["transcript_model"] = sanitize_name(cached["model"], limit=128)

    return sanitize_structure(row, string_limit=30000)


async def _resolve_alias(client: Any, raw: str):
    alias = get_alias(raw)
    if not alias:
        return None
    username = str(alias.get("username") or "").strip()
    if username:
        try:
            return await client.get_entity(username)
        except Exception:
            pass

    wanted = str(alias.get("peer_id") or "")
    if not wanted:
        raise ValueError(f"Telegram alias has no target: {sanitize_name(raw, limit=128)}")
    async for dialog in client.iter_dialogs():
        if str(dialog.id) == wanted or peer_id(dialog.entity) == wanted:
            return dialog.entity
    raise ValueError(f"Telegram alias target is no longer available: {sanitize_name(raw, limit=128)}")


# Bare words that always mean the account's own Saved Messages. Without this,
# "saved" resolves to whatever public channel owns the username @saved. A
# leading "@" keeps the old meaning: "@saved" is that username, on purpose.
SAVED_MESSAGES_NAMES = {
    "me", "self", "saved", "saved messages", "savedmessages", "favorites", "favourites",
    "избранное", "сохранённые", "сохраненные", "сохранённые сообщения",
    "сохраненные сообщения", "заметки",
}


def is_saved_messages_name(raw: str) -> bool:
    return str(raw or "").strip().casefold() in SAVED_MESSAGES_NAMES


async def resolve_chat(client: Any, chat: str, *, allow_alias: bool = True):
    raw = str(chat).strip()
    if not raw:
        raise ValueError("chat is required")
    if is_saved_messages_name(raw):
        return await client.get_me()

    if allow_alias:
        resolved_alias = await _resolve_alias(client, raw)
        if resolved_alias is not None:
            return resolved_alias

    if raw.lstrip("-").isdigit():
        return await client.get_entity(int(raw))
    try:
        return await client.get_entity(raw)
    except Exception:
        needle = raw.lstrip("@").casefold()
        exact = []
        partial = []
        async for dialog in client.iter_dialogs():
            name = (dialog.name or "").casefold()
            username = (getattr(dialog.entity, "username", None) or "").casefold()
            if needle in {name, username}:
                exact.append(dialog.entity)
            elif needle in name or (username and needle in username):
                partial.append(dialog.entity)
        choices = exact or partial
        if len(choices) == 1:
            return choices[0]
        if len(choices) > 1:
            raise ValueError(
                f"Telegram chat is ambiguous: {sanitize_name(chat, limit=128)} matches "
                f"{len(choices)} — {_candidates(choices)}. Pass the id or the exact username "
                "to pick one; repeating the name will not resolve it."
            )
        raise ValueError(f"Telegram chat not found: {sanitize_name(chat, limit=128)}")


async def find_topic_root(client: Any, entity: Any, topic: str | int):
    raw = str(topic).strip()
    if raw.isdigit():
        return int(raw)
    needle = raw.casefold()
    try:
        from telethon.tl.functions.messages import GetForumTopicsRequest

        result = await client(
            GetForumTopicsRequest(
                peer=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q=raw,
            )
        )
    except Exception as exc:
        raise ValueError(f"Could not resolve topic {sanitize_name(topic, limit=128)!r}") from exc
    matches = []
    for item in result.topics:
        title = (getattr(item, "title", None) or "").casefold()
        if title == needle:
            return int(item.id)
        if needle in title:
            matches.append(item)
    if len(matches) == 1:
        return int(matches[0].id)
    if len(matches) > 1:
        raise ValueError(
            f"Telegram topic is ambiguous: {sanitize_name(topic, limit=128)} matches "
            f"{len(matches)} — {_topic_candidates(matches)}. Pass the topic id to pick one."
        )
    raise ValueError(f"Telegram topic not found: {sanitize_name(topic, limit=128)}")


async def message_chat(message: Any):
    chat = getattr(message, "chat", None)
    if chat is not None:
        return chat
    try:
        return await message.get_chat()
    except Exception:
        return None


def media_filter(kind: str):
    normalized = (kind or "any").strip().lower()
    if normalized in {"", "any", "media"}:
        return None
    from telethon.tl import types

    names = {
        "photo": "InputMessagesFilterPhotos",
        "image": "InputMessagesFilterPhotos",
        "video": "InputMessagesFilterVideo",
        "voice": "InputMessagesFilterVoice",
        "audio": "InputMessagesFilterMusic",
        "music": "InputMessagesFilterMusic",
        "document": "InputMessagesFilterDocument",
        "file": "InputMessagesFilterDocument",
        "gif": "InputMessagesFilterGif",
        "video_note": "InputMessagesFilterRoundVideo",
        # Filters whose matching rows are not attachments; see TEXT_FILTER_KINDS.
        "url": "InputMessagesFilterUrl",
        "link": "InputMessagesFilterUrl",
        "mentions": "InputMessagesFilterMentions",
        "my_mentions": "InputMessagesFilterMyMentions",
        "chat_photos": "InputMessagesFilterChatPhotos",
        "contacts": "InputMessagesFilterContacts",
        "geo": "InputMessagesFilterGeo",
        "phone_calls": "InputMessagesFilterPhoneCalls",
        "round_voice": "InputMessagesFilterRoundVoice",
    }
    cls_name = names.get(normalized)
    if cls_name is None:
        raise ValueError(f"unsupported media kind: {sanitize_name(kind, limit=64)}")
    cls = getattr(types, cls_name, None)
    if cls is None:
        raise ValueError(
            f"media filter is unavailable in this Telethon version: {sanitize_name(kind, limit=64)}"
        )
    return cls()


# Filters whose matched rows carry no `message.media`: a URL lives in
# `message.entities`, mentions/pins are plain text, chat photos and contacts are
# service-ish rows. Callers that filter rows by `media_info(message)` must skip
# that test for these kinds or every row is dropped.
TEXT_FILTER_KINDS = frozenset(
    {
        "url",
        "link",
        "mentions",
        "my_mentions",
        "chat_photos",
        "contacts",
        "geo",
        "phone_calls",
    }
)


def kind_has_media_payload(kind: str) -> bool:
    """False when the row filter must not require a downloadable attachment."""
    return (kind or "").strip().lower() not in TEXT_FILTER_KINDS


def configured_media_limit_bytes() -> int:
    raw = (os.getenv("HERMES_TG_USER_MAX_MEDIA_MB") or "50").strip()
    try:
        mb = max(1, min(int(raw), 2048))
    except ValueError:
        mb = 50
    return mb * 1024 * 1024
