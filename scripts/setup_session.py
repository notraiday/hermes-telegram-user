"""Standalone login, for when you would rather not drive Hermes' CLI.

Normally you run the same thing through Hermes itself:

    hermes telegram-user login

This wrapper exists for the cases where that is inconvenient — a different
machine, or before the plugin is registered. Both share one implementation in
``core/login.py``.

    <hermes home>/hermes-agent/venv/bin/python scripts/setup_session.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running a script sets sys.path[0] to scripts/, so the package root has to be
# added before core.* resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.login import run_login  # noqa: E402  (after the path fix above)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Log in one Telegram account")
    parser.add_argument("--account", default="default")
    parser.add_argument("--mode", choices=["read", "write"], default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--env", type=Path, default=None)
    parser.add_argument("--print-only", action="store_true")
    a = parser.parse_args()
    raise SystemExit(
        run_login(a.env, print_only=a.print_only, account=a.account, mode=a.mode, proxy=a.proxy)
    )
