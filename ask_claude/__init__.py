"""ask_claude: the local agent consults cloud Claude, without the owner's life in the request.

The text that leaves the machine is the agent's question after fixed scrubbing
rules (``consult.scrub``), and the owner sees exactly that text and says yes to
every call (``consult.approval``). Nothing else goes: no history, no files, no
tool results. Every call is logged with its cost; a monthly cap stops the tool.
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
        requires_env=["ASK_CLAUDE_API_KEY"],
        is_async=False,
        description=consult.DESCRIPTION,
        emoji="🟠",
    )
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_tool_call", consult.approval)


__all__ = ["register"]
