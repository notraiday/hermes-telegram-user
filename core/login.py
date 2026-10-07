"""One-time interactive Telegram login, and the dotenv plumbing around it.

Telegram has no unattended way to obtain a session: the account owner answers a
code that arrives inside the Telegram app. So this runs once, in a terminal, and
stores the result where Hermes reads it. Everything after that is unattended.

Split out of the CLI wrapper so both entry points share one implementation: the
``hermes telegram-user login`` command (``cli.py``) and the standalone
``scripts/setup_session.py``.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

SESSION_KEY = "HERMES_TG_USER_SESSION"
API_ID_KEY = "HERMES_TG_USER_API_ID"
API_HASH_KEY = "HERMES_TG_USER_API_HASH"

__all__ = [
    "API_HASH_KEY",
    "API_ID_KEY",
    "SESSION_KEY",
    "hermes_env_path",
    "read_env_value",
    "run_login",
    "write_env_value",
]


def hermes_env_path() -> Path:
    """The ``.env`` Hermes itself loads: ``HERMES_HOME``, else the platform default.

    Mirrors ``hermes_constants.get_hermes_home``; duplicated rather than imported
    so this works when Hermes is not importable, which is the case for the
    standalone script.
    """
    override = (os.environ.get("HERMES_HOME") or "").strip()
    if override:
        home = Path(os.path.expandvars(os.path.expanduser(override)))
    elif sys.platform == "win32":
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        home = base / "hermes"
    else:
        home = Path.home() / ".hermes"
    return home / ".env"


def read_env_value(text: str, key: str) -> str:
    """One ``KEY=value`` out of a dotenv body; ``export`` and quotes are dropped."""
    match = re.search(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def write_env_value(path: Path, key: str, value: str) -> bool:
    """Replace one line of the dotenv file, or append it; nothing else is touched.

    Edited as text rather than re-serialized: that file holds every other secret
    Hermes has, and its other lines — line endings included — must come out
    exactly as they went in.
    """
    try:
        # newline="" everywhere: read_text/write_text translate line endings, which
        # would rewrite every other line of this file (LF -> CRLF on Windows) and
        # mangle any CRLF already present.
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        print(f"  cannot read {path}: {exc}", file=sys.stderr)
        return False

    # [^\r\n]* rather than .*$ : in a CRLF file the \r sits before the \n, so .*$
    # would swallow it and the replaced line would lose its terminator.
    pattern = re.compile(
        rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=[^\r\n]*", re.MULTILINE
    )
    if pattern.search(text):
        text = pattern.sub(lambda _match: f"{key}={value}", text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        for target in (tmp, path):
            try:
                target.chmod(0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError as exc:
        print(f"  cannot write {path}: {exc}", file=sys.stderr)
        return False
    return True


async def _login(api_id: int, api_hash: str, proxy: Optional[str] = None) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from .accounts import telethon_proxy_kwargs

    client = TelegramClient(StringSession(), api_id, api_hash, **telethon_proxy_kwargs(proxy))
    # Telethon's own interactive flow: phone, then the code Telegram sends, then
    # the 2FA password when the account has one.
    await client.start()
    try:
        return client.session.save()
    finally:
        await client.disconnect()


def run_login(
    env_path: Optional[Path] = None,
    *,
    print_only: bool = False,
    account: str = "default",
    mode: Optional[str] = None,
    proxy: Optional[str] = None,
) -> int:
    """Log in one account, then store its session (and mode/proxy). Returns an exit code."""
    import asyncio

    from .accounts import ACCOUNTS_KEY, env_key

    account = (account or "default").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", account):
        print("account name: letters, digits, _ and -, up to 32 characters", file=sys.stderr)
        return 2

    target = env_path or hermes_env_path()
    try:
        body = target.read_text(encoding="utf-8")
    except OSError:
        body = ""

    def value(key: str) -> str:
        return (os.environ.get(key) or "").strip() or read_env_value(body, key)

    api_id_raw = value(env_key(account, "API_ID")) or value(API_ID_KEY)
    api_hash = value(env_key(account, "API_HASH")) or value(API_HASH_KEY)
    if not api_id_raw or not api_hash:
        print(
            f"Need {API_ID_KEY} and {API_HASH_KEY}: neither the environment nor {target} has them.\n"
            "Install the plugin first — it asks for both — or put them in that file.",
            file=sys.stderr,
        )
        return 2
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(f"{API_ID_KEY} must be an integer", file=sys.stderr)
        return 2

    proxy = (proxy or value(env_key(account, "PROXY")) or "").strip() or None
    try:
        session = asyncio.run(_login(api_id, api_hash, proxy))
    except ModuleNotFoundError as exc:
        print(
            f"Missing Python package ({exc.name}).\n"
            "Install into the interpreter Hermes runs: pip install 'telethon>=1.44,<2' 'python-socks[asyncio]'",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("\nCancelled; nothing was written.", file=sys.stderr)
        return 1

    session_key = env_key(account, "SESSION")
    if print_only:
        print(f"\n{session_key}=")
        print(session)
        return 0

    names = [n.strip().lower() for n in value(ACCOUNTS_KEY).split(",") if n.strip()]
    if account not in names:
        names.append(account)
    writes = [(session_key, session), (ACCOUNTS_KEY, ",".join(names))]
    if mode:
        writes.append((env_key(account, "MODE"), mode))
    elif not value(env_key(account, "MODE")):
        writes.append((env_key(account, "MODE"), "read"))
    if proxy:
        writes.append((env_key(account, "PROXY"), proxy))

    for key, val in writes:
        if not write_env_value(target, key, val):
            print("\nStoring it failed. Put these lines in the .env yourself:\n")
            for k, v in writes:
                print(f"{k}={v}")
            return 1

    print(f"\nSaved account {account!r} to {target} (other lines untouched).")
    print("Now restart Hermes (gateway and the backend Desktop connects to).")
    return 0
