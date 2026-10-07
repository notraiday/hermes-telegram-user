"""Several Telegram accounts, each with its own session, mode and proxy.

Configuration lives in the Hermes ``.env``:

    HERMES_TG_USER_API_ID=...            # shared by all accounts unless overridden
    HERMES_TG_USER_API_HASH=...
    HERMES_TG_USER_ACCOUNTS=personal,agent

    HERMES_TG_USER_PERSONAL_SESSION=...  # written by `hermes telegram-user login --account personal`
    HERMES_TG_USER_PERSONAL_MODE=read    # read (default) | write
    HERMES_TG_USER_PERSONAL_PROXY=socks5://127.0.0.1:1080

    HERMES_TG_USER_AGENT_SESSION=...
    HERMES_TG_USER_AGENT_MODE=write

Per account the API id/hash can be overridden with ``..._<NAME>_API_ID`` /
``..._<NAME>_API_HASH``. Proxy URLs: ``socks5://[user:pass@]host:port``,
``socks4://host:port``, ``http://[user:pass@]host:port`` and
``mtproxy://<secret>@host:port``.

Without ``HERMES_TG_USER_ACCOUNTS`` the old single-account variables
(``HERMES_TG_USER_SESSION`` / ``HERMES_TG_USER_PROXY``) form one read-only
account called ``default``.

Which account a tool call works on is carried in a context variable that the
tool wrapper sets; the client factory and every state path read it from there,
so archives, digest marks, collections and aliases never mix between accounts.
"""

from __future__ import annotations

import contextvars
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from urllib.parse import unquote, urlparse

PREFIX = "HERMES_TG_USER_"
ACCOUNTS_KEY = PREFIX + "ACCOUNTS"
MODES = ("read", "write")

_current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "telegram_user_account", default=None
)


@dataclass(frozen=True)
class Account:
    name: str
    api_id: int
    api_hash: str
    session: str
    mode: str
    proxy: Optional[str]

    @property
    def writable(self) -> bool:
        return self.mode == "write"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def env_key(account: str, field: str) -> str:
    """``personal`` + ``SESSION`` -> ``HERMES_TG_USER_PERSONAL_SESSION``."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", account).strip("_").upper()
    return f"{PREFIX}{slug}_{field}"


def account_names() -> list[str]:
    raw = _env(ACCOUNTS_KEY)
    if raw:
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
        seen: list[str] = []
        for name in names:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
                raise RuntimeError(f"bad account name in {ACCOUNTS_KEY}: {name!r}")
            if name not in seen:
                seen.append(name)
        return seen
    return ["default"] if _env(PREFIX + "SESSION") else []


def default_account() -> Optional[str]:
    names = account_names()
    return names[0] if names else None


def _legacy() -> bool:
    return not _env(ACCOUNTS_KEY)


def _field(account: str, field: str) -> str:
    if _legacy() and account == "default":
        return _env(PREFIX + field)
    return _env(env_key(account, field))


def get_account(name: Optional[str] = None) -> Account:
    names = account_names()
    if not names:
        raise RuntimeError(
            f"No Telegram account configured: set {ACCOUNTS_KEY} and log in with "
            "`hermes telegram-user login --account <name>`"
        )
    name = (name or current_account() or names[0]).strip().lower()
    if name not in names:
        raise ValueError(f"unknown Telegram account {name!r}; configured: {', '.join(names)}")

    api_id_raw = _field(name, "API_ID") or _env(PREFIX + "API_ID")
    api_hash = _field(name, "API_HASH") or _env(PREFIX + "API_HASH")
    session = _field(name, "SESSION")
    if not (api_id_raw and api_hash):
        raise RuntimeError(f"Missing {PREFIX}API_ID / {PREFIX}API_HASH")
    if not session:
        raise RuntimeError(
            f"Account {name!r} is not logged in: run `hermes telegram-user login --account {name}`"
        )
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError(f"{PREFIX}API_ID must be an integer") from exc

    mode = (_field(name, "MODE") or "read").lower()
    if mode not in MODES:
        raise RuntimeError(f"{env_key(name, 'MODE')} must be one of {MODES}, got {mode!r}")
    proxy = _field(name, "PROXY") or None
    return Account(name, api_id, api_hash, session, mode, proxy)


def configured_accounts() -> list[dict[str, Any]]:
    """Public description of every account; never includes secrets."""
    rows = []
    for name in account_names():
        try:
            acc = get_account(name)
            rows.append({"account": name, "mode": acc.mode, "proxy": proxy_label(acc.proxy),
                         "logged_in": True})
        except Exception as exc:
            rows.append({"account": name, "logged_in": False, "error": str(exc)})
    return rows


def current_account() -> Optional[str]:
    return _current.get()


@contextmanager
def use_account(name: Optional[str]) -> Iterator[str]:
    resolved = get_account(name).name
    token = _current.set(resolved)
    try:
        yield resolved
    finally:
        _current.reset(token)


def require_write(account: Optional[str] = None) -> Account:
    acc = get_account(account)
    if not acc.writable:
        raise PermissionError(
            f"account {acc.name!r} is read-only ({env_key(acc.name, 'MODE')}=read); "
            "writing to Telegram from it is not allowed"
        )
    return acc


# --- proxies --------------------------------------------------------------------

def proxy_label(url: Optional[str]) -> Optional[str]:
    """Proxy without credentials or secret, safe to show the model."""
    if not url:
        return None
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def telethon_proxy_kwargs(url: Optional[str]) -> dict[str, Any]:
    """TelegramClient(**kwargs) for a proxy URL; empty dict for a direct connection."""
    if not url:
        return {}
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        raise RuntimeError(f"proxy needs host and port: {proxy_label(url) or url!r}")

    if scheme in ("mtproxy", "mtproto"):
        from telethon import connection

        secret = unquote(parsed.username or "")
        if not secret:
            raise RuntimeError("mtproxy URL needs the secret: mtproxy://<secret>@host:port")
        return {
            "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (host, port, secret),
        }

    kinds = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http"}
    if scheme not in kinds:
        raise RuntimeError(f"unsupported proxy scheme {scheme!r}; use socks5, socks4, http or mtproxy")
    try:
        import python_socks  # noqa: F401  (Telethon needs it for non-MTProxy proxies)
    except ImportError as exc:
        raise RuntimeError("proxy support needs python-socks: pip install 'python-socks[asyncio]'") from exc
    proxy: dict[str, Any] = {"proxy_type": kinds[scheme], "addr": host, "port": port, "rdns": True}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return {"proxy": proxy}
