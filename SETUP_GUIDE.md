# SETUP GUIDE

Полная установка hh-auto-apply с нуля.

## Что нужно для работы

1. **Python 3.10+**
2. **Anthropic API ключ** — для Claude Haiku 4.5 (~$0.001–0.003 за отклик/ответ)
3. **Аккаунт на HH.ru** с заполненным резюме
4. **Linux / macOS / Windows** (на Windows — через WSL2 для удобства, чтобы Playwright работал стабильно)
5. ~500 МБ дискового места (Chromium для Playwright + venv)

---

## Шаг 1. Клонировать репозиторий

```bash
git clone https://github.com/semernyakov/hh-auto-apply.git
cd hh-auto-apply
```

## Шаг 2. Установить зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install playwright python-dotenv anthropic fastapi uvicorn
python -m playwright install chromium
```

## Шаг 3. Создать `profile.py`

Это **ваш личный** файл (в `.gitignore`, в публичный репо не попадает):

```bash
cp profile.example.py profile.py
```

Откройте `profile.py` и заполните:

- `SEARCH_QUERIES` — 5–30 ключевых запросов для HH (например, `"Python Developer"`, `"AI Engineer"`).
- `MY_PROFILE` — текстовый «резюме-документ» для Claude: имя, опыт, проекты с метриками, стек, контакты. Чем конкретнее — тем человечнее ответы в чатах.
- `SELF_NAME_MARKERS` — варианты вашего ФИО в нижнем регистре (`"иван иванов"`, `"иванов"`). Нужно для определения, кто прислал сообщение в чате, когда HH не возвращает явный author.
- `SALARY_ADDENDUM` — фраза о ЗП, добавляется в письмо, если вакансия требует.
- `LETTER_SIGNATURE` — подпись в письмах (имя, контакты).

## Шаг 4. Anthropic API ключ

1. Получите ключ на <https://console.anthropic.com/settings/keys>
2. Добавьте в окружение (или в `.env`):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Для пополнения баланса достаточно $5 — этого хватит на ~2000–5000 откликов и ответов.

## Шаг 5. Авторизация на HH.ru

```bash
python hh_login.py
```

Откроется браузер. Залогиньтесь, при необходимости пройдите 2FA, дождитесь главной страницы HH. Сессия сохранится в `hh_session.json` (тоже в `.gitignore`).

> [!TIP]
> Раз в 1–2 недели сессия может протухнуть — просто повторите `python hh_login.py`.

## Шаг 6. Запуск

### Через дашборд (рекомендуется)

```bash
make start
```

Откройте <http://127.0.0.1:8765>. На дашборде кнопками «Старт/Стоп/Рестарт» управляйте тремя воркерами:

| Воркер | Что делает |
|---|---|
| **apply** | Ищет вакансии и откликается с письмом |
| **reply** | Отвечает на сообщения работодателей в чате |
| **boost** | Поднимает резюме в поиске (cooldown ~ 4 ч между подъёмами) |

Управление через `make`:

```bash
make start    # запустить дашборд (HH_HEADLESS=1)
make stop     # остановить дашборд и всех воркеров
make restart  # stop + start
make status   # показать что запущено
make logs     # tail -f лога дашборда
```

### Вручную (без дашборда)

```bash
# одноразовый прогон
python auto_apply_template.py

# постоянный watch с интервалами
python auto_apply_template.py --watch
python auto_reply.py --watch
python resume_boost.py --watch

# dry-run: всё сгенерировать, но не отправлять
python auto_apply_template.py --dry-run
```

---

## Конфигурация через environment

Все настройки можно подкрутить переменными окружения (см. также таблицу в [README.md](README.md)):

```bash
export HH_HEADLESS=1                    # запуск без видимого окна Chromium
export HH_PROXY=socks5://127.0.0.1:2080 # прокси (опционально)
export HH_APPLY_INTERVAL_SEC=10800      # 3 часа между проходами apply
export HH_WATCH_INTERVAL_SEC=1800       # 30 минут между проверками чатов
export HH_BOOST_INTERVAL_SEC=14700      # ~4 часа между попытками подъёма
export HH_ALL_CHATS=1                   # обходить все чаты, не только непрочитанные (для отладки)
```

---

## Частые ошибки

| Сообщение | Решение |
|---|---|
| `profile.py не найден` | `cp profile.example.py profile.py` и заполните |
| `Session file not found` | `python hh_login.py` |
| `Executable doesn't exist` (Playwright) | `python -m playwright install chromium` |
| `Anthropic API key invalid` | Проверьте `ANTHROPIC_API_KEY`, ключ должен начинаться с `sk-ant-` |
| `Не вижу прогресса в дашборде` | Бот мог пропустить все вакансии как дубли. Откройте `tail -f /tmp/hh_auto_apply.log` |
| `Bot отвечает как-то роботично` | Уточните `MY_PROFILE`: добавьте конкретные проекты, метрики, технологии. Чем содержательнее — тем человечнее |

---

## Безопасность и приватность

- **`profile.py` и `hh_session.json` НЕ коммитьте** — они в `.gitignore`. Если случайно закоммитили — `git rm --cached profile.py` и переписать историю.
- **API-ключ НЕ хардкодьте** в код — только через env или `.env`.
- Раз в N коммитов прогоняйте аудит:
  ```bash
  git grep -E "ваше_фио|ваш_email@" || echo "clean"
  ```
- Не запускайте бота на чужом аккаунте без согласия владельца.

---

## Советы по эксплуатации

- **Не больше 1–2 проходов apply в день** на одном аккаунте. HH замечает аномалии.
- **Интервал 7+ секунд между откликами** — дефолт. Не уменьшайте.
- **Раз в неделю** просматривайте «Ручную очередь» в дашборде: там вакансии, требующие персонального ответа (скрининг-вопросы, неоднозначные роли).
- **Положительные сигналы** (приглашения на интервью, запросы контактов) — реагируйте сами, бот их специально маркирует и не отвечает автоматически.
- Если HH начал показывать капчу — остановите всё на 24 часа, потом проверьте через `python hh_login.py` что сессия жива.

---

## Дальнейшие шаги

- Прочитайте [README.md](README.md) — там архитектура и список фичей.
- Откройте `auto_reply.py:SYSTEM_PROMPT` — там можно тонко настроить тон ответов под себя.
- В дашборде на вкладке «🚩 Ручная очередь» начнут появляться вакансии с скрининг-вопросами; на «🎯 Положительные» — приглашения на интервью.
