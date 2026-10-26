# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

hh-auto-apply — личный инструмент для поиска работы на HH.ru: поиск вакансий, отклики с шаблонным письмом, LLM-ответы работодателям в чате, подъём резюме, веб-дашборд.
Развивается как коммерческий продукт для других соискателей (решение 2026-10-26): в коде и шаблонах по умолчанию — никаких данных владельца, всё личное настраивается через раздел «Резюме». Сейчас бот работает с реальным аккаунтом HH владельца.
Ищем **только удалёнку**: офисные вакансии (`ON_SITE_TRIGGERS`) сразу skip при `HH_SKIP_ON_SITE=1` (дефолт).

Документация, комментарии и сообщения в коде — на русском.

## Архитектура

- `dashboard.py` — FastAPI; страница — `dashboard.html` (HTML/CSS/JS, читается на каждый `GET /`, правки видны без перезапуска), `http://127.0.0.1:8765` (`HH_DASHBOARD_HOST`/`HH_DASHBOARD_PORT`). Запускает воркеров через `subprocess.Popen` с `--watch` и управляет ими: `POST /api/{apply|reply|boost}/{start|stop|restart}`, `GET /api/status`, `/api/events`, `/api/log/{name}`. Очереди: `/api/pending/*` (ручной разбор вакансий), `/api/positive/*` (приглашения/контакты), `/api/robot-questionnaires/*` (анкеты автоботов), плюс `/api/rejections`, `/api/conversion`, `/api/cooldown-savings`, `/api/chat-history`.
- `auto_apply_template.py` — воркер `apply`: поиск по `SEARCH_QUERIES` (`&schedule=remote`) + выдача «по резюме» (`HH_RESUME_ID`, **без** фильтра remote — открытый хвост, ~строка 433), фильтры/триггеры ручной очереди, генерация письма, отклик. Рабочее окно `HH_WORK_HOUR_START`–`HH_WORK_HOUR_END`, лимит `HH_MAX_APPLIES_PER_DAY`, джиттер интервалов (антибан). Здесь же `load_dotenv()`, общие константы и LLM-клиенты — их импортируют `auto_reply.py` и `dashboard.py`.
- Отсев вакансий в `auto_apply_template.py`: `title_route(title)` — решение по названию (первым — чужой основной язык без Python: .NET/C#/C++/Java/Kotlin/Go/PHP/Ruby/Scala/1С → пропуск `lang:…`, `foreign_lang_match()`; AI+должность → дальше; чужая роль → пропуск, кроме «чужая роль + ML/AI» → очередь `mixed-role`; data/аналитика → очередь; ML без AI → разбор описания); `classify_ml_vacancy(description)` — для ML-названий считает признаки AI-инженера / ML-инженера / DS (`ML_SIGNALS`): AI перевешивает → отклик, DS или обучение моделей/MLOps/рекомендательные системы → пропуск, ничья → очередь `ml-unclear`. Отчёт — `/tmp/hh_ml_classify.jsonl`. `HH_ML_CLASSIFY=0` выключает разбор. Ручная очередь в дашборде — только за `HH_PENDING_MAX_AGE_DAYS` (14) дней.
- `auto_reply.py` — воркер `reply`: обход чатов HH, ответы через LLM, детекторы ролей (`_detect_role`, `_text_is_own`), анкет автоботов (`is_robot_questionnaire`), отказов (`is_rejection`), положительных сигналов (`detect_positive_signal`), cooldown `HH_REPLY_COOLDOWN_SEC`.
- `resume_boost.py` — воркер `boost`: подъём резюме по таймеру.
- `metrics.py` — SQLite `~/.n8n-files/hh_metrics.sqlite` (каталог — `N8N_FILES_DIR`), таблица `events` (`kind`, `model`, токены, `cost_usd`, `vacancy`, `chat_url`, `payload_json`). Все воркеры и дашборд пишут/читают её.
- `notify.py` — Telegram-уведомления (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), без них — no-op.
- `hh_login.py` — ручной логин в видимом браузере (`launch_browser`), сохраняет сессию в `~/.n8n-files/hh_session.json` сам, как только в шапке нет видимой «Войти» (без Enter — работает через `!` в Claude Code). Боты перед обходом вызывают `hh_logged_in(page)` (True / False / None — HH не открылся): при False останавливаются с `SESSION_EXPIRED`. HH не уводит гостя на страницу входа — признак гостя только видимая «Войти» в шапке.
- `profile_store.py` — профиль соискателя: `load()`/`validate()`/`save()`, хранится в `profile.json` (права 600, в `.gitignore`), его пишет раздел «Резюме» (`GET/POST /api/profile`). Пока json нет — читается `profile.py` (из `profile.example.py`). Поля: `resume_id`, `search_queries`, `letter_body`, `letter_signature`, `salary_addendum`, `my_profile`, `self_name_markers`, `own_text_markers`. `HH_RESUME_ID` в `.env` перекрывает `resume_id`. Боты читают профиль при старте — после сохранения их нужно перезапустить; дашборд перечитывает сам (`reload_profile()`).
- Разовые скрипты: `migrate_history_roles.py` (`--days N`, запись только с `--apply`), `backfill_positive.py` (`--dry-run`, `--headed`, `--limit N`).
- `REVIEW.md` — код-ревью от Qwen, все пункты закрыты (2026-10-26). `SYSTEM_PROMPT.md` — промпт для Claude Projects, к коду не относится.
- Фоновый запуск: дашборд от `make start` (`nohup`, свой сеанс), воркеры — его дети с `start_new_session`; закрытие терминала/Claude их не трогает. Чтобы не зависеть от cgroup вкладки терминала, процессы можно перенести в `hh-bots.scope` (`busctl --user … StartTransientUnit`); без linger (`Linger=no`) выход из сеанса GNOME и перезагрузка их останавливают.
- Состояние процессов: PID и логи воркеров — `/tmp/hh_{auto_apply,auto_reply,resume_boost,dashboard}.{pid,log}`, lock-файлы — `~/.n8n-files/*.lock`. Каталог `logs/` — остатки ручных запусков.

**LLM (с коммита `6011991`):** ответы в чате (**только `auto_reply.py`**) идут через **OpenRouter** (OpenAI SDK, `OPENROUTER_API_KEY` из `.env`, модель `OPENROUTER_MODEL`, дефолт `google/gemma-4-31b-it:free`). Отклики шлют шаблон `build_letter()`; `generate_cover_letter()` нигде не вызывается, `anthropic_client` не используется. Ключ нужен только воркеру `reply`. README и SETUP_GUIDE описывают Claude-версию, отличия этой ветки — в шапке README.

**Ветки:** `main` — основная версия на Claude (Anthropic SDK, `claude-haiku-4-5-20251001`); `qwen-branch` (эта) — запасной вариант на OpenRouter. Общие исправления (браузер, антибан, boost, дашборд) делать в `main` и переносить сюда через `cherry-pick`.

## Команды

Python 3.13, venv `.venv/` (pip, без `pyproject.toml`/`requirements.txt`): `playwright python-dotenv anthropic openai fastapi uvicorn`. Секреты — в `.env`.

```bash
make start | stop | restart | status | logs   # дашборд (HH_HEADLESS=1), воркеры стартуют кнопками/API
./start_bots.sh                               # dashboard + boost + reply через setsid (apply НЕ запускает); системный Chromium, без all_proxy

.venv/bin/python hh_login.py                  # обновить сессию HH (видимый браузер)
.venv/bin/python auto_apply_template.py --dry-run     # прогон без отклика (headless)
.venv/bin/python -m unittest tests.test_detectors -v  # 26 тестов детекторов, внешние зависимости замоканы в tests/conftest.py
```

Линтер — `ruff check .` (правила `E`, `F`, `I`, `W`, двойные кавычки), в `.venv` не установлен. Коммиты — Conventional Commits (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `style`).

## Правила работы

**Экономия токенов.** Отвечать только на заданный вопрос, без вступлений, итогов и расширения задачи. Без альтернатив и лекций о best practices. В коде показывать только изменённые блоки (`# ... existing code ...` для пропусков). Docstring на тривиальных функциях не нужны; комментарии только для неочевидной логики. Если ответ не помещается — вернуть `<<< PARTIAL: <причина>` последней строкой.

**Контракты вывода по режиму** (режим определяется по запросу; неоднозначный запрос = CODE):

- CODE (реализация, фикс, рефакторинг): `>>> CHANGE: <путь к файлу>` → diff или замена → `<<< END`. Однострочное пояснение только если решение неочевидно.
- PLAN: нумерованный список до 10 шагов, по одной строке, без прозы вокруг.
- AUDIT (ревью, проверка): `ISSUE: <что не так и где> / FIX: <конкретное действие>`, только P0/P1.
- CHANGELOG: `type(scope): change`, последние 20 записей.

**Границы задачи.**

- Работать в рамках текущего файла: без межфайлового рефакторинга.
- Не создавать новые файлы (`.md`/`.txt`/`.json`/`.yaml`) без явной просьбы.
- Не запускать git-команды, не делать коммиты без явной просьбы. Не писать тесты без разрешения.
- Не править архив: `archive/auto_apply_template_letters.py` (старый вариант `auto_apply_template.py` с шаблонными письмами и поиском hybrid/office).
- Не запускать воркеров и скрипты, которые ходят на HH, без просьбы: отклики и сообщения в чате уходят с реального аккаунта, отозвать их нельзя, частые прогоны — риск бана. Для проверки — `--dry-run`.
- Не вызывать модели (OpenRouter, Anthropic) без просьбы: это платные запросы.
- Не менять записи в `hh_metrics.sqlite` (миграции, `--apply`) без просьбы и без бэкапа.
- Секреты и личные данные — только в `.env` и `profile.py` (оба в `.gitignore`), в код их не переносить.

**Стек для нового кода.** Python 3.13+, типы по PEP 484, Playwright (sync API), FastAPI + uvicorn, SQLite через `metrics.py`, OpenAI SDK → OpenRouter, `python-dotenv`. Пока ничего сверх этого в проект не добавлять.

**Достоверность.** Утверждения о коде, метриках и состоянии воркеров подтверждать файлами или результатами инструментов (`make status`, `/api/status`, SQLite). Непроверенное — убрать или вернуть `<<< PARTIAL` с причиной. Не выдавать догадки за факты. Если нужного инструмента нет в среде — написать `ERROR: Tool '<имя>' not in runtime` и остановиться, не имитируя вызов.

**Контекст.** За один ход подгружать не более одного внешнего файла, если пользователь не указал иное. Большие файлы (`dashboard.py`, `auto_reply.py`, `auto_apply_template.py`) читать диапазонами. `.env`, `profile.py`, `profile.json` и `~/.n8n-files/` не читать без необходимости.
