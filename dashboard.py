"""HTTP-дашборд для HH-бота: старт/стоп/рестарт + метрики и диалоги.

Запуск:
    .venv/bin/python dashboard.py
    # затем открыть http://127.0.0.1:8765 в браузере
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import auto_apply_template
import metrics
import profile_store
from auto_apply_template import (
    DELAY_APPLY_MAX,
    DELAY_APPLY_MIN,
    MAX_APPLIES_PER_DAY,
    MAX_PAGES,
    WORK_HOUR_END,
    WORK_HOUR_START,
    apply_to_vacancy,
    build_letter,
    launch_browser,
)
from auto_reply import SESSION_FILE

PROJECT_DIR = Path(__file__).parent.resolve()
PYTHON = str(PROJECT_DIR / ".venv" / "bin" / "python")

WORKERS = {
    "reply": {
        "script": str(PROJECT_DIR / "auto_reply.py"),
        "pid_file": Path("/tmp/hh_auto_reply.pid"),
        "log_file": Path("/tmp/hh_auto_reply.log"),
        "label": "Ответы в чат",
    },
    "apply": {
        "script": str(PROJECT_DIR / "auto_apply_template.py"),
        "pid_file": Path("/tmp/hh_auto_apply.pid"),
        "log_file": Path("/tmp/hh_auto_apply.log"),
        "label": "Отклики на вакансии",
    },
    "boost": {
        "script": str(PROJECT_DIR / "resume_boost.py"),
        "pid_file": Path("/tmp/hh_resume_boost.pid"),
        "log_file": Path("/tmp/hh_resume_boost.log"),
        "label": "Подъём резюме в поиске",
    },
}

app = FastAPI(title="HH-bot dashboard")
metrics.init_db()

DASHBOARD_HOST = os.getenv("HH_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("HH_DASHBOARD_PORT", "8765"))
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", DASHBOARD_HOST}


@app.middleware("http")
async def _local_only(request: Request, call_next):
    # Любая открытая в браузере страница может слать запросы на 127.0.0.1:
    # Host отсекает DNS-rebinding, Origin — чужой POST (старт воркеров, отправка).
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if DASHBOARD_HOST != "0.0.0.0" and host not in _ALLOWED_HOSTS:
        return JSONResponse({"ok": False, "error": f"host {host!r} не разрешён"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method != "GET" and origin:
        allowed = {f"http://{h}:{DASHBOARD_PORT}" for h in _ALLOWED_HOSTS}
        if origin not in allowed:
            return JSONResponse({"ok": False, "error": "cross-origin запрос отклонён"}, status_code=403)
    return await call_next(request)


# ---------- управление процессом ----------

def _read_pid(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        pid_file.unlink(missing_ok=True)
        return None
    return pid


def _process_status(name: str) -> dict[str, Any]:
    cfg = WORKERS[name]
    pid = _read_pid(cfg["pid_file"])
    return {"name": name, "label": cfg["label"], "running": pid is not None, "pid": pid}


def _start_worker(name: str) -> dict[str, Any]:
    cfg = WORKERS[name]
    if _read_pid(cfg["pid_file"]):
        return {"ok": False, "error": f"{cfg['label']}: процесс уже запущен"}
    if not Path(PYTHON).exists():
        return {"ok": False, "error": f"python venv не найден: {PYTHON}"}

    args = [PYTHON, "-u", cfg["script"], "--watch"]
    cfg["log_file"].write_text("")
    log = open(cfg["log_file"], "a", buffering=1)

    # .env уже подгружен load_dotenv() при импорте auto_apply_template
    env = os.environ.copy()
    # Ключ нужен только ответам в чате: отклики шлют шаблон, подъём резюме LLM не трогает
    if name == "reply" and not env.get("OPENROUTER_API_KEY"):
        log.close()
        return {"ok": False, "error": "OPENROUTER_API_KEY не найден ни в env, ни в .env"}
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":1"

    p = subprocess.Popen(
        args, cwd=str(PROJECT_DIR), stdout=log, stderr=subprocess.STDOUT,
        env=env, preexec_fn=os.setsid,
    )
    cfg["pid_file"].write_text(str(p.pid))
    return {"ok": True, "pid": p.pid}


def _stop_worker(name: str) -> dict[str, Any]:
    cfg = WORKERS[name]
    pid = _read_pid(cfg["pid_file"])
    if not pid:
        return {"ok": False, "error": f"{cfg['label']}: процесс не запущен"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "error": f"SIGTERM: {e}"}

    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
    cfg["pid_file"].unlink(missing_ok=True)
    metrics.log_event(kind="run_end", payload={"reason": "stop_via_dashboard", "worker": name})
    return {"ok": True}


def _tail_log(name: str, lines: int = 60) -> str:
    cfg = WORKERS[name]
    log_file = cfg["log_file"]
    if not log_file.exists():
        return ""
    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 16 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception as e:
        return f"(ошибка чтения лога: {e})"


# ---------- API ----------

def _bad_name(name: str) -> JSONResponse | None:
    if name not in WORKERS:
        return JSONResponse({"ok": False, "error": f"unknown worker: {name}"}, status_code=404)
    return None


def _get_limits() -> dict[str, Any]:
    """Конфигурационные параметры (читаются из env с теми же дефолтами,
    что в скриптах воркеров)."""
    return {
        "apply_interval_sec": int(os.getenv("HH_APPLY_INTERVAL_SEC", "14400")),
        "reply_interval_sec": int(os.getenv("HH_WATCH_INTERVAL_SEC", "1800")),
        "boost_interval_sec": int(os.getenv("HH_BOOST_INTERVAL_SEC", "14700")),
        "delay_between_applies_sec": (DELAY_APPLY_MIN + DELAY_APPLY_MAX) // 2,
        "delay_apply_min_sec": DELAY_APPLY_MIN,
        "delay_apply_max_sec": DELAY_APPLY_MAX,
        "max_applies_per_day": MAX_APPLIES_PER_DAY,
        "work_hours": f"{WORK_HOUR_START:02d}:00–{WORK_HOUR_END:02d}:00",
        "max_chats_per_run": 30,
        "max_pages": MAX_PAGES,
        "dedup_window_days": 60,
    }


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(
        {
            "workers": {name: _process_status(name) for name in WORKERS},
            "summary": metrics.get_summary(),
            "rates": metrics.get_rate_stats(),
            "limits": _get_limits(),
        }
    )


@app.get("/api/events")
def api_events(limit: int = 100, offset: int = 0, kind: str = "") -> JSONResponse:
    kinds = [k for k in kind.split(",") if k] if kind else None
    return JSONResponse({
        "events": metrics.get_recent(limit=limit, kinds=kinds, offset=offset),
        "total": metrics.count_events(kinds=kinds),
    })


@app.get("/api/chat-history")
def api_chat_history(chat_url: str = "") -> JSONResponse:
    return JSONResponse({"history": metrics.get_chat_history(chat_url)})


def _enrich_pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Добавляет к каждому pending suggested_letter, собранный по описанию."""
    for it in items:
        p = it.get("payload") or {}
        desc = p.get("description_preview", "")
        title = p.get("title", "")
        try:
            letter, addendums = build_letter(desc, title)
        except Exception:
            letter, addendums = "", []
        it["suggested_letter"] = letter
        it["suggested_addendums"] = addendums
    return items


@app.get("/api/pending")
def api_pending() -> JSONResponse:
    return JSONResponse({"pending": _enrich_pending(metrics.get_pending_manual())})


@app.get("/api/positive")
def api_positive(limit: int = 100) -> JSONResponse:
    return JSONResponse({"positive": metrics.get_positive_signals(limit=limit)})


@app.get("/api/rejections")
def api_rejections(limit: int = 100) -> JSONResponse:
    return JSONResponse({"rejections": metrics.get_rejections(limit=limit)})


@app.get("/api/robot-questionnaires")
def api_robot_questionnaires(limit: int = 200) -> JSONResponse:
    return JSONResponse({"questionnaires": metrics.get_robot_questionnaires(limit=limit)})


@app.get("/api/conversion")
def api_conversion(days: int = 60) -> JSONResponse:
    return JSONResponse(metrics.get_reply_conversion(days=days))


@app.get("/api/cooldown-savings")
def api_cooldown_savings(days: int = 30) -> JSONResponse:
    return JSONResponse(metrics.get_cooldown_savings(days=days))


@app.post("/api/robot-questionnaires/{event_id}/resolve")
def api_robot_questionnaire_resolve(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(
        event_id, "robot_questionnaire_handled", expected_kind="robot_questionnaire"
    )
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/robot-questionnaires/{event_id}/dismiss")
def api_robot_questionnaire_dismiss(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(
        event_id, "robot_questionnaire_dismissed", expected_kind="robot_questionnaire"
    )
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/robot-questionnaires/{event_id}/send")
def api_robot_questionnaire_send(event_id: int, body: dict = Body(...)) -> JSONResponse:
    """Отправить ответ на анкету через playwright (использует сохранённую сессию)."""
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "пустой ответ"}, status_code=400)
    if len(text) > 4000:
        return JSONResponse(
            {"ok": False, "error": f"слишком длинно ({len(text)}/4000)"}, status_code=400
        )

    questionnaires = metrics.get_robot_questionnaires(limit=500)
    record = next((q for q in questionnaires if q["id"] == event_id), None)
    if not record:
        return JSONResponse(
            {"ok": False, "error": "анкета не найдена или уже обработана"}, status_code=404
        )
    chat_url = record.get("chat_url") or ""
    if not chat_url:
        return JSONResponse({"ok": False, "error": "URL чата отсутствует"}, status_code=400)

    try:
        from playwright.sync_api import sync_playwright
        from auto_reply import post_reply

        with sync_playwright() as p:
            browser = launch_browser(p, headless=True)
            ctx = browser.new_context(storage_state=str(SESSION_FILE))
            page = ctx.new_page()
            try:
                page.goto(chat_url, wait_until="commit", timeout=30000)
                page.wait_for_selector(
                    "textarea[data-qa='chatik-new-message-text'], "
                    "div[contenteditable='true'][data-qa='chatik-input']",
                    timeout=15000,
                )
                sent = post_reply(page, text)
            finally:
                browser.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"playwright: {e}"}, status_code=500)

    if not sent:
        return JSONResponse({"ok": False, "error": "не удалось отправить (поле/кнопка)"}, status_code=500)

    metrics.log_event(
        kind="reply",
        vacancy=record.get("vacancy", ""),
        chat_url=chat_url,
        payload={
            "reply": text,
            "type": "manual_via_dashboard",
            "from_robot_questionnaire_id": event_id,
        },
    )
    metrics.resolve_event(
        event_id, "robot_questionnaire_replied", expected_kind="robot_questionnaire"
    )
    return JSONResponse({"ok": True})


@app.post("/api/positive/{event_id}/resolve")
def api_positive_resolve(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(event_id, "positive_signal_handled", expected_kind="positive_signal")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/pending/{event_id}/view")
def api_pending_view(event_id: int) -> JSONResponse:
    """Пометить вакансию из ручной очереди как просмотренную."""
    pendings = metrics.get_pending_manual()
    pending = next((p for p in pendings if p["id"] == event_id), None)
    if not pending:
        return JSONResponse({"ok": False, "error": "событие не найдено или уже обработано"}, status_code=404)

    metrics.log_event(
        kind="pending_viewed",
        vacancy=pending.get("vacancy", ""),
        chat_url=pending.get("chat_url", ""),
        payload={
            "from_pending_id": event_id,
            "title": (pending.get("payload") or {}).get("title", ""),
            "employer": (pending.get("payload") or {}).get("employer", ""),
        },
    )
    return JSONResponse({"ok": True})


@app.post("/api/pending/{event_id}/apply")
def api_pending_apply(event_id: int, body: dict = Body(...)) -> JSONResponse:
    """Отправить отклик на вакансию из ручной очереди через playwright."""
    letter = (body.get("letter") or "").strip()
    if not letter:
        return JSONResponse({"ok": False, "error": "пустое письмо"}, status_code=400)
    if len(letter) > 2000:
        return JSONResponse({"ok": False, "error": f"письмо слишком длинное ({len(letter)}/2000)"}, status_code=400)

    pendings = metrics.get_pending_manual()
    pending = next((p for p in pendings if p["id"] == event_id), None)
    if not pending:
        return JSONResponse({"ok": False, "error": "событие не найдено или уже обработано"}, status_code=404)

    payload = pending.get("payload") or {}
    vacancy_url = payload.get("vacancy_url") or pending.get("chat_url")
    if not vacancy_url:
        return JSONResponse({"ok": False, "error": "URL вакансии отсутствует"}, status_code=400)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = launch_browser(p, headless=True)
            ctx = browser.new_context(storage_state=str(SESSION_FILE))
            page = ctx.new_page()
            try:
                result = apply_to_vacancy(page, vacancy_url, letter)
            finally:
                browser.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"playwright: {e}"}, status_code=500)

    # unconfirmed: кнопка нажата, HH не подтвердил — фиксируем, чтобы повтор из очереди не ушёл дважды
    if result.get("status") in ("success", "unconfirmed"):
        metrics.log_event(
            kind="apply",
            vacancy=pending.get("vacancy", ""),
            chat_url=vacancy_url,
            payload={
                "letter": letter,
                "vacancy_url": vacancy_url,
                "title": payload.get("title", ""),
                "employer": payload.get("employer", ""),
                "type": "manual_from_pending_modal",
                "send_result": result.get("reason"),
                "from_pending_id": event_id,
            },
        )
        metrics.resolve_event(event_id, "manual_applied")
        return JSONResponse({"ok": True, "reason": result.get("reason", "")})

    return JSONResponse(
        {"ok": False, "error": f"{result.get('status')}: {result.get('reason', '')}"},
        status_code=400,
    )


@app.post("/api/pending/{event_id}/resolve")
def api_pending_resolve(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(event_id, "manual_applied")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/pending/{event_id}/dismiss")
def api_pending_dismiss(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(event_id, "manual_dismissed")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/pending/{event_id}/send")
def api_pending_send(event_id: int, body: dict = Body(...)) -> JSONResponse:
    letter = (body.get("letter") or "").strip()
    if not letter:
        return JSONResponse({"ok": False, "error": "пустое письмо"}, status_code=400)
    if len(letter) > 2000:
        return JSONResponse({"ok": False, "error": f"письмо слишком длинное ({len(letter)}/2000)"}, status_code=400)

    pendings = metrics.get_pending_manual()
    pending = next((p for p in pendings if p["id"] == event_id), None)
    if not pending:
        return JSONResponse({"ok": False, "error": "событие не найдено или уже обработано"}, status_code=404)

    payload = pending.get("payload") or {}
    vacancy_url = payload.get("vacancy_url") or pending.get("chat_url")
    if not vacancy_url:
        return JSONResponse({"ok": False, "error": "URL вакансии отсутствует"}, status_code=400)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = launch_browser(p, headless=True)
            ctx = browser.new_context(storage_state=str(SESSION_FILE))
            page = ctx.new_page()
            try:
                result = apply_to_vacancy(page, vacancy_url, letter)
            finally:
                browser.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"playwright: {e}"}, status_code=500)

    # unconfirmed: кнопка нажата, HH не подтвердил — фиксируем, чтобы повтор из очереди не ушёл дважды
    if result.get("status") in ("success", "unconfirmed"):
        metrics.log_event(
            kind="apply",
            vacancy=pending.get("vacancy", ""),
            chat_url=vacancy_url,
            payload={
                "letter": letter,
                "vacancy_url": vacancy_url,
                "title": payload.get("title", ""),
                "employer": payload.get("employer", ""),
                "type": "manual_edited",
                "send_result": result.get("reason"),
                "from_pending_id": event_id,
            },
        )
        metrics.resolve_event(event_id, "manual_applied")
        return JSONResponse({"ok": True, "reason": result.get("reason", "")})

    return JSONResponse(
        {"ok": False, "error": f"{result.get('status')}: {result.get('reason', '')}"},
        status_code=400,
    )


@app.get("/api/profile")
def api_profile() -> JSONResponse:
    try:
        data = profile_store.load()
    except FileNotFoundError:
        data = profile_store.validate({})[0]
    return JSONResponse({
        "profile": data,
        "source": profile_store.source(),
        "resume_id_from_env": bool(os.getenv("HH_RESUME_ID")),
        "running": [name for name in WORKERS if _process_status(name)["running"]],
    })


@app.post("/api/profile")
def api_profile_save(body: dict = Body(...)) -> JSONResponse:
    _, errors = profile_store.save(body)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    auto_apply_template.reload_profile()  # письмо в ручной очереди — уже по новому шаблону
    return JSONResponse({"ok": True})


@app.get("/api/log/{name}", response_class=PlainTextResponse)
def api_log(name: str, lines: int = 80) -> PlainTextResponse:
    if name not in WORKERS:
        return PlainTextResponse(f"unknown worker: {name}", status_code=404)
    return PlainTextResponse(_tail_log(name, lines))


@app.post("/api/{name}/start")
def api_start(name: str) -> JSONResponse:
    bad = _bad_name(name)
    if bad: return bad
    res = _start_worker(name)
    return JSONResponse(res, status_code=200 if res["ok"] else 400)


@app.post("/api/{name}/stop")
def api_stop(name: str) -> JSONResponse:
    bad = _bad_name(name)
    if bad: return bad
    res = _stop_worker(name)
    return JSONResponse(res, status_code=200 if res["ok"] else 400)


@app.post("/api/{name}/restart")
def api_restart(name: str) -> JSONResponse:
    bad = _bad_name(name)
    if bad: return bad
    _stop_worker(name)
    time.sleep(0.5)
    res = _start_worker(name)
    return JSONResponse(res, status_code=200 if res["ok"] else 400)


# ---------- HTML ----------

INDEX_FILE = PROJECT_DIR / "dashboard.html"




@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # Читаем при каждом запросе: правки вёрстки видны без перезапуска дашборда
    return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HH_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("HH_DASHBOARD_PORT", "8765"))
    # HH_DEV_RELOAD=1 — uvicorn перечитывает dashboard.py при изменениях,
    # удобно когда правишь HTML/JS внутри HTML_TEMPLATE. В проде не использовать.
    reload = os.getenv("HH_DEV_RELOAD", "").lower() in ("1", "true", "yes")
    print(f"\n🌐 Дашборд запущен: http://{host}:{port}" + (" [reload]" if reload else "") + "\n")
    if reload:
        uvicorn.run("dashboard:app", host=host, port=port, log_level="warning", reload=True)
    else:
        uvicorn.run(app, host=host, port=port, log_level="warning")
