"""immich_view_photo: the agent looks at photos from the owner's Immich library.

The immich MCP server finds photos and returns their ids; this tool fetches
them and returns them as images, so the (vision) model sees them, not text.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from . import view

    ctx.register_tool(
        name="immich_view_photo",
        toolset="immich_view",
        schema={"name": "immich_view_photo", "description": view.DESCRIPTION, "parameters": view.PARAMETERS},
        handler=view.handle,
        check_fn=view.available,
        requires_env=["IMMICH_API_KEY"],
        is_async=False,
        description=view.DESCRIPTION,
        emoji="🖼",
    )


__all__ = ["register"]
