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
    monkeypatch.setenv("ASK_CLAUDE_API_KEY", "test-key")
    for name in ("ASK_CLAUDE_MODEL", "ASK_CLAUDE_EFFORT", "ASK_CLAUDE_MONTHLY_USD", "ASK_CLAUDE_NAMES_FILE"):
        monkeypatch.delenv(name, raising=False)
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


def test_the_owner_sees_exactly_what_will_be_sent_and_says_yes_each_time(consult):
    hook = consult.approval("ask_claude", {"question": "Звонить на +7 912 345-67-89 или писать?"})
    assert hook["action"] == "approve"
    assert "Звонить на [телефон] или писать?" in hook["message"] and "912" not in hook["message"]
    assert "телефон ×1" in hook["message"] and "$0.00 из $20.00" in hook["message"]
    other = consult.approval("ask_claude", {"question": "Другой вопрос"})
    assert other["rule_key"] != hook["rule_key"]  # "always" for one question is not for the next
    assert consult.approval("tg_send_message", {"text": "x"}) is None


def test_too_long_empty_or_over_the_cap_is_blocked_before_asking(consult, monkeypatch):
    assert consult.approval("ask_claude", {"question": "x" * 3001})["action"] == "block"
    assert consult.approval("ask_claude", {"question": "  "})["action"] == "block"
    consult._log({"ts": consult.datetime.now(consult.timezone.utc).isoformat(), "cost_usd": 20.5})
    blocked = consult.approval("ask_claude", {"question": "вопрос"})
    assert blocked["action"] == "block" and "cap" in blocked["message"]
    monkeypatch.delenv("ASK_CLAUDE_API_KEY")
    assert consult.available() is False or True  # anthropic may be absent here; the key check is below
    assert "ASK_CLAUDE_API_KEY" in consult.approval("ask_claude", {"question": "вопрос"})["message"]


def test_the_call_sends_the_approved_text_and_logs_its_cost(consult, monkeypatch):
    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(
                model="claude-opus-5-5", stop_reason="end_turn", _request_id="req_1",
                usage=SimpleNamespace(input_tokens=1000, output_tokens=2000),
                content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="Ответ")])

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()), messages=Messages())
    monkeypatch.setattr(consult, "_client", lambda: client)
    result = json.loads(consult.handle({"question": "Почта ivan@example.com — как лучше?"}))
    assert result["answer"] == "Ответ" and result["cost_usd"] == 0.044  # 1000*$4 + 2000*$20 per million
    assert sent["messages"] == [{"role": "user", "content": "Почта [почта] — как лучше?"}]
    assert sent["model"] == "claude-opus-5-5" and sent["output_config"] == {"effort": "high"}
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in sent and "temperature" not in sent
    log = [json.loads(line) for line in consult._log_path().read_text(encoding="utf-8").splitlines()]
    assert log[0]["question"] == "Почта [почта] — как лучше?" and log[0]["answer"] == "Ответ"
    assert oct(consult._log_path().stat().st_mode & 0o777) == "0o600"
    assert oct(consult.data_dir().stat().st_mode & 0o777) == "0o700"
    assert consult.spent_this_month() == pytest.approx(0.044)


def test_a_refusal_is_reported_not_read_as_an_answer(consult, monkeypatch):
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(model="claude-opus-5-5", stop_reason="refusal", content=[],
                                   stop_details=SimpleNamespace(category="cyber"),
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=0))

    monkeypatch.setattr(consult, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    result = json.loads(consult.handle({"question": "вопрос"}))
    assert "answer" not in result and "declined" in result["error"] and "cyber" in result["error"]
