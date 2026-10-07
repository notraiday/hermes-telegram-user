"""Telegram for the memory search: chats as markdown, photos and documents as text.

``hermes telegram-user export --markdown DIR`` writes one file per chat per month
under ``DIR/<account>/<chat>__<id>/YYYY-MM.md`` — Saved Messages, every private
chat and the chats of saved collections, the same scope as the archiver's inbox,
delegated chats included. The search index (qmd) reads those files; nothing here
talks to it.

Three phases per run, each bounded so a run fits between two index updates:

* **sync** — the plugin's own archive (SQLite) is brought up to date: chats with
  new messages first, then the history of chats not archived to the beginning
  yet, newest end first, within ``sync_seconds``. Resumable: the next run goes
  on where this one stopped;
* **media** (``--ocr``) — photos, image documents and PDFs become text: a PDF with
  a text layer through ``pdftotext``, everything else through the local vision
  model in Ollama (a document is transcribed, a photo described). The original
  is kept next to the text, and the chat file points at both. New attachments
  (two days) are done whenever the run happens; old ones only at night
  (``HERMES_TG_USER_OCR_NIGHT``, default 23-8), when the GPU is free;
* **render** — chats whose archive or media changed are written out again; a
  file is replaced only when its content differs.

The text is the archive's, not re-fetched: what the export shows is exactly
what ``tg_archive_search`` would find.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .core.accounts import current_account, default_account
from .core.archive import chat_kind, sync_chat
from .core.client import entity_label
from .core.helpers import peer_id
from .core.media import media_info
from .core.sanitize import sanitize_name, sanitize_text
from .core.state.archive import get_watermarks, iter_messages, open_archive
from .core.state.paths import private_file, state_dir
from .tools import _inbox_member_threads, _inbox_scopes, _inbox_tz

__all__ = ["export_account"]

_STATE_FILE = "export_state.json"
_SYNC_PER_CHAT = 1000
_THREAD_HISTORY_DAYS = 365  # a forum thread in a collection: last year only
_FRESH_MEDIA = timedelta(days=2)
_MEDIA_KINDS = {"photo", "document"}
_MAX_MEDIA_BYTES = 25 * 1024 * 1024
_PDF_PAGES = 3
_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
           "сентябрь", "октябрь", "ноябрь", "декабрь")
_VISION_PROMPT = (
    "Это изображение из переписки в Telegram. Если на нём документ или текст (паспорт, "
    "чек, билет, договор, справка, скриншот, объявление, рецепт, таблица) — первой строкой "
    "напиши «ДОКУМЕНТ: <что это>», дальше перепиши весь текст дословно, сохраняя строки. "
    "Иначе первой строкой напиши «ФОТО:» и в 1–3 предложениях опиши, что на нём: место, "
    "люди, предметы, событие. Ничего не выдумывай и не добавляй от себя.")


# --- settings and state ---------------------------------------------------------------


def _setting(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _state_path() -> Path:
    return state_dir() / _STATE_FILE


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    private_file(tmp)
    os.replace(tmp, path)


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


# --- names, times and links -------------------------------------------------------------


def _slug(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "_", str(title or ""), flags=re.UNICODE).strip(" _")
    return re.sub(r"\s+", " ", cleaned)[:60] or "chat"


def _local(epoch: Optional[float]) -> Optional[datetime]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(_inbox_tz())


def _yaml(value: Any) -> str:
    return json.dumps(str(value if value is not None else ""), ensure_ascii=False)


def message_link(chat_id: str, message_id: int, kind: str) -> str:
    """A link that opens the message in a Telegram app."""
    cid = str(chat_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    if cid.startswith("-"):
        return f"tg://openmessage?chat_id={cid[1:]}&message_id={message_id}"
    return f"tg://openmessage?user_id={cid}&message_id={message_id}"


def _night(now: datetime) -> bool:
    raw = _setting("HERMES_TG_USER_OCR_NIGHT", "23-8")
    try:
        start, end = (int(x) for x in raw.split("-", 1))
    except ValueError:
        start, end = 23, 8
    hour = now.astimezone(_inbox_tz()).hour
    return (hour >= start or hour < end) if start > end else (start <= hour < end)


# --- media to text -----------------------------------------------------------------------


def vision_text(image: bytes) -> str:
    """Transcribe or describe one image with the local vision model (Ollama /api/chat)."""
    model = _setting("HERMES_TG_USER_VISION_MODEL")
    if not model:
        raise RuntimeError("HERMES_TG_USER_VISION_MODEL is not set")
    url = _setting("HERMES_TG_USER_OLLAMA_URL", "http://10.10.10.2:11434").rstrip("/") + "/api/chat"
    body = json.dumps({
        "model": model, "stream": False, "think": False, "options": {"temperature": 0},
        "messages": [{"role": "user", "content": _VISION_PROMPT,
                      "images": [base64.b64encode(image).decode("ascii")]}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    # Ollama is on the internal network: never through an inherited proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=600) as response:
        answer = json.loads(response.read().decode("utf-8"))
    text = str((answer.get("message") or {}).get("content") or "")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _pdf_text(data: bytes) -> str:
    """Text of a PDF: its text layer, else the first pages read by the vision model."""
    with tempfile.TemporaryDirectory(prefix="tgu-pdf-") as tmp:
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(data)
        if shutil.which("pdftotext"):
            done = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                                  capture_output=True, text=True, timeout=120)
            if done.returncode == 0 and len(done.stdout.strip()) > 40:
                return "ДОКУМЕНТ: PDF\n" + done.stdout.strip()
        if not shutil.which("pdftoppm"):
            raise RuntimeError("scanned PDF and no pdftoppm (apt install poppler-utils)")
        subprocess.run(["pdftoppm", "-png", "-r", "150", "-l", str(_PDF_PAGES), str(pdf),
                        str(Path(tmp) / "page")], capture_output=True, timeout=300, check=True)
        pages = sorted(Path(tmp).glob("page*.png"))
        texts = [vision_text(page.read_bytes()) for page in pages]
        return "\n\n".join(t for t in texts if t)


def _extension(info: dict[str, Any]) -> str:
    name = str(info.get("file_name") or "")
    if "." in name:
        return "." + re.sub(r"[^A-Za-z0-9]", "", name.rsplit(".", 1)[1])[:8]
    mime = str(info.get("mime_type") or "")
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "application/pdf": ".pdf"}.get(mime, ".jpg" if info.get("kind") == "photo" else ".bin")


async def _media_item(client, entity, chat_dir: Path, chat: dict[str, Any], row: dict[str, Any]) -> str:
    """Turn one attachment into chat_dir/media/<id>.md; returns 'done' or why not."""
    message = await client.get_messages(entity, ids=int(row["message_id"]))
    info = media_info(message) if message is not None else None
    if not info:
        return "skip: no media"
    mime = str(info.get("mime_type") or "")
    is_pdf = mime == "application/pdf"
    if info["kind"] != "photo" and not (mime.startswith("image/") or is_pdf):
        return f"skip: {info['kind']} {mime}"
    if int(info.get("size") or 0) > _MAX_MEDIA_BYTES:
        return "skip: too large"
    data = await client.download_media(message, file=bytes)
    if not data:
        return "skip: empty download"
    text = _pdf_text(bytes(data)) if is_pdf else vision_text(bytes(data))
    if not text:
        return "skip: nothing recognised"
    original = Path("files") / f"{row['message_id']}{_extension(info)}"
    (chat_dir / "files").mkdir(parents=True, exist_ok=True)
    (chat_dir / original).write_bytes(bytes(data))
    when = _local(row.get("date"))
    first = text.splitlines()[0].strip()
    head = [
        "---", "source: telegram-media", f"chat: {_yaml(chat['title'])}", f"chat_id: {_yaml(chat['chat_id'])}",
        f"message_id: {row['message_id']}", f"date: {_yaml(when.isoformat() if when else '')}",
        f"original: {_yaml(original.as_posix())}",
        f"link: {_yaml(message_link(chat['chat_id'], int(row['message_id']), chat['kind']))}", "---",
        f"# {first[:120]} — {chat['title']}, {when.strftime('%d.%m.%Y') if when else ''}",
        "",
    ]
    _write_if_changed(chat_dir / "media" / f"{row['message_id']}.md", "\n".join(head) + text.strip() + "\n")
    return "done"


# --- render --------------------------------------------------------------------------------


def _media_summary(chat_dir: Path, message_id: int) -> Optional[str]:
    try:
        text = (chat_dir / "media" / f"{message_id}.md").read_text(encoding="utf-8")
    except OSError:
        return None
    body = text.split("\n---\n", 1)[-1].split("\n", 2)[-1].strip()
    return re.sub(r"\s+", " ", body)[:160]


def _line(row: dict[str, Any], chat_dir: Path) -> str:
    when = _local(row.get("date"))
    who = "я" if row.get("outgoing") else (row.get("sender") or row.get("sender_id") or "?")
    parts = [f"[{when.strftime('%d.%m %H:%M') if when else '?'}] #{row['message_id']} {who}"]
    if row.get("reply_to_msg_id"):
        parts.append(f"↩#{row['reply_to_msg_id']}")
    media = row.get("media_type")
    if media:
        summary = _media_summary(chat_dir, int(row["message_id"]))
        label = {"photo": "фото", "document": "файл", "voice": "голосовое", "video": "видео",
                 "sticker": "стикер", "audio": "аудио"}.get(media, media)
        parts.append(f"[{label}: {summary} → media/{row['message_id']}.md]" if summary else f"[{label}]")
    text = (row.get("text") or "").strip().replace("\n", "\n  ")
    return " ".join(parts) + (f": {text}" if text else "")


def _render_chat(con, chat: dict[str, Any], chat_dir: Path, threads: Optional[set[int]]) -> int:
    months: dict[str, list[str]] = defaultdict(list)
    rows = list(iter_messages(con, chat_id=chat["chat_id"]))
    rows.reverse()
    for row in rows:
        if threads is not None and not ({row.get("topic_id"), row.get("reply_to_msg_id"), row["message_id"]} & threads):
            continue
        when = _local(row.get("date"))
        if when is None:
            continue
        months[when.strftime("%Y-%m")].append(_line(row, chat_dir))
    written = 0
    for month, lines in months.items():
        year, mm = month.split("-")
        head = [
            "---", "source: telegram", f"account: {_yaml(chat['account'])}", f"chat: {_yaml(chat['title'])}",
            f"chat_id: {_yaml(chat['chat_id'])}", f"kind: {chat['kind']}", f"month: {month}", "---",
            f"# {chat['title']} — {_MONTHS[int(mm) - 1]} {year}",
            "«я» — владелец; «→ media/…» — распознанный текст вложения рядом в папке media.",
            "",
        ]
        written += _write_if_changed(chat_dir / f"{month}.md", "\n".join(head + lines) + "\n")
    return written


# --- the run ---------------------------------------------------------------------------------


async def export_account(out_root: Path, *, sync_seconds: float = 120, ocr: bool = False,
                         ocr_seconds: float = 240, now: Optional[datetime] = None) -> dict[str, Any]:
    from .tools import tool_client  # looked up at call time, so tests can swap it

    account = current_account() or default_account() or "default"
    now = now or datetime.now(timezone.utc)
    out = Path(out_root) / account
    state = _load_state()
    chats_state: dict[str, Any] = state.setdefault("chats", {})
    skipped: dict[str, str] = state.setdefault("media_skip", {})
    report = {"account": account, "synced": 0, "added": 0, "media_done": 0, "media_skipped": 0,
              "files_written": 0, "errors": []}

    member_threads = _inbox_member_threads({})
    meta: dict[str, dict[str, Any]] = {}
    con = open_archive()
    try:
        async with tool_client() as client:
            scopes = await _inbox_scopes(client, {}, member_threads, skip_delegated=False)
            chats: dict[str, dict[str, Any]] = {}
            for (key, tkey), scope in scopes.items():
                entry = chats.setdefault(key, {"entity": scope["dialog"].entity, "top": scope["top"],
                                               "threads": set(), "whole": False})
                if tkey is None:
                    entry["whole"] = True
                else:
                    entry["threads"].add(int(tkey))

            # sync: chats with something new first, then unfinished history
            def behind(key: str) -> bool:
                newest = get_watermarks(con, key)["newest"]
                return newest is None or (chats[key]["top"] or 0) > int(newest)

            order = sorted(chats, key=lambda k: (not behind(k), -(chats[k]["top"] or 0)))
            deadline = time.monotonic() + sync_seconds
            for key in order:
                if time.monotonic() > deadline:
                    break
                marks = get_watermarks(con, key)
                if not behind(key) and marks["whole"]:
                    continue
                entry = chats[key]
                since = None if entry["whole"] else (now - timedelta(days=_THREAD_HISTORY_DAYS))
                try:
                    result = await sync_chat(client, con, entry["entity"], max_sync=_SYNC_PER_CHAT, since=since)
                    report["synced"] += 1
                    report["added"] += int(result.get("added") or 0)
                except Exception as exc:  # FloodWait and friends: stop syncing for this run
                    report["errors"].append(f"sync {key}: {str(exc)[:200]}")
                    break

            for key, entry in chats.items():
                entity = entry["entity"]
                title = sanitize_name(entity_label(entity), limit=120)
                if getattr(entity, "is_self", False) or getattr(entity, "self", False):
                    title = "Избранное"
                meta[key] = {"chat_id": key, "title": title, "kind": chat_kind(entity), "account": account,
                             "dir": out / f"{_slug(title)}__{key}",
                             "threads": None if entry["whole"] else entry["threads"]}

            # media: new attachments always, the rest only at night
            if ocr and _setting("HERMES_TG_USER_VISION_MODEL"):
                media_deadline = time.monotonic() + ocr_seconds
                fresh_floor = (now - _FRESH_MEDIA).timestamp()
                night = _night(now)
                queue = []
                for key, chat in meta.items():
                    for row in iter_messages(con, chat_id=key):
                        if row.get("media_type") not in _MEDIA_KINDS:
                            continue
                        item = f"{key}:{row['message_id']}"
                        if item in skipped or (chat["dir"] / "media" / f"{row['message_id']}.md").exists():
                            continue
                        if not night and (row.get("date") or 0) < fresh_floor:
                            continue
                        queue.append((row.get("date") or 0, key, row))
                queue.sort(key=lambda q: q[0], reverse=True)
                for _, key, row in queue:
                    if time.monotonic() > media_deadline:
                        break
                    try:
                        outcome = await _media_item(client, chats[key]["entity"], meta[key]["dir"], meta[key], row)
                    except Exception as exc:
                        report["errors"].append(f"media {key}:{row['message_id']}: {str(exc)[:200]}")
                        if "HERMES_TG_USER_VISION_MODEL" in str(exc) or "Connection" in str(exc):
                            break  # the model is unreachable: no point in trying the rest now
                        outcome = f"skip: {type(exc).__name__}"
                    if outcome == "done":
                        report["media_done"] += 1
                        chats_state.setdefault(key, {})["media_rev"] = chats_state.get(key, {}).get("media_rev", 0) + 1
                    else:
                        skipped[f"{key}:{row['message_id']}"] = outcome
                        report["media_skipped"] += 1
    except Exception as exc:  # Telegram unreachable: still render what the archive has
        report["errors"].append(f"telegram: {str(exc)[:300]}")

    # render: only chats whose archive or media changed
    try:
        for key, chat in meta.items():
            marks = get_watermarks(con, key)
            signature = [marks["messages"], marks["newest"], marks["oldest"],
                         chats_state.get(key, {}).get("media_rev", 0)]
            previous = chats_state.get(key, {})
            old_dir = previous.get("dir")
            if old_dir and old_dir != chat["dir"].name and (out / old_dir).exists():
                shutil.move(str(out / old_dir), str(chat["dir"]))  # the chat was renamed
            if previous.get("signature") == signature and chat["dir"].exists():
                continue
            if not marks["messages"]:
                continue
            report["files_written"] += _render_chat(con, chat, chat["dir"], chat["threads"])
            chats_state[key] = {**previous, "signature": signature, "dir": chat["dir"].name}
    finally:
        con.close()
        _save_state(state)
    return report
