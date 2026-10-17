# immich-view

A Hermes plugin with one tool, `immich_view_photo`: the agent looks at photos from
the owner's Immich library. The immich MCP server (mcp4immich) finds photos and
returns their ids; this tool fetches them and returns them as images in Hermes'
multimodal tool-result envelope, so a vision model sees the picture itself.

Per photo, two read-only calls on the home network (never through a proxy):
`GET /api/assets/{id}` for the caption (date, file name, place, people) and
`GET /api/assets/{id}/thumbnail` for the picture, which is turned upright, shrunk
(`max_side`, default 1024 px) and re-encoded as JPEG — Immich thumbnails are often
WebP, which the local model's image loader may not read. Up to 4 photos per call.

Needs: `IMMICH_API_KEY` with **asset.view** (besides asset.read for the caption);
the address is the immich MCP server's `IMMICH_BASE_URL` unless the plugin's
`base_url` setting is given; Pillow (a Hermes core dependency); a main model with
vision (`model.supports_vision: true` makes it explicit).

Install: copy this directory to `~/.hermes/plugins/immich-view`, then
`hermes plugins enable immich-view` and restart the gateway.
