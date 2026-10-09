"""ask_claude: the local agent consults cloud Claude, without the owner's life in the request.

The question — scrubbed of what fixed rules recognise as personal data — goes
to Claude Code on the owner's Claude subscription, run with no tools, no MCP
servers and no settings of its own. Nothing else leaves: no history, no files,
no tool results. Every call is logged.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from . import consult

    ctx.register_tool(
        name="ask_claude",
        toolset="ask_claude",
        schema={"name": "ask_claude", "description": consult.DESCRIPTION, "parameters": consult.PARAMETERS},
        handler=consult.handle,
        check_fn=consult.available,
        is_async=False,
        description=consult.DESCRIPTION,
        emoji="🟠",
    )


__all__ = ["register"]
