# HH.ru Автооткликатор, Гайд по настройке

## Что это?

Автоматическая система для откликов на вакансии HH.ru:
- Ищет вакансии по твоим запросам
- Генерирует персонализированные сопроводительные письма через ChatGPT
- Автоматически откликается с этими письмами
- Не откликается повторно на те же вакансии

## Что нужно для работы

1. **Python 3.10+**, язык программирования
2. **Anthropic API ключ**, для генерации писем через Claude Haiku 4.5 (~$0.001-0.002 за отклик)
3. **Аккаунт на HH.ru**, с заполненным резюме
4. **Mac или Windows**, Linux тоже работает

## Пошаговая установка

### Шаг 1: Установка Python

**Mac:**
```bash
# Проверь есть ли Python
python3 --version

# Если нет, установи через Homebrew
brew install python
```

**Windows:**
Скачай с https://www.python.org/downloads/

### Шаг 2: Установка зависимостей

```bash
pip3 install playwright python-dotenv anthropic
python3 -m playwright install chromium
```

### Шаг 3: Создание папки проекта

```bash
mkdir ~/hh-automation
cd ~/hh-automation
```

### Шаг 4: Скачивание файлов

Скачай и положи в папку:
- `auto_apply.py`, основной скрипт
- `hh_login.py`, авторизация

### Шаг 5: Получение Anthropic API ключа

1. Зайди на https://console.anthropic.com/settings/keys
2. Создай новый ключ
3. Скопируй его (начинается с `sk-ant-...`)

### Шаг 6: Настройка скрипта

Открой `auto_apply.py` и заполни:

1. **ANTHROPIC_API_KEY**, вставь свой ключ
2. **SEARCH_QUERIES**, поисковые запросы для вакансий
3. **MY_PROFILE**, информация о тебе для писем

### Шаг 7: Авторизация на HH.ru

```bash
cd ~/hh-automation
python3 hh_login.py
```

Откроется браузер, залогинься на HH.ru и нажми Enter в терминале.

### Шаг 8: Запуск!

```bash
python3 auto_apply.py
```

## Ежедневное использование

Просто запускай:
```bash
cd ~/hh-automation
python3 auto_apply.py
```

## Советы

- Запускай 1-2 раза в день максимум
- Раз в неделю обновляй сессию через `hh_login.py`
- Не ставь паузу между откликами меньше 5 секунд

## Частые ошибки

**"Session file not found"**
→ Запусти `python3 hh_login.py`

**"Executable doesn't exist"**
→ Запусти `python3 -m playwright install chromium`

**"Anthropic API key invalid"**
→ Проверь что ключ скопирован полностью (начинается с `sk-ant-`)
