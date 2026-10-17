"""Fetch Immich photos by asset id and hand them to the model as images.

Two read-only calls per photo against the owner's Immich on the local network:
``GET /api/assets/{id}`` for the caption (date, file, place, people) and
``GET /api/assets/{id}/thumbnail?size=…`` for the picture. The picture is
shrunk and re-encoded as JPEG (Immich thumbnails are often WebP, which the
local model's image loader may not read) and returned in Hermes' multimodal
tool-result envelope, which Hermes passes to a vision model as an image.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional

PLUGIN_ID = "immich-view"
DEFAULTS = {"base_url": "", "max_side": 1024}
MAX_PHOTOS = 4
SIZES = ("preview", "thumbnail")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

DESCRIPTION = (
    "Look at photos from the owner's Immich library: returns the pictures themselves so you can "
    "see them, each with its date, place and the people on it. Pass asset ids from the immich "
    "search results (their `id`), up to 4 per call. size: 'preview' (detailed, default) or "
    "'thumbnail' (small; to scan several quickly). For a video you get one frame."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Immich asset ids, 1 to 4.",
        },
        "size": {"type": "string", "enum": list(SIZES), "description": "preview (default) or thumbnail."},
    },
    "required": ["ids"],
}


# --- settings ------------------------------------------------------------------------------


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
        from pathlib import Path

        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def settings() -> dict[str, Any]:
    config = _hermes_config()
    entry = ((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    raw = entry.get("settings") if isinstance(entry, dict) else None
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if k in DEFAULTS and v not in (None, "")})
    if not merged["base_url"]:
        server = ((config.get("mcp_servers") or {}).get("immich") or {})
        env = server.get("env") if isinstance(server, dict) else None
        merged["base_url"] = str((env or {}).get("IMMICH_BASE_URL") or "") if isinstance(env, dict) else ""
    merged["base_url"] = re.sub(r"/+(api/*)?$", "", str(merged["base_url"]).strip())
    try:
        merged["max_side"] = max(256, min(2048, int(merged["max_side"])))
    except (TypeError, ValueError):
        merged["max_side"] = DEFAULTS["max_side"]
    return merged


def _api_key() -> str:
    return (os.getenv("IMMICH_API_KEY") or "").strip()


def available() -> bool:
    return bool(_api_key()) and bool(settings()["base_url"])


# --- Immich ----------------------------------------------------------------------------------


def _get(url: str, accept: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"x-api-key": _api_key(), "Accept": accept})
    # Immich is on the home network: never through an inherited proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return response.read(), response.headers.get("Content-Type", "")


def _asset_info(base: str, asset_id: str) -> dict[str, Any]:
    data, _ = _get(f"{base}/api/assets/{asset_id}", "application/json")
    info = json.loads(data.decode("utf-8"))
    return info if isinstance(info, dict) else {}


def _caption(asset_id: str, info: dict[str, Any]) -> str:
    exif = info.get("exifInfo") or {}
    when = str(info.get("localDateTime") or info.get("fileCreatedAt") or "")[:16].replace("T", " ")
    place = ", ".join(str(p) for p in (exif.get("city"), exif.get("state"), exif.get("country")) if p)
    people = [str(p.get("name")) for p in info.get("people") or [] if isinstance(p, dict) and p.get("name")]
    parts = [f"Фото {asset_id}"]
    if info.get("type") == "VIDEO":
        parts[0] = f"Видео {asset_id} (кадр)"
    if when:
        parts.append(when)
    if info.get("originalFileName"):
        parts.append(str(info["originalFileName"]))
    if place:
        parts.append(place)
    if people:
        parts.append("люди: " + ", ".join(people))
    if exif.get("description"):
        parts.append("подпись: " + str(exif["description"])[:200])
    return " | ".join(parts)


def to_jpeg(data: bytes, max_side: int) -> bytes:
    """The picture as a JPEG no larger than max_side, upright as the camera meant it."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85)
        return out.getvalue()


# --- the tool -------------------------------------------------------------------------------


def _ids(args: dict[str, Any]) -> list[str]:
    raw = args.get("ids")
    if raw is None and args.get("id"):
        raw = [args["id"]]
    if isinstance(raw, str):
        raw = [raw]
    ids = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if not ids:
        raise ValueError("pass ids: asset ids from the immich search results")
    if len(ids) > MAX_PHOTOS:
        raise ValueError(f"at most {MAX_PHOTOS} photos per call")
    bad = [x for x in ids if not _UUID.match(x)]
    if bad:
        raise ValueError(f"not Immich asset ids: {', '.join(bad)}")
    return ids


def handle(args: Optional[dict[str, Any]] = None, **kwargs: Any) -> Any:
    try:
        args = dict(args or {})
        ids = _ids(args)
        size = args.get("size") if args.get("size") in SIZES else "preview"
        conf = settings()
        if not conf["base_url"]:
            raise RuntimeError("no Immich address: set IMMICH_BASE_URL of the immich MCP server or the plugin's base_url")
        if not _api_key():
            raise RuntimeError("IMMICH_API_KEY is not set")
        content: list[dict[str, Any]] = []
        captions: list[str] = []
        for asset_id in ids:
            try:
                info = _asset_info(conf["base_url"], asset_id)
            except Exception:
                info = {}
            try:
                data, _ = _get(f"{conf['base_url']}/api/assets/{asset_id}/thumbnail?size={size}", "image/*")
                jpeg = to_jpeg(data, conf["max_side"])
            except urllib.error.HTTPError as exc:
                hint = " (the API key needs the asset.view permission)" if exc.code in (401, 403) else ""
                captions.append(f"{asset_id}: Immich answered {exc.code}{hint}")
                content.append({"type": "text", "text": captions[-1]})
                continue
            except Exception as exc:
                captions.append(f"{asset_id}: {type(exc).__name__}: {str(exc)[:200]}")
                content.append({"type": "text", "text": captions[-1]})
                continue
            caption = _caption(asset_id, info)
            captions.append(caption)
            content.append({"type": "text", "text": caption})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}})
        if not any(part["type"] == "image_url" for part in content):
            return json.dumps({"error": "; ".join(captions)}, ensure_ascii=False)
        return {"_multimodal": True, "content": content, "text_summary": "\n".join(captions)}
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False)
