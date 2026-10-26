#!/usr/bin/env python3
"""
HH.ru Автооткликатор
Автоматический поиск вакансий и отклик с шаблонными письмами

ИНСТРУКЦИЯ:
1. Скопируй profile.example.py → profile.py и заполни своими данными
2. Положи ANTHROPIC_API_KEY в переменную окружения (или .env) — для auto_reply.py
3. Запусти: python3 auto_apply_template.py
"""

import fcntl
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote
from anthropic import Anthropic
from openai import OpenAI
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from dotenv import load_dotenv

load_dotenv()


def _acquire_singleton_lock(name: str):
    """Берёт exclusive flock на файл-локер.
    Возвращает открытый file handle (его нельзя закрывать) или None, если занят.
    Защищает от случайного запуска двух копий, которые приведут к дублирующим
    откликам (race в is_already_applied между read из БД и последующей записью).
    """
    lock_dir = os.path.expanduser("~/.n8n-files")
    os.makedirs(lock_dir, exist_ok=True)
    fh = open(os.path.join(lock_dir, f"{name}.lock"), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh

try:
    from profile import (
        SEARCH_QUERIES,
        LETTER_SIGNATURE as _LETTER_SIGNATURE,
        SALARY_ADDENDUM,
        MY_PROFILE,
    )
except ImportError as e:
    sys.exit(
        "ERROR: profile.py не найден. Скопируй profile.example.py → profile.py "
        f"и заполни своими данными.\nДетали: {e}"
    )

try:
    import metrics as _metrics
    _metrics.init_db()
except Exception:
    _metrics = None

# ============== НАСТРОЙКИ ==============

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or "YOUR_ANTHROPIC_API_KEY"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or "YOUR_OPENROUTER_API_KEY"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free"

MAX_PAGES = int(os.getenv("HH_MAX_PAGES", "3"))

DELAY_APPLY_MIN = int(os.getenv("HH_DELAY_APPLY_MIN", "25"))
DELAY_APPLY_MAX = int(os.getenv("HH_DELAY_APPLY_MAX", "50"))

MAX_APPLIES_PER_DAY = int(os.getenv("HH_MAX_APPLIES_PER_DAY", "100"))

WORK_HOUR_START = int(os.getenv("HH_WORK_HOUR_START", "8"))
WORK_HOUR_END = int(os.getenv("HH_WORK_HOUR_END", "21"))

APPLY_INTERVAL_JITTER = float(os.getenv("HH_APPLY_INTERVAL_JITTER", "0.25"))


def _apply_delay() -> float:
    """Рандомная пауза между откликами — человекоподобность против антибана."""
    return random.uniform(DELAY_APPLY_MIN, DELAY_APPLY_MAX)


def _within_work_hours() -> bool:
    h = time.localtime().tm_hour
    return WORK_HOUR_START <= h < WORK_HOUR_END


def _seconds_until_work_hours() -> int:
    """Сколько секунд спать до начала рабочего окна (если сейчас вне его)."""
    now = time.localtime()
    secs_into_day = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    start = WORK_HOUR_START * 3600
    if now.tm_hour < WORK_HOUR_START:
        wait = start - secs_into_day
    else:
        wait = (24 * 3600 - secs_into_day) + start
    return max(60, wait)

RESUME_ID = os.getenv("HH_RESUME_ID", "")

# При True чистые офисные вакансии (ON_SITE_TRIGGERS без hybrid) пропускаются.
# Гибрид всегда обрабатывается релокационным письмом.
# Чтобы откликаться и на офис: HH_SKIP_ON_SITE=0
SKIP_ON_SITE = os.getenv("HH_SKIP_ON_SITE", "1").strip().lower() not in ("0", "false", "no", "")

# Второй проход поиска без schedule=remote. По умолчанию включён.
SEARCH_HYBRID_OFFICE = os.getenv("HH_SEARCH_HYBRID_OFFICE", "1").strip().lower() not in ("0", "false", "no", "")

N8N_FILES_DIR = os.getenv("N8N_FILES_DIR", os.path.expanduser("~/.n8n-files"))
SESSION_FILE = os.path.join(N8N_FILES_DIR, "hh_session.json")

# ============== ШАБЛОНЫ ОТКЛИКА ==============

_LETTER_BODY_REMOTE = """Добрый день!

Мне очень понравилась ваша вакансия, я был бы рад применить свой опыт и знания в вашей компании, в связи с этим прошу вас рассмотреть моё резюме."""

# Личные части письма (город, подпись с контактами) — только в .env:
# HH_LETTER_BODY_HYBRID / HH_LETTER_SIGNATURE_HYBRID, переносы строк — \n.
_LETTER_BODY_HYBRID_OFFICE = os.getenv("HH_LETTER_BODY_HYBRID", """Добрый день!

Для меня предпочтителен удалённый формат работы, поэтому в настоящий момент я готов только к редким командировкам, а по истечении 3-х месяцев работы буду готов рассматривать релокацию, офисный или гибридный формат работы.

P.S. Мне очень понравилась ваша вакансия, я был бы рад применить свой опыт и знания в вашей компании, в связи с этим прошу вас рассмотреть моё резюме!""")

_LETTER_SIGNATURE_HYBRID = os.getenv("HH_LETTER_SIGNATURE_HYBRID", """С уважением,
<Имя Фамилия>""")

_LETTER_BODY = _LETTER_BODY_REMOTE

SALARY_TRIGGERS = [
    "зарплатные ожидания", "зарплатных ожиданий",
    "ожидания по зп", "ожидания по зарплате", "ваши ожидания по",
    "ожидаемая зарплата", "ожидаемой зарплаты", "ожидаемую зарплату",
    "ожидаемый доход", "ожидаемого дохода", "ожидаемый уровень",
    "желаемая зарплата", "желаемой зарплаты", "желаемую зарплату",
    "желаемый уровень зп", "желаемый доход",
    "финансовые ожидания", "финансовых ожиданий",
    "укажите зарплат", "укажите желаемую", "укажите ожидаемую",
    "укажите ожидания", "укажите вилку", "укажите ожидаемый",
    "указать зарплат", "указать желаемую", "указать ожидаемую",
    "указание зарплат", "указание желаемой", "указание ожидаемой",
    "напишите ожидания", "напишите ожидаемую",
    "уровень дохода", "уровень зп",
    "salary expectations", "expected salary",
]


def wants_salary(description: str, title: str = "") -> bool:
    text = (description or "").lower() + " " + (title or "").lower()
    return any(t in text for t in SALARY_TRIGGERS)


def build_letter(
    description: str,
    title: str = "",
    *,
    is_hybrid_or_office: bool = False,
) -> tuple[str, list[str]]:
    """Собирает письмо под конкретную вакансию. Возвращает (текст, список_аддендумов)."""
    addendums = []
    if is_hybrid_or_office:
        parts = [_LETTER_BODY_HYBRID_OFFICE]
        signature = _LETTER_SIGNATURE_HYBRID
        addendums.append("hybrid_office")
    else:
        parts = [_LETTER_BODY_REMOTE]
        signature = _LETTER_SIGNATURE
    if wants_salary(description, title):
        parts.append(SALARY_ADDENDUM)
        addendums.append("salary")
    parts.append(signature)
    return "\n\n".join(parts), addendums


STANDARD_LETTER = _LETTER_BODY_REMOTE + "\n\n" + _LETTER_SIGNATURE

MANUAL_TITLE_TRIGGERS = [
    " data ", "data-", "/data", "(data", "data,", "data.",
    " database ", " database,", "базы данных", "бд ", "бд,",
    " analytics ", " analytics,", "аналитик", "аналитика",
    " researcher ", "researcher", "исследователь",
]

MANUAL_TRIGGERS = [
    "примеры работ", "пример работ", "примеры работы", "пример работы",
    "приведите пример", "приведите примеры", "приведите примеры работ",
    "покажите пример",
    "ваше портфолио", "ссылку на портфолио", "ссылка на портфолио",
    "приложите портфолио", "прикрепите портфолио", "пришлите портфолио",
    "тестовое задание", "выполните тестовое", "выполнить тестовое",
    "выполните задание", "решить задачу",
    "тестовое испытание", "тестовый проект",
    "ответьте на вопросы", "заполните анкету", "пройдите опрос",
    "расскажите о себе", "расскажите о вашем опыте", "расскажите о своем опыте",
    "расскажите о своём опыте", "расскажите об опыте", "расскажите про опыт",
    "опишите ваш опыт", "опишите свой опыт", "опишите свой релевантный",
    "опишите ваш релевантный", "опишите опыт",
    "кратко расскажите", "кратко опишите", "кратко о себе",
    "укажите опыт", "укажите ваш опыт", "опишите релевантный опыт",
    "при отклике важно", "при отклике укажите", "при отклике опишите",
    "при отклике расскажите", "при отклике приложите", "при отклике добавьте",
    "при отклике напишите", "при отклике отправьте",
    "как откликнуться", "как откликнутся", "как отозваться",
    "в отклике важно", "в отклике укажите", "в отклике опишите",
    "в отклике расскажите", "в отклике напишите", "в отклике отправьте",
    "в ответе напишите", "напишите в ответе", "в ответе укажите",
    "в сопроводительном напишите", "напишите в сопроводительном",
    "в сопроводительном укажите", "укажите в сопроводительном",
    "в письме напишите", "напишите в письме",
    "напишите фразу", "укажите фразу", "напишите слово",
    "секретное слово", "кодовое слово", "секретный вопрос",
    "напишите в теме", "укажите в теме", "укажите в теме письма",
    "тема отклика", "тема письма", "название вакансии в теме",
    "начните сопроводительное", "начните письмо", "начните отклик",
    "первой фразой", "первым предложением",
    "прочитайте до конца", "прочитайте вакансию до конца",
    "внимательно прочитайте", "внимательно прочтите",
    "опыт архитектурных решений", "архитектурных решений",
    "опишите архитектур", "расскажите про архитектур", "расскажите об архитектур",
    "приведите архитектур",
    "проекты с похожими задачами", "проектах с похожими задачами",
    "проекты со схожими задачами", "примеры релевантных проектов",
    "приведите проекты", "опишите проекты", "перечислите проекты",
    "укажите проекты",
]

ON_SITE_TRIGGERS = [
    "работа в офисе", "работа из офиса", "работать в офисе",
    "только офис", "только из офиса", "офисный формат",
    "формат работы: офис", "график работы: офис",
    "присутствие в офисе", "обязательное присутствие",
    "5/2 в офисе", "5 дней в офисе",
    "офисная работа", "в офисе компании",
    "на территории заказчика", "у заказчика на территории",
    "в офисе заказчика", "в офисе клиента",
    "на площадке заказчика", "на площадке клиента",
]

HYBRID_TRIGGERS = [
    "гибрид", "hybrid", "гибридный формат", "гибридный график",
    "формат работы: гибрид", "формат: гибрид",
    "частично удалён", "частично удален", "частично remote",
    "несколько дней в офисе", "2-3 дня в офисе", "2–3 дня в офисе",
    "1-2 дня в офисе", "1–2 дня в офисе", "дни в офисе",
    "офис + удалён", "офис + удален", "офис/удалён", "офис/удален",
]


def on_site_triggers(description: str, title: str = "") -> list[str]:
    text = (description or "").lower() + " " + (title or "").lower()
    return [t for t in ON_SITE_TRIGGERS if t in text]


def hybrid_triggers(description: str, title: str = "") -> list[str]:
    text = (description or "").lower() + " " + (title or "").lower()
    return [t for t in HYBRID_TRIGGERS if t in text]


def is_hybrid_or_office(description: str, title: str = "") -> bool:
    return bool(hybrid_triggers(description, title) or on_site_triggers(description, title))


def needs_manual_review(description: str, title: str = "") -> list[str]:
    """Возвращает список совпавших триггеров. Пустой список = можно автоотклик.

    Офисные триггеры сюда НЕ входят — они обрабатываются отдельно
    (on_site_triggers / hybrid_triggers / SKIP_ON_SITE).
    """
    text = (description or "").lower() + " " + (title or "").lower()
    return [t for t in MANUAL_TRIGGERS if t in text]


WRONG_ROLE_TITLE_TRIGGERS = [
    "devops", "dev ops", "site reliability", " sre ", "sre engineer", "sre lead",
    "infrastructure engineer", "infra engineer",
    "системный администратор", "сисадмин",
    "data scientist", "data science", "дата-сайентист", "дата сайентист",
    "data analyst", "аналитик данных",
    "qa engineer", "qa-engineer", "qa lead", "qa инженер", "quality assurance",
    "automation qa", "qa automation", "manual qa", "sdet", "aqa",
    "test engineer", "test automation",
    " qa ", "qa-", "/qa", "(qa", "qa)",
    "тестировщик", "инженер по тестированию",
    "преподаватель", "teacher", "lecturer", "instructor",
    "frontend", "front-end", "фронтенд", "front end",
    "аналитик", "analyst",
    "тренер", "trainer", "coach", "coach ",
    "менеджер по продажам", "sales manager", "sales director",
]


def wrong_role_match(title: str) -> str:
    """Возвращает первый совпавший триггер чужой роли или пустую строку."""
    t = " " + (title or "").lower() + " "
    for trig in WRONG_ROLE_TITLE_TRIGGERS:
        if trig in t:
            return trig.strip()
    return ""


# Эти wrong_role-триггеры допустимы, если title уже AI-таргет.
# Иначе «AI Infrastructure Engineer» отсекался бы как infra.
_WRONG_ROLE_OK_WITH_AI = {
    "infrastructure engineer",
    "infra engineer",
}


def has_manual_title_trigger(title: str) -> str:
    t = " " + (title or "").lower() + " "
    for trig in MANUAL_TITLE_TRIGGERS:
        if trig in t:
            return trig.strip()
    return ""


LANGUAGE_TARGET_MARKERS = [
    "python", "питон",
    "автоматизац", "automation", "автоматизатор",
]

AI_LLM_TITLE_MARKERS = [
    " ai ", "ai-", "/ai", "(ai", "ai/", "(ai)",
    "llm", "llmops", "rag",
    "генеративн", "нейросет",
    " ии ", " ии,", " ии)", "(ии ", " ии-", "по внедрению ии",
    "искусственн",
    "промпт", "prompt engineer", "prompt-engineer",
    "genai", "gen ai", "generative ai",
]

POSITION_TITLE_MARKERS = [
    " engineer", "-engineer",
    "инженер",
    " developer", "-developer",
    "разработчик",
    " lead", "-lead",
    "тимлид",
    "руководитель",
    "автоматизатор",
    "architect", "архитектор",
    " head ", "head of",
    " product ", "продакт",
]

INFRA_TITLE_MARKERS = [
    "infrastructure", "инфраструктур",
    " infra", "-infra", "/infra",
    " инфра", "-инфра",
    "platform", "платформ",
]


def is_ai_target_role(title: str) -> bool:
    t = " " + (title or "").lower() + " "
    has_ai = any(m in t for m in AI_LLM_TITLE_MARKERS)
    if not has_ai:
        return False
    has_position = any(m in t for m in POSITION_TITLE_MARKERS)
    has_infra = any(m in t for m in INFRA_TITLE_MARKERS)
    return has_position or has_infra


def is_language_target_role(title: str) -> bool:
    t = " " + (title or "").lower() + " "
    return any(m in t for m in LANGUAGE_TARGET_MARKERS)


def is_target_role(title: str) -> bool:
    return is_ai_target_role(title) or is_language_target_role(title)


def has_ai_marker(title: str) -> bool:
    t = " " + (title or "").lower() + " "
    return any(m in t for m in AI_LLM_TITLE_MARKERS)


ML_TITLE_MARKERS = [
    " ml ", "ml-", "/ml", "(ml", "ml/", "ml,", "ml.",
    "ml engineer", "ml инженер", "ml-инженер",
    "ml platform", "ml infrastructure", "ml инфра", "ml-платформ",
    "mlops",
    "machine learning", "машинн",
]


def has_ml_marker(title: str) -> bool:
    t = " " + (title or "").lower() + " "
    return any(trig in t for trig in ML_TITLE_MARKERS)


# Anthropic SDK не умеет SOCKS-прокси в all_proxy — убираем перед клиентом,
# но сохраняем значение для Playwright.
_SAVED_ALL_PROXY = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or ""
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

openrouter_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)


def generate_cover_letter(title: str, employer: str, description: str, vacancy_url: str = "") -> str:
    """Генерирует сопроводительное письмо через OpenRouter. Основной путь отклика — шаблоны."""
    prompt = f"""Напиши сопроводительное письмо для отклика на вакансию. Пиши как живой человек, не как робот.

СТРУКТУРА ПИСЬМА:
1. Приветствие (Добрый день! или Здравствуйте!)
2. Представься кратко (имя, чем занимаюсь)
3. Почему заинтересовала именно эта вакансия/компания (найди что-то конкретное в описании)
4. Кратко релевантный опыт (1-2 примера кейсов которые подходят под вакансию)
5. Призыв к действию + ссылка на сайт с кейсами (если есть)

ПРАВИЛА:
- Длина: 4-6 предложений, не больше
- Тон: дружелюбный, профессиональный, но не официозный
- Без штампов: "с большим интересом", "буду рад", "внести вклад", "рассмотрите мою кандидатуру"
- Без длинных тире (—), без восклицательных знаков в конце каждого предложения
- Пиши так, будто реальный человек пишет реальному человеку
- Русский язык

ОБО МНЕ:
{MY_PROFILE}

ВАКАНСИЯ:
Название: {title}
Компания: {employer}
Описание: {description[:2500]}

Напиши только текст письма, без комментариев."""

    try:
        response = openrouter_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            max_tokens=350,
            temperature=0.8,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content.strip()
        return text
    except Exception as e:
        print(f"  ⚠️  Ошибка генерации письма: {e}")
        if _metrics:
            _metrics.log_event(
                kind="error",
                model=OPENROUTER_MODEL,
                vacancy=f"{title} · {employer}",
                chat_url=vacancy_url,
                payload={"error": str(e)[:500], "vacancy_url": vacancy_url},
            )
        return ""


def _safe_goto(page, url: str, *, wait_until: str = "domcontentloaded",
               timeout: int = 30000, max_retries: int = 3, delay: float = 2.0) -> None:
    for attempt in range(max_retries):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            msg = str(e)
            is_nav_error = (
                "ERR_CONNECTION_CLOSED" in msg
                or "Navigation interrupted" in msg
                or "net::ERR" in msg
                or "chrome-error://" in msg
            )
            if not is_nav_error or attempt == max_retries - 1:
                raise
            wait = delay * (attempt + 1)
            print(f"    ⚠️  Навигационная ошибка (попытка {attempt+1}/{max_retries}), "
                  f"жду {wait:.0f}с: {msg[:120]}")
            time.sleep(wait)
            try:
                if page.url.startswith("chrome-error://"):
                    page.goto("about:blank", wait_until="load", timeout=5000)
            except Exception:
                pass


def search_vacancies(
    page,
    query: str = "",
    page_num: int = 0,
    resume_id: str = "",
    *,
    schedule: str = "remote",
) -> list:
    """Ищет вакансии на HH.ru.

    schedule:
      - "remote"  — только удалёнка (&schedule=remote)
      - "any"     — без фильтра по графику (гибрид/офис/удалёнка)
    """
    if resume_id:
        url = (
            f"https://hh.ru/search/vacancy?resume={quote(resume_id)}&from=resumelist"
            "&search_period=3"
            "&order_by=publication_time"
            f"&items_on_page=50&page={page_num}"
        )
    else:
        schedule_param = "&schedule=remote" if schedule == "remote" else ""
        url = (
            f"https://hh.ru/search/vacancy?text={quote(query)}"
            "&area=113"
            f"{schedule_param}"
            "&experience=between3And6&experience=moreThan6"
            "&search_period=3"
            "&order_by=publication_time"
            f"&items_on_page=50&page={page_num}"
        )

    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=30000)

        if "captcha" in page.title().lower():
            print("  ⚠️  Сработала капча! Подожди немного и попробуй снова.")
            return []

        try:
            page.wait_for_selector("[data-qa='vacancy-serp__vacancy']", timeout=10000)
        except PlaywrightTimeoutError:
            return []

        vacancies = []
        cards = page.locator("[data-qa='vacancy-serp__vacancy']").all()

        for card in cards:
            try:
                title_el = card.locator("[data-qa='serp-item__title']")
                title_el.wait_for(state="visible", timeout=5000)

                href = title_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://hh.ru" + href
                title = title_el.inner_text()

                employer_el = card.locator("[data-qa='vacancy-serp__vacancy-employer']").first
                employer = employer_el.inner_text() if employer_el.count() > 0 else "Компания"

                vacancies.append({
                    "title": title,
                    "url": href,
                    "employer": employer
                })
            except Exception:
                continue

        return vacancies
    except Exception as e:
        print(f"  ⚠️  Ошибка поиска: {e}")
        return []


def get_vacancy_description(page, url: str) -> str:
    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector("[data-qa='vacancy-description']", timeout=10000)

        desc_el = page.locator("[data-qa='vacancy-description']")
        return desc_el.inner_text() if desc_el.count() > 0 else ""
    except Exception:
        return ""


def _has_screening_questions(page) -> bool:
    try:
        u = (page.url or "").lower()
    except Exception:
        u = ""
    if "startedwithquestion" in u:
        return True
    selectors = [
        "[data-qa='vacancy-response-question']",
        "[data-qa='vacancy-response-question-text']",
        "[data-qa='response-questions']",
        "text=Вопросы от работодателя",
        "text=Ответьте на вопросы",
    ]
    for s in selectors:
        try:
            if page.locator(s).count() > 0:
                return True
        except Exception:
            continue
    return False


def apply_to_vacancy(page, url: str, message: str) -> dict:
    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        if page.locator("text=Вы откликнулись").count() > 0:
            return {"status": "skipped", "reason": "Уже откликались"}

        if _has_screening_questions(page):
            return {"status": "has_questions", "reason": "Скрининг-вопросы от работодателя"}

        cover_link = page.locator("a:has-text('Написать сопроводительное')")
        if cover_link.count() > 0 and message:
            print("      🔍 Нашёл ссылку 'Написать сопроводительное'")
            cover_link.first.click()
            page.wait_for_timeout(2000)

            letter_area = page.locator("textarea").first
            if letter_area.count() > 0:
                print("      ✍️  Заполняю письмо...")
                letter_area.fill(message)
                page.wait_for_timeout(500)

                submit_btn = page.locator("button:has-text('Откликнуться'), button:has-text('Отправить'), button[data-qa='vacancy-response-submit-popup']").first
                if submit_btn.count() > 0:
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    return {"status": "success", "reason": "С письмом"}

        apply_btn = page.locator("[data-qa='vacancy-response-link-top']")
        if apply_btn.count() == 0:
            apply_btn = page.locator("[data-qa='vacancy-response-link-bottom']")

        if apply_btn.count() > 0 and message:
            dropdown = page.locator("[data-qa='vacancy-response-link-top'] ~ button, button[data-qa='vacancy-response-actions-dropdown']").first
            if dropdown.count() > 0:
                print("      🔍 Нашёл выпадающее меню")
                dropdown.click()
                page.wait_for_timeout(1000)

                with_letter = page.get_by_text("сопроводительн", exact=False).first
                if with_letter.count() > 0:
                    print("      🔍 Нашёл опцию 'С сопроводительным'")
                    with_letter.click()
                    page.wait_for_timeout(2000)

                    letter_area = page.locator("textarea").first
                    if letter_area.count() > 0:
                        print("      ✍️  Заполняю письмо...")
                        letter_area.fill(message)
                        page.wait_for_timeout(500)

                        submit_btn = page.locator("button:has-text('Откликнуться'), button:has-text('Отправить')").first
                        if submit_btn.count() > 0:
                            submit_btn.click()
                            page.wait_for_timeout(3000)
                            return {"status": "success", "reason": "С письмом (меню)"}

        if apply_btn.count() > 0:
            print("      🔍 Жму основную кнопку 'Откликнуться'")
            apply_btn.first.click()
            page.wait_for_timeout(3000)

            if _has_screening_questions(page):
                return {"status": "has_questions", "reason": "Скрининг-вопросы от работодателя"}

            add_letter_btn = page.locator("button:has-text('Добавить сопроводительное')")
            if add_letter_btn.count() == 0:
                add_letter_btn = page.locator("button:has-text('Добавить сопроводительное письмо')")
            if add_letter_btn.count() > 0:
                print("      🔍 Нашёл кнопку 'Добавить сопроводительное'")
                add_letter_btn.first.click()
                page.wait_for_timeout(2000)

            page.wait_for_timeout(2000)

            letter_area = None
            selectors = [
                "textarea[placeholder*='сопроводительное']",
                "textarea[name*='letter']",
                "textarea[data-qa*='letter']",
                "div[contenteditable='true']",
                "textarea",
            ]

            for selector in selectors:
                try:
                    el = page.locator(selector).first
                    if el.count() > 0 and el.is_visible(timeout=500):
                        letter_area = el
                        print(f"      ✍️  Нашёл поле: {selector}")
                        break
                except Exception:
                    continue

            if not letter_area:
                print("      ❌ Поле для письма не найдено")
                return {"status": "error", "reason": "no_textarea"}

            if message:
                print("      ✍️  Заполняю поле для письма...")

                try:
                    letter_area.focus()
                    page.wait_for_timeout(300)
                except Exception as e:
                    print(f"      ⚠️  Не удалось сфокусироваться: {e}")

                try:
                    letter_area.press("Control+a")
                    page.wait_for_timeout(200)
                    letter_area.press("Backspace")
                    page.wait_for_timeout(200)
                except Exception as e:
                    print(f"      ⚠️  Не удалось очистить: {e}")

                print(f"      📝 Вставляю письмо ({len(message)} символов)...")
                try:
                    letter_area.fill(message)
                    page.wait_for_timeout(500)

                    actual_value = letter_area.input_value(timeout=1000)
                    if actual_value != message:
                        print(f"      ⚠️  Текст не записался полностью (записано {len(actual_value)}/{len(message)})")
                        letter_area.press("Control+a")
                        letter_area.press("Backspace")
                        for i, char in enumerate(message):
                            letter_area.press(char)
                            if i and i % 50 == 0:
                                page.wait_for_timeout(50)
                        page.wait_for_timeout(500)
                        actual_value = letter_area.input_value(timeout=1000)

                    if actual_value == message:
                        print("      ✅ Текст записан успешно")
                    else:
                        print(f"      ⚠️  Текст записан частично: {len(actual_value)}/{len(message)}")

                except Exception as e:
                    print(f"      ❌ Ошибка при заполнении: {e}")
                    try:
                        letter_area.press("Control+a")
                        letter_area.press("Backspace")
                        letter_area.type(message, delay=10)
                        page.wait_for_timeout(500)
                        print("      ✅ Текст введён методом type()")
                    except Exception as e2:
                        print(f"      ❌ Не удалось ввести текст: {e2}")
                        return {"status": "error", "reason": "text_insert_failed"}

                print("      📨 Ищу кнопку отправки...")
                submit_btn = page.locator("button:has-text('Отправить'), button:has-text('Откликнуться'), button:has-text('Отправить письмо')").first
                if submit_btn.count() > 0:
                    print("      📨 Нажимаю кнопку отправки...")
                    submit_btn.click()
                    page.wait_for_timeout(3000)

                    if page.locator("text=Вы откликнулись").count() > 0:
                        return {"status": "success", "reason": "С письмом (подтверждено)"}
                    elif page.locator("text=Резюме доставлено").count() > 0:
                        return {"status": "success", "reason": "С письмом (доставлено)"}
                    else:
                        return {"status": "success", "reason": "С письмом (статус неясен)"}
                else:
                    all_buttons = page.locator("button").all()
                    for btn in all_buttons:
                        btn_text = btn.inner_text().lower()
                        if "отправ" in btn_text or "откликн" in btn_text:
                            print(f"      📨 Нашёл кнопку: {btn_text}")
                            btn.click()
                            page.wait_for_timeout(3000)
                            if page.locator("text=Вы откликнулись").count() > 0:
                                return {"status": "success", "reason": "С письмом (найдена кнопка)"}
                            return {"status": "success", "reason": "С письмом (кнопка нажата)"}

                    print("      ❌ Кнопка отправки не найдена")
                    return {"status": "error", "reason": "no_submit_button"}

            if page.locator("text=Вы откликнулись").count() > 0 or page.locator("text=Резюме доставлено").count() > 0:
                return {"status": "success", "reason": "Без письма"}

            return {"status": "success", "reason": "Статус неясен"}

        return {"status": "error", "reason": "Кнопка не найдена"}

    except Exception as e:
        return {"status": "error", "reason": str(e)}


_LOG_FH = None


def _redirect_apply_log() -> None:
    """Пишем stdout/stderr в /tmp/hh_auto_apply.log.
    Watch-режим: append, чтобы итерации не затирали лог.
    Обычный прогон: truncate.
    """
    global _LOG_FH
    if _LOG_FH is not None:
        return
    log_file = Path("/tmp/hh_auto_apply.log")
    mode = "a" if "--watch" in sys.argv else "w"
    _LOG_FH = open(log_file, mode, buffering=1)
    sys.stdout = _LOG_FH
    sys.stderr = _LOG_FH


def main():
    dry_run = "--dry-run" in sys.argv

    pid_file = Path("/tmp/hh_auto_apply.pid")
    pid_file.write_text(str(os.getpid()))
    _redirect_apply_log()

    print("\n" + "="*50)
    print("🚀 HH.ru Автооткликатор" + (" [DRY-RUN]" if dry_run else ""))
    print("="*50)

    if OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        print("\n⚠️  OPENROUTER_API_KEY не задан — генерация писем через LLM недоступна, используем шаблоны.")

    if "YOUR_SEARCH_QUERY" in SEARCH_QUERIES[0]:
        print("\n❌ Ошибка: Укажи поисковые запросы!")
        print("   Открой auto_apply.py и заполни SEARCH_QUERIES")
        return

    if "YOUR_NAME" in MY_PROFILE:
        print("\n❌ Ошибка: Заполни свой профиль!")
        print("   Открой auto_apply.py и заполни MY_PROFILE")
        return

    if not os.path.exists(SESSION_FILE):
        print("\n❌ Сессия не найдена!")
        print("   Сначала запусти: python3 hh_login.py")
        return

    print(f"\n📋 Поисковые запросы: {', '.join(SEARCH_QUERIES)}")
    if RESUME_ID:
        print(f"🎯 + HH-рекомендации по резюме {RESUME_ID[:8]}…")
    print(f"📄 Страниц на запрос: {MAX_PAGES}")
    print(f"🏠 Remote-поиск: да · Hybrid/Office-поиск: {'да' if SEARCH_HYBRID_OFFICE else 'нет'}")
    print(f"🏢 SKIP_ON_SITE (чистый офис): {'да' if SKIP_ON_SITE else 'нет (отклик с релокационным письмом)'}")
    print(f"⏱️  Пауза между откликами: {DELAY_APPLY_MIN}–{DELAY_APPLY_MAX} сек (рандом)")
    print(f"🗓️  Рабочие часы: {WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00 · суточный лимит: {MAX_APPLIES_PER_DAY}")

    stats = {"success": 0, "skipped": 0, "error": 0}

    metrics_sink = None if dry_run else _metrics
    applied_index = _metrics.load_applied_index() if _metrics else {"by_vacancy_id": {}, "by_employer_title": {}}
    seen_keys: set[str] = set()

    def _vacancy_key(v: dict) -> str:
        vid = _metrics.extract_vacancy_id(v["url"]) if _metrics else ""
        return vid or v["url"]

    def _remember_in_index(v: dict) -> None:
        if not _metrics:
            return
        entry = {
            "ts": time.time(),
            "vacancy": f"{v['title']} · {v['employer']}",
            "chat_url": v['url'],
            "payload_json": None,
        }
        vid = _metrics.extract_vacancy_id(v['url'])
        if vid:
            applied_index["by_vacancy_id"].setdefault(vid, entry)
        emp_n = _metrics._norm(v['employer'])
        tit_n = _metrics._norm(v['title'])
        if emp_n and tit_n:
            applied_index["by_employer_title"].setdefault((emp_n, tit_n), entry)

    proxy = os.getenv("HH_PROXY") or _SAVED_ALL_PROXY or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    headless = dry_run or os.getenv("HH_HEADLESS", "").lower() in ("1", "true", "yes")
    launch_kwargs = {"headless": headless, "slow_mo": 0 if headless else 300}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        print(f"🌐 Прокси: {proxy}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        sources = [
            {"label": f"🔍 Поиск (remote): {q}", "query": q, "schedule": "remote"}
            for q in SEARCH_QUERIES
        ]
        if SEARCH_HYBRID_OFFICE:
            sources += [
                {"label": f"🏢 Поиск (hybrid/office): {q}", "query": q, "schedule": "any"}
                for q in SEARCH_QUERIES
            ]
        if RESUME_ID:
            sources.append({
                "label": f"🎯 HH-рекомендации по резюме {RESUME_ID[:8]}…",
                "resume_id": RESUME_ID,
                "schedule": "any",
            })

        for src in sources:
            print(f"\n{src['label']}")

            for page_num in range(MAX_PAGES):
                if page_num > 0:
                    page.wait_for_timeout(2000)
                print(f"  📄 Страница {page_num + 1}")

                vacancies = search_vacancies(
                    page,
                    query=src.get("query", ""),
                    page_num=page_num,
                    resume_id=src.get("resume_id", ""),
                    schedule=src.get("schedule", "remote"),
                )
                print(f"  📊 Найдено вакансий: {len(vacancies)}")

                for i, vacancy in enumerate(vacancies, 1):
                    print(f"\n  [{i}/{len(vacancies)}] {vacancy['title'][:50]}...")
                    print(f"      Компания: {vacancy['employer']}")

                    if _metrics:
                        dup = _metrics.check_applied(
                            applied_index,
                            url=vacancy['url'],
                            employer=vacancy['employer'],
                            title=vacancy['title'],
                        )
                        if dup:
                            reason = (
                                f"тот же vacancy_id ({dup.get('vacancy_id')})"
                                if dup["match"] == "vacancy_id"
                                else "та же роль у того же работодателя"
                            )
                            print(f"      ⏭️  Уже откликались: {reason}, пропускаю")
                            stats['skipped'] += 1
                            continue

                    key = _vacancy_key(vacancy)
                    if key in seen_keys:
                        print("      ⏭️  Дубль в этом прогоне, пропускаю")
                        stats['skipped'] += 1
                        continue
                    seen_keys.add(key)

                    if is_ai_target_role(vacancy['title']):
                        wrong = wrong_role_match(vacancy['title'])
                        if wrong and wrong not in _WRONG_ROLE_OK_WITH_AI:
                            print(f"      ⏭️  Не моя роль ({wrong}), пропускаю")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="skip",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "reason": "wrong_role",
                                        "trigger": wrong,
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "vacancy_url": vacancy['url'],
                                    },
                                )
                            stats['skipped'] += 1
                            continue

                    if not is_ai_target_role(vacancy['title']):
                        manual_trigger = has_manual_title_trigger(vacancy['title'])
                        if manual_trigger:
                            print(f"      🚩 {manual_trigger} в названии, в ручную очередь")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "triggers": [manual_trigger],
                                        "description_preview": "(описание не загружали — отсев по title)",
                                    },
                                )
                                _remember_in_index(vacancy)
                            stats['skipped'] += 1
                            continue

                        if has_ml_marker(vacancy['title']):
                            print("      🚩 ML без AI/LLM-контекста, в ручную очередь")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "triggers": ["ml-only-title"],
                                        "description_preview": "(описание не загружали — отсев по title)",
                                    },
                                )
                                _remember_in_index(vacancy)
                            stats['skipped'] += 1
                            continue

                        wrong = wrong_role_match(vacancy['title'])
                        if wrong:
                            print(f"      ⏭️  Не моя роль ({wrong}), пропускаю")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="skip",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "reason": "wrong_role",
                                        "trigger": wrong,
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "vacancy_url": vacancy['url'],
                                    },
                                )
                            stats['skipped'] += 1
                            continue

                        if is_language_target_role(vacancy['title']):
                            pass
                        elif has_ai_marker(vacancy['title']):
                            print("      🚩 AI/LLM без явной позиции, в ручную очередь")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "triggers": ["ai-only-title"],
                                        "description_preview": "(описание не загружали — отсев по title)",
                                    },
                                )
                                _remember_in_index(vacancy)
                            stats['skipped'] += 1
                            continue
                        else:
                            print("      ⏭️  Нет python/AI/ML маркеров в названии, пропускаю")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="skip",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "reason": "not_target_role",
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "vacancy_url": vacancy['url'],
                                    },
                                )
                            stats['skipped'] += 1
                            continue

                    description = get_vacancy_description(page, vacancy['url'])

                    hybrid = hybrid_triggers(description, vacancy['title'])
                    on_site = on_site_triggers(description, vacancy['title'])
                    use_hybrid_letter = bool(hybrid or on_site)

                    if on_site and not hybrid and SKIP_ON_SITE:
                        print(f"      🏢 Чистый офис ({', '.join(on_site[:2])}) — пропускаю (SKIP_ON_SITE=1)")
                        if metrics_sink:
                            metrics_sink.log_event(
                                kind="skip",
                                vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                chat_url=vacancy['url'],
                                payload={
                                    "reason": "on_site",
                                    "title": vacancy['title'],
                                    "employer": vacancy['employer'],
                                    "vacancy_url": vacancy['url'],
                                    "triggers": on_site,
                                },
                            )
                            _remember_in_index(vacancy)
                        stats['skipped'] += 1
                        continue

                    triggers = needs_manual_review(description, vacancy['title'])
                    if triggers:
                        print(f"      🚩 В очередь на ручной отклик: {', '.join(triggers[:3])}")
                        if metrics_sink:
                            metrics_sink.log_event(
                                kind="pending_manual",
                                vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                chat_url=vacancy['url'],
                                payload={
                                    "vacancy_url": vacancy['url'],
                                    "title": vacancy['title'],
                                    "employer": vacancy['employer'],
                                    "triggers": triggers,
                                    "description_preview": description[:600],
                                },
                            )
                            _remember_in_index(vacancy)
                        stats['skipped'] += 1
                        d = _apply_delay()
                        print(f"      ⏳ Пауза {d:.0f} сек...")
                        time.sleep(d)
                        continue

                    # Суточный лимит: kind='apply' пишется ПОСЛЕ успешной отправки.
                    if not dry_run and _metrics and _metrics.count_applies_since(86400) >= MAX_APPLIES_PER_DAY:
                        print(f"      🛑 Суточный лимит {MAX_APPLIES_PER_DAY} откликов достигнут — пропускаю до завтра")
                        stats['skipped'] += 1
                        continue

                    letter, addendums = build_letter(
                        description,
                        vacancy['title'],
                        is_hybrid_or_office=use_hybrid_letter,
                    )
                    addendum_label = "+" + ",".join(addendums) if addendums else "стандарт"
                    if use_hybrid_letter:
                        fmt = "hybrid" if hybrid else "office"
                        print(f"      📝 Шаблон [{addendum_label}/{fmt}] · {len(letter)} символов")
                    else:
                        print(f"      📝 Шаблон [{addendum_label}] · {len(letter)} символов")

                    if dry_run:
                        print("      🧪 [dry-run] отклик не отправлен, в БД не пишу")
                        stats['skipped'] += 1
                        time.sleep(1)
                        continue

                    print("      📤 Отправляю отклик...")
                    result = apply_to_vacancy(page, vacancy['url'], letter)

                    if result['status'] == 'success':
                        print(f"      ✅ Успех! ({result['reason']})")
                        stats['success'] += 1
                        if metrics_sink:
                            metrics_sink.log_event(
                                kind="apply",
                                vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                chat_url=vacancy['url'],
                                payload={
                                    "letter": letter,
                                    "vacancy_url": vacancy['url'],
                                    "title": vacancy['title'],
                                    "employer": vacancy['employer'],
                                    "type": "standard" if not addendums else "standard+addendums",
                                    "addendums": addendums,
                                },
                            )
                            _remember_in_index(vacancy)
                    elif result['status'] == 'skipped':
                        print(f"      ⏭️  Пропущено: {result['reason']}")
                        stats['skipped'] += 1
                        if metrics_sink:
                            _remember_in_index(vacancy)
                    elif result['status'] == 'has_questions':
                        print(f"      🚩 {result['reason']} — в ручную очередь")
                        if metrics_sink:
                            metrics_sink.log_event(
                                kind="pending_manual",
                                vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                chat_url=vacancy['url'],
                                payload={
                                    "vacancy_url": vacancy['url'],
                                    "title": vacancy['title'],
                                    "employer": vacancy['employer'],
                                    "triggers": ["screening_questions"],
                                    "manual_note": "требуется ответить на вопросы вручную",
                                    "description_preview": (description or "")[:600],
                                },
                            )
                            _remember_in_index(vacancy)
                        stats['skipped'] += 1
                    else:
                        print(f"      ❌ Ошибка: {result['reason']}")
                        stats['error'] += 1

                    d = _apply_delay()
                    print(f"      ⏳ Пауза {d:.0f} сек...")
                    time.sleep(d)

        browser.close()

    print("\n" + "="*50)
    print("📊 ИТОГИ:")
    print(f"   ✅ Успешно: {stats['success']}")
    print(f"   ⏭️  Пропущено: {stats['skipped']}")
    print(f"   ❌ Ошибок: {stats['error']}")
    print("="*50 + "\n")


def watch_loop() -> None:
    interval = int(os.getenv("HH_APPLY_INTERVAL_SEC", "14400"))
    _redirect_apply_log()
    print(f"👀 WATCH-режим откликов, интервал ~{interval} сек ±{int(APPLY_INTERVAL_JITTER*100)}%. Ctrl-C, чтобы остановить.\n")
    if _metrics:
        _metrics.log_event(kind="run_start", payload={"mode": "apply_watch", "interval_sec": interval})
    try:
        while True:
            if not _within_work_hours():
                w = _seconds_until_work_hours()
                print(f"🌙 Вне рабочих часов {WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00 — сплю {w // 60} мин до утра...\n")
                time.sleep(w)
                continue
            applied = _metrics.count_applies_since(86400) if _metrics else 0
            if applied >= MAX_APPLIES_PER_DAY:
                print(f"🛑 Суточный лимит {MAX_APPLIES_PER_DAY} откликов достигнут ({applied}) — жду 1 час...\n")
                time.sleep(3600)
                continue
            try:
                main()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"⚠️  Итерация откликов упала: {e}")
                if _metrics:
                    _metrics.log_event(kind="error", payload={"where": "apply_watch_loop", "error": str(e)[:500]})
            nap = int(interval * random.uniform(1 - APPLY_INTERVAL_JITTER, 1 + APPLY_INTERVAL_JITTER))
            print(f"\n💤 Жду {nap} сек до следующего обхода вакансий...\n")
            time.sleep(nap)
    except KeyboardInterrupt:
        print("\n🛑 Остановлено пользователем.")
        if _metrics:
            _metrics.log_event(kind="run_end", payload={"reason": "keyboard_interrupt", "mode": "apply_watch"})


if __name__ == "__main__":
    _process_lock = _acquire_singleton_lock("hh_auto_apply")
    if _process_lock is None:
        print("\n❌ Уже работает другой экземпляр auto_apply (lock занят). Выхожу.")
        sys.exit(1)
    if "--watch" in sys.argv:
        watch_loop()
    else:
        main()
