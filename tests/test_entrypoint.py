"""The real load contract: Hermes imports the package and calls ``register(ctx)``.

Every other test here calls ``register_tools`` directly with a fake context,
which is exactly how a missing package-level entry point went unnoticed: the
plugin registered nothing at all through Hermes' own path while the suite stayed
green. Doctor caught it — `hermes plugins doctor .` reported "no register()
function", 0 tools. This pins the contract the loader actually uses.
"""

import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_support import isolated_state, plugin_package  # noqa: E402

EXPECTED_TOOL_COUNT = 54

STUBBED = (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
    "gateway.platforms.event",
    "gateway.platforms.media_cache",
    "tools",
    "tools.transcription_tools",
)


@contextmanager
def _gateway_stubs():
    """Stand in for the Hermes surfaces ``adapter.py`` imports at module level."""
    saved = {name: sys.modules.get(name) for name in STUBBED}

    def module(name, **attrs):
        stub = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(stub, key, value)
        sys.modules[name] = stub
        return stub

    message_type = type(
        "MessageType",
        (),
        {
            "PHOTO": "photo",
            "VOICE": "voice",
            "AUDIO": "audio",
            "VIDEO": "video",
            "DOCUMENT": "document",
            "TEXT": "text",
        },
    )
    module("gateway")
    module(
        "gateway.config",
        Platform=lambda name: name,
        PlatformConfig=type("PlatformConfig", (), {}),
    )
    module("gateway.platforms")
    module(
        "gateway.platforms.base",
        BasePlatformAdapter=type(
            "BasePlatformAdapter", (), {"__init__": lambda self, *args, **kwargs: None}
        ),
        SendResult=type(
            "SendResult", (), {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)}
        ),
        utf16_len=len,
    )
    module(
        "gateway.platforms.event",
        MessageEvent=type("MessageEvent", (), {}),
        MessageType=message_type,
    )
    module("gateway.platforms.media_cache", cache_media_bytes=lambda *a, **k: "/tmp/x")
    module("tools")
    module("tools.transcription_tools", transcribe_audio=lambda *a, **k: {"success": True})
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class _Ctx:
    def __init__(self):
        self.tools = []
        self.platforms = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


def test_the_package_exposes_the_register_entry_point():
    """Without this Hermes logs "no register() function" and loads nothing."""
    with isolated_state(), _gateway_stubs():
        package = plugin_package()
        assert callable(getattr(package, "register", None))


def test_the_entry_point_registers_every_tool_and_no_platform():
    with isolated_state(), _gateway_stubs():
        ctx = _Ctx()
        plugin_package().register(ctx)

        names = [entry["name"] for entry in ctx.tools]
        assert len(names) == EXPECTED_TOOL_COUNT, names
        assert len(set(names)) == EXPECTED_TOOL_COUNT, "a tool registered twice"
        assert {entry["toolset"] for entry in ctx.tools} == {"telegram_user"}
        assert ctx.platforms == [], "the .h chat platform was removed; no platform may register"


def test_every_published_tool_is_reachable_through_the_entry_point():
    """The manifest's provides_tools is a promise this call has to keep."""
    import re

    with isolated_state(), _gateway_stubs():
        ctx = _Ctx()
        plugin_package().register(ctx)
        registered = {entry["name"] for entry in ctx.tools}

        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(
            encoding="utf-8"
        )
        published = set(re.findall(r"^  - (tg_[a-z_]+)$", manifest, re.MULTILINE))

        assert published == registered, (
            f"promised but not registered: {sorted(published - registered)}; "
            f"registered but not promised: {sorted(registered - published)}"
        )
