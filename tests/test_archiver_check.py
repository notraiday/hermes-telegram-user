"""The archiver's pre-run check, offline: a throwaway vault, a fake Davis, a fake hermes.

Pins what decides whether the model is woken at all: a first run only takes a
baseline, the owner's edits are reported as diffs and calendar field changes,
the archiver's own edits (``Агент/`` and its "last edits" note) never are, and a
quiet tick prints exactly the wake gate.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = '{"wakeAgent": false}'

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _load():
    spec = importlib.util.spec_from_file_location("archiver_check", ROOT / "archiver" / "archiver_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _env(**values):
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def world(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Люди").mkdir(parents=True)
    (vault / "Люди" / "Петя.md").write_text("# Петя\n\nДрузья с 2019.\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\ncat \"$PEEK_FILE\"\n", encoding="utf-8")
    fake.chmod(0o755)
    peek = tmp_path / "peek.json"
    peek.write_text(json.dumps({"new_chats": 0, "accounts": [{"account": "personal", "new_chats": 0}]}))
    home = tmp_path / "home"
    home.mkdir()
    with _env(HERMES_HOME=home, ARCHIVER_STATE_DIR=tmp_path / "state", ARCHIVER_VAULT=vault,
              ARCHIVER_HERMES_BIN=fake, PEEK_FILE=peek, DAVIS_PASSWORD=""):
        module = _load()
        module._ENV_FILE = {}
        yield module, vault, peek, tmp_path


def _run(module, capsys):
    assert module.main() == 0
    return capsys.readouterr().out.strip()


def test_first_run_takes_a_baseline_and_stays_silent(world, capsys):
    module, vault, _, tmp = world
    out = _run(module, capsys)
    # Davis has no password here, so the calendar line wakes the agent; the vault does not.
    assert "Vault" not in out and "DAVIS_PASSWORD" in out
    assert (tmp / "state" / "vault.git" / "HEAD").exists()
    assert not (vault / ".git").exists()


def test_owner_edits_are_reported_as_diffs_but_own_edits_are_not(world, capsys):
    module, vault, _, _ = world
    with _env(DAVIS_PASSWORD=""):
        module.calendar_report = lambda state, uids: None
        assert _run(module, capsys) == GATE
        assert _run(module, capsys) == GATE  # nothing changed: the model is not woken

        (vault / "Люди" / "Петя.md").write_text("# Петя\n\nДрузья с 2019.\nТелефон: 123\n", encoding="utf-8")
        (vault / "Дневник.md").write_text("Сегодня гуляли.\n", encoding="utf-8")
        (vault / "Агент").mkdir()
        (vault / "Агент" / "Очередь.md").write_text("- задача\n", encoding="utf-8")
        (vault / ".obsidian" / "app.json").write_text('{"x": 1}', encoding="utf-8")
        out = _run(module, capsys)
        assert "### Люди/Петя.md — изменён" in out and "+Телефон: 123" in out
        assert "### Дневник.md — новый файл" in out
        assert "Агент" not in out.split("## Vault", 1)[1].split("##", 1)[0].replace("Агент/Архиватор", "")
        assert ".obsidian" not in out
        assert _run(module, capsys) == GATE  # reported once, not again

        # The archiver lists what it wrote; those files are not taken for the owner's edits.
        (vault / "Агент" / "Архиватор").mkdir()
        (vault / "Агент" / "Архиватор" / "Последние правки.md").write_text(
            "# Последние правки\n## Файлы\n- Люди/Петя.md\n- [[Проекты/Дача]]\n## События\n- uid-1\n",
            encoding="utf-8")
        (vault / "Люди" / "Петя.md").write_text("# Петя\n\nДрузья с 2019.\nТелефон: 123\nДР: 1 мая\n",
                                                 encoding="utf-8")
        assert _run(module, capsys) == GATE
        assert module.own_writes(vault) == ({"Люди/Петя.md", "Проекты/Дача.md"}, {"uid-1"})


def test_ics_fields_survive_folding_alarms_and_escapes():
    module = _load()
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Ужин с\r\n  Петей\r\n"
           "DTSTART;TZID=Europe/Moscow:20261009T190000\r\nDTEND:20261009T170000Z\r\n"
           "DESCRIPTION:строка 1\\nстрока 2\\, дальше\r\nATTENDEE:mailto:a@b\r\n"
           "BEGIN:VALARM\r\nDESCRIPTION:напоминание\r\nEND:VALARM\r\nLOCATION:\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    event = module.parse_event(ics)
    assert event["summary"] == "Ужин с Петей"
    assert event["dtstart"] == "20261009T190000 (Europe/Moscow)"
    assert event["description"] == "строка 1\nстрока 2, дальше"
    assert event["location"] == "" and event["attendees"] == 1


class _Davis:
    """Just enough sabre/dav: the calendar home, sync-collection, calendar-multiget."""

    def __init__(self):
        self.version = 1
        self.events = {"/dav/calendars/raiday/default/e1.ics": self._ics("u1", "Стрижка", "")}
        self.changes = {}  # version -> (changed hrefs, deleted hrefs)

    @staticmethod
    def _ics(uid, summary, location):
        return (f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:{uid}\nSUMMARY:{summary}\n"
                f"DTSTART:20261010T090000Z\nLOCATION:{location}\nEND:VEVENT\nEND:VCALENDAR\n")

    def put(self, href, ics):
        self.version += 1
        self.events[href] = ics
        self.changes[self.version] = ([href], [])

    def delete(self, href):
        self.version += 1
        self.events.pop(href)
        self.changes[self.version] = ([], [href])

    def handler(self):
        davis = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, xml):
                data = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                        'xmlns:c="urn:ietf:params:xml:ns:caldav">' + xml + "</d:multistatus>").encode()
                self.send_response(207)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PROPFIND(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                assert "resourcetype" in body and self.path == "/dav/calendars/raiday/"
                ok = "<d:status>HTTP/1.1 200 OK</d:status>"
                self._reply(
                    f"<d:response><d:href>/dav/calendars/raiday/</d:href><d:propstat><d:prop>"
                    f"<d:resourcetype><d:collection/></d:resourcetype></d:prop>{ok}</d:propstat></d:response>"
                    f"<d:response><d:href>/dav/calendars/raiday/default/</d:href><d:propstat><d:prop>"
                    f"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                    f"<d:displayname>Личное</d:displayname></d:prop>{ok}</d:propstat></d:response>")

            def do_REPORT(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                ok = "<d:status>HTTP/1.1 200 OK</d:status>"
                if "sync-collection" in body:
                    token = body.split("<d:sync-token>")[1].split("</d:sync-token>")[0]
                    since = int(token.rsplit("/", 1)[-1]) if token else 0
                    xml = ""
                    if since:
                        changed, deleted = set(), set()
                        for v in range(since + 1, davis.version + 1):
                            c, d = davis.changes.get(v, ([], []))
                            changed |= set(c)
                            deleted |= set(d)
                        for href in sorted(changed - deleted):
                            xml += (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                                    f"<d:getetag>\"x\"</d:getetag></d:prop>{ok}</d:propstat></d:response>")
                        for href in sorted(deleted):
                            xml += (f"<d:response><d:href>{href}</d:href>"
                                    "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
                    else:
                        for href in davis.events:
                            xml += (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                                    f"<d:getetag>\"x\"</d:getetag></d:prop>{ok}</d:propstat></d:response>")
                    self._reply(xml + f"<d:sync-token>http://sabre/sync/{davis.version}</d:sync-token>")
                    return
                xml = ""
                for href in body.split("<d:href>")[1:]:
                    href = href.split("</d:href>")[0]
                    if href in davis.events:
                        data = davis.events[href].replace("&", "&amp;").replace("<", "&lt;")
                        xml += (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                                f"<c:calendar-data>{data}</c:calendar-data></d:prop>{ok}</d:propstat></d:response>")
                self._reply(xml)

        return Handler


def test_calendar_changes_are_reported_by_field_and_own_events_skipped(world):
    module, *_ = world
    davis = _Davis()
    server = ThreadingHTTPServer(("127.0.0.1", 0), davis.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with _env(DAVIS_URL=f"http://127.0.0.1:{server.server_port}", DAVIS_USER="raiday",
                  DAVIS_PASSWORD="secret", HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9"):
            state = {}
            assert module.calendar_report(state, set()) is None  # baseline only
            assert module.calendar_report(state, set()) is None  # nothing changed

            href = "/dav/calendars/raiday/default/e1.ics"
            davis.put(href, davis._ics("u1", "Стрижка", "Барбершоп на Ленина"))
            davis.put("/dav/calendars/raiday/default/e2.ics", davis._ics("u2", "Ужин", ""))
            report = module.calendar_report(state, set())
            assert "Новое или впервые увиденное: «Стрижка»" in report  # no snapshot yet
            assert "«Ужин» (Личное; uid u2)" in report

            davis.put(href, davis._ics("u1", "Стрижка", "Барбершоп на Мира"))
            report = module.calendar_report(state, set())
            assert "Изменено: «Стрижка»" in report
            assert "место: Барбершоп на Ленина → Барбершоп на Мира" in report

            davis.put(href, davis._ics("u1", "Стрижка", "Барбершоп, д. 5"))
            assert module.calendar_report(state, {"u1"}) is None  # the archiver's own edit

            davis.delete("/dav/calendars/raiday/default/e2.ics")
            assert "Удалено: «Ужин»" in module.calendar_report(state, set())
    finally:
        server.shutdown()


def test_telegram_new_chats_and_failures_wake_the_agent(world):
    module, _, peek, _ = world
    assert module.telegram_report() is None
    peek.write_text(json.dumps({"new_chats": 3, "accounts": [
        {"account": "personal", "new_chats": 3}, {"account": "agent", "error": "proxy down"}]}))
    report = module.telegram_report()
    assert 'personal: новые сообщения в 3 чатах — tg_read_inbox(account="personal")' in report
    assert "agent: проверить не удалось (proxy down)" in report
    peek.write_text("not json")
    assert "не удалась" in module.telegram_report()


def test_the_script_runs_standalone_under_a_bare_environment(tmp_path):
    """Hermes runs it with a sanitized env: settings must come from the .env file."""
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("x\n", encoding="utf-8")
    (home / ".env").write_text(f"ARCHIVER_VAULT={vault}\nARCHIVER_HERMES_BIN=/bin/true\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "HERMES_HOME": str(home)}
    done = subprocess.run([sys.executable, "-I", str(ROOT / "archiver" / "archiver_check.py")],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "DAVIS_PASSWORD" in done.stdout  # calendar not configured: said, not crashed
    assert (home / "state" / "archiver" / "vault.git" / "HEAD").exists()


def test_quiet_hours_wrap_midnight_and_reach_the_report(world, capsys):
    from datetime import datetime

    module, _, peek, _ = world
    assert module.quiet_now(datetime(2026, 10, 7, 23, 30)) and module.quiet_now(datetime(2026, 10, 8, 7, 59))
    assert not module.quiet_now(datetime(2026, 10, 8, 8, 0)) and not module.quiet_now(datetime(2026, 10, 8, 22, 59))
    with _env(ARCHIVER_QUIET_HOURS="1-3"):
        assert module.quiet_now(datetime(2026, 10, 8, 2, 0)) and not module.quiet_now(datetime(2026, 10, 8, 3, 0))
    peek.write_text(json.dumps({"new_chats": 1, "accounts": [{"account": "personal", "new_chats": 1}]}))
    module.calendar_report = lambda state, uids: None
    out = _run(module, capsys)
    assert out.splitlines()[1].startswith("Тихие часы: ")
