#!/usr/bin/env python3
"""Ретропроход по существующим чатам HH: ищет положительные сигналы
(приглашения, просьбы написать в ТГ/позвонить, «на рассмотрении») в уже
полученных сообщениях и заводит их в БД как positive_signal — но только
для актуальных вакансий (страница не в архиве).

Запуск:
    .venv/bin/python backfill_positive.py                  # реально пишет в БД
    .venv/bin/python backfill_positive.py --dry-run        # только показывает
    .venv/bin/python backfill_positive.py --limit 80       # сколько чатов смотреть
    .venv/bin/python backfill_positive.py --headed         # с видимым браузером

Дедуп: чаты, на которые уже есть событие positive_signal* в БД, пропускаются.
"""

from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from auto_apply_template import SESSION_FILE
from auto_reply import (
    CHATS_URL,
    detect_positive_signal,
    get_chat_payload,
    is_rejection,
)
import metrics


DRY_RUN = "--dry-run" in sys.argv
HEADED = "--headed" in sys.argv
LIMIT = 80
for i, a in enumerate(sys.argv):
    if a == "--limit" and i + 1 < len(sys.argv):
        try:
            LIMIT = int(sys.argv[i + 1])
        except ValueError:
            pass

DELAY_BETWEEN_CHATS = 4


def has_existing_positive(chat_url: str) -> bool:
    """True если на этот чат уже залогирован positive_signal или _handled."""
    import sqlite3

    with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
        row = c.execute(
            "SELECT 1 FROM events WHERE chat_url = ? "
            "AND kind IN ('positive_signal','positive_signal_handled') LIMIT 1",
            (chat_url,),
        ).fetchone()
    return bool(row)


def list_all_chats(page, limit: int) -> list[tuple[str, str]]:
    """Возвращает [(chat_url, title), ...] — все чаты, без фильтра непрочитанных."""
    page.goto(CHATS_URL, wait_until="commit", timeout=30000)
    try:
        page.wait_for_selector("[data-qa^='chatik-open-chat-']", timeout=60000)
    except PlaywrightTimeoutError:
        print("⚠️  Список чатов не отрисовался за 60с")
        return []
    page.wait_for_timeout(1200)

    cards = page.evaluate(
        f"""
        () => Array.from(document.querySelectorAll('[data-qa^="chatik-open-chat-"]'))
            .slice(0, {limit})
            .map(el => ({{ qa: el.dataset.qa, text: el.innerText }}))
        """
    )
    refs: list[tuple[str, str]] = []
    for c in cards:
        cid = (c.get("qa") or "").replace("chatik-open-chat-", "").strip()
        if not cid.isdigit():
            continue
        lines = [l.strip() for l in (c.get("text") or "").split("\n") if l.strip()]
        title = lines[0][:60] if lines else ""
        refs.append((f"https://hh.ru/chat/{cid}?hhtmFrom=app", title))
    return refs


_VACANCY_ARCHIVED_MARKERS = (
    "вакансия в архиве",
    "вакансия снята",
    "вакансия закрыта",
    "вакансии больше нет",
    "вакансия не найдена",
    "страница не найдена",
)


def get_vacancy_link_from_chat(page) -> str:
    """Извлекает href из шапки открытого чата [data-qa='chatik-header-vacancy-link']."""
    try:
        return page.evaluate(
            """
            () => {
                const a = document.querySelector('[data-qa="chatik-header-vacancy-link"]');
                return a ? a.href || '' : '';
            }
            """
        ) or ""
    except Exception:
        return ""


def is_vacancy_active(context, vacancy_url: str) -> tuple[bool, str]:
    """Открывает страницу вакансии в отдельной вкладке и проверяет, что она
    не в архиве/не 404. Возвращает (active, reason)."""
    if not vacancy_url:
        return True, "no-link-assume-active"
    p = context.new_page()
    try:
        try:
            resp = p.goto(vacancy_url, wait_until="commit", timeout=30000)
        except PlaywrightTimeoutError:
            return True, "load-timeout-assume-active"
        status = resp.status if resp else 0
        if status and (status == 404 or status == 410):
            return False, f"http-{status}"
        try:
            p.wait_for_selector("body", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        p.wait_for_timeout(600)
        try:
            txt = (p.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
        except Exception:
            txt = ""
        for m in _VACANCY_ARCHIVED_MARKERS:
            if m in txt:
                return False, f"archived: {m!r}"
        return True, "active"
    finally:
        try:
            p.close()
        except Exception:
            pass


def main() -> None:
    if not os.path.exists(SESSION_FILE):
        print("❌ Сессия не найдена, сначала python3 hh_login.py")
        sys.exit(1)

    metrics.init_db()
    print(f"🔁 Backfill positive signals, limit={LIMIT}, dry_run={DRY_RUN}, headed={HEADED}")

    proxy = (
        os.getenv("HH_PROXY")
        or os.getenv("ALL_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
    )
    launch_kwargs = {"headless": not HEADED, "slow_mo": 250 if HEADED else 0}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        print(f"🌐 Прокси: {proxy}")

    stats = {"scanned": 0, "skipped_existing": 0, "no_signal": 0,
             "rejection": 0, "archived": 0, "logged": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        print("\n🔍 Список всех чатов...")
        chats = list_all_chats(page, LIMIT)
        print(f"📨 Найдено: {len(chats)}\n")

        for i, (chat_url, title) in enumerate(chats, 1):
            print(f"[{i}/{len(chats)}] {title or chat_url}")
            stats["scanned"] += 1

            if has_existing_positive(chat_url):
                print("    ⏭️  positive_signal уже есть в БД")
                stats["skipped_existing"] += 1
                continue

            try:
                payload = get_chat_payload(page, chat_url, fallback_title=title)
            except Exception as e:
                print(f"    ⚠️  не открылся чат: {e}")
                continue
            if not payload["history"]:
                print("    ⏭️  пустая история")
                continue

            if is_rejection(payload["history"]):
                print("    🚫 отказ — пропускаю")
                stats["rejection"] += 1
                continue

            sig = detect_positive_signal(payload["history"])
            if not sig:
                stats["no_signal"] += 1
                continue
            sig_type, trigger = sig
            print(f"    🎯 сигнал: {sig_type} ({trigger!r})")

            vac_link = get_vacancy_link_from_chat(page)
            active, reason = is_vacancy_active(context, vac_link)
            print(f"    📋 вакансия: {reason}")
            if not active:
                stats["archived"] += 1
                continue

            if DRY_RUN:
                print("    🧪 DRY-RUN, не пишу в БД")
                stats["logged"] += 1
            else:
                metrics.log_event(
                    kind="positive_signal",
                    vacancy=payload["title"],
                    chat_url=chat_url,
                    payload={
                        "signal_type": sig_type,
                        "trigger": trigger,
                        "history_len": len(payload["history"]),
                        "history_tail": payload["history"][-2000:],
                        "vacancy_url": vac_link,
                        "backfill": True,
                    },
                )
                stats["logged"] += 1
                print("    ✅ записано")

            time.sleep(DELAY_BETWEEN_CHATS)

        browser.close()

    print("\n" + "=" * 50)
    print("📊 ИТОГИ:")
    print(f"   просмотрено:       {stats['scanned']}")
    print(f"   уже было в БД:     {stats['skipped_existing']}")
    print(f"   без сигнала:       {stats['no_signal']}")
    print(f"   отказ работодат.:  {stats['rejection']}")
    print(f"   вакансия в архиве: {stats['archived']}")
    print(f"   {'(DRY) ' if DRY_RUN else ''}записано:        {stats['logged']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
