#!/usr/bin/env python3
"""Pre-run check for the archiver cron job: is there anything new, and what exactly?

Hermes runs this before every archiver tick (``hermes cron create ... --script``).
Its stdout becomes the "Script Output" block at the top of the agent's prompt; a
last line of ``{"wakeAgent": false}`` skips the model entirely, so a quiet tick
costs no GPU time. Three sources are checked, none of them with the model:

* the Obsidian vault — committed into a local git repository that lives outside
  the vault (nothing is pushed anywhere); the report is the diff since the last
  tick. The first run only creates the repository and reports nothing;
* the CalDAV calendar (Davis) — a WebDAV sync-collection report per calendar
  returns only what changed since the last tick; event snapshots kept here turn
  that into "these fields changed". The first run only stores the sync tokens;
* Telegram — ``hermes telegram-user inbox --peek`` says which chats have new
  messages; the agent reads them itself with tg_read_inbox.

The archiver writes to the vault and the calendar too. So that it does not take
its own edits for the owner's on the next tick, it lists what it changed in
``Агент/Архиватор/Последние правки.md``; files and events named there are left
out of the next report. Edits under ``Агент/`` are never reported.

Standard library only: Hermes runs .py scripts with its own interpreter and a
sanitized environment, so the Davis password is read from the Hermes .env here.
Settings come from the environment or that .env (ARCHIVER_VAULT, DAVIS_URL,
DAVIS_USER, DAVIS_PASSWORD, ARCHIVER_QUIET_HOURS, ARCHIVER_STATE_DIR,
ARCHIVER_HERMES_BIN). In the quiet hours (default 23-8) the report tells the agent
to work silently; the cron schedule decides whether it runs then at all.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
AGENT_DIR = "Агент"
WRITES_NOTE = "Агент/Архиватор/Последние правки.md"
SKIP_GATE = '{"wakeAgent": false}'

MAX_FILE_DIFF = 2500  # characters of diff shown per vault file
MAX_VAULT_REPORT = 12000
MAX_EVENTS = 30
MAX_TEXT = 600  # characters of an event description shown

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"


# --- settings ---------------------------------------------------------------------


def _dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = (HERMES_HOME / ".env").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip() or key.strip().startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_ENV_FILE: Optional[dict[str, str]] = None


def setting(key: str, default: str = "") -> str:
    global _ENV_FILE
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    if _ENV_FILE is None:
        _ENV_FILE = _dotenv()
    return (_ENV_FILE.get(key) or default).strip()


def state_dir() -> Path:
    return Path(setting("ARCHIVER_STATE_DIR") or HERMES_HOME / "state" / "archiver").expanduser()


def load_state() -> dict[str, Any]:
    try:
        return json.loads((state_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".state-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, folder / "state.json")


# --- what the archiver wrote last time --------------------------------------------


def own_writes(vault: Path) -> tuple[set[str], set[str]]:
    """Vault paths and event UIDs listed in the archiver's own "last edits" note."""
    try:
        text = (vault / WRITES_NOTE).read_text(encoding="utf-8")
    except OSError:
        return set(), set()
    paths: set[str] = set()
    uids: set[str] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            section = line.lower()
            continue
        if not line.startswith(("- ", "* ")):
            continue
        item = line[2:].strip().strip("`").strip()
        if item.startswith("[[") and item.endswith("]]"):
            item = item[2:-2].split("|")[0].strip()
        if not item:
            continue
        if "событ" in section or "event" in section:
            uids.add(item)
        else:
            paths.add(item if item.endswith(".md") or "." in Path(item).name else item + ".md")
    return paths, uids


# --- vault: a local git history outside the vault ---------------------------------


def _git(vault: Path, gitdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "core.quotepath=false",
         f"--git-dir={gitdir}", f"--work-tree={vault}", *args],
        capture_output=True, text=True, check=check, timeout=120)


def _ensure_repo(vault: Path, gitdir: Path) -> None:
    if (gitdir / "HEAD").exists():
        return
    gitdir.parent.mkdir(parents=True, exist_ok=True)
    _git(vault, gitdir, "init", "-q")
    for key, value in (("user.name", "archiver"), ("user.email", "archiver@localhost"),
                       ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        _git(vault, gitdir, "config", key, value)
    # Tool and sync folders are not notes; the vault stays free of any .git.
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "exclude").write_text(
        ".obsidian/\n.trash/\n.stfolder\n.stversions/\n.git/\n*.tmp\n", encoding="utf-8")


def _commit(vault: Path, gitdir: Path) -> Optional[str]:
    _git(vault, gitdir, "add", "-A")
    if _git(vault, gitdir, "diff", "--cached", "--quiet", check=False).returncode:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        _git(vault, gitdir, "commit", "-q", "--no-verify", "-m", f"archiver {stamp}")
    head = _git(vault, gitdir, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return head.stdout.strip() or None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (ещё {len(text) - limit} символов — прочитай заметку целиком)"


def vault_report(state: dict[str, Any], vault: Path, skip_paths: set[str]) -> Optional[str]:
    """Diff of the owner's vault edits since the last tick; commits as a side effect."""
    if not vault.is_dir():
        return f"Vault: папка {vault} недоступна — изменения не проверены."
    gitdir = state_dir() / "vault.git"
    _ensure_repo(vault, gitdir)
    head = _commit(vault, gitdir)
    previous = state.get("vault_commit")
    state["vault_commit"] = head
    if not previous or not head or previous == head:
        return None  # first run (baseline only) or nothing changed

    names = _git(vault, gitdir, "diff", "--name-status", "--no-renames", previous, head).stdout
    changes = []
    for line in names.splitlines():
        status, _, path = line.partition("\t")
        if not path or path == AGENT_DIR or path.startswith(AGENT_DIR + "/") or path in skip_paths:
            continue
        changes.append((status.strip()[:1], path))
    if not changes:
        return None

    labels = {"A": "новый файл", "M": "изменён", "D": "удалён"}
    parts = [f"## Vault: изменения владельца ({len(changes)})",
             "Диффы с прошлого прохода; «+» — добавлено, «-» — удалено. Целиком заметку "
             "читай инструментами vault."]
    used = 0
    for status, path in changes:
        header = f"### {path} — {labels.get(status, status)}"
        if status == "D" or not path.endswith(".md"):
            parts.append(header)
            continue
        diff = _git(vault, gitdir, "diff", "-U2", "--no-color", previous, head, "--", path).stdout
        body = "\n".join(l for l in diff.splitlines()
                         if not l.startswith(("diff --git", "index ", "--- ", "+++ ", "new file mode")))
        body = _clip(body, MAX_FILE_DIFF)
        if used + len(body) > MAX_VAULT_REPORT:
            parts.append(header + " (дифф не поместился — прочитай заметку)")
            continue
        used += len(body)
        parts.append(f"{header}\n```diff\n{body}\n```")
    return "\n\n".join(parts)


# --- calendar: WebDAV sync-collection against Davis ----------------------------------


class Dav:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/") + "/"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/xml; charset=utf-8"}
        # Davis is on the LAN: never route it through an inherited HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, url: str, body: str, depth: str) -> tuple[int, bytes]:
        req = urllib.request.Request(urljoin(self.base, url), data=body.encode("utf-8"),
                                     method=method, headers={**self.headers, "Depth": depth})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""


def _multistatus(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _ok(response: ET.Element) -> Optional[ET.Element]:
    """The <prop> of the 200 propstat, if any."""
    for propstat in response.findall(f"{DAV}propstat"):
        status = propstat.findtext(f"{DAV}status") or ""
        if " 200 " in status + " ":
            return propstat.find(f"{DAV}prop")
    return None


def calendars(dav: Dav, user: str) -> list[tuple[str, str]]:
    body = ('<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/>'
            '</d:prop></d:propfind>')
    status, data = dav.request("PROPFIND", f"dav/calendars/{user}/", body, "1")
    if status != 207:
        raise RuntimeError(f"PROPFIND списка календарей: HTTP {status}")
    found = []
    for response in _multistatus(data).findall(f"{DAV}response"):
        prop = _ok(response)
        href = (response.findtext(f"{DAV}href") or "").strip()
        if prop is None or not href:
            continue
        kind = prop.find(f"{DAV}resourcetype")
        if kind is None or kind.find(f"{CAL}calendar") is None:
            continue
        found.append((href, (prop.findtext(f"{DAV}displayname") or href.rstrip("/").split("/")[-1]).strip()))
    return found


def sync(dav: Dav, href: str, token: str) -> tuple[Optional[str], list[str], list[str]]:
    """(new token, changed hrefs, deleted hrefs); token None means the old one was refused."""
    body = ('<d:sync-collection xmlns:d="DAV:">'
            f'<d:sync-token>{_xml_escape(token)}</d:sync-token><d:sync-level>1</d:sync-level>'
            '<d:prop><d:getetag/></d:prop></d:sync-collection>')
    status, data = dav.request("REPORT", href, body, "0")
    if status in (403, 409) and token:
        return None, [], []
    if status != 207:
        raise RuntimeError(f"sync-collection {href}: HTTP {status}")
    root = _multistatus(data)
    changed, deleted = [], []
    for response in root.findall(f"{DAV}response"):
        item = (response.findtext(f"{DAV}href") or "").strip()
        if not item or item.rstrip("/") == href.rstrip("/"):
            continue
        if " 404 " in (response.findtext(f"{DAV}status") or "") + " ":
            deleted.append(item)
        elif _ok(response) is not None:
            changed.append(item)
    return (root.findtext(f"{DAV}sync-token") or "").strip(), changed, deleted


def multiget(dav: Dav, href: str, items: list[str]) -> dict[str, str]:
    hrefs = "".join(f"<d:href>{_xml_escape(i)}</d:href>" for i in items)
    body = ('<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<d:prop><d:getetag/><c:calendar-data/></d:prop>{hrefs}</c:calendar-multiget>')
    status, data = dav.request("REPORT", href, body, "1")
    if status != 207:
        raise RuntimeError(f"calendar-multiget {href}: HTTP {status}")
    out = {}
    for response in _multistatus(data).findall(f"{DAV}response"):
        prop = _ok(response)
        if prop is not None:
            out[(response.findtext(f"{DAV}href") or "").strip()] = prop.findtext(f"{CAL}calendar-data") or ""
    return out


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_event(ics: str) -> Optional[dict[str, Any]]:
    """The fields of the first VEVENT that matter to the archiver."""
    lines: list[str] = []
    for raw in ics.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    event: dict[str, Any] = {}
    inside = depth = 0
    attendees = 0
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT" and not inside:
            inside, depth = 1, 0
            continue
        if not inside:
            continue
        if upper.startswith("BEGIN:"):
            depth += 1  # VALARM and friends: skip their properties
            continue
        if upper.startswith("END:"):
            if depth:
                depth -= 1
                continue
            break
        if depth:
            continue
        name, _, value = line.partition(":")
        key, *params = name.split(";")
        key = key.upper()
        if key == "ATTENDEE":
            attendees += 1
        elif key in ("UID", "SUMMARY", "LOCATION", "DESCRIPTION", "STATUS", "RRULE"):
            event[key.lower()] = _ics_unescape(value)
        elif key in ("DTSTART", "DTEND"):
            tzid = next((p.split("=", 1)[1] for p in params if p.upper().startswith("TZID=")), "")
            event[key.lower()] = f"{value} ({tzid})" if tzid else value
    if not event:
        return None
    event["attendees"] = attendees
    return event


def _ics_unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


FIELD_NAMES = {"summary": "название", "dtstart": "начало", "dtend": "конец", "location": "место",
               "description": "описание", "status": "статус", "rrule": "повтор", "attendees": "участники"}


def _short(value: Any) -> str:
    text = str(value if value not in (None, "") else "—")
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def calendar_report(state: dict[str, Any], skip_uids: set[str]) -> Optional[str]:
    password = setting("DAVIS_PASSWORD")
    if not password:
        return "Календарь: в .env нет DAVIS_PASSWORD — изменения не проверены."
    user = setting("DAVIS_USER", "raiday")
    dav = Dav(setting("DAVIS_URL", "http://10.0.0.128:9000"), user, password)
    saved = state.setdefault("calendars", {})
    lines: list[str] = []
    for href, name in calendars(dav, user):
        entry = saved.setdefault(href, {"token": "", "events": {}})
        token, changed, deleted = sync(dav, href, entry.get("token") or "")
        if token is None:  # the server forgot our token: start over quietly
            token, changed, deleted = sync(dav, href, "")
            entry["events"] = {}
            changed, deleted = [], []
        first_run = not entry.get("token")
        entry["token"] = token or ""
        if first_run:
            continue  # baseline only: do not report the whole calendar
        events = entry.setdefault("events", {})
        bodies = multiget(dav, href, changed[:MAX_EVENTS * 2]) if changed else {}
        for item in changed:
            event = parse_event(bodies.get(item, ""))
            if not event:
                continue
            before = events.get(item)
            events[item] = event
            if event.get("uid") in skip_uids:
                continue
            title = f"«{_short(event.get('summary'))}» ({name}; uid {event.get('uid', '?')})"
            if before is None:
                lines.append(f"- Новое или впервые увиденное: {title}\n"
                             + _event_fields(event, sorted(FIELD_NAMES)))
                continue
            diff = [k for k in FIELD_NAMES if before.get(k) != event.get(k)]
            if diff:
                lines.append(f"- Изменено: {title}\n" + "\n".join(
                    f"    {FIELD_NAMES[k]}: {_short(before.get(k))} → {_short(event.get(k))}" for k in diff)
                    + "\n" + _event_fields(event, [k for k in ("dtstart", "location") if k not in diff]))
        for item in deleted:
            before = events.pop(item, None)
            if before and before.get("uid") not in skip_uids:
                lines.append(f"- Удалено: «{_short(before.get('summary'))}» ({name}), "
                             f"было {before.get('dtstart', '?')}")
    if not lines:
        return None
    if len(lines) > MAX_EVENTS:
        lines = lines[:MAX_EVENTS] + [f"- … и ещё {len(lines) - MAX_EVENTS}, посмотри календарь сам"]
    return (f"## Календарь: изменения ({len(lines)})\n"
            "Время в формате iCalendar: …Z — UTC, иначе местное время указанного пояса.\n"
            + "\n".join(lines))


def _event_fields(event: dict[str, Any], keys: list[str]) -> str:
    return "\n".join(f"    {FIELD_NAMES[k]}: {_short(event.get(k))}" for k in keys if k in FIELD_NAMES)


# --- telegram: hermes telegram-user inbox --peek -------------------------------------


def telegram_report() -> Optional[str]:
    binary = setting("ARCHIVER_HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    try:
        done = subprocess.run([binary, "telegram-user", "inbox", "--peek"],
                              capture_output=True, text=True, timeout=180)
        lines = [l for l in done.stdout.splitlines() if l.strip()]
        result = json.loads(lines[-1]) if lines else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"## Telegram\nПроверка новых сообщений не удалась ({type(exc).__name__}) — вызови tg_read_inbox сам."
    if not isinstance(result, dict):
        err = (done.stderr or "").strip().splitlines()[-1:] or ["нет вывода"]
        return f"## Telegram\nПроверка новых сообщений не удалась ({err[0][:200]}) — вызови tg_read_inbox сам."
    rows = []
    for row in result.get("accounts") or []:
        account = row.get("account", "?")
        if row.get("error"):
            rows.append(f"- {account}: проверить не удалось ({str(row['error'])[:200]}) — вызови tg_read_inbox(account=\"{account}\") сам")
        elif row.get("new_chats"):
            rows.append(f"- {account}: новые сообщения в {row['new_chats']} чатах — tg_read_inbox(account=\"{account}\"), "
                        f"после разбора tg_mark_inbox(account=\"{account}\")")
    if not rows:
        return None
    return "## Telegram\n" + "\n".join(rows)


# --- main ------------------------------------------------------------------------------


def quiet_now(now: datetime) -> bool:
    """Inside ARCHIVER_QUIET_HOURS ("23-8", local time): the archiver works but stays silent."""
    raw = setting("ARCHIVER_QUIET_HOURS", "23-8")
    try:
        start, end = (int(x) % 24 for x in raw.split("-", 1))
    except ValueError:
        return False
    hour = now.hour
    return start <= hour < end if start < end else (hour >= start or hour < end)



def main() -> int:
    state = load_state()
    vault = Path(setting("ARCHIVER_VAULT", "/srv/vault"))
    skip_paths, skip_uids = own_writes(vault)

    sections: list[str] = []
    for name, check in (("Vault", lambda: vault_report(state, vault, skip_paths)),
                        ("Календарь", lambda: calendar_report(state, skip_uids)),
                        ("Telegram", telegram_report)):
        try:
            text = check()
        except Exception as exc:  # one broken source must not hide the others
            text = f"## {name}\nПроверка не удалась: {type(exc).__name__}: {str(exc)[:300]}"
        if text:
            sections.append(text)
    save_state(state)

    if not sections:
        print(SKIP_GATE)
        return 0
    now = datetime.now().astimezone()
    mode = ("Тихие часы: да — отчёт не отправляй, ответь [SILENT]; вопросы только в Агент/Входящие.md."
            if quiet_now(now) else "Тихие часы: нет.")
    print(f"# Новое для архиватора ({now.strftime('%Y-%m-%d %H:%M')})\n{mode}\n\n" + "\n\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
