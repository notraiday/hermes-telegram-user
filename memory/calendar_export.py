#!/usr/bin/env python3
"""The calendar for the memory search: every event, one markdown file per month.

``python3 calendar_export.py OUT_DIR`` reads every calendar of the Davis account
(a CalDAV calendar-query, no date window: the whole history), and writes
``OUT_DIR/YYYY-MM.md`` with the month's events in local time — title, time,
calendar, place, attendees, repetition, description. A file is replaced only when
its content changed, and a month that no longer has events is removed, so the
search index (qmd) re-embeds only what moved. Run it as the calendar collection's
update command.

Standard library only; the CalDAV client and the event parser are the archiver
check's (``archiver_check.py`` next to this file), settings the same: DAVIS_URL,
DAVIS_USER, DAVIS_PASSWORD from the environment or the Hermes .env, and the time
zone from HERMES_TIMEZONE.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _check_module():
    """archiver_check.py from this folder (or the repository's archiver/ folder)."""
    for candidate in (HERE / "archiver_check.py", HERE.parent / "archiver" / "archiver_check.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("archiver_check", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("archiver_check.py must be next to calendar_export.py")


def _tz(check):
    name = check.setting("HERMES_TIMEZONE")
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    return None


def parse_when(value: Optional[str], tz) -> tuple[Optional[datetime], bool]:
    """(local datetime, all_day) from an iCalendar value as the archiver check stores it."""
    if not value:
        return None, False
    raw, _, zone = value.partition(" (")
    zone = zone.rstrip(")")
    try:
        if len(raw) == 8:
            return datetime.combine(date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])), datetime.min.time()), True
        moment = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if raw.endswith("Z"):
        moment = moment.replace(tzinfo=timezone.utc)
    elif zone:
        try:
            from zoneinfo import ZoneInfo

            moment = moment.replace(tzinfo=ZoneInfo(zone))
        except Exception:
            return moment, False
    else:
        return moment, False  # floating time: already local
    return moment.astimezone(tz), False


def all_events(check, dav, user: str) -> list[dict[str, Any]]:
    body = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/>'
            '</c:comp-filter></c:filter></c:calendar-query>')
    events = []
    for href, name in check.calendars(dav, user):
        status, data = dav.request("REPORT", href, body, "1")
        if status != 207:
            raise RuntimeError(f"calendar-query {href}: HTTP {status}")
        for response in check._multistatus(data).findall(f"{check.DAV}response"):
            prop = check._ok(response)
            if prop is None:
                continue
            event = check.parse_event(prop.findtext(f"{check.CAL}calendar-data") or "")
            if event:
                event["calendar"] = name
                events.append(event)
    return events


def render(events: list[dict[str, Any]], tz) -> dict[str, str]:
    months: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for event in events:
        start, all_day = parse_when(event.get("dtstart"), tz)
        if start is None:
            continue
        end, _ = parse_when(event.get("dtend"), tz)
        when = start.strftime("%d.%m") + f" ({WEEKDAYS[start.weekday()]})"
        if not all_day:
            when += " " + start.strftime("%H:%M") + (f"–{end.strftime('%H:%M')}" if end else "")
        lines = [f"## {when} · {event.get('summary') or 'без названия'}"]
        details = [f"Календарь: {event['calendar']}"]
        if event.get("location"):
            details.append(f"Место: {event['location']}")
        if event.get("attendees"):
            details.append(f"Участников: {event['attendees']}")
        if event.get("rrule"):
            details.append(f"Повтор: {event['rrule']}")
        if event.get("status") and event["status"].upper() != "CONFIRMED":
            details.append(f"Статус: {event['status']}")
        lines.append(" · ".join(details))
        if event.get("description"):
            lines.append("Описание: " + event["description"].replace("\n", "\n  "))
        months[start.strftime("%Y-%m")].append((start.replace(tzinfo=None), "\n".join(lines)))
    files = {}
    for month, items in months.items():
        year, mm = month.split("-")
        items.sort(key=lambda item: item[0])
        head = ["---", "source: calendar", f"month: {month}", "---",
                f"# Календарь — {MONTHS[int(mm) - 1]} {year}", ""]
        files[f"{month}.md"] = "\n".join(head) + "\n\n".join(text for _, text in items) + "\n"
    return files


def write(out: Path, files: dict[str, str]) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    written = removed = 0
    for name, content in files.items():
        path = out / name
        try:
            if path.read_text(encoding="utf-8") == content:
                continue
        except OSError:
            pass
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        written += 1
    for stale in out.glob("????-??.md"):
        if stale.name not in files:
            stale.unlink()
            removed += 1
    return {"months": len(files), "written": written, "removed": removed}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: calendar_export.py OUT_DIR", file=sys.stderr)
        return 2
    check = _check_module()
    password = check.setting("DAVIS_PASSWORD")
    if not password:
        print(json.dumps({"error": "DAVIS_PASSWORD is not set"}))
        return 1
    user = check.setting("DAVIS_USER", "raiday")
    dav = check.Dav(check.setting("DAVIS_URL", "http://10.0.0.128:9000"), user, password)
    events = all_events(check, dav, user)
    result = write(Path(argv[1]).expanduser(), render(events, _tz(check)))
    print(json.dumps({"events": len(events), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
