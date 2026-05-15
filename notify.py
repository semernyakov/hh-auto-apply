"""Telegram-нотификации для важных событий HH-бота.

Шлёт сообщение в Telegram при появлении положительного сигнала
(приглашение на интервью, запрос контакта, отклик на рассмотрении)
и при анкете от робота-рекрутёра — то есть на события, которые
требуют немедленного внимания живого человека.

Конфигурация через переменные окружения:
    TELEGRAM_BOT_TOKEN  — токен бота (получить у @BotFather)
    TELEGRAM_CHAT_ID    — id чата/пользователя (получить у @userinfobot)

Если переменные не заданы — модуль silently превращается в no-op,
чтобы не ломать бота у пользователей без настроенной интеграции.
Все ошибки сети/HTTP логируются и подавляются — нотификация не
должна ронять основной воркер.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_TIMEOUT_SEC = float(os.getenv("TELEGRAM_TIMEOUT_SEC", "5"))

SIGNAL_LABELS = {
    "interview": "🎯 Приглашение на интервью",
    "contact_request": "📞 Запрос контакта",
    "under_review": "👀 Отклик на рассмотрении",
}


def is_configured() -> bool:
    return bool(_TOKEN and _CHAT_ID)


def _send_raw(text: str) -> bool:
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": _CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ok = (json.loads(body) or {}).get("ok", False)
            if not ok:
                print(f"  ⚠️  Telegram: API ответил not ok — {body[:200]}")
            return bool(ok)
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  Telegram HTTPError {e.code}: {e.reason}")
    except Exception as e:
        print(f"  ⚠️  Telegram: {e}")
    return False


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_positive_signal(
    signal_type: str,
    trigger: str,
    vacancy: str,
    chat_url: str,
    last_message: str = "",
) -> bool:
    if not is_configured():
        return False
    label = SIGNAL_LABELS.get(signal_type, f"🎯 {signal_type}")
    lines = [
        f"<b>{label}</b>",
        f"<b>Вакансия:</b> {_esc(vacancy or '(чат)')}",
    ]
    if trigger:
        lines.append(f"<b>Триггер:</b> «{_esc(trigger)}»")
    if last_message:
        snippet = last_message.strip().replace("\n", " ")[:300]
        lines.append(f"<b>Сообщение:</b> {_esc(snippet)}")
    if chat_url:
        lines.append(f'<a href="{_esc(chat_url)}">Открыть чат</a>')
    return _send_raw("\n".join(lines))


def notify_robot_questionnaire(
    reason: str,
    vacancy: str,
    chat_url: str,
    questions: list[str] | None = None,
    last_question: str = "",
) -> bool:
    if not is_configured():
        return False
    qs = questions or ([last_question] if last_question else [])
    lines = [
        "<b>🤖 Анкета от автобота — нужен ручной ответ</b>",
        f"<b>Вакансия:</b> {_esc(vacancy or '(чат)')}",
        f"<b>Причина:</b> {_esc(reason or '—')}",
        f"<b>Вопросов:</b> {len(qs)}",
    ]
    if qs:
        lines.append("")
        for i, q in enumerate(qs[:10], 1):
            lines.append(f"{i}. {_esc(q[:250])}")
    if chat_url:
        lines.append("")
        lines.append(f'<a href="{_esc(chat_url)}">Открыть чат</a>')
    return _send_raw("\n".join(lines))


if __name__ == "__main__":
    if not is_configured():
        print("Telegram не настроен. Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
    else:
        ok = _send_raw("✅ HH-бот: проверка связи. Telegram-нотификации работают.")
        print("OK" if ok else "FAIL")
