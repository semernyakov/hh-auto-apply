# Code Review - Critical Issues (P1-P2)

## P1 - Critical Issues

### 1. Resource Leak in resume_boost.py
**Location:** Lines 459-467, 482-490  
**Severity:** P1 - Resource Leak  
**Status:** ❌ Not Fixed

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
**Status:** ❌ Not Fixed

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
**Status:** ❌ Not Fixed

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
**Status:** ❌ Not Fixed

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
**Status:** ❌ Not Fixed

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
**Status:** ❌ Not Fixed

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

- **P1 Issues:** 2 (Resource leak, incomplete code)
- **P2 Issues:** 4 (Race condition, security, error handling, fragility)
- **Total Critical Issues:** 6

**Recommended Action Order:**
1. Fix P1 #2 (incomplete code) - blocks execution
2. Fix P1 #1 (resource leak) - prevents resource exhaustion
3. Fix P2 #4 (security) - prevents credential exposure
4. Fix P2 #3 (race condition) - prevents duplicate processes
5. Fix P2 #5 (error suppression) - improves debuggability
6. Fix P2 #6 (fragile parsing) - improves robustness
