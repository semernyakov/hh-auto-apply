# Code Review - Critical Issues (P1-P2)

## P1 - Critical Issues

### 1. Resource Leak in resume_boost.py
**Location:** Lines 459-467, 482-490  
**Severity:** P1 - Resource Leak  
**Status:** ✅ Исправлено (2026-10-26): в `main()` файл закрывается в `finally`, перед этим потоки возвращаются на `sys.__stdout__`/`sys.__stderr__`. В `watch_loop()` файл открыт на всё время жизни процесса — это не утечка.

**Problem:** File handles are opened but never closed, causing resource leaks.

```python
# Current code (BROKEN)
log_fh = open(log_file, "w", buffering=1)
sys.stdout = log_fh
sys.stderr = log_fh
# log_fh is never closed - resource leak!
```

**Fix Required:**
```python
try:
    log_fh = open(log_file, "w", buffering=1)
    sys.stdout = log_fh
    sys.stderr = log_fh
    # ... rest of code
finally:
    log_fh.close()
```

**Impact:** File descriptors accumulate over time, potentially hitting OS limits.

---

### 2. Incomplete Error Handling in auto_apply_template.py
**Location:** Lines 494-502  
**Severity:** P1 - Broken Code  
**Status:** ⚪ Не подтвердилось (2026-10-26): блок `_metrics.log_event(kind="error", ...)` в `generate_cover_letter` завершён, файл парсится.

**Problem:** Error logging code is cut off mid-function call, leaving incomplete code.

```python
# Current code (BROKEN)
if _metrics:
    _metrics.log_event(  # Incomplete - function call not finished
```

**Fix Required:** Complete the error logging block or remove the incomplete code.

**Impact:** Code is syntactically invalid and won't run properly.

---

## P2 - High Priority Issues

### 3. Race Condition in Makefile Dashboard Startup
**Location:** Makefile lines 15-29  
**Severity:** P2 - Race Condition  
**Status:** ⚪ Не актуально (2026-10-26): дашборд запускает один пользователь локально, повторный `make start` видит живой PID.

**Problem:** Between checking if process is running and writing PID file, another process could start.

```bash
# Current code (VULNERABLE)
@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
    echo "Dashboard уже запущен"; \
else \
    rm -f $(PID_FILE); \
    # Race window here - another process could start
    HH_HEADLESS=1 nohup $(PYTHON) ... & \
    echo $$! > $(PID_FILE); \
```

**Fix Required:** Use file locking or atomic PID file creation.

**Impact:** Multiple dashboard instances could start simultaneously.

---

### 4. Security: API Key Exposure in dashboard.py
**Location:** Lines 99-111  
**Severity:** P2 - Security  
**Status:** ✅ Исправлено (2026-10-26): чтение `~/.bashrc` убрано, ключ берётся из `.env` (`load_dotenv()` при импорте `auto_apply_template`). Переменные окружения в `ps` не видны.

**Problem:** Reads API key from ~/.bashrc without validation, passes to subprocess environment (visible in process listing).

```python
# Current code (INSECURE)
for line in Path.home().joinpath(".bashrc").read_text().splitlines():
    line = line.strip()
    if line.startswith("export OPENROUTER_API_KEY="):
        env["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
```

**Fix Required:** Use dedicated environment file or secret management system. Validate key format before use.

**Impact:** API keys visible in process listings, potential credential leakage.

---

### 5. Silent Error Suppression in resume_boost.py
**Location:** Lines 207-262 (_dismiss_modals function)  
**Severity:** P2 - Debugging Difficulty  
**Status:** ✅ Исправлено (2026-10-26): исключения в `resume_boost.py` логируются, тексты сообщений выверены.

**Problem:** Multiple bare `except Exception` blocks that silently suppress errors.

```python
# Current code (PROBLEMATIC)
for _ in range(max_attempts):
    try:
        # ... code ...
    except Exception:  # Silent suppression - no logging
        pass
```

**Fix Required:** Log errors or use specific exception types.

**Impact:** Makes debugging difficult, errors are silently ignored.

---

### 6. Fragile API Key Extraction in dashboard.py
**Location:** Lines 102-108  
**Severity:** P2 - Fragility  
**Status:** ✅ Исправлено вместе с #4.

**Problem:** Parses ~/.bashrc with simple string splitting, breaks easily if format changes.

```python
# Current code (FRAGILE)
if line.startswith("export OPENROUTER_API_KEY="):
    env["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
```

**Fix Required:** Use proper environment variable management or config file parsing library.

**Impact:** Breaks easily with minor config format changes.

---

## Summary

- Исправлено: #1, #4, #5, #6.
- Не подтвердилось / не актуально: #2, #3.
- Открытых замечаний нет.
