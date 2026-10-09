"""ask_claude: scrub the question, ask Claude Code on the owner's subscription, log.

Claude Code is the only client a Claude subscription may be used through, so
the question goes to a ``claude -p`` run of the unmodified binary: safe mode (no
CLAUDE.md, skills, plugins, hooks, MCP servers or memory), no tools, an empty
working directory, its own config directory, and nothing in its environment but
what it needs. The token is passed to that process only, and is kept under a
name Hermes does not read (``ASK_CLAUDE_TOKEN``): Hermes picks up
``CLAUDE_CODE_OAUTH_TOKEN`` and ``~/.claude`` as Anthropic credentials of its
own, and could then send its own traffic to Claude.

The question is the only thing that leaves: the agent writes it self-contained
and impersonal, and ``scrub`` masks what fixed rules can recognise (phones,
e-mail, card, passport, SNILS and INN numbers, Telegram handles and links, home
network addresses, keys and tokens, names from the owner's list).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "ask-claude"
MAX_QUESTION = 6000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULTS = {"model": "opus", "effort": "high", "claude_path": "", "timeout_minutes": 15}

SYSTEM = (
    "Тебя консультирует локальный ИИ-помощник человека: он сам не справляется с вопросом. "
    "Вопрос обезличен — личные данные заменены метками в квадратных скобках ([телефон], [имя], "
    "[номер] и т.п.); не пытайся их восстановить. Инструментов и файлов у тебя нет, уточнить ничего "
    "нельзя: если данных не хватает, назови допущения и разбери варианты. Отвечай по-русски, по "
    "существу и проверяемо: где важна точность, укажи, что стоит перепроверить."
)

DESCRIPTION = (
    "Ask cloud Claude, a much stronger model, a hard question: tricky reasoning or math, a plan or "
    "decision with many trade-offs, a code or document review, a second opinion when you are "
    "unsure. Use it as often as it helps; it is not for lookups (search the web for those). The "
    "question leaves the house: write it self-contained and impersonal — no names, contacts, "
    "addresses, account or document numbers, chat excerpts, nothing that identifies the owner or "
    "other people; describe the situation in general terms. Known personal data is masked "
    "automatically ([телефон], [имя], ...), but you are the first filter. Numbers of 7 or more "
    "digits are masked as ids; write big quantities as 4.3e9 or in words. Claude sees only this "
    "text (up to 6000 characters), has no tools and remembers nothing between calls, so include "
    "the context it needs. An answer can take a few minutes. Treat it as advice: check facts "
    "before acting on them."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The whole question with its context, impersonal, up to 6000 characters.",
        },
    },
    "required": ["question"],
}


# --- settings and files ----------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
        if isinstance(config, dict):
            return config
    except Exception:
        pass
    try:
        import yaml

        data = yaml.safe_load((_hermes_home() / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def settings() -> dict[str, Any]:
    """plugins.entries.ask-claude.settings from Hermes' config, over the defaults."""
    entry = ((_hermes_config().get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    raw = entry.get("settings") if isinstance(entry, dict) else None
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if k in DEFAULTS and v not in (None, "")})
    merged["effort"] = str(merged["effort"]).lower()
    if merged["effort"] not in EFFORTS:
        merged["effort"] = DEFAULTS["effort"]
    try:
        merged["timeout_minutes"] = max(1, min(60, int(merged["timeout_minutes"])))
    except (TypeError, ValueError):
        merged["timeout_minutes"] = DEFAULTS["timeout_minutes"]
    return merged


def _token() -> str:
    return (os.getenv("ASK_CLAUDE_TOKEN") or "").strip()


def data_dir() -> Path:
    """<hermes home>/plugin-data/ask-claude, private to its owner."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        path = Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        path = _hermes_home() / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _log_path() -> Path:
    return data_dir() / "log.jsonl"


def names() -> list[str]:
    """Names to mask, one per line in <plugin data>/names.txt; # starts a comment."""
    try:
        lines = (data_dir() / "names.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def _log(row: dict[str, Any]) -> None:
    fd = os.open(_log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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


def prepare(args: dict[str, Any]) -> tuple[str, Counter]:
    question = str((args or {}).get("question") or "").strip()
    if not question:
        raise ValueError("ask_claude needs a question")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"the question is {len(question)} characters; shorten it to {MAX_QUESTION}")
    text, found = scrub(question)
    if not text:
        raise ValueError("nothing is left of the question after masking personal data")
    return text, found


# --- the call: Claude Code, alone in an empty room ----------------------------------------


def claude_command(configured: str = "") -> Optional[str]:
    candidates = [configured] if configured else []
    candidates += [shutil.which("claude") or "", str(Path.home() / ".local" / "bin" / "claude")]
    for candidate in candidates:
        path = Path(candidate).expanduser() if candidate else None
        if path and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def available() -> bool:
    return bool(_token()) and claude_command(str(settings()["claude_path"] or "")) is not None


def argv(claude: str, conf: dict[str, Any]) -> list[str]:
    return [
        claude, "-p",
        "--safe-mode",                      # no CLAUDE.md, skills, plugins, hooks, MCP, memory
        "--tools", "",                      # no built-in tools
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--max-turns", "2",
        "--no-session-persistence",
        "--output-format", "json",
        "--model", str(conf["model"]),
        "--effort", str(conf["effort"]),
        "--system-prompt", SYSTEM,
    ]


def child_env(config_dir: Path, home: Path) -> dict[str, str]:
    """Only what Claude Code needs: no Hermes secrets, no API key that would bill instead."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
           "CLAUDE_CONFIG_DIR": str(config_dir), "CLAUDE_CODE_OAUTH_TOKEN": _token(),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
                 "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "TZ"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def ask(text: str) -> dict[str, Any]:
    conf = settings()
    claude = claude_command(str(conf["claude_path"] or ""))
    if claude is None:
        raise RuntimeError("the claude command is not installed (see the ask-claude README)")
    if not _token():
        raise RuntimeError("no Claude token: run `claude setup-token` and put it into the plugin's token setting")
    config_dir = data_dir() / "claude"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ask-claude-") as room:
        done = subprocess.run(argv(claude, conf), input=text, capture_output=True, text=True, cwd=room,
                              env=child_env(config_dir, Path(room)), timeout=conf["timeout_minutes"] * 60)
    try:
        reply = json.loads(done.stdout or "{}")
    except ValueError:
        reply = {}
    if not isinstance(reply, dict) or not reply:
        detail = (done.stderr or done.stdout or "").strip()[-500:]
        raise RuntimeError(f"claude exited with {done.returncode}: {detail or 'no output'}")
    result: dict[str, Any] = {"model": conf["model"], "effort": conf["effort"]}
    if reply.get("is_error") or reply.get("subtype", "success") != "success":
        result["error"] = str(reply.get("result") or reply.get("subtype") or "Claude Code failed")[:1000]
    else:
        result["answer"] = str(reply.get("result") or "").strip()
    if reply.get("duration_ms") is not None:
        result["seconds"] = round(float(reply["duration_ms"]) / 1000)
    return result


def handle(args: Optional[dict[str, Any]] = None, **kwargs: Any) -> str:
    try:
        text, found = prepare(dict(args or {}))
        result = ask(text)
        try:  # the answer is already there: a log that cannot be written must not lose it
            _log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "question": text,
                  "masked": dict(found), **result})
        except OSError as exc:
            result["note"] = f"not logged: {exc}"
        if found:
            result["masked"] = dict(found)
        return json.dumps(result, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Claude did not answer in time; ask a narrower question"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False)
