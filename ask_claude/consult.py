"""ask_claude: scrub, ask the owner, send, log.

The question is the only thing that leaves: the agent writes it self-contained
and impersonal, ``scrub`` masks what fixed rules can recognise (phones, e-mail,
card, passport, SNILS and INN numbers, Telegram handles and links, home network
addresses, keys and tokens, names from the owner's list), and ``approval`` shows
the owner the result word for word. The handler sends exactly that result: the
scrubbing is deterministic, so what was approved is what goes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
DEFAULT_MONTHLY_USD = 20.0
MAX_QUESTION = 3000
MAX_TOKENS = 16000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# USD per million tokens (input, output); a fallback model is priced as itself.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Models that take server-side refusal fallbacks ("default" routing).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

SYSTEM = (
    "Тебя консультирует локальный ИИ-помощник человека: он сам не справляется с вопросом. "
    "Вопрос обезличен — личные данные заменены метками в квадратных скобках ([телефон], [имя], "
    "[номер] и т.п.); не пытайся их восстановить. Уточнить ничего нельзя: если данных не хватает, "
    "назови допущения и разбери варианты. Отвечай по-русски, по существу и проверяемо: где важна "
    "точность, укажи, что стоит перепроверить."
)

DESCRIPTION = (
    "Ask cloud Claude, a much stronger model, one hard question: tricky reasoning or math, a "
    "plan or decision with many trade-offs, a code or document review, a second opinion when you "
    "are unsure. Every call costs money and the owner approves it, so use it when your own answer "
    "would likely be weak, not for lookups (search the web for those). The question leaves the "
    "house: write it self-contained and impersonal — no names, contacts, addresses, account or "
    "document numbers, chat excerpts, nothing that identifies the owner or other people; describe "
    "the situation in general terms. Known personal data is masked automatically ([телефон], "
    "[имя], ...), but you are the first filter. Claude sees only this text (up to 3000 characters) "
    "and remembers nothing between calls, so include the context it needs. Numbers of 7 or more "
    "digits are masked as ids; write big quantities as 4.3e9 or in words. Treat the answer as "
    "advice: check facts before acting on them."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The whole question with its context, impersonal, up to 3000 characters.",
        },
    },
    "required": ["question"],
}


# --- settings and files ----------------------------------------------------------------


def _setting(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def model() -> str:
    return _setting("ASK_CLAUDE_MODEL", DEFAULT_MODEL)


def effort() -> str:
    value = _setting("ASK_CLAUDE_EFFORT", DEFAULT_EFFORT).lower()
    return value if value in EFFORTS else DEFAULT_EFFORT


def monthly_cap() -> float:
    try:
        return max(0.0, float(_setting("ASK_CLAUDE_MONTHLY_USD", str(DEFAULT_MONTHLY_USD))))
    except ValueError:
        return DEFAULT_MONTHLY_USD


def data_dir() -> Path:
    """<hermes home>/plugin-data/ask-claude, private to its owner."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        path = Path(plugin_data_dir("ask-claude"))
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        path = home / "plugin-data" / "ask-claude"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _log_path() -> Path:
    return data_dir() / "log.jsonl"


def names() -> list[str]:
    """Names to mask, one per line in the names file; # starts a comment."""
    path = Path(_setting("ASK_CLAUDE_NAMES_FILE") or data_dir() / "names.txt").expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def spent_this_month(now: Optional[datetime] = None) -> float:
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    total = 0.0
    try:
        with _log_path().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("ts", "")).startswith(month):
                    total += float(row.get("cost_usd") or 0)
    except OSError:
        pass
    return total


def _log(row: dict[str, Any]) -> None:
    path = _log_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# --- scrubbing ---------------------------------------------------------------------------


def _luhn(digits: str) -> bool:
    total, odd = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if odd:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        odd = not odd
    return total % 10 == 0


_PRIVATE_HOST = re.compile(
    r"^(?:localhost|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|127(?:\.\d{1,3}){3}|[\w.-]+\.(?:lan|local|home|internal|localdomain))(?::\d+)?$", re.I)


def _url(match: re.Match) -> tuple[str, str]:
    url = match.group(0)
    tail = re.search(r"[.,;:!?]*$", url).group(0)  # sentence punctuation is not part of the link
    url = url[: len(url) - len(tail)]
    host = re.sub(r"^[a-z]+://", "", url, flags=re.I).split("/", 1)[0].split("@")[-1]
    if _PRIVATE_HOST.match(host):
        return "[домашний адрес]" + tail, "домашний адрес"
    if re.match(r"^(?:www\.)?(?:t\.me|telegram\.me)$", host, re.I):
        return "[ссылка Telegram]" + tail, "ссылка Telegram"
    kept = re.split(r"[?#]", url, maxsplit=1)[0]  # query strings carry tokens and ids
    return kept + tail, ""


def _card(match: re.Match) -> tuple[str, str]:
    digits = re.sub(r"\D", "", match.group(0))
    grouped = re.fullmatch(r"\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?", match.group(0))
    if 13 <= len(digits) <= 19 and (grouped or _luhn(digits)):
        return "[карта]", "карта"
    return match.group(0), ""


def _long_number(match: re.Match) -> tuple[str, str]:
    text = match.group(0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):  # an ISO date stays
        return text, ""
    return "[номер]", "номер"


# (pattern, replacement or function, label) — applied in this order.
_RULES: list[tuple[re.Pattern, Any, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[ключ]", "ключ"),
    (re.compile(r"\b(?:sk-ant-[\w-]{10,}|sk-[\w-]{20,}|gh[pousr]_\w{20,}|xox[abpr]-[\w-]{10,}|AKIA[0-9A-Z]{16}"
                r"|\d{8,10}:[\w-]{30,})"), "[ключ]", "ключ"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[почта]", "почта"),
    (re.compile(r"\btg://\S+", re.I), "[ссылка Telegram]", "ссылка Telegram"),
    (re.compile(r"\b(?:https?://)[^\s<>\"')\]]+", re.I), _url, ""),
    (re.compile(r"\b(?:t\.me|telegram\.me)/\S+", re.I), "[ссылка Telegram]", "ссылка Telegram"),
    (re.compile(r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
                r"(?::\d+)?(?![\w.])"), "[домашний адрес]", "домашний адрес"),
    (re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b"), "[ник]", "ник"),
    (re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), "[СНИЛС]", "СНИЛС"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _card, ""),
    (re.compile(r"(?<![\w+])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "[телефон]", "телефон"),
    (re.compile(r"(?<![\w+])\+\d[\d\s()-]{8,16}\d(?!\d)"), "[телефон]", "телефон"),
    (re.compile(r"(?<!\d)\d{2}\s\d{2}\s(?:№\s?)?\d{6}(?!\d)"), "[паспорт]", "паспорт"),
    (re.compile(r"(?<!\d)(?<!\d[.,])\d[\d-]{5,}\d(?!\d|[.,]\d)"), _long_number, ""),  # not inside decimals
    (re.compile(r"(?<![\w/+=-])(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{32,}"),
     "[ключ]", "ключ"),
]


def _name_pattern(name: str) -> Optional[re.Pattern]:
    name = name.strip()
    if len(name) < 2:
        return None
    stem = name[:-1] if len(name) >= 4 and name[-1].lower() in "аяеёиоуыэюйь" else name
    return re.compile(r"(?<!\w)" + re.escape(stem) + r"\w{0,3}(?!\w)", re.I)


def scrub(text: str, extra_names: Optional[list[str]] = None) -> tuple[str, Counter]:
    """The text with recognisable personal data masked, and what was masked."""
    found: Counter = Counter()
    out = str(text or "")
    for pattern, replacement, label in _RULES:
        def sub(match: re.Match) -> str:
            if callable(replacement):
                value, what = replacement(match)
            else:
                value, what = replacement, label
            if what:
                found[what] += 1
            return value
        out = pattern.sub(sub, out)
    for name in (extra_names if extra_names is not None else names()):
        pattern = _name_pattern(name)
        if pattern is None:
            continue
        out, count = pattern.subn("[имя]", out)
        if count:
            found["имя"] += count
    return out.strip(), found


# --- the owner's yes ----------------------------------------------------------------------


def prepare(args: dict[str, Any]) -> tuple[str, Counter]:
    question = str((args or {}).get("question") or "").strip()
    if not question:
        raise ValueError("ask_claude needs a question")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"the question is {len(question)} characters; shorten it to {MAX_QUESTION} "
                         "(the owner reads all of it before it is sent)")
    text, found = scrub(question)
    if not text:
        raise ValueError("nothing is left of the question after masking personal data")
    return text, found


def _found_line(found: Counter) -> str:
    return ", ".join(f"{what} ×{n}" for what, n in sorted(found.items()))


def _budget_error() -> Optional[str]:
    if not _setting("ASK_CLAUDE_API_KEY"):
        return "ASK_CLAUDE_API_KEY is not set"
    spent, cap = spent_this_month(), monthly_cap()
    if spent >= cap:
        return (f"the monthly cap for ask_claude is reached (${spent:.2f} of ${cap:.2f}); "
                "the owner can raise ASK_CLAUDE_MONTHLY_USD")
    return None


def approval(*args: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
    """pre_tool_call: every ask_claude call shows the owner the exact text and waits for a yes.

    Hermes lets a call through when a hook raises, so nothing here may: any
    failure blocks the call. In cron there is nobody to say yes, and Hermes
    refuses the call there.
    """
    try:
        tool_name = kwargs.get("tool_name", args[0] if args else None)
        if tool_name != "ask_claude":
            return None
        call = kwargs.get("args", args[1] if len(args) > 1 else None) or {}
        call = call if isinstance(call, dict) else {}
        problem = _budget_error()
        if problem:
            return {"action": "block", "message": problem}
        try:
            text, found = prepare(call)
        except ValueError as exc:
            return {"action": "block", "message": str(exc)}
        spent, cap = spent_this_month(), monthly_cap()
        lines = [
            f"Отправить вопрос в облачный Claude ({model()}, {effort()})? "
            f"Потрачено в этом месяце ${spent:.2f} из ${cap:.2f}. Уйдёт ровно этот текст:",
            "",
            text,
        ]
        if found:
            lines += ["", f"Замаскировано: {_found_line(found)}."]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        # A per-call key: "always" for one question must not approve the next one.
        return {"action": "approve", "message": "\n".join(lines), "rule_key": f"ask_claude:{digest}"}
    except Exception:
        return {"action": "block", "message": "Could not prepare the approval for ask_claude."}


# --- the call -------------------------------------------------------------------------------


def available() -> bool:
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return bool(_setting("ASK_CLAUDE_API_KEY"))


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=_setting("ASK_CLAUDE_API_KEY"), timeout=900.0, max_retries=2)


def _cost(usage: Any, served: str, requested: str) -> float:
    price_in, price_out = PRICES.get(served) or PRICES.get(requested) or PRICES[DEFAULT_MODEL]
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_in += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tokens_in += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 4)


def ask(text: str) -> dict[str, Any]:
    requested = model()
    request: dict[str, Any] = {
        "model": requested,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "output_config": {"effort": effort()},
        "messages": [{"role": "user", "content": text}],
    }
    client = _client()
    if requested in _FALLBACK_MODELS:
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default", **request)
    else:
        response = client.messages.create(**request)
    served = str(getattr(response, "model", "") or requested)
    cost = _cost(getattr(response, "usage", None), served, requested)
    result: dict[str, Any] = {"model": served, "cost_usd": cost}
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        result["error"] = ("Claude declined to answer"
                           + (f" ({details.category})" if getattr(details, "category", None) else ""))
    else:
        answer = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        result["answer"] = answer.strip()
        if response.stop_reason == "max_tokens":
            result["note"] = "the answer was cut off at the length limit"
    result["request_id"] = getattr(response, "_request_id", None)
    return result


def handle(args: Optional[dict[str, Any]] = None, **kwargs: Any) -> str:
    try:
        problem = _budget_error()
        if problem:
            return json.dumps({"error": problem}, ensure_ascii=False)
        text, found = prepare(dict(args or {}))
        result = ask(text)
        spent = spent_this_month() + float(result.get("cost_usd") or 0)
        try:  # the answer is paid for: a log that cannot be written must not lose it
            _log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "requested_model": model(),
                  "effort": effort(), "question": text, "masked": dict(found), **result})
        except OSError as exc:
            result["note"] = (result.get("note", "") + f"; not logged: {exc}").lstrip("; ")
        result["spent_this_month_usd"] = round(spent, 2)
        result.pop("request_id", None)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False)
