"""The ``hermes telegram-user ...`` command.

The one-time Telegram login has to run in a terminal: Telegram delivers the code
to the account owner's app, so nobody can type it on their behalf, and the gateway
runs without a TTY. Hermes lets a plugin hang a subcommand off its own CLI, and
that is where this belongs — no separate script to locate, no value to copy.
"""

from __future__ import annotations

import argparse
from pathlib import Path

__all__ = ["login_command", "register_cli"]


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the parser for ``hermes telegram-user`` (Hermes calls this during argparse setup)."""
    subparser.set_defaults(func=login_command)

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
    login.set_defaults(func=login_command)


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
