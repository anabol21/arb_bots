"""Entrypoint: python -m app.bot"""

from __future__ import annotations

from app.bot.runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
