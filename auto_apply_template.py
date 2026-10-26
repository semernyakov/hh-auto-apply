#!/usr/bin/env python3
"""
HH.ru Автооткликатор
Автоматический поиск вакансий и отклик с шаблонными письмами (build_letter)

ИНСТРУКЦИЯ:
1. Скопируй profile.example.py → profile.py и заполни своими данными
2. Положи ANTHROPIC_API_KEY в переменную окружения (или .env) — для auto_reply.py
3. Запусти: python3 auto_apply_template.py
"""

import fcntl
import os
import random
import shutil
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

# Anthropic API ключ (получить на https://console.anthropic.com/settings/keys)
# Приоритет: переменная окружения ANTHROPIC_API_KEY, потом значение ниже.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or "YOUR_ANTHROPIC_API_KEY"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# OpenRouter API ключ (получить на https://openrouter.ai/keys)
# Приоритет: переменная окружения OPENROUTER_API_KEY, потом значение ниже.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or "YOUR_OPENROUTER_API_KEY"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free"

# Сколько страниц парсить для каждого запроса (1 страница = ~20 вакансий)
# Увеличь до 3-5 для большего охвата, но это замедлит прогон.
MAX_PAGES = int(os.getenv("HH_MAX_PAGES", "3"))

# Пауза между откликами — РАНДОМНЫЙ диапазон (сек). Фиксированная пауза — маркер
# бота: живой человек читает вакансию разное время. Не ставь меньше ~15с.
DELAY_APPLY_MIN = int(os.getenv("HH_DELAY_APPLY_MIN", "25"))
DELAY_APPLY_MAX = int(os.getenv("HH_DELAY_APPLY_MAX", "50"))

# Суточный лимит откликов (жёсткий потолок против бана по объёму). За окно 24ч.
# Внимание: увеличение лимита повышает риск бана HH. Не ставь больше 100 без необходимости.
MAX_APPLIES_PER_DAY = int(os.getenv("HH_MAX_APPLIES_PER_DAY", "100"))

# Рабочие часы (локальное время сервера, МСК) [start, end) — вне окна бот НЕ
# откликается: массовые отклики ночью — яркий признак бота.
WORK_HOUR_START = int(os.getenv("HH_WORK_HOUR_START", "8"))
WORK_HOUR_END = int(os.getenv("HH_WORK_HOUR_END", "23"))

# Джиттер интервала между обходами (доля): реальная пауза = interval * (1 ± JITTER).
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
    else:  # уже после конца окна — ждём до утра
        wait = (24 * 3600 - secs_into_day) + start
    return max(60, wait)

# ID резюме для «подходящих» вакансий от HH (https://hh.ru/applicant/resumes
# → кнопка «N подходящих вакансий» рядом с резюме). HH сам подбирает релевантное
# по нашему профилю — добавляем как ещё один источник к SEARCH_QUERIES.
RESUME_ID = os.getenv("HH_RESUME_ID", "")

# Мы ищем только удалёнку. При True офисные вакансии (сработал ON_SITE_TRIGGERS)
# сразу пропускаются (skip), а не копятся в ручной очереди pending_manual.
# Чтобы вернуться к разбору офисных вручную (на будущее, если начнём смотреть
# и офис) — задай HH_SKIP_ON_SITE=0.
SKIP_ON_SITE = os.getenv("HH_SKIP_ON_SITE", "1").strip().lower() not in ("0", "false", "no", "")

# Путь к файлу сессии
N8N_FILES_DIR = os.getenv("N8N_FILES_DIR", os.path.expanduser("~/.n8n-files"))
SESSION_FILE = os.path.join(N8N_FILES_DIR, "hh_session.json")

# ============== СТАНДАРТНЫЙ ШАБЛОН ОТКЛИКА ==============
# По умолчанию шлём этот текст без вызова Claude (быстро, бесплатно).
# Claude используется только для ответов в чатах (auto_reply.py).

_LETTER_BODY = """Добрый день!

Мне очень понравилась ваша вакансия, я был бы рад применить свой опыт и знания в вашей компании, в связи с этим прошу вас рассмотреть моё резюме."""

# Триггеры, которые означают «работодатель просит указать ожидаемую ЗП».
# При срабатывании добавляем SALARY_ADDENDUM к стандартному письму
# (сама вакансия в ручную очередь НЕ уходит).
SALARY_TRIGGERS = [
    # «зарплатные ожидания» / «ожидаемая зарплата»
    "зарплатные ожидания", "зарплатных ожиданий",
    "ожидания по зп", "ожидания по зарплате", "ваши ожидания по",
    "ожидаемая зарплата", "ожидаемой зарплаты", "ожидаемую зарплату",
    "ожидаемый доход", "ожидаемого дохода", "ожидаемый уровень",
    "желаемая зарплата", "желаемой зарплаты", "желаемую зарплату",
    "желаемый уровень зп", "желаемый доход",
    "финансовые ожидания", "финансовых ожиданий",
    # «укажите / напишите / указать ...»
    "укажите зарплат", "укажите желаемую", "укажите ожидаемую",
    "укажите ожидания", "укажите вилку", "укажите ожидаемый",
    "указать зарплат", "указать желаемую", "указать ожидаемую",
    "указание зарплат", "указание желаемой", "указание ожидаемой",
    "напишите ожидания", "напишите ожидаемую",
    "уровень дохода", "уровень зп",
    # английские
    "salary expectations", "expected salary",
]


def wants_salary(description: str, title: str = "") -> bool:
    text = (description or "").lower() + " " + (title or "").lower()
    return any(t in text for t in SALARY_TRIGGERS)


def build_letter(description: str, title: str = "") -> tuple[str, list[str]]:
    """Собирает письмо под конкретную вакансию. Возвращает (текст, список_аддендумов)."""
    addendums = []
    parts = [_LETTER_BODY]
    if wants_salary(description, title):
        parts.append(SALARY_ADDENDUM)
        addendums.append("salary")
    parts.append(_LETTER_SIGNATURE)
    return "\n\n".join(parts), addendums


# Совместимость, где-то в коде может использоваться константа.
STANDARD_LETTER = _LETTER_BODY + "\n\n" + _LETTER_SIGNATURE


# Триггеры в названии вакансии для ручной очереди (не skip, не автоотклик).
# Data/Database/Analytics/Researcher — требуют ручной проверки контекста.
MANUAL_TITLE_TRIGGERS = [
    " data ", "data-", "/data", "(data", "data,", "data.",
    " database ", " database,", "базы данных", "бд ", "бд,",
    " analytics ", " analytics,", "аналитик", "аналитика",
    " researcher ", "researcher", "исследователь",
]


# Если в тексте вакансии встречается любая из этих фраз, не отправляем
# автоотклик и кладём вакансию в очередь на ручной разбор.
MANUAL_TRIGGERS = [
    # Телеграм/GitHub НЕ включаем, они уже есть в подписи стандартного письма.
    # Портфолио / примеры работ
    "примеры работ", "пример работ", "примеры работы", "пример работы",
    "приведите пример", "приведите примеры", "приведите примеры работ",
    "покажите пример",
    "ваше портфолио", "ссылку на портфолио", "ссылка на портфолио",
    "приложите портфолио", "прикрепите портфолио", "пришлите портфолио",
    # Тестовое / задание
    "тестовое задание", "выполните тестовое", "выполнить тестовое",
    "выполните задание", "решить задачу",
    "тестовое испытание", "тестовый проект",
    # Анкета / опрос
    "ответьте на вопросы", "заполните анкету", "пройдите опрос",
    # Просьба рассказать об опыте — кастомное письмо обязательно
    "расскажите о себе", "расскажите о вашем опыте", "расскажите о своем опыте",
    "расскажите о своём опыте", "расскажите об опыте", "расскажите про опыт",
    "опишите ваш опыт", "опишите свой опыт", "опишите свой релевантный",
    "опишите ваш релевантный", "опишите опыт",
    "кратко расскажите", "кратко опишите", "кратко о себе",
    "укажите опыт", "укажите ваш опыт", "опишите релевантный опыт",
    # «при отклике …» — работодатель ждёт кастомное письмо
    "при отклике важно", "при отклике укажите", "при отклике опишите",
    "при отклике расскажите", "при отклике приложите", "при отклике добавьте",
    "при отклике напишите", "при отклике отправьте",
    "как откликнуться", "как откликнутся", "как отозваться",
    "в отклике важно", "в отклике укажите", "в отклике опишите",
    "в отклике расскажите", "в отклике напишите", "в отклике отправьте",
    # «в ответе / сопроводительном …» — кастомное письмо
    "в ответе напишите", "напишите в ответе", "в ответе укажите",
    "в сопроводительном напишите", "напишите в сопроводительном",
    "в сопроводительном укажите", "укажите в сопроводительном",
    "в письме напишите", "напишите в письме",
    # Фразы/кодовые слова (тест на внимательность)
    "напишите фразу", "укажите фразу", "напишите слово",
    "секретное слово", "кодовое слово", "секретный вопрос",
    "напишите в теме", "укажите в теме", "укажите в теме письма",
    "тема отклика", "тема письма", "название вакансии в теме",
    # Инструкции «начните письмо с …»
    "начните сопроводительное", "начните письмо", "начните отклик",
    "первой фразой", "первым предложением",
    # Внимательное чтение
    "прочитайте до конца", "прочитайте вакансию до конца",
    "внимательно прочитайте", "внимательно прочтите",
    # архитектурные решения / проекты с похожими задачами
    "опыт архитектурных решений", "архитектурных решений",
    "опишите архитектур", "расскажите про архитектур", "расскажите об архитектур",
    "приведите архитектур",
    "проекты с похожими задачами", "проектах с похожими задачами",
    "проекты со схожими задачами", "примеры релевантных проектов",
    "приведите проекты", "опишите проекты", "перечислите проекты",
    "укажите проекты",
]


# Фразы, указывающие что работа не чистая удалёнка (офис / на территории
# заказчика). HH-фильтр &schedule=remote ловит большую часть, но через
# «подходящие под резюме» и неаккуратную классификацию HH такие вакансии
# всё равно прорываются. Срабатывание → ручная очередь, не автоотклик.
ON_SITE_TRIGGERS = [
    # Прямой офис работодателя
    "работа в офисе", "работа из офиса", "работать в офисе",
    "только офис", "только из офиса", "офисный формат",
    "формат работы: офис", "график работы: офис",
    "присутствие в офисе", "обязательное присутствие",
    "5/2 в офисе", "5 дней в офисе",
    # Аутстафф / на территории клиента
    "на территории заказчика", "у заказчика на территории",
    "в офисе заказчика", "в офисе клиента",
    "на площадке заказчика", "на площадке клиента",
]


def on_site_triggers(description: str, title: str = "") -> list[str]:
    """Совпавшие офисные триггеры (не удалёнка). См. SKIP_ON_SITE."""
    text = (description or "").lower() + " " + (title or "").lower()
    return [t for t in ON_SITE_TRIGGERS if t in text]


def needs_manual_review(description: str, title: str = "") -> list[str]:
    """Возвращает список совпавших триггеров. Пустой список = можно автоотклик.

    Офисные триггеры сюда НЕ входят — они обрабатываются отдельно (см.
    on_site_triggers / SKIP_ON_SITE): при SKIP_ON_SITE=True офис идёт в skip,
    иначе добавляется к ручной очереди на этапе обработки вакансии.
    """
    text = (description or "").lower() + " " + (title or "").lower()
    return [t for t in MANUAL_TRIGGERS if t in text]


# Если в названии вакансии явно указана роль, на которую соискатель не претендует,
# вакансия пропускается полностью (не отклик, не ручная очередь).
# Матчим только по title, чтобы упоминание DevOps/QA/DS в описании AI-вакансии
# не приводило к ложному скипу.
WRONG_ROLE_TITLE_TRIGGERS = [
    # DevOps / SRE / Infra
    "devops", "dev ops", "site reliability", " sre ", "sre engineer", "sre lead",
    "infrastructure engineer", "infra engineer",
    "системный администратор", "сисадмин",
    # Data Science / Analytics
    "data scientist", "data science", "дата-сайентист", "дата сайентист",
    "data analyst", "аналитик данных",
    # QA / Testing
    "qa engineer", "qa-engineer", "qa lead", "qa инженер", "quality assurance",
    "automation qa", "qa automation", "manual qa", "sdet", "aqa",
    "test engineer", "test automation", "qa", 
    "тестировщик", "инженер по тестированию",
    # Преподаватели / менторы (не наша роль)
    "преподаватель", "teacher", "lecturer", "instructor",
    # Frontend (не backend/python)
    "frontend", "front-end", "фронтенд", "front end",
    # Аналитики (не data science)
    "аналитик", "analyst",
    # Тренеры / преподаватели (альтернативные названия)
    "тренер", "trainer", "coach", "coach ",
    # Продажи (не наша роль)
    "менеджер по продажам", "sales manager", "sales director",
]


def wrong_role_match(title: str) -> str:
    """Возвращает первый совпавший триггер чужой роли или пустую строку."""
    t = " " + (title or "").lower() + " "
    for trig in WRONG_ROLE_TITLE_TRIGGERS:
        if trig in t:
            return trig.strip()
    return ""


def has_manual_title_trigger(title: str) -> str:
    """Возвращает первый совпавший триггер для ручной очереди или пустую строку."""
    t = " " + (title or "").lower() + " "
    for trig in MANUAL_TITLE_TRIGGERS:
        if trig in t:
            return trig.strip()
    return ""


# Языки/домены, которые сами по себе делают вакансию таргетной (без
# обязательного position-маркера). Python и автоматизация — наш core.
LANGUAGE_TARGET_MARKERS = [
    "python", "питон",
    "автоматизац", "automation", "автоматизатор",
]

# AI/LLM маркеры в названии. Сами по себе НЕ делают вакансию таргетной —
# должны идти вместе с position- или infra-маркером (см. is_target_role).
# Иначе — ручная очередь («ai-only-title»).
AI_LLM_TITLE_MARKERS = [
    " ai ", "ai-", "/ai", "(ai", "ai/", "(ai)",
    "llm", "llmops", "rag",
    "генеративн", "нейросет",
    " ии ", " ии,", " ии)", "(ии ", " ии-", "по внедрению ии",
    "искусственн",
    "промпт", "prompt engineer", "prompt-engineer",
    "genai", "gen ai", "generative ai",
]

# Position-маркеры — указывают на «ролевую» (IC/Lead) вакансию.
# В паре с AI/LLM-маркером дают автоотклик.
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
    "trainer", "тренер",
    " product ", "продакт",
]

# Infra/Platform-маркеры — в паре с AI/LLM дают автоотклик
# (AI Infrastructure, LLM Platform и т.п.).
INFRA_TITLE_MARKERS = [
    "infrastructure", "инфраструктур",
    " infra", "-infra", "/infra",
    " инфра", "-инфра",
    "platform", "платформ",
]


def is_ai_target_role(title: str) -> bool:
    """AI/LLM маркер в паре с позицией или инфра-словом → автоотклик.
    Этот таргет перебивает wrong_role (для случаев типа AI Infrastructure Engineer)."""
    t = " " + (title or "").lower() + " "
    has_ai = any(m in t for m in AI_LLM_TITLE_MARKERS)
    if not has_ai:
        return False
    has_position = any(m in t for m in POSITION_TITLE_MARKERS)
    has_infra = any(m in t for m in INFRA_TITLE_MARKERS)
    return has_position or has_infra


def is_language_target_role(title: str) -> bool:
    """Python / automation / автоматизатор сами по себе. Применяется ТОЛЬКО
    после wrong_role-фильтра, иначе ловит SDET Python, QA Engineer (Python) и т.п."""
    t = " " + (title or "").lower() + " "
    return any(m in t for m in LANGUAGE_TARGET_MARKERS)


def is_target_role(title: str) -> bool:
    """Совместимость: True, если автоотклик уместен по any-маркеру.
    Для приоритетной логики используй is_ai_target_role + is_language_target_role
    отдельно (см. основной цикл)."""
    return is_ai_target_role(title) or is_language_target_role(title)


def has_ai_marker(title: str) -> bool:
    """AI/LLM в title есть, но position/infra нет → ручная очередь."""
    t = " " + (title or "").lower() + " "
    return any(m in t for m in AI_LLM_TITLE_MARKERS)


# Если ни target, ни ai-only — но в названии явно фигурирует ML, идём
# в ручную очередь (вдруг в описании есть AI/LLM/Platform-контекст).
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


# ============== КОД ==============

# Anthropic SDK не умеет SOCKS-прокси в all_proxy — убираем перед клиентом,
# но сохраняем значение для Playwright (перенесено из archive/ в qwen-branch).
_SAVED_ALL_PROXY = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or ""
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)

def launch_browser(p, headless: bool = True):
    """Chromium с общими настройками для ботов и дашборда: системный бинарь,
    если встроенный в Playwright не установлен, флаги sandbox и прокси."""
    kwargs: dict = {
        "headless": headless,
        "slow_mo": 0 if headless else 300,
        "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    }
    exe = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if not exe and not os.path.exists(p.chromium.executable_path):
        exe = shutil.which("chromium") or shutil.which("chromium-browser") or ""
    if exe:
        kwargs["executable_path"] = exe
    proxy = os.getenv("HH_PROXY") or _SAVED_ALL_PROXY or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        kwargs["proxy"] = {"server": proxy}
        print(f"🌐 Прокси: {proxy}")
    return p.chromium.launch(**kwargs)


# Anthropic клиент
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# OpenRouter клиент (OpenAI-совместимый API)
openrouter_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)


def generate_cover_letter(title: str, employer: str, description: str, vacancy_url: str = "") -> str:
    """Генерирует сопроводительное письмо через OpenRouter (Gemma 4 31B)"""
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
        if _metrics:
            usage = getattr(response, "usage", None)
            _metrics.log_event(
                kind="apply",
                model=OPENROUTER_MODEL,
                tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
                vacancy=f"{title} · {employer}",
                chat_url=vacancy_url,
                payload={"letter": text, "vacancy_url": vacancy_url, "title": title, "employer": employer},
            )
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
    """page.goto() с retry при навигационных ошибках (ERR_CONNECTION_CLOSED,
    Navigation interrupted). Между попытками — пауза, чтобы браузер успел
    стабилизироваться."""
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
            # Закрываем вкладку ошибки если браузер открыл chrome-error://
            try:
                if page.url.startswith("chrome-error://"):
                    page.goto("about:blank", wait_until="load", timeout=5000)
            except Exception:
                pass


class StopRun(Exception):
    """Прервать весь прогон откликов (капча, закрылось рабочее окно)."""


def search_vacancies(page, query: str = "", page_num: int = 0, resume_id: str = "") -> list:
    """Ищет вакансии на HH.ru.

    Если задан resume_id, использует персональную выдачу HH «подходящие
    под резюме» (URL вида ?resume=ID&from=resumelist). Иначе обычный
    text-поиск с фильтрами area/schedule/experience.
    """
    if resume_id:
        url = (
            f"https://hh.ru/search/vacancy?resume={resume_id}&from=resumelist"
            "&schedule=remote"
            "&experience=between3And6&experience=moreThan6"
            "&search_period=3"
            "&order_by=publication_time"
            f"&items_on_page=50&page={page_num}"
        )
    else:
        url = (
            f"https://hh.ru/search/vacancy?text={quote(query, safe='')}"
            "&area=113"
            "&schedule=remote"
            "&experience=between3And6&experience=moreThan6"
            "&search_period=3"
            "&order_by=publication_time"
            f"&items_on_page=50&page={page_num}"
        )
    
    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=30000)

        # Проверка на капчу
        if "captcha" in page.title().lower():
            # Продолжать обход после капчи — прямой путь к бану
            raise StopRun("капча на странице поиска")
        
        # Ждём загрузки вакансий. Если карточек нет — это пустая страница
        # (за пределами пагинации или узкий запрос), а не ошибка: тихо возвращаем 0.
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
            except:
                continue
        
        return vacancies
    except StopRun:
        raise
    except Exception as e:
        print(f"  ⚠️  Ошибка поиска: {e}")
        return []


def get_vacancy_description(page, url: str) -> str:
    """Получает описание вакансии"""
    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=15000)
        if "captcha" in page.title().lower():
            raise StopRun("капча на странице вакансии")
        page.wait_for_selector("[data-qa='vacancy-description']", timeout=10000)
        
        desc_el = page.locator("[data-qa='vacancy-description']")
        return desc_el.inner_text() if desc_el.count() > 0 else ""
    except StopRun:
        raise
    except Exception:
        return ""


def _has_screening_questions(page) -> bool:
    """True если HH перевёл на форму со скрининг-вопросами от работодателя.

    Признаки:
    - URL содержит startedWithQuestion (любое значение — вопросы есть);
    - на странице видны контейнеры вопросов или фраза «Вопросы от работодателя».
    """
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


def _apply_confirmed(page) -> bool:
    return any(
        page.locator(f"text={t}").count() > 0
        for t in ("Вы откликнулись", "Резюме доставлено", "Отклик отправлен")
    )


def _apply_outcome(page, reason: str) -> dict:
    """success — только если HH подтвердил отклик; иначе unconfirmed → ручная очередь."""
    if _apply_confirmed(page):
        return {"status": "success", "reason": reason}
    return {"status": "unconfirmed", "reason": f"{reason}: нет подтверждения HH"}


def _submit_button(page, field=None):
    """Кнопка отправки — только в окне отклика или в ближайшем к полю письма
    контейнере с кнопкой (встроенная форма). Поиск по всей странице мог нажать
    «Откликнуться» в блоке похожих вакансий."""
    exact = page.locator("button[data-qa='vacancy-response-submit-popup']")
    if exact.count() > 0:
        return exact.first
    scopes = [page.locator("[role='dialog'], [data-qa='modal-overlay']").last]
    if field is not None:
        scopes.append(field.locator("xpath=ancestor::*[.//button][1]"))
    for scope in scopes:
        if scope.count() == 0:
            continue
        btn = scope.locator(
            "button:has-text('Отправить'), button:has-text('Откликнуться')"
        ).first
        if btn.count() > 0:
            return btn
    return None


def apply_to_vacancy(page, url: str, message: str) -> dict:
    """Откликается на вакансию с сопроводительным письмом"""
    try:
        _safe_goto(page, url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        # Уже откликались?
        if page.locator("text=Вы откликнулись").count() > 0:
            return {"status": "skipped", "reason": "Уже откликались"}

        # Вакансия с вопросами скрининга — стандартное письмо не подойдёт,
        # отправляем в ручную очередь с пометкой.
        if _has_screening_questions(page):
            return {"status": "has_questions", "reason": "Скрининг-вопросы от работодателя"}
        
        # СПОСОБ 1: Ищем ссылку "Написать сопроводительное" 
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
                
                submit_btn = _submit_button(page, letter_area)
                if submit_btn:
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    return _apply_outcome(page, "С письмом")
        
        # СПОСОБ 2: Кнопка с выпадающим меню
        apply_btn = page.locator("[data-qa='vacancy-response-link-top']")
        if apply_btn.count() == 0:
            apply_btn = page.locator("[data-qa='vacancy-response-link-bottom']")
        
        if apply_btn.count() > 0 and message:
            dropdown = page.locator("[data-qa='vacancy-response-link-top'] ~ button, button[data-qa='vacancy-response-actions-dropdown']").first
            if dropdown.count() > 0:
                print("      🔍 Нашёл выпадающее меню")
                dropdown.click()
                page.wait_for_timeout(1000)
                
                with_letter = page.locator("text=С сопроводительным, text=сопроводительным письмом").first
                if with_letter.count() > 0:
                    print("      🔍 Нашёл опцию 'С сопроводительным'")
                    with_letter.click()
                    page.wait_for_timeout(2000)
                    
                    letter_area = page.locator("textarea").first
                    if letter_area.count() > 0:
                        print("      ✍️  Заполняю письмо...")
                        letter_area.fill(message)
                        page.wait_for_timeout(500)
                        
                        submit_btn = _submit_button(page, letter_area)
                        if submit_btn:
                            submit_btn.click()
                            page.wait_for_timeout(3000)
                            return _apply_outcome(page, "С письмом (меню)")
        
        # СПОСОБ 3: Просто нажать "Откликнуться" и потом добавить письмо
        if apply_btn.count() > 0:
            print("      🔍 Жму основную кнопку 'Откликнуться'")
            apply_btn.first.click()
            page.wait_for_timeout(3000)

            # после клика HH мог перевести на форму вопросов
            if _has_screening_questions(page):
                return {"status": "has_questions", "reason": "Скрининг-вопросы от работодателя"}

            # Проверяем, появилось ли всплывающее окно с кнопкой "Добавить сопроводительное"
            add_letter_btn = page.locator("button:has-text('Добавить сопроводительное')")
            if add_letter_btn.count() == 0:
                add_letter_btn = page.locator("button:has-text('Добавить сопроводительное письмо')")
            if add_letter_btn.count() > 0:
                print("      🔍 Нашёл кнопку 'Добавить сопроводительное'")
                add_letter_btn.first.click()
                page.wait_for_timeout(2000)

            # Ждём появления textarea с письмом (HH может подгружать динамически)
            page.wait_for_timeout(2000)
            
            # Пробуем несколько селекторов для textarea
            letter_area = None
            selectors = [
                "textarea[placeholder*='сопроводительное']",
                "textarea[name*='letter']",
                "textarea[data-qa*='letter']",
                "div[contenteditable='true']",
                "textarea",  # fallback
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
                
                # Фокусируемся на поле
                try:
                    letter_area.focus()
                    page.wait_for_timeout(300)
                except Exception as e:
                    print(f"      ⚠️  Не удалось сфокусироваться: {e}")
                
                # Очищаем поле (если там что-то есть)
                try:
                    letter_area.press("Control+a")
                    page.wait_for_timeout(200)
                    letter_area.press("Backspace")
                    page.wait_for_timeout(200)
                except Exception as e:
                    print(f"      ⚠️  Не удалось очистить: {e}")
                
                # Вставляем текст
                print(f"      📝 Вставляю письмо ({len(message)} символов)...")
                try:
                    letter_area.fill(message)
                    page.wait_for_timeout(500)
                    
                    # Проверяем, что текст записался
                    actual_value = letter_area.input_value(timeout=1000)
                    if actual_value != message:
                        print(f"      ⚠️  Текст не записался полностью (записано {len(actual_value)}/{len(message)})")
                        # Пробуем ещё раз методом press
                        letter_area.press("Control+a")
                        letter_area.press("Backspace")
                        for idx, char in enumerate(message, 1):
                            letter_area.press(char)
                            if idx % 50 == 0:
                                page.wait_for_timeout(50)
                        page.wait_for_timeout(500)
                        actual_value = letter_area.input_value(timeout=1000)
                    
                    if actual_value == message:
                        print("      ✅ Текст записан успешно")
                    else:
                        print(f"      ⚠️  Текст записан частично: {len(actual_value)}/{len(message)}")
                        
                except Exception as e:
                    print(f"      ❌ Ошибка при заполнении: {e}")
                    # Альтернативный метод: keyboard type
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
                submit_btn = _submit_button(page, letter_area)
                if submit_btn:
                    print("      📨 Нажимаю кнопку отправки...")
                    submit_btn.click()
                    page.wait_for_timeout(3000)
                    return _apply_outcome(page, "С письмом")
                if _apply_confirmed(page):
                    # основная кнопка уже доставила отклик, письмо приложить не вышло
                    return {"status": "success", "reason": "Без письма (кнопка письма не найдена)"}
                print("      ❌ Кнопка отправки в окне отклика не найдена")
                return {"status": "error", "reason": "no_submit_button"}

            return _apply_outcome(page, "Без письма")
        
        return {"status": "error", "reason": "Кнопка не найдена"}
        
    except Exception as e:
        return {"status": "error", "reason": str(e)}


_log_fh = None


def main():
    global _log_fh
    dry_run = "--dry-run" in sys.argv

    # Создаём PID-файл для дашборда
    pid_file = Path("/tmp/hh_auto_apply.pid")
    pid_file.write_text(str(os.getpid()))

    # Лог открываем один раз на процесс: watch-итерации дописывают, а не затирают
    if _log_fh is None:
        _log_fh = open(Path("/tmp/hh_auto_apply.log"), "w", buffering=1)
        sys.stdout = _log_fh
        sys.stderr = _log_fh

    print("\n" + "="*50)
    print("🚀 HH.ru Автооткликатор" + (" [DRY-RUN]" if dry_run else ""))
    print("="*50)

    # Проверка настроек (LLM откликам не нужен — письмо шаблонное)
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
    print(f"⏱️  Пауза между откликами: {DELAY_APPLY_MIN}–{DELAY_APPLY_MAX} сек (рандом)")
    print(f"🗓️  Рабочие часы: {WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00 · суточный лимит: {MAX_APPLIES_PER_DAY}")
    
    stats = {"success": 0, "skipped": 0, "error": 0}

    metrics_sink = None if dry_run else _metrics
    applied_index = _metrics.load_applied_index() if _metrics else {"by_vacancy_id": {}, "by_employer_title": {}}

    def _remember_in_index(v: dict) -> None:
        """Добавляет вакансию в in-memory applied_index, чтобы не плодить дубли в одном прогоне."""
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

    headless = dry_run or os.getenv("HH_HEADLESS", "").lower() in ("1", "true", "yes")

    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()
        
        sources = [
            {"label": f"🔍 Поиск: {q}", "query": q, "source": f"query:{n}"}
            for n, q in enumerate(SEARCH_QUERIES, 1)
        ]
        if RESUME_ID:
            sources.append({
                "label": f"🎯 HH-рекомендации по резюме {RESUME_ID[:8]}…",
                "resume_id": RESUME_ID,
                "source": "resume",
            })

        try:
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
                    )
                    print(f"  📊 Найдено вакансий: {len(vacancies)}")
                    # query:N — номер в SEARCH_QUERIES, resume — выдача «по резюме»
                    for v in vacancies:
                        v["source"] = src["source"]
                
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

                        # Приоритет фильтров (строго в этом порядке):
                        # 1) AI/LLM + позиция/инфра — таргет, перебивает wrong_role
                        # 2) ML-маркер — ручная очередь, перебивает wrong_role
                        # 3) Data/Database/Analytics/Researcher — ручная очередь
                        # 4) wrong_role — скип
                        # 5) Python/automation сами по себе — таргет
                        # 6) AI-only (без позиции) — ручная очередь
                        # 7) иначе — скип not_target_role
                        if not is_ai_target_role(vacancy['title']):
                            # Data/Database/Analytics/Researcher — в ручную очередь
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
                                            "source": vacancy.get("source", ""),
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
                                            "source": vacancy.get("source", ""),
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
                                            "source": vacancy.get("source", ""),
                                        },
                                    )
                                stats['skipped'] += 1
                                continue

                            if is_language_target_role(vacancy['title']):
                                pass  # Python/automation таргет — продолжаем обработку
                            elif has_ai_marker(vacancy['title']):
                                print("      🚩 AI/LLM без явной позиции, в ручную очередь")
                                if metrics_sink:
                                    metrics_sink.log_event(
                                        kind="pending_manual",
                                        vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                        chat_url=vacancy['url'],
                                        payload={
                                            "vacancy_url": vacancy['url'],
                                            "source": vacancy.get("source", ""),
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
                                            "source": vacancy.get("source", ""),
                                        },
                                    )
                                stats['skipped'] += 1
                                continue

                        description = get_vacancy_description(page, vacancy['url'])

                        on_site = on_site_triggers(description, vacancy['title'])
                        if on_site and SKIP_ON_SITE:
                            print(f"      🏢 Не удалёнка ({', '.join(on_site[:2])}) — пропускаю")
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
                                        "source": vacancy.get("source", ""),
                                        "triggers": on_site,
                                    },
                                )
                                _remember_in_index(vacancy)
                            stats['skipped'] += 1
                            continue

                        triggers = needs_manual_review(description, vacancy['title'])
                        # SKIP_ON_SITE=False: возвращаем офисные в ручную очередь (старое поведение).
                        if on_site and not SKIP_ON_SITE:
                            triggers = triggers + on_site
                        if triggers:
                            print(f"      🚩 В очередь на ручной отклик: {', '.join(triggers[:3])}")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "source": vacancy.get("source", ""),
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

                        if not dry_run and not _within_work_hours():
                            raise StopRun(f"рабочее окно {WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00 закрылось")

                        # Суточный лимит откликов (антибан по объёму): kind='apply'
                        # пишется после каждого успешного отклика, поэтому счётчик уже
                        # включает отклики этого прогона. Достигли потолка — дальше не откликаемся.
                        if not dry_run and _metrics and _metrics.count_applies_since(86400) >= MAX_APPLIES_PER_DAY:
                            print(f"      🛑 Суточный лимит {MAX_APPLIES_PER_DAY} откликов достигнут — пропускаю до завтра")
                            stats['skipped'] += 1
                            continue

                        letter, addendums = build_letter(description, vacancy['title'])
                        addendum_label = "+" + ",".join(addendums) if addendums else "стандарт"
                        print(f"      📝 Шаблон [{addendum_label}] · {len(letter)} символов")

                        if dry_run:
                            print("      🧪 [dry-run] отклик не отправлен, в БД не пишу")
                            stats['skipped'] += 1
                            time.sleep(1)
                            continue

                        print("      📤 Отправляю отклик...")
                        result = apply_to_vacancy(page, vacancy['url'], letter)

                        if result['status'] in ('success', 'unconfirmed'):
                            if result['status'] == 'success':
                                print(f"      ✅ Успех! ({result['reason']})")
                                stats['success'] += 1
                            # Кнопка нажата — отклик мог уйти: считаем его в суточный лимит и дедуп,
                            # неподтверждённый дополнительно уходит на проверку в ручную очередь ниже.
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="apply",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "letter": letter,
                                        "vacancy_url": vacancy['url'],
                                        "source": vacancy.get("source", ""),
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "type": "standard" if not addendums else "standard+addendums",
                                        "addendums": addendums,
                                        "send_result": result['reason'],
                                        "confirmed": result['status'] == 'success',
                                    },
                                )
                                _remember_in_index(vacancy)
                        if result['status'] == 'success':
                            pass
                        elif result['status'] == 'unconfirmed':
                            print(f"      ❓ {result['reason']} — в ручную очередь, проверь на HH")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "source": vacancy.get("source", ""),
                                        "title": vacancy['title'],
                                        "employer": vacancy['employer'],
                                        "triggers": ["unconfirmed_apply"],
                                        "manual_note": "кнопка нажата, но HH не подтвердил отклик — проверь вакансию",
                                        "description_preview": (description or "")[:600],
                                    },
                                )
                                _remember_in_index(vacancy)
                            stats['skipped'] += 1
                        elif result['status'] == 'skipped':
                            print(f"      ⏭️  Пропущено: {result['reason']}")
                            stats['skipped'] += 1
                        elif result['status'] == 'has_questions':
                            print(f"      🚩 {result['reason']} — в ручную очередь")
                            if metrics_sink:
                                metrics_sink.log_event(
                                    kind="pending_manual",
                                    vacancy=f"{vacancy['title']} · {vacancy['employer']}",
                                    chat_url=vacancy['url'],
                                    payload={
                                        "vacancy_url": vacancy['url'],
                                        "source": vacancy.get("source", ""),
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

        except StopRun as e:
            print(f"\n🛑 Прогон остановлен: {e}")
            if _metrics:
                _metrics.log_event(kind="error", payload={"where": "apply_run", "error": str(e)})
        browser.close()
    
    print("\n" + "="*50)
    print("📊 ИТОГИ:")
    print(f"   ✅ Успешно: {stats['success']}")
    print(f"   ⏭️  Пропущено: {stats['skipped']}")
    print(f"   ❌ Ошибок: {stats['error']}")
    print("="*50 + "\n")


def watch_loop() -> None:
    interval = int(os.getenv("HH_APPLY_INTERVAL_SEC", "14400"))  # 4 часа (базовый)
    print(f"👀 WATCH-режим откликов, интервал ~{interval} сек ±{int(APPLY_INTERVAL_JITTER*100)}%. Ctrl-C, чтобы остановить.\n")
    if _metrics:
        _metrics.log_event(kind="run_start", payload={"mode": "apply_watch", "interval_sec": interval})
    try:
        while True:
            # Гейт 1: рабочие часы — массовые отклики ночью палят бота.
            if not _within_work_hours():
                w = _seconds_until_work_hours()
                print(f"🌙 Вне рабочих часов {WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00 — сплю {w // 60} мин до утра...\n")
                time.sleep(w)
                continue
            # Гейт 2: суточный лимит откликов.
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
            # Джиттер интервала: обходы не по ровному таймеру (±APPLY_INTERVAL_JITTER).
            nap = int(interval * random.uniform(1 - APPLY_INTERVAL_JITTER, 1 + APPLY_INTERVAL_JITTER))
            print(f"\n💤 Жду {nap} сек до следующего обхода вакансий...\n")
            time.sleep(nap)
    except KeyboardInterrupt:
        print("\n🛑 Остановлено пользователем.")
        if _metrics:
            _metrics.log_event(kind="run_end", payload={"reason": "keyboard_interrupt", "mode": "apply_watch"})


if __name__ == "__main__":
    # Lock берём один раз на весь процесс, чтобы watch-итерации не блокировали
    # сами себя (flock — на open file description, а не на pid).
    _process_lock = _acquire_singleton_lock("hh_auto_apply")
    if _process_lock is None:
        print("\n❌ Уже работает другой экземпляр auto_apply (lock занят). Выхожу.")
        sys.exit(1)
    if "--watch" in sys.argv:
        watch_loop()
    else:
        main()
