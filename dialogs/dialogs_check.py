#!/usr/bin/env python3
"""Pre-run check for the dialogs cron job: did anyone answer in a delegated dialog?

Runs ``hermes telegram-user dialogs --peek``. Nothing waiting and nothing expired:
prints ``{"wakeAgent": false}`` and the model is not started. Otherwise prints the
list for the agent's prompt. Standard library only; settings from the environment
or the Hermes .env (ARCHIVER_HERMES_BIN to point at the hermes command).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
SKIP_GATE = '{"wakeAgent": false}'


def _setting(key: str) -> str:
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    try:
        for line in (HERMES_HOME / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, raw = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == key:
                return raw.strip().strip("\"'")
    except OSError:
        pass
    return ""


def main() -> int:
    binary = _setting("ARCHIVER_HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    try:
        done = subprocess.run([binary, "telegram-user", "dialogs", "--peek"],
                              capture_output=True, text=True, timeout=180)
        lines = [l for l in done.stdout.splitlines() if l.strip()]
        result = json.loads(lines[-1]) if lines else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"# Переписки по поручениям\nПроверка не удалась ({type(exc).__name__}). "
              "Посмотри tg_list_delegations и прочитай активные диалоги сам.")
        return 0
    if not isinstance(result, dict):
        err = (done.stderr or "").strip().splitlines()[-1:] or ["нет вывода"]
        print(f"# Переписки по поручениям\nПроверка не удалась ({err[0][:200]}). "
              "Посмотри tg_list_delegations и прочитай активные диалоги сам.")
        return 0

    waiting, expired, errors = [], [], []
    for row in result.get("accounts") or []:
        account = row.get("account", "?")
        if row.get("error"):
            errors.append(f"- {account}: проверить не удалось ({str(row['error'])[:200]})")
        for item in row.get("waiting") or []:
            waiting.append(f"- аккаунт {account}, поручение {item['id']} «{item['chat']}»: "
                           f"новых сообщений {item['new_messages']}. Цель: {item['goal']}")
        for item in row.get("expired") or []:
            expired.append(f"- аккаунт {account}, поручение {item['id']} «{item['chat']}» истекло. "
                           f"Цель была: {item['goal']}")
    if not (waiting or expired or errors):
        print(SKIP_GATE)
        return 0
    parts = ["# Переписки по поручениям"]
    if waiting:
        parts.append("## Ждут ответа\n" + "\n".join(waiting))
    if expired:
        parts.append("## Истекли (уже закрыты)\n" + "\n".join(expired))
    if errors:
        parts.append("## Ошибки проверки\n" + "\n".join(errors))
    print("\n\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
