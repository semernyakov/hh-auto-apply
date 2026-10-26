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
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from auto_apply_template import _SAVED_ALL_PROXY, SESSION_FILE
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
    "a[data-qa='resume-update-button']",
    "button:has-text('Поднять в поиске')",
    "a:has-text('Поднять в поиске')",
    # Новый UI HH (2025+): кликабельный div с текстом «Поднять»
    "[data-qa='resume-renewal-manual-text']",
]

# Селектор кулдаун-текста «Поднять в 12:47» / «Поднять вручную можно через…»
COOLDOWN_TEXT_SELECTORS = [
    "[data-qa='resume-renewal-manual-text']",
    "[data-qa='resume-renewal-manual-text']",
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
# Новый UI HH (2025+): «Поднять в 12:47» — без слова «можно»
COOLDOWN_SHORT_RE = re.compile(
    r"поднять\s+в\s*(\d{1,2})[:.](\d{2})",
    re.IGNORECASE,
)


def _parse_cooldown(text: str, now: datetime | None = None) -> int | None:
    """Возвращает оставшееся время до следующего подъёма в секундах, или None.

    Поддерживает форматы HH:
    - «через X ч Y мин»
    - «сегодня в 17:18», «завтра в 09:00»
    - «Поднять в 12:47» (новый UI 2025+)
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
            target += timedelta(days=1)
        delta = (target - now).total_seconds()
        return int(delta) if delta > 0 else None

    # Новый UI: «Поднять в 12:47»
    m = COOLDOWN_SHORT_RE.search(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        now = now or datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delta = (target - now).total_seconds()
        return int(delta) if delta > 0 else None

    return None


def _has_cooldown_label(text: str) -> bool:
    """True, если на карточке есть подпись «Поднять вручную можно...» или «Поднять в HH:MM»."""
    if not text:
        return False
    s = text.replace("\xa0", " ").lower()
    return (
        "поднять вручную можно" in s
        or bool(COOLDOWN_ABSOLUTE_RE.search(s))
        or bool(COOLDOWN_SHORT_RE.search(s))
    )


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
                    el = loc.first
                    # Новый UI: resume-renewal-manual-text — это текст «Поднять в HH:MM»
                    # Это кулдаун, а не кнопка. Возвращаем None, чтобы логика
                    # кулдауна отработала через _has_cooldown_label.
                    if sel == "[data-qa='resume-renewal-manual-text']":
                        return None
                    return el
            except Exception as e:
                print(f"  ⚠️  Ошибка при поиске кнопки: {e}")

    # 2) Селекторо-независимый поиск: любой кликабельный элемент в области,
    #    чей видимый текст содержит «Поднять в поиске».
    #    Исключаем подпись «Поднять вручную можно…» и «Поднять в HH:MM» (кулдаун).
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
            except Exception as e:
                print(f"  ⚠️  Ошибка чтения {css}[{i}]: {e}")
                continue
            if "поднять" not in txt:
                continue
            # Кулдаун-текст: «Поднять вручную можно…» или «Поднять в 12:47»
            if "вручную" in txt:
                continue
            if re.search(r"поднять\s+в\s*\d", txt):
                continue
            if "поднять в поиске" in txt or "поднять резюме" in txt or txt.strip() == "поднять":
                return el
    return None


def _dismiss_modals(page, max_attempts: int = 3) -> None:
    """Закрывает модальные окна HH.ru, которые блокируют клики.

    Стратегия (по приоритету):
    1. Escape — закрывает большинство модалок
    2. Клик по крестику / кнопке закрытия внутри модалки
    3. JS: скрыть overlay (фоллбэк, если ничего не помогло)
    """
    for _ in range(max_attempts):
        # Проверяем, есть ли вообще overlay
        try:
            overlay = page.locator("[data-qa='modal-overlay']")
            if overlay.count() == 0:
                return
        except Exception as e:
            print(f"  ⚠️  Ошибка при проверке модалки: {e}")
            return

        # 1. Escape — самый надёжный способ закрыть модалку на HH
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            if page.locator("[data-qa='modal-overlay']").count() == 0:
                return
        except Exception as e:
            print(f"  ⚠️  Ошибка закрытия модалки через Escape: {e}")

        # 2. Клик по кнопке закрытия внутри модалки
        close_selectors = [
            "[data-qa='modal-overlay'] [data-qa='modal-close']",
            "[data-qa='modal-overlay'] button[aria-label='Закрыть']",
            "[data-qa='modal-overlay'] button[aria-label='Close']",
            "[data-qa='modal-overlay'] .magritte-modal__close",
            "[data-qa='modal-overlay'] button:has(svg)",
            "[data-qa='modal-overlay'] [class*='close']",
            "[data-qa='modal-overlay'] button:last-child",
        ]
        closed = False
        for sel in close_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible(timeout=500):
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(500)
                    if page.locator("[data-qa='modal-overlay']").count() == 0:
                        return
                    closed = True
                    break
            except Exception as e:
                print(f"  ⚠️  Ошибка при клике по {sel}: {e}")
                continue

        if closed:
            continue

        # 3. JS: скрыть overlay, если ничего не помогло
        try:
            page.evaluate("""
                document.querySelectorAll('[data-qa="modal-overlay"]').forEach(el => {
                    el.style.display = 'none';
                });
            """)
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  ⚠️  Ошибка при скрытии overlay JS: {e}")
        return


def _scope_text(scope) -> str:
    try:
        return scope.inner_text(timeout=3000)
    except Exception as e:
        print(f"  ⚠️  Ошибка чтения текста блока: {e}")
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
            except Exception as e:
                print(f"  ⚠️  Ошибка чтения заголовка {sel}: {e}")
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
        except Exception as e:
            print(f"  ⚠️  Ошибка при глобальном поиске кнопки: {e}")
            btn = None

    if not btn:
        # Нет кнопки, но есть подпись «Поднять вручную можно…» — кулдаун.
        if has_label:
            return {"status": "cooldown", "title": title, "cooldown_sec": cooldown}
        return {"status": "no_button", "title": title, "cooldown_sec": cooldown}

    try:
        is_disabled = btn.is_disabled(timeout=2000)
    except Exception as e:
        print(f"  ⚠️  Ошибка проверки disabled кнопки: {e}")
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
        # Закрываем модальные окна перед кликом
        _dismiss_modals(card.page)

        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
    except PlaywrightTimeoutError as e:
        # Фоллбэк: принудительный клик, если overlay не закрылся
        try:
            btn.click(timeout=3000, force=True)
        except Exception as force_err:
            print(f"  ⚠️  Принудительный клик не удался: {force_err}")
            return {"status": "error", "title": title, "reason": f"click timeout: {e}"}
    except Exception as e:
        return {"status": "error", "title": title, "reason": str(e)[:200]}

    # Дать странице обновить состояние кнопки/подписи.
    try:
        card.page.wait_for_timeout(2500)
    except Exception as e:
        print(f"  ⚠️  Ошибка ожидания обновления страницы: {e}")

    new_cooldown = _parse_cooldown(_scope_text(card))
    return {
        "status": "boosted",
        "title": title,
        "cooldown_sec": new_cooldown,
    }


def boost_all(page) -> dict[str, Any]:
    # Safe navigation with retries
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=60000)
            break
        except PlaywrightTimeoutError:
            if attempt == max_attempts:
                raise
            print(f"  ⚠️  Таймаут при загрузке страницы резюме (попытка {attempt}/{max_attempts}), пробую снова...")
            page.wait_for_timeout(2000)  # небольшая пауза перед повтором

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeoutError:
        pass

    # Закрываем модальные окна сразу после загрузки страницы
    _dismiss_modals(page)

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
        or _SAVED_ALL_PROXY
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
    )
    launch_kwargs = {"headless": True, "slow_mo": 0}
    # Add Chrome args to avoid sandbox issues in some environments
    launch_kwargs["args"] = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    # Use system Chromium if specified
    chrom_exec = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if chrom_exec:
        launch_kwargs["executable_path"] = chrom_exec
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
    # Создаём PID-файл для дашборда
    pid_file = Path("/tmp/hh_resume_boost.pid")
    pid_file.write_text(str(os.getpid()))

    # Очищаем и открываем лог-файл для записи
    log_file = Path("/tmp/hh_resume_boost.log")
    log_fh = open(log_file, "w", buffering=1)
    sys.stdout = log_fh
    sys.stderr = log_fh
    try:
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
    finally:
        # Вернуть потоки до закрытия, иначе traceback упавшего прохода пропадёт
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        log_fh.close()


def watch_loop() -> None:
    # Создаём PID-файл для дашборда (для watch-режима тоже)
    pid_file = Path("/tmp/hh_resume_boost.pid")
    pid_file.write_text(str(os.getpid()))
    
    # Очищаем и открываем лог-файл для записи
    log_file = Path("/tmp/hh_resume_boost.log")
    log_fh = open(log_file, "w", buffering=1)
    sys.stdout = log_fh
    sys.stderr = log_fh

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
