"""Pytest-conftest и одновременно общий setup для unittest.

Цель: дать тестам импортировать auto_reply без установленных anthropic/playwright/dotenv
и без profile.py с реальными ФИО. Все тяжёлые зависимости подменяются заглушками.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "anthropic" not in sys.modules:
        m = types.ModuleType("anthropic")
        m.Anthropic = lambda **kw: None  # type: ignore[attr-defined]
        sys.modules["anthropic"] = m

    if "playwright" not in sys.modules:
        sys.modules["playwright"] = types.ModuleType("playwright")
    if "playwright.sync_api" not in sys.modules:
        ps = types.ModuleType("playwright.sync_api")
        ps.sync_playwright = lambda: None  # type: ignore[attr-defined]
        ps.TimeoutError = Exception  # type: ignore[attr-defined]
        sys.modules["playwright.sync_api"] = ps

    if "dotenv" not in sys.modules:
        d = types.ModuleType("dotenv")
        d.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]
        sys.modules["dotenv"] = d

    if "auto_apply_template" not in sys.modules:
        t = types.ModuleType("auto_apply_template")
        t.ANTHROPIC_API_KEY = ""  # type: ignore[attr-defined]
        t.ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # type: ignore[attr-defined]
        t.MY_PROFILE = ""  # type: ignore[attr-defined]
        t.SESSION_FILE = "/tmp/_session"  # type: ignore[attr-defined]
        t._acquire_singleton_lock = lambda: None  # type: ignore[attr-defined]
        sys.modules["auto_apply_template"] = t

    # Изолируем БД метрик в /tmp, чтобы тесты не трогали продовую SQLite.
    os.environ.setdefault("N8N_FILES_DIR", "/tmp/hh-tests-metrics")


_install_stubs()
