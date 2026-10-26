#!/usr/bin/env python3
"""
HH.ru Авто-ответчик на сообщения от работодателей.

Заходит в чаты HH, находит непрочитанные диалоги, читает последнее сообщение
от работодателя (включая авто-анкеты от ботов) и генерирует ответ через
Claude Haiku 4.5 на основе MY_PROFILE из auto_apply_template.py.

Запуск:
    python3 auto_reply.py            # реальный режим, постит ответы
    python3 auto_reply.py --dry-run  # генерирует, но не отправляет

Перед запуском нужна сохранённая HH-сессия (python3 hh_login.py).
"""

import json
import os
import random
import re
import sqlite3
import sys
import time
from anthropic import Anthropic
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from auto_apply_template import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    MY_PROFILE,
    SESSION_FILE,
    launch_browser,
    _acquire_singleton_lock,
)
import metrics
import notify
import profile_store

load_dotenv()
metrics.init_db()

# Пауза между чатами — РАНДОМНЫЙ диапазон (сек). Ровный ритм палит бота.
DELAY_CHAT_MIN = int(os.getenv("HH_DELAY_CHAT_MIN", "20"))
DELAY_CHAT_MAX = int(os.getenv("HH_DELAY_CHAT_MAX", "40"))
MAX_CHATS_PER_RUN = 30
CHATS_URL = "https://hh.ru/chat"


def _chat_delay() -> float:
    """Рандомная пауза между чатами — человекоподобность против антибана."""
    return random.uniform(DELAY_CHAT_MIN, DELAY_CHAT_MAX)

# Минимальный интервал между повторными генерациями в одном и том же чате,
# если содержимое последнего входящего блока НЕ изменилось. Защита от карусели:
# когда Claude возвращает SKIP (отвечать нечего), бот ничего не отправляет в
# HH, чат остаётся «непрочитанным», и следующий watch-обход через 30 мин
# дёргает Claude по нему снова. Каждое такое срабатывание — $0.005–0.007.
# При 5 повторных обходах на одном чате это $0.025+ впустую.
#
# Дефолт 21600s (6 часов) рассчитан так, чтобы покрывать ≥10 обходов при
# дефолтном HH_WATCH_INTERVAL_SEC=1800. Cooldown не «замораживает» чат: если
# работодатель напишет что-то новое, `_last_incoming_block` это увидит,
# сравнение покажет «изменилось» — cooldown пропустит запрос к Claude.
# 0 = отключить cooldown полностью (не рекомендуется).
REPLY_COOLDOWN_SEC = int(os.getenv("HH_REPLY_COOLDOWN_SEC", "21600"))

DRY_RUN = "--dry-run" in sys.argv
WATCH_MODE = "--watch" in sys.argv
WATCH_INTERVAL = int(os.getenv("HH_WATCH_INTERVAL_SEC", "1800"))  # 30 минут по умолчанию
# Джиттер интервала обхода чатов (доля): реальная пауза = WATCH_INTERVAL * (1 ± JITTER)
WATCH_INTERVAL_JITTER = float(os.getenv("HH_WATCH_INTERVAL_JITTER", "0.2"))

# OpenRouter клиент (OpenAI-совместимый API)
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)


# --- Антиповтор проектов в рамках одного чата --------------------------------
# Если бот в каждом ответе тянет одни и те же проекты (ProjectA, ProjectB…)
# — диалог звучит как заевшая пластинка. Извлекаем имена проектов из профиля,
# при генерации очередного ответа смотрим, сколько раз каждый уже всплывал
# в наших прошлых ответах ИМЕННО В ЭТОМ ЧАТЕ, и просим Claude использовать
# другие проекты или обтекаемые формулировки.

# Технологии и аббревиатуры, которые случайно попадают под CamelCase/CAPS-эвристику,
# но проектами не являются.
_PROJECT_BLACKLIST = {
    "LangChain", "TensorFlow", "PostgreSQL", "OpenStreetMap", "IndexedDB",
    "JavaScript", "TypeScript", "FastAPI", "MongoDB", "PyTorch", "Magritte",
    "GitHub", "DockerHub", "ClickHouse", "CockroachDB", "Kubernetes",
    "Prometheus", "Grafana", "Selenium", "Playwright", "OpenCV", "NumPy",
    "Pandas", "NetworkX", "RabbitMQ", "ElasticSearch", "OpenAI", "ChatGPT",
    "DeepSeek", "Anthropic", "Linkedin", "LinkedIn", "WhatsApp", "Telegram",
    "MacBook", "JetBrains", "VSCode", "WebSocket", "WebRTC", "GraphQL",
    "Solidity", "Polygon", "Granian", "MinIO", "Qdrant", "Redis",
    "РФ", "ТК", "НДС", "ИП", "ГПХ", "НПД", "ООО", "СНГ", "АО", "НДА",
    "НИИ", "ОКР", "НИОКР", "ML", "AI", "LLM", "RAG", "API", "MCP",
    "SQL", "ORM", "DDD", "CQRS", "TDD", "CI", "CD", "SaaS", "B2B", "B2C",
    "MVP", "KPI", "OKR", "CTO", "CIO", "DSL", "JWT", "SSO", "RLS", "MoE",
    "GMT",
}


def _extract_project_names(profile_text: str) -> set[str]:
    """Эвристика: имена собственных проектов в профиле.

    Берём:
      - Длинные CamelCase из ≥2 «слов» и ≥8 букв (например, ProjectAlpha, BetaPlatform).
      - Кириллические аббревиатуры из ≥3 заглавных подряд (например, НИИ, ЦБ).
    Отсеиваем технологический мусор через _PROJECT_BLACKLIST.
    """
    if not profile_text:
        return set()
    found: set[str] = set()
    for m in re.finditer(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", profile_text):
        name = m.group(0)
        if len(name) >= 8:
            found.add(name)
    for m in re.finditer(r"[А-ЯЁ]{3,}", profile_text):
        found.add(m.group(0))
    return {n for n in found if n not in _PROJECT_BLACKLIST}


PROJECT_NAMES: set[str] = _extract_project_names(MY_PROFILE)


def _count_project_in_text(name: str, text: str) -> int:
    """Подсчёт вхождений имени проекта в текст (case-insensitive, с границами).

    Границей считается отсутствие буквы/цифры слева и справа — чтобы 'РТ' не
    срабатывало внутри «уверенноСТРТ-чего-то» и т.п.
    """
    if not name or not text:
        return 0
    n, t = name.lower(), text.lower()
    cnt, i = 0, 0
    while True:
        idx = t.find(n, i)
        if idx < 0:
            break
        left = t[idx - 1] if idx > 0 else ""
        right = t[idx + len(n)] if idx + len(n) < len(t) else ""
        if not left.isalnum() and not right.isalnum():
            cnt += 1
        i = idx + len(n)
    return cnt


PROJECT_OVERUSE_THRESHOLD = 2


def get_last_self_replies(chat_url: str, n: int = 3) -> list[str]:
    """Последние n собственных ответов бота в этом же чате, новейшие — первыми.

    Используется чтобы Claude видел, что уже отправлял, и не повторял ту же
    мысль (паттерн из цепочек 7/9, где бот два раза подряд писал одинаковое)."""
    if not chat_url:
        return []
    try:
        with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
            rows = c.execute(
                """SELECT payload_json FROM events
                   WHERE kind = 'reply' AND chat_url = ?
                   ORDER BY ts DESC LIMIT ?""",
                (chat_url, n),
            ).fetchall()
    except Exception:
        return []
    out: list[str] = []
    for (pj,) in rows:
        if not pj:
            continue
        try:
            txt = (json.loads(pj).get("reply") or "").strip()
        except Exception:
            continue
        if txt and txt.upper() != "SKIP":
            out.append(txt)
    return out


def get_last_processed_meta(chat_url: str) -> tuple[str, float, str] | None:
    """(kind, ts, history_tail) последнего события, которым мы уже отреагировали
    на этот чат — reply, robot_questionnaire или skip-cooldown. None если такого
    нет. Используется для cooldown: если этот же входящий блок мы недавно уже
    видели и отметили, повторно дёргать Claude нет смысла."""
    if not chat_url:
        return None
    try:
        with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
            row = c.execute(
                """SELECT kind, ts, payload_json FROM events
                   WHERE chat_url = ?
                     AND kind IN ('reply', 'robot_questionnaire', 'skip')
                   ORDER BY ts DESC LIMIT 1""",
                (chat_url,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    kind, ts, pj = row
    try:
        tail = (json.loads(pj or "{}") or {}).get("history_tail", "")
    except Exception:
        tail = ""
    return str(kind), float(ts), tail or ""


def get_chats_to_retry(days: int = 7, limit: int = 10, max_attempts: int = 3) -> list[tuple[str, str]]:
    """Чаты, где последняя запись — ошибка модели или неотправленный ответ.
    Открытый чат HH уже пометил прочитанным, фильтр «непрочитанные» его больше
    не вернёт — подбираем такие из БД."""
    try:
        with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
            rows = c.execute(
                """SELECT e.chat_url, MAX(e.vacancy), MAX(e.ts) AS last_ts FROM events e
                   WHERE ((e.kind = 'error' AND json_extract(e.payload_json, '$.retryable') = 1)
                          OR (e.kind = 'reply_unsent'
                              AND json_extract(e.payload_json, '$.reason') != 'dry_run'))
                     AND e.chat_url LIKE 'https://hh.ru/chat/%' AND e.ts > ?
                     AND NOT EXISTS (
                         SELECT 1 FROM events x
                         WHERE x.chat_url = e.chat_url AND x.ts > e.ts
                           AND x.kind IN ('reply', 'skip', 'rejection',
                                          'positive_signal', 'robot_questionnaire'))
                   GROUP BY e.chat_url
                   HAVING COUNT(*) < ?
                   ORDER BY last_ts DESC LIMIT ?""",
                (time.time() - days * 86400, max_attempts, limit),
            ).fetchall()
    except Exception:
        return []
    return [(url, vac or "") for url, vac, _ in rows]


def _last_incoming_block(history: str) -> str:
    """Текст последнего блока истории, который НЕ от нас (для сравнения cooldown)."""
    if not history:
        return ""
    for b in reversed([x.strip() for x in history.strip().split("\n\n") if x.strip()]):
        if not b.startswith(SELF_ROLE_PREFIX):
            return b
    return ""


def get_overused_projects_in_chat(chat_url: str) -> list[str]:
    """Имена проектов, которые в наших прошлых reply в этом чате
    встречались ≥ PROJECT_OVERUSE_THRESHOLD раз суммарно. Свежие — первыми."""
    if not chat_url or not PROJECT_NAMES:
        return []
    counts: dict[str, int] = {}
    try:
        with sqlite3.connect(metrics.DB_PATH, timeout=10) as c:
            rows = c.execute(
                """SELECT payload_json FROM events
                   WHERE kind = 'reply' AND chat_url = ?
                   ORDER BY ts DESC LIMIT 50""",
                (chat_url,),
            ).fetchall()
    except Exception:
        return []
    for (pj,) in rows:
        if not pj:
            continue
        try:
            txt = (json.loads(pj).get("reply") or "")
        except Exception:
            continue
        if not txt:
            continue
        for name in PROJECT_NAMES:
            n = _count_project_in_text(name, txt)
            if n:
                counts[name] = counts.get(name, 0) + n
    return [name for name, c in sorted(counts.items(), key=lambda kv: -kv[1])
            if c >= PROJECT_OVERUSE_THRESHOLD]


SYSTEM_PROMPT = """Ты помогаешь соискателю отвечать рекрутёрам и автоматическим ботам работодателей в чате HH.ru. Имя, проекты и опыт соискателя берёшь из блока ПРОФИЛЬ ниже.

ПРАВИЛА:
- Отвечай по-русски, как живой человек, кратко и по делу.
- Опирайся В ОСНОВНОМ на профиль ниже: проекты, стек, метрики, роли. Допустимы небольшие отступления (общеизвестные технические факты, естественные связки), но НЕ выдумывай конкретные числа, названия проектов, должности и сроки, которых в профиле нет.
- НАЗВАНИЯ ПРОЕКТОВ: используй ИСКЛЮЧИТЕЛЬНО так, как они написаны в блоке ПРОФИЛЬ. НЕ сокращай, НЕ переименовывай, НЕ комбинируй части разных имён, НЕ выдумывай похожие или новые. Каждое имя из профиля — это отдельный конкретный проект; не путай их между собой и не подменяй чем-то «правдоподобным».
- Если бот задал список вопросов, отвечай по пунктам в том же порядке.
- Если последнее сообщение это просто приветствие или приглашение на интервью, согласись коротко и спроси про удобное время.
- Если последнее сообщение от соискателя (то есть отвечать не на что), верни одно слово: SKIP
- Если сообщение требует решения, которое только сам соискатель может принять (зарплата ниже ожиданий, релокация, конкретные условия), верни: SKIP
- Длина: 3–5 предложений. Не больше. Если получается длиннее — сокращай, оставляй главное.
- РЕЖИМ БИНАРНОГО ВОПРОСА: если последний вопрос работодателя — это бинарный/фактический («Был ли опыт X?», «Есть ли Y?», «Знакомы ли с Z?», «Готовы ли к удалёнке?», «Сколько лет опыта?», «Какой стек?», «Какие RPS поддерживали?», «Удобно ли N часов?») — ответ должен быть ≤ 25 слов, 1–2 предложения. Первое слово — «Да», «Нет» или конкретная цифра/название инструмента. Без встречного вопроса в конце. Один пример из профиля, без перечислений нескольких проектов. Такой стиль читается как живой инженер; длинная простыня — главный признак ChatGPT-копипасты, которую палит рекрутёр на бинарных вопросах.
- ОДИН ПРОЕКТ НА ОТВЕТ. Не больше. Выбирай ОДИН наиболее релевантный проект и раскрывай его. Структура «В A… В B… В C…» через перечисление 3–4 проектов в одном сообщении — главный признак машинного текста, рекрутёры это палят. Исключение: если вопрос явно про несколько разных доменов (A или B, в каких ролях работал на разных стеках) — допустимо ДВА проекта, не больше.
- ЧИСЛА И МЕТРИКИ: конкретные показатели из профиля (например «95% coverage», «6→12 FPS», «16+ лет») используй ТОЛЬКО когда вопрос прямо касается этой метрики (вопрос про тесты → про coverage; вопрос про опыт → про годы). Не вставляй одну и ту же цифру в каждый ответ для «вес» — это обесценивает её.
- ФИНАЛЬНЫЙ ВОПРОС — НЕ ОБЯЗАТЕЛЕН. Заканчивать каждое сообщение встречным вопросом — устойчивый паттерн, который выдаёт бота. Иногда заверши коротким утверждением, предложением следующего шага («покажу на созвоне», «готов прислать ссылку на проект») или вообще точкой. Вопрос — только когда он реально нужен по контексту.
- АНГЛИЙСКИЕ ТЕРМИНЫ В СКОБКАХ: не более ОДНОГО на ответ. Конструкции вроде «RLS (Row-Level Security)», «MoE (Mixture of Experts)», «CRO (conversion rate optimization)» в подряд — академический штамп. Используй термин либо без расшифровки (если в IT он общеизвестен), либо просто по-русски.

СТИЛЬ ПИСЬМА (человечность, обход AI-детекторов):
Ответ читают и живые рекрутёры, и автоматические парсеры/детекторы. Текст должен звучать как реальная переписка инженера, а не как причёсанный GPT-ответ. Внутренние метрики, которые ты должен ломать: низкая перплексия → сделать выше, ровная burstiness → дать ритмический разброс, формальный синтаксис → лёгкая небрежность.
- Варьируй длину предложений. Чередуй короткие (3–6 слов) и средние (12–18 слов). Не пиши подряд два одинаковых по длине.
- Допускай лёгкую разговорность: «коротко», «по сути», «если кратко», «там как раз», «именно это», «по факту», «как раз», «в общем». Без перегиба — это инженер пишет, а не блогер.
- Иногда — риторический вопрос или встречное уточнение к собеседнику. Не в каждом ответе, но как способ ритма.
- Можно начинать предложение с «И», «А», «Но» (там, где это естественно). Можно вводное слово в начале — «Да», «Кстати», «По опыту».
- Не строй симметричные параллельные конструкции («сделал X, реализовал Y, настроил Z»). Лучше — асимметрия: «настроил X — это закрыло Y; параллельно Z доехал позже».
- Не вылизывай. Допускается одно живое сокращение или просторечие на ответ («ок», «норм», «по сути», «по делу»). Но НЕ сленг и НЕ панибратство.

ЗАПРЕЩЕНО ПИСАТЬ (GPT-маркеры, по которым палится машинный текст):
- НИКОГДА не начинай ответ с признания отсутствия опыта. Запрещены формулировки: "прямого опыта … нет", "у меня нет опыта с", "не работал с", "не знаком с". Сразу переходи к смежному опыту из профиля.
- "Готов быстро освоить", "готов изучить", "буду рад освоить", "не составит труда разобраться" — слабые штампы, выдают неуверенность.
- "Уточню детали при созвоне" как ЗАВЕРШАЮЩАЯ фраза-заглушка. Используй ТОЛЬКО если вопрос реально не покрыт профилем (зарплата, дата выхода, релокация) — и тогда верни SKIP.
- Перечисления технологий списком без результата. Если упоминаешь стек — обязательно с конкретным проектом/итогом из профиля.
- Штампы: "буду рад", "с большим интересом", "рассмотрите мою кандидатуру", "хотел бы пройти".
- Канцелярит и «вылизанные» зачины: «В рамках…», «В части…», «Стоит отметить, что…», «Хотелось бы подчеркнуть…», «Важно понимать, что…».
- Триады из трёх синонимов («эффективный, надёжный и масштабируемый», «гибкий, быстрый и удобный») — типичный GPT-почерк.
- Гладкие связки: «более того», «таким образом», «в свою очередь», «не только…, но и…», «как известно».
- Длинные тире, восклицательные знаки в конце предложений, многоточия в роли «загадочной» паузы.
- Эмодзи. Совсем.

ШАБЛОН ОТВЕТА:
1. Сразу — конкретный смежный опыт из профиля с результатом (название проекта, метрика, что именно делал). НЕ упоминай, чего у тебя нет.
2. Свяжи этот опыт с тем, что спрашивает работодатель.
3. Открытый вопрос или предложение следующего шага.

ЕСЛИ ВОПРОС ПРО НЕСКОЛЬКО ДОМЕНОВ/ИНСТРУМЕНТОВ (A или B), А В ПРОФИЛЕ ТОЛЬКО ЧАСТЬ:
- Подробно отвечай про знакомое (A) с конкретикой.
- Про незнакомое (B) НЕ пиши «опыта нет / не работал». Вместо этого выбери одно из двух:
  (а) переведи стрелки: "по [B] специфику разберём на созвоне — там удобнее обсудить детали";
  (б) обоснованно пропусти этот пункт, не упоминая B вообще, и переведи разговор обратно к A через открытый вопрос.

ПРИМЕРЫ:

❌ ПЛОХО (вопрос: «Есть ли опыт с Claude Code?»):
"Прямого опыта с Claude Code нет, но работал с LLM через API. Готов быстро освоить специфику. Уточню при созвоне."

✅ ХОРОШО (тот же вопрос):
"В одном из последних AI-проектов строил унифицированный слой над Claude/OpenAI/Groq с маршрутизацией промптов и tool use в multi-agent пайплайнах — это то же ядро, что и Claude Code: модель + инструменты + контекст. Подойдёт ли вам, если на созвоне покажу конкретный кейс с code-генерирующим агентом?"

✅ ХОРОШО (вопрос про A/B, например «опыт с тревел-продуктами или маркетплейсами?»; в профиле есть маркетплейс-паттерны, но не тревел):
"В крупном госпроекте строил «Единое окно» — multi-tenant платформу с RBAC, real-time нотификациями и сложной бизнес-логикой между ролями: всё это та же основа, что и в маркетплейсе. По тревел-специфике детали удобнее обсудить на созвоне с конкретными кейсами на руках. Какие именно архитектурные вызовы в вашем маркетплейсе сейчас приоритет?"

Заметь: ни «нет опыта», ни «не работал» — про незнакомый домен мягко переводим на созвон.

ПРИМЕРЫ БИНАРНОГО РЕЖИМА (вопрос — фактический/да-нет):

❌ ПЛОХО (вопрос: «Был ли опыт shared memory?»):
"В одном из проектов реализовал inter-process communication через Redis для мультитенантной изоляции и shared state management — кэширование квот, billing, аналитики. Параллельно оптимизировал обработку видеопотоков через асинхронные очереди (RabbitMQ + Celery), что требовало синхронизации доступа к shared inference-ресурсам. Какие конкретные сценарии shared memory критичны в вашей системе?"
(80 слов, два проекта, counter-question — выдаёт бот.)

✅ ХОРОШО (тот же вопрос):
"Да, через multiprocessing.shared_memory — передавал кадры между воркерами в видеоаналитике."
(11 слов, ответ — сразу. Один инструмент, один контекст.)

❌ ПЛОХО (вопрос: «Какие RPS поддерживали?»):
"В одном AI-проекте обрабатывал multi-provider LLM-запросы с маршрутизацией между провайдерами — система спроектирована на масштабируемость через async/await и connection pooling. В self-hosted платформе поддерживал мультитенантные inference-запросы с graceful degradation. Какие целевые RPS в вашем проекте?"

✅ ХОРОШО:
"В пике ~500 RPS на inference-эндпоинт, типично 100–200."
"""


# Стоп-фразы: если они есть в сгенерированном ответе, ответ заблокирован
# (возвращаем SKIP, пусть соискатель ответит руками). Защита от случайного промаха
# модели даже при усиленном промпте — плохой авто-ответ HR-бот пересылает
# работодателю в summary, и его уже не отозвать.
BANNED_REPLY_PATTERNS = (
    "прямого опыта",
    "у меня нет опыта",
    "у меня нет прямого",
    "опыта с этим у меня нет",
    "не работал с",
    "не знаком с",
    "готов быстро освоить",
    "готов освоить",
    "готов изучить",
    "буду рад освоить",
    "не составит труда разобраться",
    # «всё освою / разберусь / смогу курировать» — слабые штампы из цепочки Tech Lead Go/Vue
    "смогу освоить",
    "смогу разобраться",
    "смогу курировать",
    "смогу поддерживать",
    "освою за",
    "разберусь со спецификой",
    "разберусь на практике",
    "освоить на практике",
    "языковой синтаксис — вторично",
    "синтаксис вторичен",
    "принципы универсальны",
)


def reply_violates_rules(reply: str) -> str:
    if not reply:
        return ""
    low = reply.lower()
    for p in BANNED_REPLY_PATTERNS:
        if p in low:
            return p
    return ""


def is_skip_response(text: str) -> bool:
    """SKIP даже с обоснованием/префиксами/markdown — это SKIP, в чат не уходит.
    Клод иногда возвращает «SKIP **Обоснование:** ...» вместо чистого SKIP;
    раньше такой ответ улетал рекрутёру."""
    if not text or not text.strip():
        return True
    head = text.strip().upper().lstrip("*_`").lstrip()
    return head.startswith("SKIP")


# Максимум блоков истории чата, уходящих в промпт. Хвост диалога несёт основной
# контекст; ранние блоки лишь раздувают вход (некэшируемую часть) и стоят токенов.
MAX_HISTORY_BLOCKS = 12


def _truncate_history(chat_history: str, max_blocks: int = MAX_HISTORY_BLOCKS) -> str:
    """Оставляет последние max_blocks блоков истории (блоки разделены пустой строкой).
    Свежие сообщения важнее для ответа; старые режем, чтобы не гнать лишние токены."""
    if not chat_history:
        return chat_history
    blocks = [b for b in chat_history.strip().split("\n\n") if b.strip()]
    if len(blocks) <= max_blocks:
        return chat_history
    kept = blocks[-max_blocks:]
    return "[...более ранние сообщения опущены...]\n\n" + "\n\n".join(kept)


# Явные «ответ не требуется» от работодателя/HH — ловим правилами ДО вызова Claude.
# Консервативный набор: только однозначные авто-уведомления и финальные отказы,
# где генерировать ответ бессмысленно. Раньше то же решение принимал LLM ценой
# полного вызова с профилем и историей — это и есть основная статья skip-издержек.
NO_REPLY_NEEDED_PATTERNS = (
    "ваш отклик просмотрен",
    "резюме доставлено",
    "вакансия закрыта",
    "вакансия снята с публикации",
    "вакансия в архиве",
    "приглашение больше не действительно",
    "к сожалению, мы приняли решение",
    "к сожалению, вынуждены отказать",
    "мы остановились на другом кандидате",
    "мы выбрали другого кандидата",
    "вы не подходите на данную позицию",
    "спасибо за отклик, но",
)


def no_reply_needed(chat_history: str) -> str:
    """Возвращает сработавший паттерн, если последнее ВХОДЯЩЕЕ сообщение —
    авто-уведомление/финальный отказ, на который отвечать не нужно. Иначе ''."""
    last = _last_incoming_block(chat_history)
    if not last:
        return ""
    low = last.lower()
    for p in NO_REPLY_NEEDED_PATTERNS:
        if p in low:
            return p
    return ""


class LLMUnavailable(Exception):
    """Модель не ответила: обход прерываем, чтобы не открывать (и не помечать
    прочитанными) остальные чаты впустую."""


# Событие reply последней генерации — main() пишет его только после реальной
# отправки. Иначе dry-run и неудачная отправка включают cooldown и попадают в
# «наши прошлые ответы».
LAST_REPLY_EVENT: dict | None = None


def generate_reply(chat_history: str, vacancy_title: str = "", chat_url: str = "") -> str:
    """Генерирует ответ через Claude Haiku 4.5 на основе MY_PROFILE и истории чата."""
    global LAST_REPLY_EVENT
    LAST_REPLY_EVENT = None
    # Hard guard: если последний блок history принадлежит нам — отвечать не на что.
    # Это страховка на случай, если парсер уже отметил блок как [Я], либо если
    # текст последнего блока совпадает с нашим прошлым ответом (защита от
    # генерации второго ответа на собственное сообщение).
    if chat_history:
        blocks = [b.strip() for b in chat_history.strip().split("\n\n") if b.strip()]
        if blocks:
            last = blocks[-1]
            last_role = _strip_role_prefix(last)[0].lower()
            if last.startswith(SELF_ROLE_PREFIX) or (
                last_role and any(m in last_role for m in SELF_NAME_MARKERS)
            ):
                print("    ⏭️  Последний блок — от нас, отвечать не на что")
                return "SKIP"
            # Снимаем префикс [Роль] и сверяем содержимое с маркерами «своё»
            content = last
            if content.startswith("["):
                end = content.find("]")
                if end > 0:
                    content = content[end + 1:].strip()
            prev_self_known = tuple(get_last_self_replies(chat_url, n=5))
            if _text_is_own(content, prev_self_known):
                print("    ⏭️  Содержимое последнего блока совпадает с нашим текстом — SKIP")
                return "SKIP"

    # Cooldown: если этот же входящий блок мы уже обрабатывали недавно
    # (reply / robot_questionnaire / предыдущий cooldown-skip), нет смысла дёргать
    # Claude повторно — токены потратятся, рекрутёр получит почти тот же текст.
    if chat_url and REPLY_COOLDOWN_SEC > 0:
        meta = get_last_processed_meta(chat_url)
        if meta:
            prev_kind, prev_ts, prev_tail = meta
            age = time.time() - prev_ts
            if age < REPLY_COOLDOWN_SEC:
                cur_last = _last_incoming_block(chat_history)
                prev_last = _last_incoming_block(prev_tail)
                if cur_last and cur_last == prev_last:
                    print(f"    ⏳ Cooldown {int(age)}s · последний входящий не изменился — SKIP")
                    metrics.log_event(
                        kind="skip",
                        vacancy=vacancy_title,
                        chat_url=chat_url,
                        payload={
                            "reason": "cooldown",
                            "age_sec": int(age),
                            "prev_kind": prev_kind,
                            "history_len": len(chat_history),
                            "history_tail": chat_history[-2000:] if chat_history else "",
                        },
                    )
                    return "SKIP"

    # Дешёвый предфильтр: явные авто-уведомления и финальные отказы — ответа не ждут,
    # незачем тратить полный LLM-вызов (это основная статья наших skip-издержек).
    nr = no_reply_needed(chat_history)
    if nr:
        print(f"    ⏭️  Авто-уведомление/отказ ({nr!r}) — ответ не требуется, SKIP")
        metrics.log_event(
            kind="skip",
            vacancy=vacancy_title,
            chat_url=chat_url,
            payload={
                "reason": "no_reply_needed",
                "trigger": nr,
                "history_len": len(chat_history),
                "history_tail": chat_history[-2000:] if chat_history else "",
            },
        )
        return "SKIP"

    # Робот-анкета: автобот HH присылает серию однотипных вопросов («Был ли опыт…?»),
    # которые уходят работодателю в summary. Любой авто-ответ от Claude тут — риск:
    # неверная связка вскроется на оффере, отозвать нельзя. Кидаем в отдельную
    # очередь и пусть отвечает человек.
    robot_flag, robot_reason = is_robot_questionnaire(chat_history)
    if robot_flag:
        questions: list[str] = []
        last_question = ""
        if chat_history:
            for b in [x.strip() for x in chat_history.strip().split("\n\n") if x.strip()]:
                role, body = _strip_role_prefix(b)
                if role == SELF_ROLE or not body:
                    continue
                body_norm = body.strip()
                # «Здравствуйте» и прочие приветствия — не вопросы анкеты.
                lb = body_norm.lower()
                if len(body_norm.split()) < 3 and not body_norm.endswith("?"):
                    continue
                if any(g in lb for g in ("здравствуйте", "добрый день", "спасибо за интерес", "начнем")):
                    if "?" not in body_norm:
                        continue
                questions.append(body_norm[:500])
            if questions:
                last_question = questions[-1]
        print(
            f"    🤖 Робот-анкета ({robot_reason}) · {len(questions)} вопрос(ов) "
            f"— в отдельную очередь, ответит человек"
        )
        metrics.log_event(
            kind="robot_questionnaire",
            vacancy=vacancy_title,
            chat_url=chat_url,
            payload={
                "reason": robot_reason,
                "last_question": last_question[:600],
                "questions": questions,
                "questions_count": len(questions),
                "history_len": len(chat_history),
                "history_tail": chat_history[-2000:] if chat_history else "",
            },
        )
        # Дедуп нотификаций: если вчера уже шлали тегу о ЭТОЙ анкете — не дублируем.
        # Простой признак — был ли любой robot_questionnaire по этому чату ранее.
        # Полная дедупликация делается на стороне БД (видна в дашборде).
        try:
            with sqlite3.connect(metrics.DB_PATH, timeout=5) as c:
                row = c.execute(
                    """SELECT COUNT(*) FROM events
                       WHERE kind = 'robot_questionnaire' AND chat_url = ?""",
                    (chat_url,),
                ).fetchone()
                prior_count = int(row[0] if row else 0)
        except Exception:
            prior_count = 0
        # На первое срабатывание шлём тегу, на повторы для того же чата — нет
        # (по сути это уведомление «появилась новая анкета», а не «новый вопрос
        # в существующей»). Если в payload.questions выросло количество — это
        # видно в дашборде по обновлённой свежей записи.
        if prior_count <= 1:
            notify.notify_robot_questionnaire(
                reason=robot_reason,
                vacancy=vacancy_title,
                chat_url=chat_url,
                questions=questions,
                last_question=last_question,
            )
        return "SKIP"

    overused = get_overused_projects_in_chat(chat_url)
    antirepeat_block = ""
    if overused:
        names = ", ".join(overused)
        antirepeat_block = (
            "\n\nВАЖНО — АНТИПОВТОР В ЭТОМ ЧАТЕ:\n"
            f"В прошлых твоих ответах в этом же диалоге уже неоднократно упоминались проекты: {names}.\n"
            "В этом ответе НЕ упоминай их по имени снова. Используй:\n"
            "  • другой проект из профиля, если он подходит к вопросу;\n"
            "  • либо обобщённую формулировку: «в одном из частных проектов (под NDA)», "
            "«в коммерческом проекте, который не могу назвать», «в недавнем pet-проекте», "
            "«в одной из прошлых ролей».\n"
            "Цель — не звучать как заевшая пластинка."
        )
        print(f"    🔁 Антиповтор: уже было {names}")

    # Свои последние ответы в этом чате — чтобы не повторять ту же мысль (как в цепочках 7/9)
    prev_self = get_last_self_replies(chat_url, n=3)
    self_history_block = ""
    if prev_self:
        numbered = "\n\n".join(f"[{i}] {t}" for i, t in enumerate(prev_self, 1))
        self_history_block = (
            "\n\nТВОИ ПРЕДЫДУЩИЕ ОТВЕТЫ В ЭТОМ ЖЕ ЧАТЕ (новейший — [1]):\n"
            f"{numbered}\n"
            "ВАЖНО: не повторяй ту же мысль, ту же структуру и те же примеры из этих ответов. "
            "Если работодатель задал вопрос, на который ты уже отвечал ранее — кратко сошлись на сказанное "
            "(«как писал выше — …») и добавь НОВЫЙ ракурс или деталь, которой ещё не было. "
            "Если в этих ответах ты уже закончил вопросом 2 раза подряд — в этот раз заверши утверждением "
            "или предложением следующего шага, без встречного вопроса."
        )
        print(f"    📜 В контексте {len(prev_self)} прошлых ответов бота")

    user_msg = f"""ВАКАНСИЯ: {vacancy_title or "не указано"}

ИСТОРИЯ ЧАТА (последние сообщения, сверху старые, снизу новые):
{_truncate_history(chat_history)}{antirepeat_block}{self_history_block}

Сформулируй ответ соискателя на последнее сообщение от работодателя. Если отвечать не нужно, верни SKIP."""

    try:
        # OpenRouter (Gemma 4 31B) — OpenAI-совместимый API
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            max_tokens=500,
            temperature=0.5,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + "\n\nПРОФИЛЬ:\n" + MY_PROFILE},
                {"role": "user", "content": user_msg},
            ],
        )
        text = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tin = getattr(usage, "prompt_tokens", 0) if usage else 0
        tout = getattr(usage, "completion_tokens", 0) if usage else 0

        history_snippet = chat_history[-2000:] if chat_history else ""

        violation = reply_violates_rules(text)
        if violation:
            print(f"    🛑  Стоп-фраза в ответе ({violation!r}), возвращаю SKIP — отправит человек")
            metrics.log_event(
                kind="skip",
                model=OPENROUTER_MODEL,
                tokens_in=tin,
                tokens_out=tout,
                vacancy=vacancy_title,
                chat_url=chat_url,
                payload={
                    "reply": "SKIP",
                    "reason": "banned_phrase",
                    "trigger": violation,
                    "blocked_text": text,
                    "history_len": len(chat_history),
                    "history_tail": history_snippet,
                },
            )
            return "SKIP"

        skip = is_skip_response(text)
        event = dict(
            kind="skip" if skip else "reply",
            model=OPENROUTER_MODEL,
            tokens_in=tin,
            tokens_out=tout,
            vacancy=vacancy_title,
            chat_url=chat_url,
            payload={
                "reply": text,
                "history_len": len(chat_history),
                "history_tail": history_snippet,
            },
        )
        if skip:
            metrics.log_event(**event)
            return "SKIP"
        LAST_REPLY_EVENT = event
        return text
    except Exception as e:
        print(f"  ⚠️  Ошибка генерации ответа: {e}")
        # Сеть/таймаут (нет status_code), ключ, лимиты, перегрузка — модель недоступна
        # целиком. 400 и прочие 4xx — проблема конкретного запроса, обход не рвём.
        status = getattr(e, "status_code", None)
        retryable = status is None or status in (401, 403, 429) or status >= 500
        metrics.log_event(
            kind="error",
            model=OPENROUTER_MODEL,
            vacancy=vacancy_title,
            chat_url=chat_url,
            payload={"error": str(e)[:500], "where": "generate_reply", "retryable": retryable},
        )
        if retryable:
            raise LLMUnavailable(str(e)) from e
        return "SKIP"


def find_unread_chats(page) -> list:
    """Возвращает список ссылок на непрочитанные диалоги.

    HH-чат отдаёт элементы вида data-qa="chatik-open-chat-<id>".
    Фильтр непрочитанных, чекбокс data-qa="chatik-checkbox-only-unread".
    """
    last_err = None
    for attempt in range(3):
        try:
            page.goto(CHATS_URL, wait_until="commit", timeout=30000)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"  ⚠️  goto({CHATS_URL}) попытка {attempt+1}/3: {str(e)[:120]}")
            page.wait_for_timeout(2000)
    if last_err:
        raise last_err
    try:
        page.wait_for_selector("[data-qa^='chatik-open-chat-'], [data-qa='chatik-checkbox-only-unread']", timeout=60000)
    except PlaywrightTimeoutError:
        print("  ⚠️  Список чатов не отрисовался за 60с, пробую дальше")
    page.wait_for_timeout(800)

    # Включаем фильтр «только непрочитанные» через клик по label (Magritte UI
    # не реагирует на page.check()). HH_ALL_CHATS=1 — для диагностики, без фильтра.
    if os.getenv("HH_ALL_CHATS") == "1":
        print("  🔘 HH_ALL_CHATS=1, фильтр непрочитанных не включаю")
    else:
        try:
            r = page.evaluate("""
                () => {
                    const sel = '[data-qa="chatik-checkbox-only-unread"]';
                    const input = document.querySelector(sel);
                    if (!input) return {state: 'no-checkbox'};
                    if (input.checked) return {state: 'already-on'};
                    (input.closest('label') || input.parentElement || input).click();
                    return {state: 'clicked'};
                }
            """)
            state = r.get("state")
            if state == "no-checkbox":
                print("  ⚠️  Чекбокс не найден, обходить НЕ буду")
                return []
            if state == "already-on":
                print("  🔘 Фильтр непрочитанных: уже был включён")
            else:
                try:
                    page.wait_for_function(
                        "() => !!document.querySelector('[data-qa=\"chatik-checkbox-only-unread\"]')?.checked",
                        timeout=4000,
                    )
                    print("  🔘 Фильтр непрочитанных: включён")
                except PlaywrightTimeoutError:
                    print("  ⚠️  Фильтр непрочитанных не включился за 4с, обходить НЕ буду")
                    return []
            page.wait_for_timeout(600)
        except Exception as e:
            print(f"  ⚠️  Сбой при проверке фильтра: {e}, обходить НЕ буду")
            return []

    # Метаданные всех карточек одним round-trip'ом, чтобы не плодить CDP-вызовы
    # на каждую карточку (раньше: 30 карточек × 2 вызова × ~50мс ≈ 3 сек оверхеда).
    cards = page.evaluate(f"""
        () => Array.from(document.querySelectorAll('[data-qa^="chatik-open-chat-"]'))
            .slice(0, {MAX_CHATS_PER_RUN})
            .map(el => ({{
                qa: el.dataset.qa,
                text: el.innerText,
                hasDelivered: !!el.querySelector('[data-qa="status-icon-delivered"]'),
            }}))
    """)
    refs = []
    skipped_own = 0
    skipped_rejected = 0
    for c in cards:
        chat_id = (c.get("qa") or "").replace("chatik-open-chat-", "").strip()
        if not chat_id.isdigit():
            continue
        lines = [l.strip() for l in (c.get("text") or "").split("\n") if l.strip()]
        title = lines[0][:60] if lines else ""
        preview_last = lines[-1].lower() if lines else ""
        if c.get("hasDelivered") or preview_last == "отклик на вакансию":
            skipped_own += 1
            continue
        if preview_last == "отказ":
            skipped_rejected += 1
            continue
        refs.append((f"https://hh.ru/chat/{chat_id}?hhtmFrom=app", title))
    if skipped_own:
        print(f"  ⏭️  Без ответа работодателя (отсеяно по карточке): {skipped_own}")
    if skipped_rejected:
        print(f"  🚫 Отказы (отсеяно по карточке): {skipped_rejected}")
    return refs


SELF_ROLE = "Я"
SELF_ROLE_PREFIX = f"[{SELF_ROLE}]"
# Варианты написания ФИО владельца резюме — для определения авторства сообщения
# в чате, когда HH не отдаёт явный author. Берём из profile.py, чтобы реальные
# ФИО не попадали в публичный код. Если переменная не задана — fallback на пусто
# (тогда сторона определяется только по другим эвристикам).
try:
    _PROFILE = profile_store.load()
except Exception:
    _PROFILE = {}
SELF_NAME_MARKERS: tuple[str, ...] = tuple(s.lower() for s in _PROFILE.get("self_name_markers", ()))

# Длинные строки подписи (≥10 символов) из LETTER_SIGNATURE — характерные
# фрагменты, по которым легко идентифицировать собственное письмо. HH часто
# рендерит подпись отклика отдельным сообщением в чате с пустым author —
# парсер по геометрии может ошибочно отнести её к работодателю.
try:
    SELF_SIGNATURE_LINES = tuple(
        l.strip().lower() for l in (_PROFILE.get("letter_signature") or "").splitlines()
        if l.strip() and len(l.strip()) >= 10
    )
except Exception:
    SELF_SIGNATURE_LINES: tuple[str, ...] = ()

# Дополнительные текстовые маркеры собственных ручных сообщений в чате —
# фразы, которые вы реально пишете рекрутёрам (отказы, вежливые формулы,
# характерные обороты). Сравнение идёт по подстроке, case-insensitive.
# Это последняя линия защиты на случай, если HH рендерит без CSS-маркера
# и без author-поля, а геометрия даёт ложный 'left'. Пример содержимого
# OWN_TEXT_MARKERS в profile.py:
#   OWN_TEXT_MARKERS = (
#       "вынужден вам отказать", "извините, не подойдёт",
#       "с уважением, иван петров", "благодарю за предложение",
#   )
OWN_TEXT_MARKERS: tuple[str, ...] = tuple(
    s.strip().lower() for s in _PROFILE.get("own_text_markers", ()) if s and s.strip()
)


def _text_is_own(text: str, prev_self_replies: tuple[str, ...] = ()) -> bool:
    """Эвристика «этот текст — наш».

    Срабатывает если:
      • в тексте есть характерная строка из LETTER_SIGNATURE (подпись отклика),
      • в тексте упоминается одно из SELF_NAME_MARKERS (наше ФИО),
      • первые 60 символов совпадают с одним из наших УЖЕ ОТПРАВЛЕННЫХ ответов
        в этом же чате (передаётся через prev_self_replies),
      • в тексте встречается одна из подстрок из OWN_TEXT_MARKERS — это
        опциональные пользовательские маркеры для ручных сообщений в чате
        (отказы, прощальные формулы), задаются в profile.py.
    """
    if not text:
        return False
    tl = text.lower()
    if any(l in tl for l in SELF_SIGNATURE_LINES):
        return True
    if any(m in tl for m in SELF_NAME_MARKERS):
        return True
    if any(m in tl for m in OWN_TEXT_MARKERS):
        return True
    for prev in prev_self_replies:
        # текст чата приходит со схлопнутыми абзацами (get_chat_payload) — так же и здесь
        head = re.sub(r"\n\s*\n+", "\n", (prev or "").strip())[:60].lower()
        if head and head in tl:
            return True
    return False

REJECT_MARKERS = (
    "отказ", "к сожалению", "не подходит", "не подошл",
    "вакансия закр", "выбрали другого", "не готовы рассм",
    "не сможем рассм", "не смогли рассм", "не рассматрива",
    "отклика отклон", "ваш отклик отклон", "ваша кандидатура отклон",
    "не подойдёт", "не подойдет",
    "двигаться дальше с кандид", "решили двигаться дальше",
    "продолжим поиск", "продолжаем поиск",
    "остановили выбор", "сделали выбор в пользу",
    "более подходящ", "нашли подходящ", "выбрали кандидат",
    "приняли в работу другого", "уже выбрали",
    "вернёмся к вам", "вернемся к вам",
    "вернёмся с обратной", "вернемся с обратной",
    "свяжемся с вами позже", "свяжемся позже",
    "ответим вам позже", "ответим позже",
    "сообщим вам позже", "сообщим позже",
    "напишем позже", "напишем вам позже",
    "дадим знать позже", "дадим обратную связь позже",
    "позже сообщ", "позже напиш", "позже свяж", "позже ответ", "позже проинформ",
    "сообщит о своем решении", "сообщит о своём решении",
    "сообщит вам о своем", "сообщит вам о своём",
    "сообщим о решении", "сообщим о своем решении", "сообщим о своём решении",
    "о своем решении", "о своём решении",
    "примем решение позже", "решение примем позже",
)


# Положительные сигналы: что-то, на что соискатель хочет сам решить, что отвечать.
# Делим на типы, чтобы потом сортировать/фильтровать в дашборде.
#   interview        — приглашение на интервью/созвон, обсуждение времени.
#   contact_request  — просят написать в ТГ/WA, оставить телефон, перезвонить.
#   under_review     — кандидатура передана на рассмотрение / понравилась.
POSITIVE_MARKERS: dict[str, tuple[str, ...]] = {
    "interview": (
        "приглашаем на интервью", "приглашаем на собесед", "приглашаем на созвон",
        "приглашаю на интервью", "приглашаю на собесед", "приглашаю на созвон",
        "хотим пригласить", "хотели бы пригласить",
        "готовы пригласить", "пригласить вас на",
        "удобно созвон", "удобно для созвон", "удобно созвониться",
        "когда удобно созв", "когда вам удобно", "когда вам было бы удобно",
        "когда сможете", "когда будет удобно",
        "назначим интервью", "назначим встречу", "назначим созвон",
        "записать на интервью", "записать на собесед",
        "интервью с тимлид", "собесед с тимлид",
        "техническое интервью", "техсобес", "тех. собес",
        "финальный этап", "финальное интервью",
        "следующий этап", "следующего этапа",
        "первичное интервью", "screening call", "knowledge check",
    ),
    "contact_request": (
        "напишите мне в", "напишите в телеграм", "напишите в тг",
        "напиши в телеграм", "напиши в тг", "напиши мне в тг",
        "пишите в телеграм", "пишите в тг", "телеграм: @", "тг: @", "tg: @",
        "мой телеграм", "мой тг ", "мой ник в тг", "мой ник в телеграм",
        "напишите на почту", "пишите на почту",
        "оставьте телефон", "оставьте ваш телефон", "ваш номер телефон",
        "перезвоним", "перезвоните", "позвоните мне", "позвоните нам",
        "созвонимся", "давайте созвон", "наберу вас", "наберём вас",
        "+7 9", "+79", "whatsapp", "ватсап", "вотсап",
    ),
    "under_review": (
        "передал команд", "передала команд", "передали команд",
        "передал руководител", "передала руководител", "передали руководител",
        "передал нанимающ", "передала нанимающ", "передали нанимающ",
        "передал заказчик", "передала заказчик", "передали заказчик",
        "на рассмотрен", "находится на рассмотрен",
        "ваш отклик прошёл", "ваш отклик прошел",
        "понравилось ваше резюме", "ваше резюме понравил",
        "заинтересовала ваша кандидатур", "интересна ваша кандидатур",
        "продолжаем рассматр", "продолжим рассматр",
        "хотим обсудить", "хотели бы обсудить", "давайте обсудим",
        "обсудим детали", "обсудить детали",
        "ваш отклик заинтересов", "отклик заинтересовал",
    ),
}


def detect_positive_signal(history: str) -> tuple[str, str] | None:
    """Возвращает (signal_type, trigger_phrase) если последнее сообщение от
    работодателя похоже на приглашение/запрос контакта/положительный отзыв.

    Анализируем только ПОСЛЕДНЕЕ входящее сообщение (после своих сообщений не смотрим),
    чтобы не подсветить старое приглашение, на которое уже ответили.
    """
    if not history:
        return None
    blocks = [b.strip() for b in history.strip().split("\n\n") if b.strip()]
    if not blocks:
        return None
    last = blocks[-1]
    if last.startswith(SELF_ROLE_PREFIX):
        return None
    low = last.lower()
    for sig_type, markers in POSITIVE_MARKERS.items():
        for m in markers:
            if m in low:
                return sig_type, m
    return None


HARD_REJECT_MARKERS = (
    "отказ", "выбрали другого", "выбрали кандидат", "не готовы рассм",
    "не сможем рассм", "не смогли рассм", "отклон", "вынуждены отказать",
    "остановили выбор", "сделали выбор в пользу", "решили двигаться дальше",
    "двигаться дальше с кандид", "приняли в работу другого", "уже выбрали",
    "не готовы пригласить", "не можем пригласить",
)


def _last_block_if_incoming(history: str) -> str:
    blocks = [b.strip() for b in (history or "").strip().split("\n\n") if b.strip()]
    if not blocks or blocks[-1].startswith(SELF_ROLE_PREFIX):
        return ""
    return blocks[-1].lower()


def is_hard_rejection(history: str) -> bool:
    """Однозначный отказ — его не перебивает маркер приглашения в том же сообщении."""
    last = _last_block_if_incoming(history)
    return bool(last) and any(m in last for m in HARD_REJECT_MARKERS)


def is_rejection(history: str) -> bool:
    if not history:
        return False
    blocks = [b.strip() for b in history.strip().split("\n\n") if b.strip()]
    if not blocks:
        return False
    last = blocks[-1]
    if last.startswith(SELF_ROLE_PREFIX):
        return False
    return any(m in last.lower() for m in REJECT_MARKERS)


# Маркеры автора, по которым диалог считается анкетой автобота HH/работодателя.
# Сравнение по подстроке, case-insensitive — так что «Робот-рекрутёр», «HR-бот»,
# «Recruiting bot», «Чат-бот компании X» отлавливаются одинаково.
ROBOT_AUTHOR_MARKERS = (
    "робот", "чат-бот", "чатбот", "автоотклик", "автоответ",
    "hr-бот", "hr бот", "bot", "auto-recruiter", "recruiter bot",
)

# Шаблонные зачины вопросов из анкет автоботов. Если ≥2 последних входящих
# сообщения подряд начинаются с такого зачина — это анкета, даже если автор
# не помечен как «робот» (HH иногда отдаёт пустого author у системных шаблонов).
QUESTIONNAIRE_LEAD_PATTERNS = (
    "был ли у вас", "были ли у вас", "был ли опыт", "есть ли у вас",
    "есть ли опыт", "имеется ли", "имеете ли", "знакомы ли вы",
    "владеете ли вы", "приходилось ли", "доводилось ли",
    "какой у вас опыт", "какие у вас", "какие именно",
    "сколько лет опыта", "сколько у вас опыта", "сколько вам лет",
    "укажите ваш", "укажите вашу", "оцените свой", "оцените ваш",
    "уровень владения", "уровень знания",
)


def _strip_role_prefix(block: str) -> tuple[str, str]:
    """Возвращает (role, body) из блока истории '[Роль]\\nтекст…'."""
    b = block.strip()
    if b.startswith("["):
        end = b.find("]")
        if end > 0:
            return b[1:end].strip(), b[end + 1:].strip()
    return "", b


def _looks_like_questionnaire_question(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t.split()) > 40:
        return False
    tl = t.lower()
    return any(p in tl for p in QUESTIONNAIRE_LEAD_PATTERNS)


def is_robot_questionnaire(history: str) -> tuple[bool, str]:
    """Определяет, что последнее входящее сообщение — это вопрос из анкеты автобота.

    Возвращает (флаг, причина). Причина — короткая метка для логов и payload:
      'author_marker:<author>' — последний входящий помечен ролью с маркером робота,
      'pattern:series'         — последние ≥2 входящих подряд — шаблонные вопросы анкеты.
    """
    if not history:
        return False, ""
    blocks = [b.strip() for b in history.strip().split("\n\n") if b.strip()]
    if not blocks:
        return False, ""

    incoming: list[tuple[str, str]] = []
    for b in blocks:
        role, body = _strip_role_prefix(b)
        if role == SELF_ROLE:
            continue
        incoming.append((role, body))
    if not incoming:
        return False, ""

    last_role, _ = incoming[-1]
    rl = last_role.lower()
    if rl and any(m in rl for m in ROBOT_AUTHOR_MARKERS):
        return True, f"author_marker:{last_role[:40]}"

    # Pattern fallback: автор не помечен, но последние два входящих сообщения
    # подряд — шаблонные вопросы анкеты. Реальный HR редко делает так подряд.
    tail = incoming[-2:]
    if len(tail) == 2 and all(_looks_like_questionnaire_question(b) for _, b in tail):
        return True, "pattern:series"
    return False, ""


def _detect_role(
    author: str,
    has_author_field: bool,
    side: str = "",
    text: str = "",
    prev_self_replies: tuple[str, ...] = (),
    own_by_class: bool | None = None,
) -> str:
    """Определяет роль отправителя.

    Порядок приоритетов сигналов (от самого надёжного к самому шаткому):
      1. own_by_class — CSS-класс пузыря/родителя (outgoing / own / --mine /
         self-message) однозначно говорит, чьё сообщение. Самый надёжный
         сигнал, потому что HH/Magritte рендерит исходящие иначе, чем входящие.
      2. _text_is_own — content-override: подпись LETTER_SIGNATURE, ФИО из
         SELF_NAME_MARKERS, совпадение с прошлыми ответами бота в этом чате.
      3. side — геометрия пузыря относительно центра размаха.
      4. author + SELF_NAME_MARKERS — fallback по тексту author-поля.

    Раньше использовались только 2–4: у ручных коротких сообщений
    пользователя через UI HH ни один не срабатывал (нет подписи, нет ФИО,
    геометрия могла дать ложный 'left' при узком контейнере, author —
    имя собеседника). В итоге сообщение типа «Извините, отказываюсь»
    приписывалось работодателю и ловилось is_rejection как отказ нам.
    """
    # 1. CSS-класс — наиболее надёжный признак.
    if own_by_class is True:
        return SELF_ROLE
    if own_by_class is False:
        a0 = (author or "").strip()
        if has_author_field and a0 and not any(m in a0.lower() for m in SELF_NAME_MARKERS):
            return a0
        return "Работодатель"

    # 2. Content-override.
    if text and _text_is_own(text, prev_self_replies):
        return SELF_ROLE

    # 3. Геометрия.
    a = (author or "").strip()
    if side == "right":
        return SELF_ROLE
    if side == "left":
        if has_author_field and a and not any(m in a.lower() for m in SELF_NAME_MARKERS):
            return a
        return "Работодатель"

    # 4. Fallback по author.
    if not has_author_field or not a:
        return SELF_ROLE
    if any(m in a.lower() for m in SELF_NAME_MARKERS):
        return SELF_ROLE
    return a


def get_chat_payload(page, chat_url: str, fallback_title: str = "") -> dict:
    """Открывает диалог и собирает историю + название вакансии.

    Разметка чата HH (Magritte):
      - название вакансии: [data-qa="chatik-header-vacancy-link"]
      - блок сообщения:    [data-qa^="chatik-chat-message-<id>"]
      - текст внутри:      [data-qa="chat-bubble-text"]
      - автор:             [data-qa="chat-bubble-author-name"]
      - время:             [data-qa="chat-buble-display-time"]
    """
    page.goto(chat_url, wait_until="commit", timeout=30000)
    try:
        page.wait_for_selector("[data-qa^='chatik-chat-message-'], [data-qa='chatik-header-vacancy-link']", timeout=60000)
    except PlaywrightTimeoutError:
        print("    ⚠️  Сообщения не подгрузились за 60с")
    page.wait_for_timeout(600)

    # Название вакансии: HH в шапке открытого чата отдаёт только текст кнопки
    # «Перейти», без самого названия. Поэтому используем заголовок, который мы
    # уже собрали при обходе списка чатов (find_unread_chats).
    vacancy_title = (fallback_title or "").strip()

    # Собираем все сообщения одним JS-вызовом: id, текст, автор, флаг own
    # по CSS-классу (наиболее надёжный сигнал) и сторона пузырька (right=исходящее,
    # left=входящее) — считаем mid не по родителю (он бывает шире окна и сбивает
    # геометрию), а по реальному размаху самих пузырей: середина между min(left)
    # и max(right). Если размах слишком мал — геометрия ненадёжна, side=''.
    raw = page.evaluate("""
        () => {
            const blocks = Array.from(document.querySelectorAll('[data-qa^="chatik-chat-message-"]'))
                .filter(el => {
                    const qa = el.dataset.qa || '';
                    return !/-text$|-menu$/.test(qa);
                });
            if (!blocks.length) return [];

            // Собираем геометрию пузырей и вычисляем середину по их размаху.
            const rects = blocks.map(el => el.getBoundingClientRect()).filter(r => r.width > 0);
            let minLeft = Infinity, maxRight = -Infinity;
            for (const r of rects) {
                if (r.left < minLeft) minLeft = r.left;
                if (r.right > maxRight) maxRight = r.right;
            }
            const span = maxRight - minLeft;
            const mid = span > 120 ? (minLeft + maxRight) / 2 : null;

            // Регэкспы по className/data-атрибутам для определения «своих» пузырей.
            // HH/Magritte рендерит выходящие сообщения с маркерами в имени класса
            // или родителя — пробуем поймать самые распространённые шаблоны.
            const OWN_RE = /(outgoing|own(?![a-z])|--mine|sent--own|sender--self|bubble--right|message--right|chat-message_owner|self-message)/i;
            const THEIRS_RE = /(incoming|--theirs|sender--other|message--left|bubble--left)/i;

            return blocks.map(el => {
                const qa = el.dataset.qa;
                const id = qa.replace('chatik-chat-message-','');
                const textEl = el.querySelector('[data-qa="chat-bubble-text"]');
                let text = (textEl && textEl.innerText || '').trim();
                if (!text) text = (el.innerText || '').trim();
                const authorEl = el.querySelector('[data-qa="chat-bubble-author-name"]');
                const author = authorEl ? (authorEl.innerText || '').trim() : '';

                // Объединяем классы самого блока и до 3-х предков — маркер часто на родителе.
                let cls = el.className || '';
                let p = el.parentElement;
                for (let i=0; i<3 && p; i++) {
                    cls += ' ' + (p.className || '');
                    p = p.parentElement;
                }
                let ownByClass = null;
                if (OWN_RE.test(cls)) ownByClass = true;
                else if (THEIRS_RE.test(cls)) ownByClass = false;

                const r = el.getBoundingClientRect();
                const center = r.left + r.width/2;
                let side = '';
                if (mid !== null && r.width > 0) side = center > mid ? 'right' : 'left';
                return {id, text, author, hasAuthor: !!authorEl, side, ownByClass};
            });
        }
    """) or []

    # Известные собственные отправки в ЭТОМ чате — content-override в парсере
    # снимает ошибочную маркировку «[Работодатель]» с наших же сообщений.
    prev_self = tuple(get_last_self_replies(chat_url, n=20))

    seen_ids = set()
    items = []  # (msg_id_int, role, text)
    for m in raw:
        msg_id = (m.get("id") or "").strip()
        if not msg_id.isdigit() or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        # Абзацы внутри сообщения схлопываем: история режется на блоки по "\n\n",
        # и иначе хвостовые абзацы теряют префикс [Роль] (анкеты, guard «наш блок»)
        text = re.sub(r"\n\s*\n+", "\n", (m.get("text") or "").strip())
        if not text:
            continue
        own = m.get("ownByClass")
        role = _detect_role(
            m.get("author") or "",
            bool(m.get("hasAuthor")),
            m.get("side") or "",
            text=text,
            prev_self_replies=prev_self,
            own_by_class=(own if isinstance(own, bool) else None),
        )
        items.append((int(msg_id), role, text))

    # Сортируем по msg_id (чем больше, тем позже), берём последние 15.
    items.sort(key=lambda x: x[0])
    items = items[-15:]
    messages = [f"[{role}] {text}" for _, role, text in items]
    history = "\n\n".join(messages)
    return {"title": vacancy_title, "history": history}


def post_reply(page, text: str) -> bool:
    """Вводит ответ и нажимает отправить."""
    text_sel = [
        "textarea[data-qa='chatik-new-message-text']",
        "div[contenteditable='true'][data-qa='chatik-input']",
        "textarea",
        "div[contenteditable='true']",
    ]
    field = None
    for sel in text_sel:
        loc = page.locator(sel).first
        if loc.count() > 0 and loc.is_visible():
            field = loc
            break
    if not field:
        print("    ⚠️  Поле ввода не найдено")
        return False

    try:
        field.click()
        field.fill(text) if "textarea" in str(field) else field.type(text, delay=15)
    except Exception:
        try:
            field.type(text, delay=15)
        except Exception as e:
            print(f"    ⚠️  Не получилось ввести текст: {e}")
            return False

    page.wait_for_timeout(800)

    send_sel = [
        "[data-qa='chatik-do-send-message']",
        "button[data-qa='chatik-send-button']",
        "button:has-text('Отправить')",
    ]
    for sel in send_sel:
        btn = page.locator(sel).first
        if btn.count() > 0 and btn.is_enabled():
            btn.click()
            page.wait_for_timeout(2000)
            return True
    print("    ⚠️  Кнопка отправки не найдена")
    return False


_log_fh = None


def main():
    global _log_fh
    # Создаём PID-файл для дашборда
    from pathlib import Path
    pid_file = Path("/tmp/hh_auto_reply.pid")
    pid_file.write_text(str(os.getpid()))

    # Лог открываем один раз на процесс: watch-итерации дописывают, а не затирают
    if _log_fh is None:
        _log_fh = open(Path("/tmp/hh_auto_reply.log"), "w", buffering=1)
        sys.stdout = _log_fh
        sys.stderr = _log_fh

    print("\n" + "=" * 50)
    print("💬 HH.ru Авто-ответчик")
    print("=" * 50)

    if OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        print("\n❌ Ошибка, укажи OPENROUTER_API_KEY в .env или auto_apply_template.py")
        print("   Получи на https://openrouter.ai/keys")
        return
    if not os.path.exists(SESSION_FILE):
        print("\n❌ Сессия не найдена, сначала запусти python3 hh_login.py")
        return
    if DRY_RUN:
        print("\n🧪 DRY-RUN, ответы будут сгенерированы, но не отправлены\n")

    stats = {"replied": 0, "skipped": 0, "errors": 0}

    headless = os.getenv("HH_HEADLESS", "").lower() in ("1", "true", "yes")

    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        print("\n🔍 Открываю список чатов...")
        from auto_apply_template import SESSION_EXPIRED, hh_logged_in
        if hh_logged_in(page) is False:
            print(f"\n🛑 {SESSION_EXPIRED}")
            metrics.log_event(kind="error", payload={"where": "reply_run", "error": SESSION_EXPIRED})
            browser.close()
            return
        chats = find_unread_chats(page)
        print(f"📨 Непрочитанных чатов: {len(chats)}\n")
        seen = {u for u, _ in chats}
        retry = [(u, t) for u, t in get_chats_to_retry() if u not in seen]
        if retry:
            print(f"🔁 Повтор чатов без ответа (ошибка/не отправлено): {len(retry)}\n")
            chats += retry

        for i, (chat_url, preview) in enumerate(chats, 1):
            print(f"[{i}/{len(chats)}] {preview or chat_url}")
            try:
                payload = get_chat_payload(page, chat_url, fallback_title=preview)
                if not payload["history"]:
                    print("    ⏭️  История пустая, пропускаю")
                    stats["skipped"] += 1
                    continue

                print(f"    📋 Вакансия: {payload['title'][:60] or '?'}")
                print(f"    💬 Сообщений: {payload['history'].count('[')}")

                if DRY_RUN:
                    blocks = [b for b in payload["history"].split("\n\n") if b.strip()]
                    role_counts = {}
                    for b in blocks:
                        if b.startswith("[") and "]" in b:
                            r = b[1:b.index("]")]
                            role_counts[r] = role_counts.get(r, 0) + 1
                    print(f"    🔬 Роли: {role_counts}")
                    print("    ── история ──")
                    for b in blocks:
                        print(f"    | {b[:300]}")
                    print("    ── /история ──")

                # Явный отказ («выбрали другого») сильнее любых маркеров приглашения;
                # конкретное приглашение/запрос контакта сильнее мягких «к сожалению»,
                # «не подходит» («если время не подходит — напишите»).
                positive = detect_positive_signal(payload["history"])
                if positive and (
                    is_hard_rejection(payload["history"])
                    or (positive[0] == "under_review" and is_rejection(payload["history"]))
                ):
                    positive = None
                if positive:
                    sig_type, trigger = positive
                    print(f"    🎯 Положительный сигнал ({sig_type}: {trigger!r}) — кладу в очередь, не отвечаю")
                    metrics.log_event(
                        kind="positive_signal",
                        vacancy=payload["title"],
                        chat_url=chat_url,
                        payload={
                            "signal_type": sig_type,
                            "trigger": trigger,
                            "history_len": len(payload["history"]),
                            "history_tail": payload["history"][-2000:],
                        },
                    )
                    # Последний блок от работодателя — для сниппета в нотификации.
                    last_hr = ""
                    for b in reversed(
                        [x.strip() for x in payload["history"].split("\n\n") if x.strip()]
                    ):
                        if not b.startswith(SELF_ROLE_PREFIX):
                            _, last_hr = _strip_role_prefix(b)
                            break
                    notify.notify_positive_signal(
                        signal_type=sig_type,
                        trigger=trigger,
                        vacancy=payload["title"],
                        chat_url=chat_url,
                        last_message=last_hr,
                    )
                    stats["skipped"] += 1
                    continue

                if is_rejection(payload["history"]):
                    print("    🚫 Отказ работодателя, не отвечаю")
                    metrics.log_event(
                        kind="rejection",
                        vacancy=payload["title"],
                        chat_url=chat_url,
                        payload={
                            "history_len": len(payload["history"]),
                            "history_tail": payload["history"][-2000:],
                        },
                    )
                    stats["skipped"] += 1
                    continue

                reply = generate_reply(payload["history"], payload["title"], chat_url=chat_url)

                if is_skip_response(reply) or len(reply) < 5:
                    print("    ⏭️  Claude вернул SKIP, пропускаю")
                    stats["skipped"] += 1
                    continue

                print(f"    📝 Ответ: {reply[:100]}...")

                event = LAST_REPLY_EVENT
                if DRY_RUN:
                    print("    🧪 DRY-RUN, не отправляю")
                    stats["replied"] += 1
                    sent, unsent_reason = False, "dry_run"
                else:
                    try:
                        sent = post_reply(page, reply)
                    except Exception as e:
                        print(f"    ⚠️  Сбой отправки: {e}")
                        sent = False
                    unsent_reason = "" if sent else "send_failed"
                    if sent:
                        print("    ✅ Отправлено")
                        stats["replied"] += 1
                    else:
                        print("    ❌ Не удалось отправить")
                        stats["errors"] += 1
                if event:
                    if not sent:
                        # токены учтены, но cooldown и «наши ответы» не трогаем
                        event["kind"] = "reply_unsent"
                        event["payload"]["reason"] = unsent_reason
                    metrics.log_event(**event)

                d = _chat_delay()
                print(f"    ⏳ Пауза {d:.0f} сек...")
                time.sleep(d)
            except LLMUnavailable as e:
                print(f"    ❌ Модель недоступна ({str(e)[:120]}) — прерываю обход, чат повторю в следующий раз")
                stats["errors"] += 1
                break
            except Exception as e:
                print(f"    ❌ Ошибка: {e}")
                stats["errors"] += 1

        browser.close()

    print("\n" + "=" * 50)
    print("📊 ИТОГИ:")
    print(f"   ✅ Отвечено: {stats['replied']}")
    print(f"   ⏭️  Пропущено: {stats['skipped']}")
    print(f"   ❌ Ошибок: {stats['errors']}")
    print("=" * 50 + "\n")


def watch_loop() -> None:
    print(f"👀 WATCH-режим, опрос каждые {WATCH_INTERVAL} сек. Ctrl-C, чтобы остановить.\n")
    metrics.log_event(kind="run_start", payload={"mode": "watch", "interval_sec": WATCH_INTERVAL})
    try:
        while True:
            try:
                main()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"⚠️  Итерация упала: {e}")
                metrics.log_event(kind="error", payload={"where": "watch_loop", "error": str(e)[:500]})
            nap = int(WATCH_INTERVAL * random.uniform(1 - WATCH_INTERVAL_JITTER, 1 + WATCH_INTERVAL_JITTER))
            print(f"\n💤 Жду {nap} сек до следующего обхода...\n")
            time.sleep(nap)
    except KeyboardInterrupt:
        print("\n🛑 Остановлено пользователем.")
        metrics.log_event(kind="run_end", payload={"reason": "keyboard_interrupt"})


if __name__ == "__main__":
    _process_lock = _acquire_singleton_lock("hh_auto_reply")
    if _process_lock is None:
        print("\n❌ Уже работает другой экземпляр auto_reply (lock занят). Выхожу.")
        sys.exit(1)
    if WATCH_MODE:
        watch_loop()
    else:
        main()
