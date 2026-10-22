"""copyparty_share, offline: a fake Copyparty on localhost, and the guard that keeps links home."""

import base64
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "copyparty_share"


def _share():
    if "copyparty_share_under_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location("copyparty_share_under_test", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["copyparty_share_under_test"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("copyparty_share_under_test.share")


@pytest.fixture
def copyparty(monkeypatch, tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), body))
            user, _, password = base64.b64decode(self.headers["Authorization"].split()[1]).decode().partition(":")
            vp = body["vp"][0]
            if password != "secret":
                reply, status = "you dont have all the perms you tried to grant", 400
            elif not vp.startswith("/agent/"):
                reply, status = "you dont have perms to create shares from this volume", 400
            elif vp.endswith("missing.pdf"):
                reply, status = "selected file not found on disk: 'missing.pdf'", 400
            else:
                name = "" if vp.endswith("/") else vp.rsplit("/", 1)[1]
                reply, status = f"created share: http://10.0.0.125:3923/share/{body['k']}/{name}", 201
            self.send_response(status)
            self.end_headers()
            self.wfile.write(reply.encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    share = _share()
    monkeypatch.setenv("COPYPARTY_AGENT_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {"plugins": {"entries": {"copyparty-share": {"settings": {
        "base_url": f"http://127.0.0.1:{server.server_port}/"}}}}}
    monkeypatch.setattr(share, "_hermes_config", lambda: config)
    monkeypatch.setattr(share, "data_dir", lambda: tmp_path)
    yield share, seen, config
    server.shutdown()


def test_a_file_in_the_agents_folder_becomes_a_read_only_link(copyparty, tmp_path):
    share, seen, _ = copyparty
    result = json.loads(share.handle({"path": "trip/tickets.pdf", "days": 3}))
    assert result["url"].startswith("http://10.0.0.125:3923/share/") and result["url"].endswith("/tickets.pdf")
    assert result["path"] == "/agent/trip/tickets.pdf"
    path, auth, ctype, body = seen[-1]
    assert path == "/?share" and ctype == "application/json"
    assert base64.b64decode(auth.split()[1]).decode() == "agent:secret"
    assert body["vp"] == ["/agent/trip/tickets.pdf"] and body["perms"] == ["read"] and body["exp"] == str(3 * 1440)
    assert len(body["k"]) == 20 and body["k"].isalnum()
    stored = json.loads((tmp_path / "shares.json").read_text())
    assert stored[0]["key"] == body["k"] and (tmp_path / "shares.json").stat().st_mode & 0o777 == 0o600


def test_paths_stay_inside_the_agents_folder(copyparty):
    share, seen, _ = copyparty
    assert share._inside("/agent", "/agent/a.txt") == "/agent/a.txt"
    assert share._inside("/agent", "trip/") == "/agent/trip/"
    assert share._inside("/agent", "/agent") == "/agent/"
    assert share._inside("/agent", "/music/song.mp3") == "/agent/music/song.mp3"
    assert "may not contain .." in json.loads(share.handle({"path": "../music/song.mp3"}))["error"]
    assert not seen
    assert json.loads(share.handle({"path": "x", "days": 999}))  # clamped, not refused
    assert seen[-1][3]["exp"] == str(30 * 1440)


def test_copyparty_refusals_come_back_with_a_hint(copyparty, monkeypatch):
    share, _, _ = copyparty
    error = json.loads(share.handle({"path": "missing.pdf"}))["error"]
    assert "not found on disk" in error and "webdav_list_remote_directory" in error
    monkeypatch.setenv("COPYPARTY_AGENT_PASSWORD", "wrong")
    error = json.loads(share.handle({"path": "a.pdf"}))["error"]
    assert "/agent/a.pdf" in error and "folder setting" in error and "password is wrong" in error
    monkeypatch.delenv("COPYPARTY_AGENT_PASSWORD")
    assert share.available() is False


def test_the_link_reaches_only_the_owner_and_the_calendar(copyparty):
    share, _, _ = copyparty
    url = json.loads(share.handle({"path": "tickets.pdf"}))["url"]
    key = url.split("/share/")[1].split("/")[0]

    def guard(tool, args):
        return share.guard(tool_name=tool, args=args)

    # The owner: send_message to a home channel, and the calendar.
    assert guard("send_message", {"target": "telegram", "message": f"Билеты: {url}"}) is None
    assert guard("mcp_calendar_create_event", {"summary": "Поезд", "description": url}) is None
    # Anyone else: other chats, Telegram accounts, mail, notes, the cloud, files.
    for tool, args in [
        ("send_message", {"target": "telegram:123456", "message": url}),
        ("tg_send_message", {"account": "agent", "chat": "@vasya", "text": url}),
        ("tg_dialog_message", {"delegation": "d1", "text": f"вот ссылка {url}"}),
        ("mcp_gmail_send_email", {"to": "x@example.com", "body": url}),
        ("mcp_vault_write_note", {"path": "a.md", "content": url}),
        ("ask_claude", {"question": url}),
        ("webdav_create_remote_file", {"path": "/agent/l.txt", "content": url}),
    ]:
        assert guard(tool, args)["action"] == "block", tool
    # Broken up, upper-cased or split across fields: still the same link.
    spaced = " ".join(key[i:i + 4] for i in range(0, len(key), 4)).upper()
    assert guard("tg_send_message", {"chat": "@vasya", "text": f"share / {spaced}"})["action"] == "block"
    # Wrapped by tool search's bridge, judged by the tool inside.
    assert guard("tool_call", {"name": "tg_send_message", "arguments": {"chat": "@v", "text": url}})["action"] == "block"
    assert guard("tool_call", {"name": "mcp_calendar_create_event", "arguments": {"description": url}}) is None
    # Calls without the link are none of the guard's business.
    assert guard("tg_send_message", {"chat": "@vasya", "text": "http://10.0.0.125:3923/share/other/"}) is None


def test_link_tools_are_a_setting(copyparty):
    share, _, config = copyparty
    url = json.loads(share.handle({"path": "tickets.pdf"}))["url"]
    config["plugins"]["entries"]["copyparty-share"]["settings"]["link_tools"] = "mcp_davis_*, mcp_vault_write_note"
    assert share.guard(tool_name="mcp_vault_write_note", args={"content": url}) is None
    assert share.guard(tool_name="mcp_davis_add", args={"x": url}) is None
    assert share.guard(tool_name="mcp_calendar_create_event", args={"x": url})["action"] == "block"


def test_no_links_yet_means_no_checks(copyparty):
    share, _, _ = copyparty
    assert share.guard(tool_name="tg_send_message", args={"text": "anything"}) is None
    assert share.guard("tg_send_message", {"text": "positional too"}) is None
