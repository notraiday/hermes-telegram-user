"""copyparty_share: temporary Copyparty links that never leave the owner's circle.

The agent shares something from Copyparty; the link goes to the
owner or into the calendar and nowhere else. The pre_tool_call guard blocks
every other tool call whose arguments carry one of the links it made.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from . import share

    ctx.register_tool(
        name="copyparty_share",
        toolset="copyparty_share",
        schema={"name": "copyparty_share", "description": share.DESCRIPTION, "parameters": share.PARAMETERS},
        handler=share.handle,
        check_fn=share.available,
        requires_env=["COPYPARTY_AGENT_PASSWORD"],
        is_async=False,
        description=share.DESCRIPTION,
        emoji="🔗",
    )
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_tool_call", share.guard)


__all__ = ["register"]
