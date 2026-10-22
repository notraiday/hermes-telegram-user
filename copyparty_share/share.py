"""Share a file or folder from Copyparty; keep the link with the owner.

``copyparty_share`` asks Copyparty (``POST /?share``, the call its web UI
makes) for a temporary read-only link to anything the agent's account can
read (or, with the ``folder`` setting, only to things inside that folder).
Copyparty checks the account's read access and the volume's ``shr_who``.

Every link made here is remembered (``shares.json`` in the plugin's data
folder), and ``guard`` — a ``pre_tool_call`` hook — blocks any tool call whose
arguments carry one: the link may reach the owner (the agent's own reply, or
``send_message`` to a platform's home channel) and the calendar, nothing else.
The share key is matched with separators and case ignored, so a link broken up
with spaces or dashes is still caught.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import secrets
import string
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "copyparty-share"
DEFAULTS = {"base_url": "", "user": "agent", "folder": "/", "days": 7, "link_tools": "mcp_calendar_*"}
MAX_DAYS = 30
KEY_LENGTH = 20
_KEY_ALPHABET = string.ascii_letters + string.digits

DESCRIPTION = (
    "Make a temporary read-only download link to a file or folder in Copyparty (any path you can "
    "list with the webdav tools). The link is for the owner only: put it "
    "in your reply to the owner or in a calendar event. Any other tool refuses it — never send "
    "it to other people, chats, mail or notes. path: the Copyparty path, e.g. '/fileshare/report.pdf' "
    "or '/fileshare/trip/' (a trailing / shares the folder). days: how long it works, default 7, at most 30. "
    "password: optional."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Copyparty path of a file, or of a folder ending with /."},
        "days": {"type": "integer", "description": "Days the link works, 1 to 30; default 7."},
        "password": {"type": "string", "description": "Optional password for the link."},
    },
    "required": ["path"],
}

BLOCKED = (
    "Blocked: this carries a Copyparty link the agent made. Such links go only to the owner "
    "(your reply, or send_message to a home channel) and into the calendar."
)


# --- settings and files ----------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
        if isinstance(config, dict):
            return config
    except Exception:
        pass
    try:
        import yaml

        data = yaml.safe_load((_hermes_home() / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def settings() -> dict[str, Any]:
    """plugins.entries.copyparty-share.settings from Hermes' config, over the defaults."""
    entry = ((_hermes_config().get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    raw = entry.get("settings") if isinstance(entry, dict) else None
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if k in DEFAULTS and v not in (None, "")})
    merged["base_url"] = str(merged["base_url"]).strip().rstrip("/")
    merged["folder"] = "/" + str(merged["folder"]).strip().strip("/")
    try:
        merged["days"] = max(1, min(MAX_DAYS, int(merged["days"])))
    except (TypeError, ValueError):
        merged["days"] = DEFAULTS["days"]
    return merged


def _password() -> str:
    return (os.getenv("COPYPARTY_AGENT_PASSWORD") or "").strip()


def available() -> bool:
    return bool(_password()) and bool(settings()["base_url"])


def data_dir() -> Path:
    """<hermes home>/plugin-data/copyparty-share, private to its owner."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        path = Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        path = _hermes_home() / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _shares_file() -> Path:
    return data_dir() / "shares.json"


def load_shares() -> list[dict[str, Any]]:
    try:
        data = json.loads(_shares_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return [s for s in data if isinstance(s, dict) and s.get("key")] if isinstance(data, list) else []


def _remember(share: dict[str, Any]) -> None:
    shares = load_shares() + [share]
    path = _shares_file()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(shares, out, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --- Copyparty -------------------------------------------------------------------------


def _inside(folder: str, path: str) -> str:
    """The Copyparty path of ``path`` (relative to ``folder``, or absolute inside it)."""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError("pass path: a Copyparty file or folder")
    is_dir = raw.endswith("/")
    if raw.startswith("/") and (raw.rstrip("/") + "/").startswith(folder + "/"):
        rel = raw[len(folder):]
    else:
        rel = raw
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("path may not contain ..")
    if not parts and folder == "/":
        raise ValueError("pass a file or folder, not the whole of Copyparty")
    joined = posixpath.join(folder, *parts) if parts else folder
    return joined + "/" if (is_dir or not parts) else joined


def _create(conf: dict[str, Any], vpath: str, days: int, password: str) -> tuple[str, str]:
    key = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(KEY_LENGTH))
    body = json.dumps({"k": key, "vp": [vpath], "pw": password, "exp": str(days * 24 * 60),
                       "perms": ["read"]}).encode("utf-8")
    auth = base64.b64encode(f"{conf['user']}:{_password()}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{conf['base_url']}/?share", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    # Copyparty is on the home network: never through an inherited proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        text = response.read().decode("utf-8", "replace").strip()
    match = re.search(r"created share:\s*(\S+)", text)
    if not match or key not in match.group(1):
        raise RuntimeError(f"unexpected answer from Copyparty: {text[:200]}")
    return key, match.group(1)


def _hint(message: str) -> str:
    lowered = message.lower()
    if "perms to create shares" in lowered:
        return " (Copyparty: this volume does not let the agent share; set shr-who: auth, or the volflag shr_who: auth)"
    if "sharing must be disabled" in lowered:
        return " (Copyparty: shares are off; set shr: /share in [global])"
    if "dont have all the perms" in lowered:
        return (" (the agent cannot read that path — check it with webdav_list_remote_directory; "
                "or the password is wrong)")
    if "not found on disk" in lowered:
        return " (no such file; check the name with webdav_list_remote_directory)"
    return ""


def handle(args: Optional[dict[str, Any]] = None, **kwargs: Any) -> str:
    try:
        args = dict(args or {})
        conf = settings()
        if not conf["base_url"]:
            raise RuntimeError("no Copyparty address: set base_url in the copyparty-share plugin settings")
        if not _password():
            raise RuntimeError("COPYPARTY_AGENT_PASSWORD is not set")
        vpath = _inside(conf["folder"], args.get("path"))
        try:
            days = max(1, min(MAX_DAYS, int(args.get("days") or conf["days"])))
        except (TypeError, ValueError):
            days = conf["days"]
        password = str(args.get("password") or "")
        try:
            key, url = _create(conf, vpath, days, password)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
            raise RuntimeError(f"Copyparty answered {exc.code} for {vpath}: {detail}{_hint(detail)}") from None
        expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + days * 86400))
        _remember({"key": key, "url": url, "path": vpath, "created": int(time.time()), "expires": expires})
        return json.dumps({"url": url, "path": vpath, "expires": expires, "password": bool(password),
                           "only_for": "the owner's reply or a calendar event"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False)


# --- the guard -------------------------------------------------------------------------


def _squash(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _carries_link(value: Any, keys: list[str]) -> bool:
    text = _squash(value)
    return any(key in text for key in keys)


def _allowed(tool_name: str, call: dict[str, Any], patterns: list[str]) -> bool:
    if tool_name == "send_message":
        # A bare platform name ("telegram") is that platform's home channel: the owner.
        target = str(call.get("target") or "").strip()
        return call.get("action", "send") == "send" and bool(re.fullmatch(r"[a-z_]+", target))
    for pattern in patterns:
        if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
            return True
        if tool_name == pattern:
            return True
    return False


def _entries(tool_name: str, call: dict[str, Any]) -> list[tuple[str, Any]]:
    """The tool calls this call really makes: tool search's bridge wraps the deferred ones."""
    if tool_name != "tool_call":
        return [(tool_name, call)]
    try:
        from tools.tool_search import normalize_tool_call_entries

        entries, err = normalize_tool_call_entries(call)
        if not err:
            return [(str(e.get("name") or ""), e.get("arguments")) for e in entries]
    except Exception:
        pass
    if isinstance(call.get("calls"), list):
        return [(str(e.get("name") or ""), e.get("arguments")) for e in call["calls"] if isinstance(e, dict)]
    return [(str(call.get("name") or ""), call.get("arguments", call))]


def guard(*args: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
    """pre_tool_call: a call carrying one of our links goes through only to the owner or the calendar.

    Hermes lets a call through when a hook raises, so nothing here may. Once a
    call is known to carry a link, any failure blocks it.
    """
    try:
        tool_name = str(kwargs.get("tool_name", args[0] if args else "") or "")
        call = kwargs.get("args", args[1] if len(args) > 1 else None) or {}
        call = call if isinstance(call, dict) else {}
        keys = [s["key"].lower() for s in load_shares()]
        if not keys or tool_name == "copyparty_share" or not _carries_link(call, keys):
            return None
    except Exception:
        return None  # no links made yet, or none here: nothing to protect
    try:
        patterns = [p.strip() for p in str(settings()["link_tools"]).split(",") if p.strip()]
        for name, inner in _entries(tool_name, call):
            inner = inner if isinstance(inner, dict) else {"value": inner}
            if _carries_link(inner, keys) and not _allowed(name, inner, patterns):
                return {"action": "block", "message": BLOCKED}
        return None
    except Exception:
        return {"action": "block", "message": BLOCKED}
