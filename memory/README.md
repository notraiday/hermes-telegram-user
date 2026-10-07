# Memory search (qmd collections), not part of the plugin

Three qmd collections make up the owner's searchable memory:

| collection | path | refreshed by |
|---|---|---|
| `vault` | `/srv/vault` | the files themselves |
| `telegram` | `~/.hermes/state/memory/telegram` | `hermes telegram-user export --markdown … --ocr` |
| `calendar` | `~/.hermes/state/memory/calendar` | `python3 ~/.hermes/scripts/calendar_export.py …` |

`calendar_export.py` needs `archiver_check.py` next to it (it reuses its CalDAV
client); copy both into `~/.hermes/scripts/`.

With `--ocr` the export turns photos, image documents and PDFs into text with the
vision model from Hermes' own config (`auxiliary.vision`, else the main model; an
Ollama endpoint is called through its own API with thinking off): new ones at
every run, old ones only at night (`HERMES_TG_USER_OCR_NIGHT`, default `23-8`).
`HERMES_TG_USER_VISION_MODEL` / `HERMES_TG_USER_VISION_URL` override the config.
PDFs with a text layer need `pdftotext`, scanned ones `pdftoppm` (poppler-utils).

`~/.config/qmd/index.yml` (collections part):

```yaml
collections:
  vault:
    path: /srv/vault
    pattern: "**/*.md"
    ignore: [".obsidian/**", ".trash/**", "Агент/Архиватор/**"]
    context: {"/": "Заметки Obsidian владельца: люди, дневник, проекты; Агент/ — рабочие файлы помощника"}
  telegram:
    path: /home/hermes/.hermes/state/memory/telegram
    pattern: "**/*.md"
    update: "hermes telegram-user export --markdown /home/hermes/.hermes/state/memory/telegram --sync-seconds 60 --ocr --ocr-seconds 180"
    context: {"/": "Переписка Telegram владельца по месяцам: Избранное, личные чаты, коллекции; media/ — распознанные фото и документы"}
  calendar:
    path: /home/hermes/.hermes/state/memory/calendar
    pattern: "**/*.md"
    update: "python3 /home/hermes/.hermes/scripts/calendar_export.py /home/hermes/.hermes/state/memory/calendar"
    context: {"/": "Календарь владельца: все события по месяцам"}
```

The search server: `qmd mcp --http --host 127.0.0.1 --port 8181` as a user service.
`--host 127.0.0.1` matters: without it qmd binds `localhost`, which current Node
resolves to IPv6 `::1` only, and Hermes connecting to `127.0.0.1` finds nothing.

`qmd-relevance.patch` (for qmd 2.8.3) makes the MCP `query` tool return the
reranker's own judgement as `relevance` (0–1) next to `score`, and show it in the
text summary. qmd computes it anyway but its MCP server drops it; `score` is a blend
that keeps the top hit at ≥0.75 even when nothing relevant was found, so the agent
cannot tell "found" from "nothing there". Apply it after every install or update of
qmd, then restart the search service:

```sh
P=~/.hermes/plugins/telegram-user/memory/qmd-relevance.patch
cd "$(npm root -g)/@tobilu/qmd"
if patch -p1 -R --dry-run -s -f < "$P" >/dev/null; then echo "already applied"
elif patch -p1 --forward --dry-run < "$P"; then patch -p1 --forward < "$P"; fi
systemctl --user restart qmd-mcp
```

If npm's global directory belongs to root, put `sudo` before the last `patch` (the one after `then`).
A failed hunk means qmd changed; do not force it, the patch needs redoing.
