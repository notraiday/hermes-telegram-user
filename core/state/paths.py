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
