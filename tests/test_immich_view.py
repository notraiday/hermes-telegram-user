"""immich_view_photo, offline: a fake Immich on localhost, the envelope the model gets."""

import base64
import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "immich_view"
PHOTO = "11111111-2222-3333-4444-555555555555"
LOCKED = "99999999-2222-3333-4444-555555555555"


def _view():
    if "immich_view_under_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location("immich_view_under_test", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["immich_view_under_test"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("immich_view_under_test.view")


def _webp(width, height):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="WEBP")
    return out.getvalue()


@pytest.fixture
def immich(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("x-api-key")))
            if self.path.startswith(f"/api/assets/{LOCKED}/thumbnail"):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == f"/api/assets/{PHOTO}":
                body = json.dumps({"id": PHOTO, "type": "IMAGE", "localDateTime": "2026-07-14T18:05:00.000Z",
                                   "originalFileName": "IMG_0420.HEIC",
                                   "exifInfo": {"city": "Сочи", "country": "Россия"},
                                   "people": [{"name": "Маша"}, {"name": ""}]}).encode()
                ctype = "application/json"
            elif self.path.startswith(f"/api/assets/{PHOTO}/thumbnail"):
                body, ctype = _webp(3000, 2000), "image/webp"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    view = _view()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("IMMICH_API_KEY", "read-only-key")
    monkeypatch.setattr(view, "_hermes_config", lambda: {"mcp_servers": {"immich": {"env": {"IMMICH_BASE_URL": base + "/api/"}}}})
    yield view, seen
    server.shutdown()


def test_the_model_gets_the_picture_itself_with_a_caption(immich):
    view, seen = immich
    result = view.handle({"ids": [PHOTO]})
    assert result["_multimodal"] is True
    caption, image = result["content"]
    assert caption == {"type": "text", "text": f"Фото {PHOTO} | 2026-07-14 18:05 | IMG_0420.HEIC | Сочи, Россия | люди: Маша"}
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    jpeg = base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    with Image.open(io.BytesIO(jpeg)) as picture:  # WebP in, JPEG out, shrunk to 1024 on the long side
        assert picture.format == "JPEG" and picture.size == (1024, 683)
    assert result["text_summary"] == caption["text"]
    assert ("/api/assets/" + PHOTO + "/thumbnail?size=preview", "read-only-key") in seen  # /api/ not doubled


def test_a_locked_photo_says_which_permission_is_missing(immich):
    view, _ = immich
    result = view.handle({"ids": [LOCKED]})
    assert isinstance(result, str) and "asset.view" in json.loads(result)["error"]
    mixed = view.handle({"ids": [PHOTO, LOCKED], "size": "thumbnail"})
    texts = [p["text"] for p in mixed["content"] if p["type"] == "text"]
    assert any("asset.view" in t for t in texts) and sum(p["type"] == "image_url" for p in mixed["content"]) == 1


def test_bad_input_is_refused_before_any_request(immich):
    view, seen = immich
    assert "not Immich asset ids" in json.loads(view.handle({"ids": ["../../server/version"]}))["error"]
    assert "at most 4" in json.loads(view.handle({"ids": [PHOTO] * 5}))["error"]
    assert "pass ids" in json.loads(view.handle({}))["error"]
    assert not seen


def test_settings_fall_back_to_the_mcp_servers_address(immich, monkeypatch):
    view, _ = immich
    assert view.settings()["base_url"].endswith(":" + view.settings()["base_url"].rsplit(":", 1)[1])
    assert not view.settings()["base_url"].endswith("/api")
    monkeypatch.setattr(view, "_hermes_config", lambda: {"plugins": {"entries": {"immich-view": {"settings": {
        "base_url": "http://10.0.0.110:2283/", "max_side": 99999}}}}})
    assert view.settings() == {"base_url": "http://10.0.0.110:2283", "max_side": 2048}
    monkeypatch.delenv("IMMICH_API_KEY")
    assert view.available() is False
