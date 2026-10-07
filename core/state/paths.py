from __future__ import annotations

import os
from pathlib import Path

#: The name Hermes knows this plugin by; also the directory under plugin-data/.
PLUGIN_NAME = "telegram-user"


def _plugin_home() -> Path | None:
    """``<hermes home>/plugin-data/telegram-user`` when Hermes provides it.

    The sanctioned home for a plugin's state: it survives ``hermes plugins
    update``/``remove`` (which git-pull or delete the install tree) and follows
    the active profile, because Hermes resolves its home per call. Imported
    lazily and never fatally — outside Hermes the import simply fails and the
    fallback below applies.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        return None


def _legacy_home() -> Path:
    """Where state lived before Hermes' plugin-data convention was adopted.

    On Linux this is the same tree as ``<hermes home>``, which is why it went
    unnoticed; elsewhere it is a different home entirely.
    """
    return Path.home() / ".hermes" / "state" / PLUGIN_NAME


def _base_dir() -> Path:
    raw = (os.getenv("HERMES_TG_USER_STATE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _plugin_home() or _legacy_home()


def _account_name() -> str | None:
    """The account the current tool call works on (see core.accounts)."""
    try:
        from ..accounts import current_account, default_account

        return current_account() or default_account()
    except Exception:
        return None


def state_dir() -> Path:
    """Private persistent state owned by this plugin (aliases/transcripts/archive).

    Every configured account gets its own subtree ``accounts/<name>/``: chat ids,
    digest marks, collections and archives belong to one Telegram account and
    must never leak into another one's view.

    Resolution order: the explicit ``HERMES_TG_USER_STATE_DIR`` override, then
    Hermes' per-plugin data root, then the pre-convention path. The override is
    first so it remains the escape hatch it is documented to be.
    """
    path = _base_dir()
    account = _account_name()
    if account:
        path = path / "accounts" / account
    path.mkdir(parents=True, exist_ok=True)
    for part in (path, path.parent, path.parent.parent) if account else (path,):
        try:
            part.chmod(0o700)
        except OSError:
            pass
    return path


def private_file(path: Path) -> Path:
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# --- several processes, one state file ----------------------------------------------
#
# The gateway, the dashboard and `hermes telegram-user ...` are separate processes
# working on the same files. Each state module keeps an in-memory copy and saves
# the whole of it, so a copy taken before another process wrote would erase that
# write. Two guards: the copy is dropped when the file on disk is no longer the
# one it was read from, and a read-modify-write holds an flock on a sibling file.

try:
    import fcntl
except ImportError:  # windows-footgun: ok - no flock there; threads are still serialised
    fcntl = None  # type: ignore[assignment]

import threading
from typing import Callable, Optional


def file_stamp(path: Path) -> Optional[tuple[int, int, int]]:
    """Identity of the file's current content as far as the filesystem tells; None if absent."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class StateLock:
    """A re-entrant thread lock that also holds ``flock(<file>.lock)`` while held."""

    def __init__(self, path_fn: Callable[[], Path]):
        self._path_fn = path_fn
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self) -> "StateLock":
        self._lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.handle = self._lock_file()
        self._local.depth = depth + 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._local.depth -= 1
        if self._local.depth == 0:
            handle, self._local.handle = self._local.handle, None
            if handle is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    handle.close()
        self._lock.release()

    def _lock_file(self):
        if fcntl is None:
            return None
        try:
            path = self._path_fn()
            handle = open(path.with_name(path.name + ".lock"), "a")
        except OSError:
            return None  # an unwritable state dir fails at the save, with a real error
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle
