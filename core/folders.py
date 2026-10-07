from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .client import entity_label
from .state.aliases import aliases_for_peer
from .sanitize import sanitize_name


@dataclass(frozen=True)
class FolderView:
    id: int
    title: str
    flags: dict[str, bool]
    include: frozenset[int]
    exclude: frozenset[int]
    pinned: frozenset[int]


def _peer_ids(peers: Any) -> frozenset[int]:
    from telethon import utils

    values: set[int] = set()
    for peer in peers or []:
        try:
            values.add(int(utils.get_peer_id(peer)))
        except Exception:
            continue
    return frozenset(values)


async def load_folders(client: Any) -> list[FolderView]:
    """Load Telegram dialog filters without changing account state."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    result = await client(GetDialogFiltersRequest())
    filters = getattr(result, "filters", result) or []
    out: list[FolderView] = []
    for item in filters:
        folder_id = getattr(item, "id", None)
        if folder_id is None or type(item).__name__ == "DialogFilterDefault":
            continue
        title_obj = getattr(item, "title", "")
        title = sanitize_name(getattr(title_obj, "text", title_obj) or "", limit=256)
        flags = {
            name: bool(getattr(item, name, False))
            for name in (
                "contacts",
                "non_contacts",
                "groups",
                "broadcasts",
                "bots",
                "exclude_muted",
                "exclude_read",
                "exclude_archived",
            )
        }
        out.append(
            FolderView(
                id=int(folder_id),
                title=title,
                flags=flags,
                include=_peer_ids(getattr(item, "include_peers", None)),
                exclude=_peer_ids(getattr(item, "exclude_peers", None)),
                pinned=_peer_ids(getattr(item, "pinned_peers", None)),
            )
        )
    return out


def resolve_folder(folders: list[FolderView], token: str) -> FolderView:
    """Resolve a folder by stable numeric id or an unambiguous visible title."""
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("folder is required")
    if raw.lstrip("-").isdigit():
        wanted = int(raw)
        for folder in folders:
            if folder.id == wanted:
                return folder
    needle = raw.casefold()
    exact = [f for f in folders if f.title.casefold() == needle]
    matches = exact or [f for f in folders if needle in f.title.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Telegram folder is ambiguous: {sanitize_name(raw, limit=128)}")
    raise ValueError(f"Telegram folder not found: {sanitize_name(raw, limit=128)}")


def dialog_is_muted(dialog: Any) -> bool:
    raw = getattr(dialog, "dialog", None)
    settings = getattr(raw, "notify_settings", None)
    until = getattr(settings, "mute_until", None)
    if until is None:
        return False
    now = datetime.now(timezone.utc)
    if isinstance(until, datetime):
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until.astimezone(timezone.utc) > now
    try:
        return int(until) > int(now.timestamp())
    except (TypeError, ValueError):
        return False


def dialog_waiting(dialog: Any) -> bool:
    raw = getattr(dialog, "dialog", None)
    return bool(
        int(getattr(dialog, "unread_count", 0) or 0)
        or int(getattr(raw, "unread_mentions_count", 0) or 0)
        or bool(getattr(raw, "unread_mark", False))
    )


def folder_contains(folder: FolderView, dialog: Any) -> bool:
    """Evaluate Telegram's custom-folder rules for one dialog.

    Explicit exclusions win. Explicit includes/pins then win over category-level
    rules. Category folders respect exclude-muted/read/archived, including the
    user's manual "mark as unread" state and unread mentions.
    """
    chat_id = int(dialog.id)
    if chat_id in folder.exclude:
        return False
    if chat_id in folder.include or chat_id in folder.pinned:
        return True

    entity = dialog.entity
    if dialog.is_group:
        admitted = folder.flags["groups"]
    elif dialog.is_channel:
        admitted = folder.flags["broadcasts"]
    elif dialog.is_user:
        if bool(getattr(entity, "bot", False)):
            admitted = folder.flags["bots"]
        elif bool(getattr(entity, "contact", False)):
            admitted = folder.flags["contacts"]
        else:
            admitted = folder.flags["non_contacts"]
    else:
        admitted = False
    if not admitted:
        return False

    raw = getattr(dialog, "dialog", None)
    mentions = int(getattr(raw, "unread_mentions_count", 0) or 0)
    archived = int(getattr(raw, "folder_id", 0) or 0) == 1
    if folder.flags["exclude_muted"] and dialog_is_muted(dialog) and mentions == 0:
        return False
    if folder.flags["exclude_read"] and not dialog_waiting(dialog):
        return False
    if folder.flags["exclude_archived"] and archived:
        return False
    return True


def dialog_summary(dialog: Any) -> dict[str, Any]:
    raw = getattr(dialog, "dialog", None)
    aliases = aliases_for_peer(str(dialog.id))
    saved = bool(getattr(dialog.entity, "is_self", False))
    return {
        "id": str(dialog.id),
        "name": "Saved Messages (Избранное)" if saved else sanitize_name(
            dialog.name or entity_label(dialog.entity), limit=256),
        "saved_messages": saved,
        "aliases": aliases,
        "username": (
            sanitize_name(getattr(dialog.entity, "username", None), limit=128)
            if getattr(dialog.entity, "username", None)
            else None
        ),
        "is_group": bool(dialog.is_group),
        "is_channel": bool(dialog.is_channel),
        "is_user": bool(dialog.is_user),
        "unread": int(getattr(dialog, "unread_count", 0) or 0),
        "mentions": int(getattr(raw, "unread_mentions_count", 0) or 0),
        "unread_mark": bool(getattr(raw, "unread_mark", False)),
        "muted": dialog_is_muted(dialog),
        "archived": int(getattr(raw, "folder_id", 0) or 0) == 1,
    }
