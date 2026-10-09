"""ask_claude, offline: what is masked, what the owner sees, what is sent, the cap."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ask_claude"


def _consult():
    if "ask_claude_under_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location("ask_claude_under_test", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["ask_claude_under_test"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("ask_claude_under_test.consult")


@pytest.fixture
def consult(tmp_path, monkeypatch):
    module = _consult()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("ASK_CLAUDE_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-claude-code")
    monkeypatch.setattr(module, "_hermes_config", lambda: {})
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)  # outside Hermes
    return module


def test_personal_data_is_masked_and_ordinary_text_is_not(consult):
    text = ("Петя (тел. +7 912 345-67-89, petya@mail.ru, @petya_smirnov) прислал карту 4276 3800 1234 5678 "
            "и СНИЛС 112-233-445 95, паспорт 45 07 123456, ИНН 7707083893. Сервер на 10.0.0.128:9000, "
            "ссылка https://t.me/c/123/45 и https://docs.python.org/3/library/re.html?x=secret#y. "
            "Ключ sk-ant-api03-abcdefghijklmnop. Ипотека 5 000 000 руб на 20 лет под 18,5% с 2026-10-09.")
    out, found = consult.scrub(text, extra_names=["Петя"])
    for leaked in ("912", "petya", "4276", "112-233", "123456", "7707083893", "10.0.0.128", "t.me",
                   "secret", "sk-ant", "Петя"):
        assert leaked not in out, (leaked, out)
    assert "https://docs.python.org/3/library/re.html. Ключ" in out  # a public link stays, minus its query
    assert "5 000 000 руб на 20 лет под 18,5% с 2026-10-09" in out  # amounts, rates and dates stay
    assert found["телефон"] == 1 and found["карта"] == 1 and found["имя"] == 1
    assert "[паспорт]" in out and "[СНИЛС]" in out and "[номер]" in out and "[ник]" in out


def test_names_are_masked_in_their_russian_forms(consult):
    out, _ = consult.scrub("Спроси Машу, Маше и Марии понравилось", extra_names=["Маша", "Мария"])
    assert out == "Спроси [имя], [имя] и [имя] понравилось"


def _fake_claude(tmp_path, monkeypatch, consult, reply):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")

    monkeypatch.setattr(consult.subprocess, "run", run)
    monkeypatch.setattr(consult, "_hermes_config", lambda: {"plugins": {"entries": {"ask-claude": {"settings": {
        "claude_path": str(claude), "model": "fable", "effort": "XHIGH"}}}}})
    return calls


def test_the_question_goes_to_claude_code_alone_and_masked(consult, tmp_path, monkeypatch):
    calls = _fake_claude(tmp_path, monkeypatch, consult,
                         {"type": "result", "subtype": "success", "is_error": False, "result": "Ответ",
                          "duration_ms": 41000, "total_cost_usd": 0.31})
    result = json.loads(consult.handle({"question": "Почта ivan@example.com — как лучше?"}))
    assert result == {"model": "fable", "effort": "xhigh", "answer": "Ответ", "seconds": 41, "masked": {"почта": 1}}
    argv, kwargs = calls[0]
    assert kwargs["input"] == "Почта [почта] — как лучше?"  # only the masked question, on stdin
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "xhigh" and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    env = kwargs["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert "ANTHROPIC_API_KEY" not in env and "ASK_CLAUDE_TOKEN" not in env and "HERMES_HOME" not in env
    assert env["CLAUDE_CONFIG_DIR"].endswith("plugin-data/ask-claude/claude") and env["HOME"] == kwargs["cwd"]
    assert not Path(kwargs["cwd"]).exists()  # the empty room is gone
    log = [json.loads(line) for line in consult._log_path().read_text(encoding="utf-8").splitlines()]
    assert log[0]["question"] == "Почта [почта] — как лучше?" and log[0]["answer"] == "Ответ"
    assert oct(consult._log_path().stat().st_mode & 0o777) == "0o600"
    assert oct(consult.data_dir().stat().st_mode & 0o777) == "0o700"


def test_a_failed_run_is_reported_not_read_as_an_answer(consult, tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, consult,
                 {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
    result = json.loads(consult.handle({"question": "вопрос"}))
    assert "answer" not in result and result["error"] == "error_max_turns"


def test_without_a_token_or_claude_the_tool_is_off(consult, monkeypatch):
    monkeypatch.setattr(consult, "claude_command", lambda configured="": None)
    assert consult.available() is False
    assert "not installed" in json.loads(consult.handle({"question": "вопрос"}))["error"]
    monkeypatch.setattr(consult, "claude_command", lambda configured="": "/usr/bin/true")
    monkeypatch.delenv("ASK_CLAUDE_TOKEN")
    assert consult.available() is False
    assert "setup-token" in json.loads(consult.handle({"question": "вопрос"}))["error"]


def test_settings_come_from_the_hermes_config_with_sane_defaults(consult, monkeypatch):
    assert consult.settings() == {"model": "opus", "effort": "high", "claude_path": "", "timeout_minutes": 15,
                                  "proxy": ""}
    monkeypatch.setattr(consult, "_hermes_config", lambda: {"plugins": {"entries": {"ask-claude": {"settings": {
        "model": "sonnet", "effort": "lots", "timeout_minutes": 999, "other": 1}}}}})
    assert consult.settings() == {"model": "sonnet", "effort": "high", "claude_path": "", "timeout_minutes": 60,
                                  "proxy": ""}


def test_long_or_empty_questions_are_refused(consult):
    assert "shorten" in json.loads(consult.handle({"question": "x" * 6001}))["error"]
    assert "needs a question" in json.loads(consult.handle({"question": " "}))["error"]


def test_the_proxy_is_claude_codes_alone_and_http_only(consult, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://hermes-own:3128")
    env = consult.child_env(tmp_path, tmp_path, consult.proxy_url("http://u:p@10.0.0.108:8080"))
    assert env["HTTPS_PROXY"] == env["https_proxy"] == env["HTTP_PROXY"] == "http://u:p@10.0.0.108:8080"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert consult.child_env(tmp_path, tmp_path)["HTTPS_PROXY"] == "http://hermes-own:3128"  # none set: inherited
    with pytest.raises(ValueError, match="SOCKS"):
        consult.proxy_url("socks5://10.0.0.108:1080")
    calls = _fake_claude(tmp_path, monkeypatch, consult, {"subtype": "success", "result": "ok"})
    monkeypatch.setattr(consult, "_hermes_config", lambda: {"plugins": {"entries": {"ask-claude": {"settings": {
        "claude_path": str(tmp_path / "claude"), "proxy": "socks5://x:1"}}}}})
    assert "SOCKS" in json.loads(consult.handle({"question": "вопрос"}))["error"] and not calls


def test_a_network_failure_points_at_the_proxy_setting(consult, tmp_path, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    _fake_claude(tmp_path, monkeypatch, consult, {"subtype": "success", "is_error": True,
        "result": "API Error: No internet route — check your connection or VPN (EHOSTUNREACH)"})
    result = json.loads(consult.handle({"question": "вопрос"}))
    assert "EHOSTUNREACH" in result["error"] and "No proxy is set" in result["hint"]
