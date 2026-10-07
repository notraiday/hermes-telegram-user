"""The ``hermes telegram-user ...`` command.

The one-time Telegram login has to run in a terminal: Telegram delivers the code
to the account owner's app, so nobody can type it on their behalf, and the gateway
runs without a TTY. Hermes lets a plugin hang a subcommand off its own CLI, and
that is where this belongs — no separate script to locate, no value to copy.

``inbox --peek`` lives here for the same reason: a cron pre-run script has no
tools, only a shell, and must learn whether there is anything new before it
decides to wake the model.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__all__ = ["inbox_command", "login_command", "register_cli", "run_command"]


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the parser for ``hermes telegram-user`` (Hermes calls this during argparse setup)."""
    subparser.set_defaults(func=run_command)

    actions = subparser.add_subparsers(dest="telegram_user_action")
    login = actions.add_parser(
        "login",
        help="Log in to Telegram once and store the session in the Hermes .env",
        description=(
            "Prompts for the phone number, the login code Telegram sends you, and the 2FA "
            "password if the account has one, then writes HERMES_TG_USER_<ACCOUNT>_SESSION "
            "(and the account's mode/proxy if given) into the Hermes .env and adds the "
            "account to HERMES_TG_USER_ACCOUNTS. Restart Hermes afterwards."
        ),
    )
    login.add_argument(
        "--env",
        type=Path,
        default=None,
        help="dotenv file to read the API id/hash from and write the session to",
    )
    login.add_argument(
        "--account",
        default="default",
        help="account name, e.g. personal or agent (letters, digits, _ and -)",
    )
    login.add_argument(
        "--mode",
        choices=["read", "write"],
        default=None,
        help="read (default for new accounts) or write",
    )
    login.add_argument(
        "--proxy",
        default=None,
        help="proxy for this account: socks5://host:port, http://host:port or mtproxy://secret@host:port",
    )
    login.add_argument(
        "--qr",
        action="store_true",
        help="log in by scanning a QR code with your phone instead of typing a code",
    )
    login.add_argument(
        "--print-only",
        action="store_true",
        help="print the session instead of storing it (it is not echoed otherwise)",
    )
    login.set_defaults(func=run_command)

    inbox = actions.add_parser(
        "inbox",
        help="Check for new inbox messages without reading or marking them",
        description=(
            "With --peek, prints JSON saying which chats tg_read_inbox would return "
            "something for, per account, without fetching the messages or moving any "
            "mark. Meant for a cron pre-run script deciding whether to wake the agent. "
            "Exit code 1 if any account could not be checked."
        ),
    )
    inbox.add_argument(
        "--peek",
        action="store_true",
        help="report which chats have new messages (the only mode)",
    )
    inbox.add_argument(
        "--account",
        action="append",
        default=None,
        help="account to check; repeat for several (default: every configured account)",
    )
    inbox.add_argument(
        "--env",
        type=Path,
        default=None,
        help="dotenv file to read the accounts from (default: the Hermes .env)",
    )
    inbox.set_defaults(func=run_command)


def run_command(args: argparse.Namespace) -> int:
    """Dispatch on the subcommand; a bare ``hermes telegram-user`` stays the login."""
    if getattr(args, "telegram_user_action", None) == "inbox":
        return inbox_command(args)
    return login_command(args)


def login_command(args: argparse.Namespace) -> int:
    """Run the interactive login; the return value becomes the process exit code."""
    from .core.login import run_login

    return run_login(
        getattr(args, "env", None),
        print_only=bool(getattr(args, "print_only", False)),
        account=str(getattr(args, "account", None) or "default"),
        mode=getattr(args, "mode", None),
        proxy=getattr(args, "proxy", None),
        qr=bool(getattr(args, "qr", False)),
    )


def _load_account_env(path: Path | None) -> None:
    """Fill in the plugin's variables from the Hermes .env where the process lacks them.

    The gateway has them in its environment; a shell or a cron pre-run script may
    not. Only HERMES_TG_USER_* is read, and nothing already set is overridden.
    """
    import os

    from .core.login import hermes_env_path

    target = path or hermes_env_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.startswith("HERMES_TG_USER_") or os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def inbox_command(args: argparse.Namespace) -> int:
    """``hermes telegram-user inbox --peek``: JSON of chats with new messages, per account."""
    import asyncio
    import json

    if not getattr(args, "peek", False):
        print("only --peek is supported: hermes telegram-user inbox --peek", file=sys.stderr)
        return 2
    _load_account_env(getattr(args, "env", None))

    from .core.accounts import account_names, use_account
    from .tools import inbox_peek

    try:
        names = [n.strip().lower() for n in (getattr(args, "account", None) or account_names())]
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    rows, failed = [], False
    for name in names:
        try:
            with use_account(name):
                rows.append(asyncio.run(inbox_peek()))
        except Exception as exc:
            failed = True
            rows.append({"account": name, "error": str(exc) or type(exc).__name__})
    total = sum(int(row.get("new_chats") or 0) for row in rows)
    print(json.dumps({"new_chats": total, "accounts": rows}, ensure_ascii=False))
    return 1 if failed else 0
