# Archiver (cron job), not part of the plugin

Files for a Hermes cron job that files new Telegram messages, vault edits and calendar
changes into the Obsidian vault. They live here because the plugin clone is already on the
server; Hermes does not load anything from this folder.

- `archiver_check.py` — the pre-run script. Copy it to `~/.hermes/scripts/`. Without the
  model it commits the vault into a local git repository outside the vault, asks Davis for
  calendar changes (WebDAV sync-collection), runs `hermes telegram-user inbox --peek`, and
  prints either a report for the agent or `{"wakeAgent": false}`. State lives in
  `~/.hermes/state/archiver/`. Settings (environment or the Hermes `.env`):
  `ARCHIVER_VAULT` (default `/srv/vault`), `DAVIS_URL` (default `http://10.0.0.128:9000`),
  `DAVIS_USER` (default `raiday`), `DAVIS_PASSWORD`.
- `SKILL.md` — the archiver's rules. Copy it to `~/.hermes/skills/archiver/SKILL.md`.

The job: `hermes cron create "*/5 8-22 * * *" "<prompt>" --name archiver --skill archiver
--script archiver_check.py --deliver telegram`, with `platform_toolsets.cron` limited to
`[telegram_user, vault, calendar, kagi]`.

The archiver records what it changed in `Агент/Архиватор/Последние правки.md`; the next
check leaves those files and events out, so it never reacts to its own edits.
