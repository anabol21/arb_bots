"""Entrypoint: ``python -m app.hl_v2``."""

from __future__ import annotations

import sys

from app.hl_v2.runtime import main

raise SystemExit(main())
