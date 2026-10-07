"""Shared helpers for the offline test suite.

The plugin directory name contains a hyphen, so it is not a legal module name:
it has to be loaded through an explicit spec, the same way the host loader must.
These tests never talk to Telegram and never need telethon.
"""

import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = "hermes_telegram_user_under_test"


def plugin_module(name: str):
    """Import ``<name>`` from the plugin package, e.g. ``core.state.archive``."""
    if PLUGIN_PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PLUGIN_PACKAGE,
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not build an import spec for the plugin package")
        module = importlib.util.module_from_spec(spec)
        sys.modules[PLUGIN_PACKAGE] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{PLUGIN_PACKAGE}.{name}")


STATE_MODULES = ("core.state.aliases", "core.state.collections", "core.state.watermarks")


def plugin_package():
    """The plugin package itself, so a test can call its ``register`` entry point."""
    plugin_module("core")
    return sys.modules[PLUGIN_PACKAGE]


@contextmanager
def isolated_state():
    """Point the plugin's state directory at a throwaway directory.

    The state modules cache their file in-process, so switching the directory is
    not enough on its own: each one is reloaded here, which is what a fresh
    process would see.
    """
    previous = os.environ.get("HERMES_TG_USER_STATE_DIR")
    root = tempfile.mkdtemp(prefix="tgu-test-")
    os.environ["HERMES_TG_USER_STATE_DIR"] = root
    try:
        for name in STATE_MODULES:
            importlib.reload(plugin_module(name))
        yield Path(root)
    finally:
        if previous is None:
            os.environ.pop("HERMES_TG_USER_STATE_DIR", None)
        else:
            os.environ["HERMES_TG_USER_STATE_DIR"] = previous
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def account_env(name="acct", mode="write", extra=None):
    """Configure one Telegram account in the environment for the duration."""
    slug = name.upper()
    values = {
        "HERMES_TG_USER_API_ID": "1",
        "HERMES_TG_USER_API_HASH": "hash",
        "HERMES_TG_USER_ACCOUNTS": name,
        f"HERMES_TG_USER_{slug}_SESSION": "session",
        f"HERMES_TG_USER_{slug}_MODE": mode,
        **(extra or {}),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield name
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
