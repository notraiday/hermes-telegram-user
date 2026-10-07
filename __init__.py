"""Hermes Telegram User (MTProto) plugin package — tools only.

There is no chat platform here: talking to Hermes in Telegram is the job of the
regular bot-token platform. This plugin gives the agent tools over one or more
Telegram user accounts (read-only or read-write, each with its own proxy) and the
``hermes telegram-user login`` command that creates their sessions.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from . import tools

    tools.register_tools(ctx)

    # `hermes telegram-user login`: the one-time interactive login belongs on the
    # CLI, because Telegram delivers the code to the account owner's app.
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if callable(register_cli_command):
        from .cli import register_cli, run_command

        register_cli_command(
            name="telegram-user",
            help="Telegram user accounts: log in, check the inbox",
            setup_fn=register_cli,
            handler_fn=run_command,
            description=(
                "Log in to a Telegram account the plugin works with. "
                "Run `hermes telegram-user login --account <name>`, then restart Hermes."
            ),
        )


__all__ = ["register"]
