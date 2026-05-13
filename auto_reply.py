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
import re
import sqlite3
import sys
import time
from anthropic import Anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

from auto_apply_template import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    MY_PROFILE,
    SESSION_FILE,
    _acquire_singleton_lock,
)
import metrics

load_dotenv()
metrics.init_db()

DELAY_BETWEEN_CHATS = 8
MAX_CHATS_PER_RUN = 30
CHATS_URL = "https://hh.ru/chat"

DRY_RUN = "--dry-run" in sys.argv
WATCH_MODE = "--watch" in sys.argv
WATCH_INTERVAL = int(os.getenv("HH_WATCH_INTERVAL_SEC", "1800"))  # 30 минут по умолчанию

client = Anthropic(api_key=ANTHROPIC_API_KEY)


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


def generate_reply(chat_history: str, vacancy_title: str = "", chat_url: str = "") -> str:
    """Генерирует ответ через Claude Haiku 4.5 на основе MY_PROFILE и истории чата."""
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

    user_msg = f"""ПРОФИЛЬ:
{MY_PROFILE}

ВАКАНСИЯ: {vacancy_title or "не указано"}

ИСТОРИЯ ЧАТА (последние сообщения, сверху старые, снизу новые):
{chat_history}{antirepeat_block}{self_history_block}

Сформулируй ответ соискателя на последнее сообщение от работодателя. Если отвечать не нужно, верни SKIP."""

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=500,
            temperature=0.5,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        usage = getattr(response, "usage", None)
        tin = getattr(usage, "input_tokens", 0) if usage else 0
        tout = getattr(usage, "output_tokens", 0) if usage else 0

        history_snippet = chat_history[-2000:] if chat_history else ""

        violation = reply_violates_rules(text)
        if violation:
            print(f"    🛡  Стоп-фраза в ответе ({violation!r}), возвращаю SKIP — отправит человек")
            metrics.log_event(
                kind="skip",
                model=ANTHROPIC_MODEL,
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
        metrics.log_event(
            kind="skip" if skip else "reply",
            model=ANTHROPIC_MODEL,
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
        return "SKIP" if skip else text
    except Exception as e:
        print(f"  ⚠️  Ошибка генерации ответа: {e}")
        metrics.log_event(
            kind="error",
            model=ANTHROPIC_MODEL,
            vacancy=vacancy_title,
            chat_url=chat_url,
            payload={"error": str(e)[:500]},
        )
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
    from profile import SELF_NAME_MARKERS as _SNM  # type: ignore
    SELF_NAME_MARKERS = tuple(s.lower() for s in _SNM)
except Exception:
    SELF_NAME_MARKERS: tuple[str, ...] = ()

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


def _detect_role(author: str, has_author_field: bool, side: str = "") -> str:
    """Определяет роль отправителя.

    Раньше эвристика была: «нет author field → значит исходящее (мы)».
    Но HH не всегда рендерит author и у ВХОДЯЩИХ сообщений HR-ботов
    (системные шаблоны от компании), и тогда вопрос бота помечался как
    «от себя» → Claude видел, что отвечать нечего, и возвращал SKIP.
    Поэтому в первую очередь полагаемся на геометрию пузырька (side):
    HH ставит исходящие справа, входящие слева.

    side: 'right' → мы, 'left' → собеседник, '' → неизвестно (fallback).
    """
    a = (author or "").strip()
    if side == "right":
        return SELF_ROLE
    if side == "left":
        if has_author_field and a and not any(m in a.lower() for m in SELF_NAME_MARKERS):
            return a
        return "Работодатель"
    # Fallback: геометрия не определилась — используем старую логику по author.
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

    # Собираем все сообщения одним JS-вызовом: id, текст, автор и сторона
    # пузырька (right=исходящее, left=входящее). Сторона определяется по
    # геометрии центра пузырька относительно центра общего контейнера.
    raw = page.evaluate("""
        () => {
            const blocks = Array.from(document.querySelectorAll('[data-qa^="chatik-chat-message-"]'))
                .filter(el => {
                    const qa = el.dataset.qa || '';
                    return !/-text$|-menu$/.test(qa);
                });
            if (!blocks.length) return [];
            // Контейнер: ближайший общий родитель, по которому считаем центр
            let parent = blocks[0].parentElement;
            for (let i=0; i<8 && parent; i++) {
                const r = parent.getBoundingClientRect();
                if (r.width > 300) break;
                parent = parent.parentElement;
            }
            const pr = parent ? parent.getBoundingClientRect() : null;
            const mid = pr ? pr.left + pr.width/2 : null;
            return blocks.map(el => {
                const qa = el.dataset.qa;
                const id = qa.replace('chatik-chat-message-','');
                const textEl = el.querySelector('[data-qa="chat-bubble-text"]');
                let text = (textEl && textEl.innerText || '').trim();
                if (!text) text = (el.innerText || '').trim();
                const authorEl = el.querySelector('[data-qa="chat-bubble-author-name"]');
                const author = authorEl ? (authorEl.innerText || '').trim() : '';
                const r = el.getBoundingClientRect();
                const center = r.left + r.width/2;
                let side = '';
                if (mid !== null && r.width > 0) side = center > mid ? 'right' : 'left';
                return {id, text, author, hasAuthor: !!authorEl, side};
            });
        }
    """) or []

    seen_ids = set()
    items = []  # (msg_id_int, role, text)
    for m in raw:
        msg_id = (m.get("id") or "").strip()
        if not msg_id.isdigit() or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        text = (m.get("text") or "").strip()
        if not text:
            continue
        role = _detect_role(m.get("author") or "", bool(m.get("hasAuthor")), m.get("side") or "")
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


def main():
    print("\n" + "=" * 50)
    print("💬 HH.ru Авто-ответчик")
    print("=" * 50)

    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY":
        print("\n❌ Ошибка, укажи ANTHROPIC_API_KEY в auto_apply_template.py")
        return
    if not os.path.exists(SESSION_FILE):
        print("\n❌ Сессия не найдена, сначала запусти python3 hh_login.py")
        return
    if DRY_RUN:
        print("\n🧪 DRY-RUN, ответы будут сгенерированы, но не отправлены\n")

    stats = {"replied": 0, "skipped": 0, "errors": 0}

    proxy = os.getenv("HH_PROXY") or os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    headless = os.getenv("HH_HEADLESS", "").lower() in ("1", "true", "yes")
    launch_kwargs = {"headless": headless, "slow_mo": 0 if headless else 300}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        print(f"🌐 Использую прокси: {proxy}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        print("\n🔍 Открываю список чатов...")
        chats = find_unread_chats(page)
        print(f"📨 Непрочитанных чатов: {len(chats)}\n")

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

                positive = detect_positive_signal(payload["history"])
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
                    stats["skipped"] += 1
                    continue

                reply = generate_reply(payload["history"], payload["title"], chat_url=chat_url)

                if is_skip_response(reply) or len(reply) < 5:
                    print("    ⏭️  Claude вернул SKIP, пропускаю")
                    stats["skipped"] += 1
                    continue

                print(f"    📝 Ответ: {reply[:100]}...")

                if DRY_RUN:
                    print("    🧪 DRY-RUN, не отправляю")
                    stats["replied"] += 1
                else:
                    if post_reply(page, reply):
                        print("    ✅ Отправлено")
                        stats["replied"] += 1
                    else:
                        print("    ❌ Не удалось отправить")
                        stats["errors"] += 1

                print(f"    ⏳ Пауза {DELAY_BETWEEN_CHATS} сек...")
                time.sleep(DELAY_BETWEEN_CHATS)
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
            print(f"\n💤 Жду {WATCH_INTERVAL} сек до следующего обхода...\n")
            time.sleep(WATCH_INTERVAL)
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
