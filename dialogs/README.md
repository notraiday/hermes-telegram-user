# Dialogs (cron job), not part of the plugin

Carries delegated conversations (see `tg_delegate_dialog`) without asking the owner
about every message.

- `dialogs_check.py` — the pre-run script: runs `hermes telegram-user dialogs --peek`
  and prints the dialogs with unseen answers, or `{"wakeAgent": false}`. Copy it to
  `~/.hermes/scripts/`.
- `SKILL.md` — how to carry a dialog. Copy it to `~/.hermes/skills/dialogs/SKILL.md`.

The job: `hermes cron create "*/2 8-22 * * *" "<prompt>" --name dialogs --skill dialogs
--script dialogs_check.py --deliver telegram`.
