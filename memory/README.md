# Memory search (qmd collections), not part of the plugin

Three qmd collections make up the owner's searchable memory:

| collection | path | refreshed by |
|---|---|---|
| `vault` | `/srv/vault` | the files themselves |
| `telegram` | `~/.hermes/state/memory/telegram` | `hermes telegram-user export --markdown … --ocr` |
| `calendar` | `~/.hermes/state/memory/calendar` | `python3 ~/.hermes/scripts/calendar_export.py …` |

`calendar_export.py` needs `archiver_check.py` next to it (it reuses its CalDAV
client); copy both into `~/.hermes/scripts/`.

The export turns photos, image documents and PDFs into text when
`HERMES_TG_USER_VISION_MODEL` names a vision model in Ollama
(`HERMES_TG_USER_OLLAMA_URL`, default `http://10.10.10.2:11434`): new ones at
every run, old ones only at night (`HERMES_TG_USER_OCR_NIGHT`, default `23-8`).
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
