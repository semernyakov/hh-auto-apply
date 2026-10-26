"""Профиль соискателя: что искать, чем подписывать письма, что бот знает о вас.

Источник — profile.json рядом с проектом (его пишет раздел «Резюме» в дашборде).
Пока файла нет, значения берутся из profile.py (старый формат), так что
существующие установки работают без миграции.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).parent.resolve()
PROFILE_JSON = PROJECT_DIR / "profile.json"
PROFILE_PY = PROJECT_DIR / "profile.py"

DEFAULT_LETTER_BODY = """Добрый день!

Мне очень понравилась ваша вакансия, я был бы рад применить свой опыт и знания в вашей компании, в связи с этим прошу вас рассмотреть моё резюме."""

# поле: (тип, значение по умолчанию, лимит длины строки, лимит числа элементов)
FIELDS: dict[str, tuple[type, Any, int, int]] = {
    "resume_id": (str, "", 64, 0),
    "search_queries": (list, [], 3000, 50),
    "letter_body": (str, DEFAULT_LETTER_BODY, 3000, 0),
    "letter_signature": (str, "", 1000, 0),
    "salary_addendum": (str, "", 1000, 0),
    "my_profile": (str, "", 20000, 0),
    "self_name_markers": (list, [], 100, 20),
    "own_text_markers": (list, [], 300, 50),
}

# имя в profile.py → поле профиля
_PY_NAMES = {
    "SEARCH_QUERIES": "search_queries",
    "LETTER_SIGNATURE": "letter_signature",
    "SALARY_ADDENDUM": "salary_addendum",
    "MY_PROFILE": "my_profile",
    "SELF_NAME_MARKERS": "self_name_markers",
    "OWN_TEXT_MARKERS": "own_text_markers",
}


def _defaults() -> dict[str, Any]:
    return {k: (list(d) if isinstance(d, list) else d) for k, (_, d, _, _) in FIELDS.items()}


def _from_profile_py() -> dict[str, Any] | None:
    # Грузим по пути, а не `import profile`: в stdlib есть одноимённый модуль.
    if not PROFILE_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_user_profile", PROFILE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    data = _defaults()
    for py_name, key in _PY_NAMES.items():
        if hasattr(mod, py_name):
            value = getattr(mod, py_name)
            data[key] = list(value) if isinstance(value, (list, tuple)) else value
    return data


def source() -> str:
    """Откуда сейчас читается профиль: 'profile.json', 'profile.py' или ''."""
    if PROFILE_JSON.exists():
        return "profile.json"
    return "profile.py" if PROFILE_PY.exists() else ""


def load() -> dict[str, Any]:
    """Профиль со всеми полями. FileNotFoundError, если нет ни json, ни py."""
    if PROFILE_JSON.exists():
        data = _defaults()
        data.update(json.loads(PROFILE_JSON.read_text(encoding="utf-8")))
        return data
    data = _from_profile_py()
    if data is None:
        raise FileNotFoundError(
            "Профиль не заполнен: откройте раздел «Резюме» в дашборде "
            "или скопируйте profile.example.py → profile.py"
        )
    return data


def validate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Чистит присланные из формы значения. Возвращает (профиль, ошибки)."""
    clean, errors = _defaults(), []
    for key, (kind, _, max_len, max_items) in FIELDS.items():
        value = raw.get(key, clean[key])
        if kind is list:
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, list):
                errors.append(f"{key}: ожидается список")
                continue
            value = [str(v).strip() for v in value if str(v).strip()]
            if len(value) > max_items:
                errors.append(f"{key}: не больше {max_items} строк")
            if any(len(v) > max_len for v in value):
                errors.append(f"{key}: строка длиннее {max_len} символов")
        else:
            value = str(value or "").strip()
            if len(value) > max_len:
                errors.append(f"{key}: длиннее {max_len} символов")
        clean[key] = value
    if not clean["search_queries"]:
        errors.append("search_queries: нужен хотя бы один поисковый запрос")
    if clean["resume_id"] and not re.fullmatch(r"[0-9a-fA-F]{8,64}", clean["resume_id"]):
        errors.append("resume_id: это шестнадцатеричный код из адреса резюме на HH")
    if not clean["letter_body"]:
        clean["letter_body"] = DEFAULT_LETTER_BODY
    return clean, errors


def save(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Проверяет и атомарно записывает profile.json (права 600 — там личные данные)."""
    clean, errors = validate(raw)
    if errors:
        return clean, errors
    fd, tmp = tempfile.mkstemp(dir=PROJECT_DIR, prefix=".profile-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PROFILE_JSON)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return clean, []
