#!/usr/bin/env python3
"""HH.ru авто-подъём резюме в поиске.

Заходит на https://hh.ru/applicant/resumes и для каждого резюме жмёт кнопку
«Поднять в поиске». HH разрешает поднимать вручную раз в 4 часа: если кулдаун
ещё не прошёл, скрипт читает оставшееся время с подписи «Поднять вручную можно
через…» и просто его логирует.

Запуск:
    python3 resume_boost.py            # один проход
    python3 resume_boost.py --watch    # в цикле, по интервалу
    python3 resume_boost.py --dry-run  # ничего не кликать, только распознать состояние
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from auto_apply_template import SESSION_FILE
import metrics

load_dotenv()
metrics.init_db()

RESUMES_URL = "https://hh.ru/applicant/resumes?hhtmFrom=chat&hhtmFromLabel=header"
WATCH_INTERVAL = int(os.getenv("HH_BOOST_INTERVAL_SEC", "14700"))  # 4ч 5мин
DRY_RUN = "--dry-run" in sys.argv
WATCH_MODE = "--watch" in sys.argv

# Селекторы кнопки «Поднять в поиске» — пробуем по очереди, чтобы пережить
# мелкие правки разметки на стороне HH.
BOOST_BUTTON_SELECTORS = [
    "button[data-qa='resume-update-button_button']",
    "button[data-qa^='resume-update-button']",
    "[data-qa='resume-update-button']",
    "button:has-text('Поднять в поиске')",
    "a:has-text('Поднять в поиске')",
]

# Возможные формы подписи c кулдауном:
#  - "Поднять вручную можно через 3 ч 24 мин"
#  - "Поднять вручную можно через 14 мин"
#  - "Поднять вручную можно сегодня в 17:18"
#  - "Поднять вручную можно завтра в 09:00"
COOLDOWN_RELATIVE_RE = re.compile(
    r"можно\s+через\s+(?:(\d+)\s*ч)?\s*(?:(\d+)\s*мин)?",
    re.IGNORECASE,
)
COOLDOWN_ABSOLUTE_RE = re.compile(
    r"можно\s+(сегодня|завтра)\s+в\s*(\d{1,2})[:.](\d{2})",
    re.IGNORECASE,
)


def _parse_cooldown(text: str, now: datetime | None = None) -> int | None:
    """Возвращает оставшееся время до следующего подъёма в секундах, или None.

    Поддерживает оба формата HH: относительный («через X ч Y мин») и
    абсолютный («сегодня в 17:18», «завтра в 09:00»).
    """
    if not text:
        return None
    s = text.replace("\xa0", " ")

    m = COOLDOWN_RELATIVE_RE.search(s)
    if m:
        hours = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        if hours or mins:
            return hours * 3600 + mins * 60

    m = COOLDOWN_ABSOLUTE_RE.search(s)
    if m:
        when_word = m.group(1).lower()
        hh, mm = int(m.group(2)), int(m.group(3))
        now = now or datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when_word == "завтра":
            target += timedelta(days=1)
        elif target <= now:
            # «сегодня в 09:00», когда уже 14:00 — значит на самом деле завтра.
            target += timedelta(days=1)
        delta = (target - now).total_seconds()
        return int(delta) if delta > 0 else None

    return None


def _has_cooldown_label(text: str) -> bool:
    """True, если на карточке есть подпись «Поднять вручную можно...»."""
    if not text:
        return False
    s = text.replace("\xa0", " ").lower()
    return "поднять вручную можно" in s


def _resume_cards(page) -> list:
    """Список локаторов на блок каждого резюме."""
    candidates = [
        "[data-qa='resume']",
        "[data-qa='resume-block']",
        "div.resume-applicant-block",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.all()
    # Фоллбэк: считаем, что страница содержит «глобальные» кнопки поднятия.
    return [page.locator("body")]


def _find_boost_button(scope) -> Any | None:
    # 1) Известные стабильные data-qa и точные текстовые селекторы.
    for sel in BOOST_BUTTON_SELECTORS:
        loc = scope.locator(sel)
        if loc.count() > 0:
            try:
                if loc.first.is_visible(timeout=1500):
                    return loc.first
            except Exception:
                pass

    # 2) Селекторо-независимый поиск: любой кликабельный элемент в области,
    #    чей видимый текст содержит «Поднять в поиске».
    #    Исключаем подпись «Поднять вручную можно…» (это не кнопка, а текст).
    for css in [
        "button",
        "a",
        "[role='button']",
        "span.magritte-button___-text-content",  # magritte-кнопки иногда так рендерятся
    ]:
        loc = scope.locator(css)
        n = loc.count()
        for i in range(min(n, 60)):
            el = loc.nth(i)
            try:
                if not el.is_visible(timeout=500):
                    continue
                txt = (el.inner_text(timeout=800) or "").lower()
            except Exception:
                continue
            if "поднять" not in txt:
                continue
            if "вручную" in txt:
                continue  # это подпись с таймером, не кнопка
            if "поднять в поиске" in txt or "поднять резюме" in txt or txt.strip() == "поднять":
                return el
    return None


def _scope_text(scope) -> str:
    try:
        return scope.inner_text(timeout=3000)
    except Exception:
        return ""


def _resume_title(scope) -> str:
    for sel in [
        "[data-qa='resume-title']",
        "[data-qa='resume-title-text']",
        "a[data-qa^='resume-title']",
        "h2",
    ]:
        loc = scope.locator(sel)
        if loc.count() > 0:
            try:
                return (loc.first.inner_text(timeout=2000) or "").strip().splitlines()[0][:120]
            except Exception:
                continue
    return ""


def boost_resume(card) -> dict[str, Any]:
    """Пытается нажать «Поднять в поиске» в одной карточке резюме."""
    title = _resume_title(card) or "(без названия)"
    text = _scope_text(card)
    cooldown = _parse_cooldown(text)
    has_label = _has_cooldown_label(text)
    btn = _find_boost_button(card)
    # Magritte иногда выносит кнопку в обёртку выше карточки —
    # если в самой карточке не нашли, но кулдауна тоже нет, ищем глобально.
    if not btn and not has_label:
        try:
            btn = _find_boost_button(card.page)
        except Exception:
            btn = None

    if not btn:
        # Нет кнопки, но есть подпись «Поднять вручную можно…» — кулдаун.
        if has_label:
            return {"status": "cooldown", "title": title, "cooldown_sec": cooldown}
        return {"status": "no_button", "title": title, "cooldown_sec": cooldown}

    try:
        is_disabled = btn.is_disabled(timeout=2000)
    except Exception:
        is_disabled = False

    # Если у кнопки виден таймер ожидания, считаем её недоступной.
    if cooldown and cooldown > 0:
        is_disabled = True

    if DRY_RUN:
        return {
            "status": "dry_run",
            "title": title,
            "cooldown_sec": cooldown,
            "disabled": is_disabled,
        }

    if is_disabled:
        return {"status": "cooldown", "title": title, "cooldown_sec": cooldown}

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
    except PlaywrightTimeoutError as e:
        return {"status": "error", "title": title, "reason": f"click timeout: {e}"}
    except Exception as e:
        return {"status": "error", "title": title, "reason": str(e)[:200]}

    # Дать странице обновить состояние кнопки/подписи.
    try:
        card.page.wait_for_timeout(2500)
    except Exception:
        pass

    new_cooldown = _parse_cooldown(_scope_text(card))
    return {
        "status": "boosted",
        "title": title,
        "cooldown_sec": new_cooldown,
    }


def boost_all(page) -> dict[str, Any]:
    page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeoutError:
        pass

    if "captcha" in page.title().lower():
        print("  ⚠️  Сработала капча, пропускаю проход")
        metrics.log_event(kind="error", payload={"where": "resume_boost", "error": "captcha"})
        return {"boosted": 0, "cooldown": 0, "errors": 1, "no_button": 0, "min_cooldown_sec": None}

    cards = _resume_cards(page)
    print(f"  📋 Резюме на странице: {len(cards)}")

    stats: dict[str, Any] = {"boosted": 0, "cooldown": 0, "errors": 0, "no_button": 0, "min_cooldown_sec": None}
    for i, card in enumerate(cards, 1):
        result = boost_resume(card)
        title = result.get("title", "?")
        status = result["status"]
        cooldown = result.get("cooldown_sec")
        cooldown_str = f", след. через {cooldown // 60} мин" if cooldown else ""

        if status == "boosted":
            print(f"  [{i}] ✅ Поднял: {title}{cooldown_str}")
            stats["boosted"] += 1
            if cooldown:
                cur = stats["min_cooldown_sec"]
                stats["min_cooldown_sec"] = cooldown if cur is None else min(cur, cooldown)
            metrics.log_event(
                kind="boost",
                vacancy=title,
                chat_url=RESUMES_URL,
                payload={"resume_title": title, "next_in_sec": cooldown},
            )
        elif status == "cooldown":
            print(f"  [{i}] ⏳ Ждём: {title}{cooldown_str}")
            stats["cooldown"] += 1
            if cooldown:
                cur = stats["min_cooldown_sec"]
                stats["min_cooldown_sec"] = cooldown if cur is None else min(cur, cooldown)
            metrics.log_event(
                kind="skip",
                vacancy=title,
                chat_url=RESUMES_URL,
                payload={
                    "reason": "boost_cooldown",
                    "resume_title": title,
                    "next_in_sec": cooldown,
                },
            )
        elif status == "dry_run":
            disabled = result.get("disabled")
            print(f"  [{i}] 🧪 DRY: {title} · disabled={disabled}{cooldown_str}")
        elif status == "no_button":
            print(f"  [{i}] ⚠️  Кнопка не найдена: {title}")
            stats["no_button"] += 1
        else:
            reason = result.get("reason", "?")
            print(f"  [{i}] ❌ Ошибка: {title} · {reason}")
            stats["errors"] += 1
            metrics.log_event(
                kind="error",
                vacancy=title,
                chat_url=RESUMES_URL,
                payload={"where": "resume_boost", "error": reason},
            )

    return stats


def _run_once() -> dict[str, int]:
    if not os.path.exists(SESSION_FILE):
        print("\n❌ Сессия не найдена, сначала запусти python3 hh_login.py")
        return {"boosted": 0, "cooldown": 0, "errors": 1, "no_button": 0}

    proxy = (
        os.getenv("HH_PROXY")
        or os.getenv("ALL_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
    )
    launch_kwargs = {"headless": True, "slow_mo": 0}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        print(f"🌐 Прокси: {proxy}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(storage_state=SESSION_FILE)
        page = ctx.new_page()
        try:
            return boost_all(page)
        finally:
            browser.close()


def main() -> None:
    print("\n" + "=" * 50)
    print("⬆️  HH.ru авто-подъём резюме" + (" [DRY-RUN]" if DRY_RUN else ""))
    print("=" * 50)
    stats = _run_once()
    print(
        f"\nИтоги: ✅ {stats['boosted']} поднято · "
        f"⏳ {stats['cooldown']} в кулдауне · "
        f"⚠️ {stats['no_button']} без кнопки · "
        f"❌ {stats['errors']} ошибок"
    )


def watch_loop() -> None:
    print(f"👀 WATCH-режим, проход каждые {WATCH_INTERVAL} сек. Ctrl-C, чтобы остановить.\n")
    metrics.log_event(
        kind="run_start",
        payload={"mode": "boost_watch", "interval_sec": WATCH_INTERVAL},
    )
    try:
        while True:
            min_cd: int | None = None
            try:
                stats = _run_once()
                print(f"  итоги прохода: {stats}")
                min_cd = stats.get("min_cooldown_sec") if isinstance(stats, dict) else None
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ❌ Ошибка прохода: {e}")
                metrics.log_event(
                    kind="error",
                    payload={"where": "boost_watch_loop", "error": str(e)[:500]},
                )

            # Адаптивная пауза: если в кулдауне — спим ровно до его конца
            # (плюс 60 сек запаса), иначе — стандартный WATCH_INTERVAL.
            if min_cd and min_cd > 0:
                sleep_sec = min(min_cd + 60, 6 * 3600)
                print(f"\n💤 Кулдаун {min_cd} сек, жду {sleep_sec} сек до повторной попытки...\n")
            else:
                sleep_sec = WATCH_INTERVAL
                print(f"\n💤 Жду {sleep_sec} сек до следующего подъёма...\n")
            time.sleep(sleep_sec)
    except KeyboardInterrupt:
        print("\n👋 Остановлено")
        metrics.log_event(kind="run_end", payload={"reason": "keyboard_interrupt", "mode": "boost_watch"})


if __name__ == "__main__":
    if WATCH_MODE:
        watch_loop()
    else:
        main()
