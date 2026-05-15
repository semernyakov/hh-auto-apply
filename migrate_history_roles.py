#!/usr/bin/env python3
"""Миграция: исправить ошибочные роли в `payload.history_tail` уже залогированных
событий. Применяет ту же эвристику `_text_is_own`, что и парсер при сборе чата
(LETTER_SIGNATURE, SELF_NAME_MARKERS, OWN_TEXT_MARKERS, prev_self_replies).

Контекст: до фикса в db56e48 ручные сообщения пользователя через UI HH могли
быть атрибутированы работодателю — текст блока в history_tail сохранён в виде
«[Лидия] Извините, отказываю». Этот скрипт не пересобирает чат заново (исходных
DOM-сигналов уже нет), а делает content-override на УЖЕ размеченных блоках:
если текст совпадает с маркерами «своего», переписывает префикс на [Я].

Запуск:
    python3 migrate_history_roles.py            # dry-run, печатает превью изменений
    python3 migrate_history_roles.py --apply    # реально записывает в БД
    python3 migrate_history_roles.py --days 30  # окно сканирования

Безопасность: пишет только в payload.history_tail; ts/kind/chat_url/исходные
блоки сообщений в чате HH не трогает.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Iterable

import metrics
from auto_reply import SELF_ROLE, _text_is_own, get_last_self_replies


TARGET_KINDS = ("reply", "skip", "rejection", "positive_signal", "robot_questionnaire")


def _rewrite_block(block: str, prev_self_replies: tuple[str, ...]) -> tuple[str, bool]:
    """Если первая строка — [Роль] и тело совпадает с маркерами «своего»,
    переписывает префикс на [Я]. Возвращает (новый_блок, изменено?)."""
    b = block.strip()
    if not b.startswith("["):
        return block, False
    end = b.find("]")
    if end <= 0:
        return block, False
    role = b[1:end].strip()
    body = b[end + 1 :].lstrip("\n").lstrip()
    if role == SELF_ROLE:
        return block, False
    if _text_is_own(body, prev_self_replies):
        return f"[{SELF_ROLE}] {body}", True
    return block, False


def _rewrite_history(history: str, prev_self_replies: tuple[str, ...]) -> tuple[str, int]:
    if not history:
        return history, 0
    blocks = history.strip().split("\n\n")
    out: list[str] = []
    changes = 0
    for b in blocks:
        new_b, changed = _rewrite_block(b, prev_self_replies)
        out.append(new_b)
        if changed:
            changes += 1
    return "\n\n".join(out), changes


def _events_to_process(days: int) -> Iterable[tuple[int, str, str, str]]:
    """Возвращает (id, kind, chat_url, payload_json) для всех событий за окно."""
    since = __import__("time").time() - days * 86400
    placeholders = ",".join("?" * len(TARGET_KINDS))
    with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
        rows = c.execute(
            f"""SELECT id, kind, chat_url, payload_json
                FROM events
                WHERE ts >= ?
                  AND kind IN ({placeholders})
                  AND payload_json IS NOT NULL
                  AND payload_json LIKE '%"history_tail"%'
                ORDER BY ts ASC""",
            (since, *TARGET_KINDS),
        ).fetchall()
    for r in rows:
        yield int(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="реально писать в БД")
    ap.add_argument("--days", type=int, default=60, help="окно в днях (default 60)")
    ap.add_argument("--limit-preview", type=int, default=5, help="сколько примеров печатать")
    args = ap.parse_args()

    total = 0
    changed_events = 0
    changed_blocks = 0
    previews_left = args.limit_preview

    for event_id, kind, chat_url, pj in _events_to_process(args.days):
        total += 1
        try:
            payload = json.loads(pj) if pj else {}
        except Exception:
            continue
        tail = payload.get("history_tail") or ""
        if not tail:
            continue
        prev_self = tuple(get_last_self_replies(chat_url, n=20))
        new_tail, n_changes = _rewrite_history(tail, prev_self)
        if n_changes == 0:
            continue
        changed_events += 1
        changed_blocks += n_changes

        if previews_left > 0:
            print(f"\n— event #{event_id} kind={kind} chat={chat_url[:80]}")
            print(f"  блоков переписано: {n_changes}")
            # Покажем дифф первых ~600 символов.
            old_snip = (tail[:300] + ("…" if len(tail) > 300 else "")).replace("\n", "\\n")
            new_snip = (new_tail[:300] + ("…" if len(new_tail) > 300 else "")).replace("\n", "\\n")
            print(f"  было: {old_snip}")
            print(f"  стало: {new_snip}")
            previews_left -= 1

        if args.apply:
            payload["history_tail"] = new_tail
            payload["_role_migration"] = {"changed_blocks": n_changes}
            with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
                c.execute(
                    "UPDATE events SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), event_id),
                )

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"\n[{mode}] окно={args.days}д · всего просмотрено: {total} · "
        f"событий с правками: {changed_events} · блоков переписано: {changed_blocks}"
    )
    if not args.apply and changed_events:
        print("Запусти повторно с --apply, чтобы записать в БД.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
