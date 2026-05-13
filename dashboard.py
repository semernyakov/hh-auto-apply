"""HTTP-дашборд для HH-бота: старт/стоп/рестарт + метрики и диалоги.

Запуск:
    .venv/bin/python dashboard.py
    # затем открыть http://127.0.0.1:8765 в браузере
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import metrics
from auto_apply_template import MAX_PAGES, apply_to_vacancy, build_letter
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

    env = os.environ.copy()
    if not env.get("ANTHROPIC_API_KEY"):
        try:
            for line in Path.home().joinpath(".bashrc").read_text().splitlines():
                line = line.strip()
                if line.startswith("export ANTHROPIC_API_KEY="):
                    env["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    if not env.get("ANTHROPIC_API_KEY"):
        log.close()
        return {"ok": False, "error": "ANTHROPIC_API_KEY не найден ни в env, ни в ~/.bashrc"}
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
        "apply_interval_sec": int(os.getenv("HH_APPLY_INTERVAL_SEC", "10800")),
        "reply_interval_sec": int(os.getenv("HH_WATCH_INTERVAL_SEC", "1800")),
        "boost_interval_sec": int(os.getenv("HH_BOOST_INTERVAL_SEC", "14700")),
        "delay_between_applies_sec": 7,
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


@app.post("/api/positive/{event_id}/resolve")
def api_positive_resolve(event_id: int) -> JSONResponse:
    ok = metrics.resolve_event(event_id, "positive_signal_handled", expected_kind="positive_signal")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


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
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(SESSION_FILE))
            page = ctx.new_page()
            try:
                result = apply_to_vacancy(page, vacancy_url, letter)
            finally:
                browser.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"playwright: {e}"}, status_code=500)

    if result.get("status") == "success":
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

INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<title>HH-bot</title>
<style>
:root {
  --bg: #0f1115; --panel: #181b22; --panel-2: #1f232c;
  --fg: #e6e9ef; --muted: #8b93a7; --accent: #4f8cff;
  --ok: #2ecc71; --warn: #f1c40f; --err: #e74c3c;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--fg); font-size: 14px;
}
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 24px; background: var(--panel); border-bottom: 1px solid #232733;
}
header h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.3px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }
.dot.run { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.dot.idle { background: var(--muted); }
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 14px; border-radius: 6px; border: 1px solid #2c3140;
  background: var(--panel-2); color: var(--fg); cursor: pointer; font-size: 13px;
}
.btn:hover { background: #262b36; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.danger  { background: var(--err); border-color: var(--err); color: #fff; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.controls { display: flex; gap: 8px; }
.workers { padding: 14px 24px 0; max-width: 1280px; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }
.worker-row { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; background: var(--panel); border: 1px solid #232733; border-radius: 8px; }
.worker-meta { display: flex; align-items: center; gap: 16px; }
.worker-label { font-weight: 600; }
.worker-status { color: var(--muted); font-size: 13px; }
main { padding: 16px 24px 32px; max-width: 1280px; margin: 0 auto; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.card {
  background: var(--panel); border: 1px solid #232733; border-radius: 8px; padding: 14px 16px;
}
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.4px; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.card .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
section { margin-top: 22px; }
section h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
table { width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 8px; overflow: hidden; }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid #232733; vertical-align: top; font-size: 13px; }
th { background: var(--panel-2); color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; }
tr:last-child td { border-bottom: none; }
.kind { font-weight: 600; padding: 2px 8px; border-radius: 4px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px; }
.k-reply { background: rgba(46, 204, 113, 0.15); color: var(--ok); }
.k-apply { background: rgba(79, 140, 255, 0.15); color: var(--accent); }
.k-boost { background: rgba(46, 204, 113, 0.15); color: var(--ok); }
.k-skip  { background: rgba(139, 147, 167, 0.18); color: var(--muted); }
.k-error { background: rgba(231, 76, 60, 0.18); color: var(--err); }
.k-run_start, .k-run_end { background: rgba(241, 196, 15, 0.15); color: var(--warn); }
.k-pending_manual { background: rgba(241, 196, 15, 0.18); color: var(--warn); }
.k-manual_applied { background: rgba(46, 204, 113, 0.15); color: var(--ok); }
.k-manual_dismissed { background: rgba(139, 147, 167, 0.18); color: var(--muted); }
.k-positive_signal { background: rgba(46, 204, 113, 0.20); color: var(--ok); }
.k-positive_signal_handled { background: rgba(139, 147, 167, 0.18); color: var(--muted); }
.k-rejection { background: rgba(231, 76, 60, 0.18); color: var(--err); }
.sig-badge { font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; }
.sig-interview        { background: rgba(46, 204, 113, 0.20); color: var(--ok); }
.sig-contact_request  { background: rgba(79, 140, 255, 0.20); color: var(--accent); }
.sig-under_review     { background: rgba(241, 196, 15, 0.20); color: var(--warn); }
.trigger { display: inline-block; padding: 2px 6px; margin: 1px 2px; background: rgba(241, 196, 15, 0.12); color: var(--warn); border-radius: 3px; font-size: 11px; }
.btn-mini { padding: 4px 9px; font-size: 11px; border-radius: 4px; border: 1px solid #2c3140; background: var(--panel-2); color: var(--fg); cursor: pointer; }
.btn-mini:hover { background: #262b36; }
.btn-mini.ok { border-color: var(--ok); color: var(--ok); }
.btn-mini.dismiss { border-color: #6a7080; color: var(--muted); }
pre.log {
  background: #0a0c10; color: #cad3e1; padding: 12px 14px; border-radius: 8px;
  border: 1px solid #232733; max-height: 320px; overflow: auto;
  font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; font-size: 12px; line-height: 1.45;
  white-space: pre-wrap;
}
.preview { color: var(--muted); font-size: 12px; max-width: 520px; cursor: pointer; }
.preview b { color: var(--fg); font-weight: 500; }
.preview:hover b { color: var(--accent); }
.row-link { color: var(--accent); text-decoration: none; }
.row-link:hover { text-decoration: underline; }
.muted { color: var(--muted); }
.events-table tr { cursor: pointer; }
.events-table tr:hover td { background: rgba(79, 140, 255, 0.05); }
.pager {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  font-size: 12px; color: var(--muted); flex-wrap: wrap;
}
.pager label { display: inline-flex; align-items: center; gap: 6px; }
.pager select {
  background: var(--panel-2); color: var(--fg); border: 1px solid #2c3140;
  border-radius: 4px; padding: 3px 6px; font-size: 12px; cursor: pointer;
}
.pager #page-info { padding: 0 4px; font-variant-numeric: tabular-nums; }
.pager button[disabled] { opacity: 0.4; cursor: not-allowed; }
.view-toggle { margin: 8px 0 4px; font-size: 12px; color: var(--muted); }
.view-toggle label { cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 6px; }
.chat-group-row td { background: rgba(79, 140, 255, 0.06); }
.chat-group-row .chat-count {
  display: inline-block; padding: 1px 7px; border-radius: 10px;
  background: var(--accent); color: #fff; font-size: 11px; margin-left: 6px;
}
.chat-group-row.expanded td { background: rgba(79, 140, 255, 0.12); }
.chat-child-row td:first-child { padding-left: 28px; }
.chat-child-row { display: none; }
.chat-group-row.expanded + tbody .chat-child-row,
tr.chat-child-row.visible { display: table-row; }
.tabs { display: flex; gap: 4px; margin-bottom: 10px; flex-wrap: wrap; }
.tab { padding: 6px 12px; border-radius: 6px; background: var(--panel); border: 1px solid var(--panel);
       cursor: pointer; font-size: 13px; color: var(--muted); user-select: none; }
.tab:hover { color: var(--text); }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tab .count { opacity: 0.7; margin-left: 4px; font-size: 11px; }
.reason-badge { font-size: 11px; padding: 1px 6px; border-radius: 3px; background: rgba(241, 196, 15, 0.15);
                color: var(--warn); margin-right: 6px; }

/* модалка */
.modal-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.65);
  display: none; align-items: center; justify-content: center; z-index: 100;
}
.modal-backdrop.show { display: flex; }
.modal {
  background: var(--panel); border: 1px solid #2c3140; border-radius: 10px;
  padding: 20px 24px; max-width: 720px; width: 92%; max-height: 86vh; overflow: auto;
}
.modal h3 { margin: 0 0 12px; font-size: 16px; }
.modal .meta { color: var(--muted); font-size: 12px; margin-bottom: 16px; line-height: 1.6; }
.modal .meta a { color: var(--accent); }
.modal .body {
  background: #0a0c10; padding: 14px 16px; border-radius: 6px; border: 1px solid #232733;
  white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.55;
}
.thread-item {
  background: #0a0c10; border: 1px solid #232733; border-radius: 6px;
  padding: 10px 14px; margin-bottom: 10px;
}
.thread-item .head {
  display: flex; gap: 10px; align-items: baseline; font-size: 11px;
  color: var(--muted); margin-bottom: 6px;
}
.thread-item .head .kind { font-size: 11px; padding: 1px 6px; }
.thread-item .text {
  white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.55;
}
.thread-item.skip { opacity: 0.7; }
.thread-empty { color: var(--muted); font-size: 12px; padding: 12px 0; }
.modal-close {
  float: right; background: transparent; border: 1px solid #2c3140; color: var(--fg);
  width: 28px; height: 28px; border-radius: 4px; cursor: pointer; font-size: 14px;
}
.editor-area {
  width: 100%; min-height: 200px; padding: 12px 14px; border-radius: 6px;
  border: 1px solid #2c3140; background: #0a0c10; color: var(--fg);
  font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; font-size: 13px;
  line-height: 1.55; resize: vertical; box-sizing: border-box;
}
.editor-area:focus { outline: none; border-color: var(--accent); }
.editor-actions { margin-top: 12px; display: flex; gap: 8px; justify-content: flex-end; align-items: center; }
.editor-status { color: var(--muted); font-size: 12px; margin-right: auto; }
.editor-status.err { color: var(--err); }
.editor-status.ok { color: var(--ok); }
.char-counter { color: var(--muted); font-size: 11px; margin-top: 4px; text-align: right; }
.char-counter.warn { color: var(--warn); }
.char-counter.over  { color: var(--err); }
.btn-send { background: var(--ok); border-color: var(--ok); color: #fff; }
.btn-send:hover { background: #27ae60; }
.btn-send:disabled { opacity: 0.5; cursor: not-allowed; }
details.desc-fold { margin-bottom: 12px; }
details.desc-fold > summary { cursor: pointer; color: var(--muted); font-size: 12px; user-select: none; }
details.desc-fold > div { margin-top: 8px; padding: 10px 12px; background: #0a0c10; border-radius: 6px; border: 1px solid #232733; font-size: 12px; line-height: 1.5; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <h1>HH-bot · мониторинг и управление</h1>
</header>

<div class="workers">
  <div class="worker-row" data-name="apply">
    <div class="worker-meta">
      <span class="worker-label">📨 Отклики на вакансии</span>
      <span class="worker-status"><span class="dot idle"></span><span class="status-text">…</span></span>
    </div>
    <div class="controls">
      <button class="btn primary btn-start">▶ Старт</button>
      <button class="btn btn-restart">↻ Рестарт</button>
      <button class="btn danger btn-stop">■ Стоп</button>
    </div>
  </div>
  <div class="worker-row" data-name="reply">
    <div class="worker-meta">
      <span class="worker-label">💬 Ответы в чатах</span>
      <span class="worker-status"><span class="dot idle"></span><span class="status-text">…</span></span>
    </div>
    <div class="controls">
      <button class="btn primary btn-start">▶ Старт</button>
      <button class="btn btn-restart">↻ Рестарт</button>
      <button class="btn danger btn-stop">■ Стоп</button>
    </div>
  </div>
  <div class="worker-row" data-name="boost">
    <div class="worker-meta">
      <span class="worker-label">⬆️ Подъём резюме в поиске</span>
      <span class="worker-status"><span class="dot idle"></span><span class="status-text">…</span></span>
    </div>
    <div class="controls">
      <button class="btn primary btn-start">▶ Старт</button>
      <button class="btn btn-restart">↻ Рестарт</button>
      <button class="btn danger btn-stop">■ Стоп</button>
    </div>
  </div>
</div>

<main>
  <div class="grid">
    <div class="card"><div class="label">Сегодня · стоимость</div><div class="value" id="m-today-cost">$0.0000</div><div class="sub" id="m-today-tokens">tokens 0 → 0</div></div>
    <div class="card"><div class="label">Всего · стоимость</div><div class="value" id="m-total-cost">$0.0000</div><div class="sub" id="m-total-tokens">tokens 0 → 0</div></div>
    <div class="card"><div class="label">Reply / Apply</div><div class="value" id="m-rep-app">0 / 0</div><div class="sub">в чат / в отклик</div></div>
    <div class="card"><div class="label">Skip / Error</div><div class="value" id="m-skip-err">0 / 0</div><div class="sub">пропущено / ошибок</div></div>
  </div>

  <section>
    <h2>⏱ Активность за последние</h2>
    <table class="events-table">
      <thead><tr>
        <th>Окно</th><th>Откликов</th><th>Ответов в чат</th><th>Подъёмов резюме</th><th>Дублей пропущено</th><th>Прочих skip</th><th>Ошибок</th><th>В ручную очередь</th><th>Отказов</th><th>🎯 Положительных</th>
      </tr></thead>
      <tbody id="rates-body">
        <tr><td colspan="10" class="muted">загружаю…</td></tr>
      </tbody>
    </table>
  </section>

  <section>
    <h2>🚀 Запуски и расписание</h2>
    <div class="grid">
      <div class="card">
        <div class="label">📨 Отклики · последний прогон</div>
        <div class="value" id="run-apply-when">—</div>
        <div class="sub" id="run-apply-next">интервал —</div>
      </div>
      <div class="card">
        <div class="label">💬 Ответы в чат · последний прогон</div>
        <div class="value" id="run-reply-when">—</div>
        <div class="sub" id="run-reply-next">интервал —</div>
      </div>
      <div class="card">
        <div class="label">⬆️ Подъём резюме · последний прогон</div>
        <div class="value" id="run-boost-when">—</div>
        <div class="sub" id="run-boost-next">интервал —</div>
      </div>
    </div>
  </section>

  <section>
    <h2>⚙️ Временные лимиты и настройки</h2>
    <div class="grid">
      <div class="card">
        <div class="label">Интервал обхода вакансий</div>
        <div class="value" id="lim-apply-interval">—</div>
        <div class="sub">HH_APPLY_INTERVAL_SEC</div>
      </div>
      <div class="card">
        <div class="label">Интервал ответов в чат</div>
        <div class="value" id="lim-reply-interval">—</div>
        <div class="sub">HH_WATCH_INTERVAL_SEC</div>
      </div>
      <div class="card">
        <div class="label">Пауза между откликами</div>
        <div class="value" id="lim-delay">—</div>
        <div class="sub">DELAY_BETWEEN_APPLIES</div>
      </div>
      <div class="card">
        <div class="label">Чатов за прогон</div>
        <div class="value" id="lim-chats">—</div>
        <div class="sub">MAX_CHATS_PER_RUN</div>
      </div>
      <div class="card">
        <div class="label">Страниц поиска</div>
        <div class="value" id="lim-pages">—</div>
        <div class="sub">MAX_PAGES (≈50 ваканс./стр.)</div>
      </div>
      <div class="card">
        <div class="label">Окно дедупликации</div>
        <div class="value" id="lim-dedup">—</div>
        <div class="sub">по vacancy_id и (employer, title)</div>
      </div>
      <div class="card">
        <div class="label">Интервал подъёма резюме</div>
        <div class="value" id="lim-boost-interval">—</div>
        <div class="sub">HH_BOOST_INTERVAL_SEC (HH-кулдаун 4 ч)</div>
      </div>
    </div>
  </section>

  <section>
    <h2>📌 Очередь на ручной отклик <span class="muted" id="pending-count" style="font-weight:400;text-transform:none;letter-spacing:0">— 0</span></h2>
    <table id="pending" class="events-table">
      <thead><tr>
        <th>Когда</th><th>Вакансия</th><th>Триггеры</th><th>Описание</th><th>Действия</th>
      </tr></thead>
      <tbody><tr><td colspan="5" class="muted">пусто</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>🎯 Положительные ответы работодателей <span class="muted" id="positive-count" style="font-weight:400;text-transform:none;letter-spacing:0">— 0</span></h2>
    <table id="positive" class="events-table">
      <thead><tr>
        <th>Когда</th><th>Вакансия / чат</th><th>Сигнал</th><th>Триггер</th><th>Последнее сообщение</th><th>Действия</th>
      </tr></thead>
      <tbody><tr><td colspan="6" class="muted">пусто</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>🚫 Отказы работодателей <span class="muted" id="rejection-count" style="font-weight:400;text-transform:none;letter-spacing:0">— 0</span></h2>
    <table id="rejections" class="events-table">
      <thead><tr>
        <th>Когда</th><th>Вакансия / чат</th><th>Последнее сообщение</th>
      </tr></thead>
      <tbody><tr><td colspan="3" class="muted">пусто</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>Последние действия — клик по строке открывает полный текст</h2>
    <div class="tabs" id="event-tabs">
      <div class="tab active" data-filter="all">Все <span class="count" id="cnt-all">0</span></div>
      <div class="tab" data-filter="apply">📤 Отклики <span class="count" id="cnt-apply">0</span></div>
      <div class="tab" data-filter="reply">💬 Ответы в чат <span class="count" id="cnt-reply">0</span></div>
      <div class="tab" data-filter="pending_manual">🚩 Ручная очередь <span class="count" id="cnt-pending_manual">0</span></div>
      <div class="tab" data-filter="positive_signal">🎯 Положительные <span class="count" id="cnt-positive_signal">0</span></div>
      <div class="tab" data-filter="rejection">🚫 Отказы <span class="count" id="cnt-rejection">0</span></div>
      <div class="tab" data-filter="skip">⏭️ Пропуски <span class="count" id="cnt-skip">0</span></div>
      <div class="tab" data-filter="error">❌ Ошибки <span class="count" id="cnt-error">0</span></div>
    </div>
    <div class="view-toggle">
      <label><input type="checkbox" id="group-by-chat"> 📁 Свернуть по чату</label>
    </div>
    <table id="events" class="events-table">
      <thead><tr>
        <th>Время</th><th>Тип</th><th>Вакансия</th><th>Компания</th><th>Tokens</th><th>$ </th><th>Превью</th>
      </tr></thead>
      <tbody><tr><td colspan="7" class="muted">загружаю…</td></tr></tbody>
    </table>
    <div id="events-pager" class="pager">
      <label>На странице:
        <select id="page-size">
          <option value="50">50</option>
          <option value="100" selected>100</option>
          <option value="200">200</option>
          <option value="500">500</option>
        </select>
      </label>
      <button id="page-first" class="btn-mini">«</button>
      <button id="page-prev"  class="btn-mini">‹</button>
      <span id="page-info" class="muted">— / —</span>
      <button id="page-next"  class="btn-mini">›</button>
      <button id="page-last"  class="btn-mini">»</button>
    </div>
  </section>

  <div class="modal-backdrop" id="modal">
    <div class="modal">
      <button class="modal-close" id="modal-close">×</button>
      <h3 id="modal-title">—</h3>
      <div class="meta" id="modal-meta"></div>
      <div class="body" id="modal-body">—</div>
    </div>
  </div>

  <section>
    <h2>📨 Лог откликов на вакансии</h2>
    <pre class="log" id="log-apply">пусто</pre>
  </section>

  <section>
    <h2>💬 Лог ответов в чатах</h2>
    <pre class="log" id="log-reply">пусто</pre>
  </section>

  <section>
    <h2>⬆️ Лог подъёма резюме</h2>
    <pre class="log" id="log-boost">пусто</pre>
  </section>
</main>

<script>
const $ = (s) => document.querySelector(s);

function fmtTime(ts){
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2,'0');
  return `${pad(d.getDate())}.${pad(d.getMonth()+1)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtCost(c){ return '$' + (c || 0).toFixed(4); }
function fmtNum(n){ return (n || 0).toLocaleString('ru-RU'); }
function fmtDuration(sec){
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + ' сек';
  const m = Math.floor(sec / 60);
  if (m < 60) { const s = sec % 60; return s ? `${m} мин ${s} сек` : `${m} мин`; }
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return mm ? `${h} ч ${mm} мин` : `${h} ч`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return hh ? `${d} д ${hh} ч` : `${d} д`;
}
function fmtAgo(ts, now){
  if (!ts) return '—';
  return fmtDuration((now || Date.now()/1000) - ts) + ' назад';
}

async function refreshStatus(){
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    document.querySelectorAll('.worker-row').forEach(row => {
      const name = row.dataset.name;
      const w = d.workers[name];
      if (!w) return;
      row.querySelector('.dot').className = 'dot ' + (w.running ? 'run' : 'idle');
      row.querySelector('.status-text').textContent = w.running ? `работает · PID ${w.pid}` : 'остановлен';
      row.querySelector('.btn-start').disabled = w.running;
      row.querySelector('.btn-stop').disabled  = !w.running;
    });
    const s = d.summary;
    $('#m-today-cost').textContent = fmtCost(s.today_cost_usd);
    $('#m-today-tokens').textContent = `tokens ${fmtNum(s.today_tokens_in)} → ${fmtNum(s.today_tokens_out)}`;
    $('#m-total-cost').textContent = fmtCost(s.cost_usd);
    $('#m-total-tokens').textContent = `tokens ${fmtNum(s.tokens_in)} → ${fmtNum(s.tokens_out)}`;
    $('#m-rep-app').textContent = `${s.replies} / ${s.applies}`;
    $('#m-skip-err').textContent = `${s.skips} / ${s.errors}`;

    // лимиты
    const lim = d.limits || {};
    $('#lim-apply-interval').textContent = fmtDuration(lim.apply_interval_sec);
    $('#lim-reply-interval').textContent = fmtDuration(lim.reply_interval_sec);
    $('#lim-delay').textContent = fmtDuration(lim.delay_between_applies_sec);
    $('#lim-chats').textContent = fmtNum(lim.max_chats_per_run);
    $('#lim-pages').textContent = fmtNum(lim.max_pages);
    $('#lim-dedup').textContent = (lim.dedup_window_days || 0) + ' дней';

    // активность по окнам
    const rates = d.rates || {windows: {}, last_runs: {}, now: Date.now()/1000};
    const order = [['1h','за 1 час'], ['24h','за 24 часа'], ['7d','за 7 дней']];
    $('#rates-body').innerHTML = order.map(([k, label]) => {
      const w = rates.windows[k] || {};
      return `<tr>
        <td class="muted">${label}</td>
        <td><b>${fmtNum(w.apply)}</b>${w.manual_applied ? ` <span class="muted">(+${w.manual_applied} вручную)</span>` : ''}</td>
        <td>${fmtNum(w.reply)}</td>
        <td>${fmtNum(w.boost)}</td>
        <td>${fmtNum(w.duplicate)}</td>
        <td>${fmtNum((w.skip || 0) - (w.duplicate || 0))}</td>
        <td>${fmtNum(w.error)}</td>
        <td>${fmtNum(w.pending_manual)}</td>
        <td>${fmtNum(w.rejection)}</td>
        <td>${fmtNum(w.positive_signal)}</td>
      </tr>`;
    }).join('');

    // запуски
    const lr = rates.last_runs || {};
    function setRun(prefix, info, fallbackInterval){
      const whenEl = $(`#run-${prefix}-when`);
      const nextEl = $(`#run-${prefix}-next`);
      if (!info || !info.ts){
        whenEl.textContent = 'не запускался';
        nextEl.textContent = `интервал ${fmtDuration(fallbackInterval)}`;
        return;
      }
      whenEl.textContent = fmtAgo(info.ts, rates.now);
      const interval = info.interval_sec || fallbackInterval;
      const nextIn = (info.ts + interval) - rates.now;
      nextEl.textContent = `интервал ${fmtDuration(interval)} · ` +
        (nextIn > 0 ? `следующий через ${fmtDuration(nextIn)}` : `следующий обход просрочен на ${fmtDuration(-nextIn)}`);
    }
    setRun('apply', lr.apply_watch, lim.apply_interval_sec);
    setRun('reply', lr.watch, lim.reply_interval_sec);
    setRun('boost', lr.boost_watch, lim.boost_interval_sec);
    $('#lim-boost-interval').textContent = fmtDuration(lim.boost_interval_sec);
  } catch(e){ /* ignore */ }
}

function escapeHtml(s){
  return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]));
}

let lastEvents = [];
let eventFilter = 'all';
let eventsPage = 0;
let eventsPageSize = 100;
let eventsTotal = 0;
let groupByChat = false;
const expandedChats = new Set();

function eventPreview(e){
  const p = e.payload || {};
  if (e.kind === 'reply') return p.reply || '';
  if (e.kind === 'apply') return p.letter || p.letter_preview || '';
  if (e.kind === 'error') return p.error || '';
  if (e.kind === 'pending_manual') {
    const t = p.triggers || [];
    const arr = Array.isArray(t) ? t : [];
    return (arr.length ? '🚩 ' + arr.slice(0, 3).join(', ') + ' · ' : '') + (p.description_preview || '');
  }
  if (e.kind === 'positive_signal') {
    const sig = p.signal_type || '';
    const label = ({interview:'Интервью', contact_request:'Контакт', under_review:'На рассмотрении'})[sig] || sig;
    return `🎯 ${label}${p.trigger ? ' · ' + p.trigger : ''}`;
  }
  if (e.kind === 'rejection') {
    const tail = p.history_tail || '';
    const blocks = tail.split('\\n\\n').map(s => s.trim()).filter(Boolean);
    let lastHr = '';
    for (let i = blocks.length - 1; i >= 0; i--){
      if (!blocks[i].startsWith('[Я')) { lastHr = blocks[i]; break; }
    }
    return '🚫 ' + lastHr.slice(0, 220);
  }
  if (e.kind === 'skip') {
    const reason = p.reason || '';
    let label = '';
    if (reason === 'duplicate') label = `дубль · ${p.match || ''}`;
    else if (reason === 'wrong_role') label = `чужая роль: ${p.trigger || ''}`;
    else if (reason === 'not_target_role') label = 'нет Python/AI/ML маркеров';
    else if (reason === 'rejection') label = 'отказ работодателя';
    else label = reason || (p.reply === 'SKIP' ? 'Claude вернул SKIP' : '—');
    return `<span class="reason-badge">${escapeHtml(label)}</span>`;
  }
  return '';
}

function renderEventRow(e){
  const p = e.payload || {};
  const url = p.vacancy_url || e.chat_url || '';
  const title = p.title || (e.vacancy || '').split(' · ')[0] || (e.kind === 'reply' ? '(чат)' : '');
  const employer = p.employer || ((e.vacancy || '').split(' · ')[1] || '');
  const titleCell = url
    ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank" onclick="event.stopPropagation()">${escapeHtml(title)}</a>`
    : escapeHtml(title);
  const tokens = (e.tokens_in || 0) + ' → ' + (e.tokens_out || 0);
  const previewHtml = eventPreview(e);
  const isHtml = previewHtml.startsWith('<');
  const previewCell = isHtml
    ? `<td class="preview">${previewHtml}<b>${escapeHtml(String(p.title || '').slice(0, 100))}</b></td>`
    : `<td class="preview"><b>${escapeHtml(previewHtml.slice(0, 220))}</b></td>`;
  return `
    <td class="muted">${fmtTime(e.ts)}</td>
    <td><span class="kind k-${e.kind}">${e.kind}</span></td>
    <td>${titleCell}</td>
    <td class="muted">${escapeHtml(employer)}</td>
    <td>${tokens}</td>
    <td>${e.cost_usd ? '$' + e.cost_usd.toFixed(4) : ''}</td>
    ${previewCell}`;
}

function applyFilter(){
  const tbody = $('#events tbody');
  const list = eventFilter === 'all'
    ? lastEvents
    : lastEvents.filter(e => e.kind === eventFilter);
  if (!list.length){
    tbody.innerHTML = `<tr><td colspan="7" class="muted">нет событий в этом фильтре</td></tr>`;
    return;
  }

  // Режим «свернуть по чату»: группируем reply+skip по chat_url, остальные — как обычно
  if (groupByChat) {
    const groups = new Map(); // key = chat_url, value = {newest, items[]}
    const ungrouped = [];
    for (const e of list) {
      const groupable = (e.kind === 'reply' || e.kind === 'skip') && e.chat_url;
      if (!groupable) { ungrouped.push(e); continue; }
      const key = e.chat_url;
      if (!groups.has(key)) groups.set(key, { url: key, items: [] });
      groups.get(key).items.push(e);
    }
    // Сортируем группы по времени самого свежего события (он же первый — list уже DESC)
    const rows = [];
    const flat = [];
    for (const [key, g] of groups) flat.push({ type: 'group', key, items: g.items });
    for (const e of ungrouped)    flat.push({ type: 'single', event: e });
    flat.sort((a, b) => {
      const ta = a.type === 'group' ? a.items[0].ts : a.event.ts;
      const tb = b.type === 'group' ? b.items[0].ts : b.event.ts;
      return tb - ta;
    });

    let html = '';
    for (const entry of flat) {
      if (entry.type === 'single') {
        const e = entry.event;
        html += `<tr data-id="${e.id}">${renderEventRow(e)}</tr>`;
        continue;
      }
      const head = entry.items[0];
      const count = entry.items.length;
      const isOpen = expandedChats.has(entry.key);
      const p = head.payload || {};
      const url = p.vacancy_url || head.chat_url || '';
      const title = p.title || (head.vacancy || '').split(' · ')[0] || '(чат)';
      const employer = p.employer || ((head.vacancy || '').split(' · ')[1] || '');
      const replies = entry.items.filter(x => x.kind === 'reply').length;
      const skips   = entry.items.filter(x => x.kind === 'skip').length;
      const titleCell = url
        ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank" onclick="event.stopPropagation()">${escapeHtml(title)}</a>`
        : escapeHtml(title);
      const totalTokens = entry.items.reduce((s, x) => s + (x.tokens_in||0) + (x.tokens_out||0), 0);
      const totalCost   = entry.items.reduce((s, x) => s + (x.cost_usd||0), 0);
      const lastReply   = entry.items.find(x => x.kind === 'reply');
      const previewTxt  = lastReply && lastReply.payload ? (lastReply.payload.reply || '') : '';
      html += `<tr class="chat-group-row ${isOpen ? 'expanded' : ''}" data-chat="${escapeHtml(entry.key)}">
        <td class="muted">${fmtTime(head.ts)}</td>
        <td><span class="kind k-reply">${isOpen ? '▼' : '▶'} chat</span>
            <span class="chat-count">${count}</span>
            ${replies ? `<span class="chat-count" style="background:var(--ok)">${replies}r</span>` : ''}
            ${skips ? `<span class="chat-count" style="background:var(--muted)">${skips}s</span>` : ''}</td>
        <td>${titleCell}</td>
        <td class="muted">${escapeHtml(employer)}</td>
        <td>${totalTokens.toLocaleString('ru-RU')}</td>
        <td>${totalCost ? '$' + totalCost.toFixed(4) : ''}</td>
        <td class="preview"><b>${escapeHtml(String(previewTxt).slice(0, 220))}</b></td>
      </tr>`;
      for (const e of entry.items) {
        html += `<tr class="chat-child-row ${isOpen ? 'visible' : ''}" data-id="${e.id}" data-parent="${escapeHtml(entry.key)}">${renderEventRow(e)}</tr>`;
      }
    }
    tbody.innerHTML = html;

    tbody.querySelectorAll('tr.chat-group-row').forEach(tr => {
      tr.addEventListener('click', (ev) => {
        if (ev.target.closest('a')) return;
        const key = tr.dataset.chat;
        if (expandedChats.has(key)) expandedChats.delete(key);
        else expandedChats.add(key);
        applyFilter();
      });
    });
    tbody.querySelectorAll('tr.chat-child-row').forEach(tr => {
      tr.addEventListener('click', () => {
        const id = parseInt(tr.dataset.id);
        const ev = lastEvents.find(x => x.id === id);
        if (ev) openModal(ev);
      });
    });
    return;
  }

  tbody.innerHTML = list.map(e => {
    const p = e.payload || {};
    const url = p.vacancy_url || e.chat_url || '';
    const title = p.title || (e.vacancy || '').split(' · ')[0] || (e.kind === 'reply' ? '(чат)' : '');
    const employer = p.employer || ((e.vacancy || '').split(' · ')[1] || '');
    const titleCell = url
      ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank" onclick="event.stopPropagation()">${escapeHtml(title)}</a>`
      : escapeHtml(title);
    const tokens = (e.tokens_in || 0) + ' → ' + (e.tokens_out || 0);
    const previewHtml = eventPreview(e);
    const isHtml = previewHtml.startsWith('<');
    const previewCell = isHtml
      ? `<td class="preview">${previewHtml}<b>${escapeHtml(String(p.title || '').slice(0, 100))}</b></td>`
      : `<td class="preview"><b>${escapeHtml(previewHtml.slice(0, 220))}</b></td>`;
    return `<tr data-id="${e.id}">
      <td class="muted">${fmtTime(e.ts)}</td>
      <td><span class="kind k-${e.kind}">${e.kind}</span></td>
      <td>${titleCell}</td>
      <td class="muted">${escapeHtml(employer)}</td>
      <td>${tokens}</td>
      <td>${e.cost_usd ? '$' + e.cost_usd.toFixed(4) : ''}</td>
      ${previewCell}
    </tr>`;
  }).join('');

  tbody.querySelectorAll('tr[data-id]').forEach(tr => {
    tr.addEventListener('click', () => {
      const id = parseInt(tr.dataset.id);
      const ev = lastEvents.find(x => x.id === id);
      if (ev) openModal(ev);
    });
  });
}

async function updateTabCounts(){
  // считаем по summary из /api/status (он и так загружается каждые 3с)
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    const s = d.summary || {};
    const map = {apply: s.applies, reply: s.replies, skip: s.skips, error: s.errors};
    let total = 0;
    for (const k of Object.keys(map)){
      const el = document.getElementById('cnt-' + k);
      if (el && map[k] !== undefined) { el.textContent = map[k]; total += map[k]; }
    }
    // pending_manual нет в summary — берём отдельным запросом
    const elPM = document.getElementById('cnt-pending_manual');
    if (elPM) {
      const r2 = await fetch('/api/events?limit=500&kind=pending_manual');
      const d2 = await r2.json();
      elPM.textContent = d2.events.length;
    }
    const elPS = document.getElementById('cnt-positive_signal');
    if (elPS) {
      const r3 = await fetch('/api/events?limit=500&kind=positive_signal');
      const d3 = await r3.json();
      elPS.textContent = d3.events.length;
    }
    const elRJ = document.getElementById('cnt-rejection');
    if (elRJ) {
      const r4 = await fetch('/api/events?limit=500&kind=rejection');
      const d4 = await r4.json();
      elRJ.textContent = d4.events.length;
    }
    const elAll = document.getElementById('cnt-all');
    if (elAll) elAll.textContent = total;
  } catch(e){ /* ignore */ }
}

document.addEventListener('click', (ev) => {
  const tab = ev.target.closest('#event-tabs .tab');
  if (!tab) return;
  document.querySelectorAll('#event-tabs .tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');
  eventFilter = tab.dataset.filter;
  eventsPage = 0; // сменили фильтр — на первую страницу
  refreshEvents();
});

$('#group-by-chat').addEventListener('change', (ev) => {
  groupByChat = ev.target.checked;
  try { localStorage.setItem('hh-group-by-chat', groupByChat ? '1' : '0'); } catch(_) {}
  applyFilter();
});
// восстановить настройку из localStorage
try {
  if (localStorage.getItem('hh-group-by-chat') === '1') {
    groupByChat = true;
    $('#group-by-chat').checked = true;
  }
} catch(_) {}

$('#page-size').addEventListener('change', (ev) => {
  eventsPageSize = parseInt(ev.target.value, 10) || 100;
  eventsPage = 0;
  refreshEvents();
});
$('#page-first').addEventListener('click', () => { eventsPage = 0; refreshEvents(); });
$('#page-prev').addEventListener('click',  () => { if (eventsPage > 0) { eventsPage--; refreshEvents(); }});
$('#page-next').addEventListener('click',  () => { eventsPage++; refreshEvents(); });
$('#page-last').addEventListener('click',  () => {
  eventsPage = Math.max(0, Math.ceil(eventsTotal / eventsPageSize) - 1);
  refreshEvents();
});

async function refreshEvents(){
  try {
    const offset = eventsPage * eventsPageSize;
    const params = new URLSearchParams({ limit: eventsPageSize, offset });
    if (eventFilter !== 'all') params.set('kind', eventFilter);
    const r = await fetch('/api/events?' + params.toString());
    const d = await r.json();
    lastEvents = d.events || [];
    eventsTotal = d.total || 0;
    // Если страница оказалась пустой (например, число записей уменьшилось) — отступим назад
    if (!lastEvents.length && eventsPage > 0) {
      eventsPage = Math.max(0, Math.ceil(eventsTotal / eventsPageSize) - 1);
      return refreshEvents();
    }
    applyFilter();
    updatePager();
    updateTabCounts();
  } catch(e){ /* ignore */ }
}

function updatePager(){
  const totalPages = Math.max(1, Math.ceil(eventsTotal / eventsPageSize));
  if (eventsPage >= totalPages) eventsPage = totalPages - 1;
  $('#page-info').textContent =
    `Стр. ${eventsPage + 1} / ${totalPages} · всего ${eventsTotal.toLocaleString('ru-RU')}`;
  $('#page-first').disabled = eventsPage <= 0;
  $('#page-prev').disabled  = eventsPage <= 0;
  $('#page-next').disabled  = eventsPage >= totalPages - 1;
  $('#page-last').disabled  = eventsPage >= totalPages - 1;
}

function openModal(e){
  const p = e.payload || {};
  const fullText = p.reply || p.letter || p.letter_preview || p.error || '(нет содержимого)';
  const url = p.vacancy_url || e.chat_url || '';
  const title = e.vacancy || e.kind;
  const tokens = `${e.tokens_in || 0} → ${e.tokens_out || 0}`;
  const cost = e.cost_usd ? '$' + e.cost_usd.toFixed(4) : '$0';
  const time = new Date(e.ts * 1000).toLocaleString('ru-RU');

  const linkHtml = url ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url)}</a>` : '<span class="muted">—</span>';
  const meta = `
    <div><b>Тип:</b> <span class="kind k-${e.kind}">${e.kind}</span> · <b>Время:</b> ${escapeHtml(time)}</div>
    <div><b>Модель:</b> ${escapeHtml(e.model || '—')} · <b>Токены:</b> ${tokens} · <b>Стоимость:</b> ${cost}</div>
    <div><b>Ссылка:</b> ${linkHtml}</div>
  `;
  $('#modal-title').textContent = title;
  $('#modal-meta').innerHTML = meta;
  const body = $('#modal-body');
  body.classList.add('body');
  body.textContent = fullText;
  $('#modal').classList.add('show');

  // Для событий из чата подгружаем полную историю ответов
  const isChatEvent = (e.kind === 'reply' || e.kind === 'skip') && e.chat_url;
  if (isChatEvent) {
    loadChatThread(e.chat_url, e.id);
  }
}

async function loadChatThread(chatUrl, currentId){
  const body = $('#modal-body');
  try {
    const r = await fetch('/api/chat-history?chat_url=' + encodeURIComponent(chatUrl));
    const d = await r.json();
    const items = d.history || [];
    if (items.length <= 1) return; // одно сообщение — оставляем как было
    body.classList.remove('body');
    body.innerHTML = `<div class="thread-empty">Вся история ответов бота в этом чате (${items.length} событий):</div>` +
      items.map(it => {
        const pp = it.payload || {};
        const t = new Date(it.ts * 1000).toLocaleString('ru-RU');
        const text = pp.reply === 'SKIP' || it.kind === 'skip'
          ? `[SKIP${pp.reason ? ' · ' + pp.reason : ''}${pp.blocked_text ? '] заблокированный текст: ' + pp.blocked_text : ']'}`
          : (pp.reply || '(пусто)');
        const tok = `${it.tokens_in || 0} → ${it.tokens_out || 0}`;
        const cost = it.cost_usd ? '$' + it.cost_usd.toFixed(4) : '';
        const cur = (it.id === currentId) ? ' ← это событие' : '';
        return `<div class="thread-item ${it.kind === 'skip' ? 'skip' : ''}">
          <div class="head">
            <span class="kind k-${it.kind}">${it.kind}</span>
            <span>${escapeHtml(t)}</span>
            <span>${tok}</span>
            <span>${cost}</span>
            <span style="color:var(--accent)">${cur}</span>
          </div>
          <div class="text">${escapeHtml(text)}</div>
        </div>`;
      }).join('');
  } catch(err){ /* оставляем одиночный текст */ }
}

function closeModal(){ $('#modal').classList.remove('show'); }
$('#modal-close').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (ev) => { if (ev.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeModal(); });

async function refreshLog(name){
  try {
    const r = await fetch('/api/log/' + name + '?lines=60');
    const t = await r.text();
    const el = document.getElementById('log-' + name);
    if (!el) return;
    const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
    el.textContent = t || '(пусто)';
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
  } catch(e){ /* ignore */ }
}

async function action(name, op){
  try {
    const r = await fetch(`/api/${name}/${op}`, { method: 'POST' });
    const d = await r.json();
    if (!d.ok && d.error) alert(d.error);
  } catch(e){ alert(e); }
  await refreshStatus();
}

document.querySelectorAll('.worker-row').forEach(row => {
  const name = row.dataset.name;
  row.querySelector('.btn-start').addEventListener('click', () => action(name, 'start'));
  row.querySelector('.btn-stop').addEventListener('click',  () => action(name, 'stop'));
  row.querySelector('.btn-restart').addEventListener('click', () => action(name, 'restart'));
});

async function refreshPending(){
  try {
    const r = await fetch('/api/pending'); const d = await r.json();
    $('#pending-count').textContent = '— ' + d.pending.length;
    const tbody = $('#pending tbody');
    if (!d.pending.length){
      tbody.innerHTML = '<tr><td colspan="5" class="muted">пусто</td></tr>';
      return;
    }
    tbody.innerHTML = d.pending.map(p => {
      const url = (p.payload && p.payload.vacancy_url) || p.chat_url || '';
      const triggers = (p.payload && p.payload.triggers) || [];
      const desc = (p.payload && p.payload.description_preview) || '';
      const link = url
        ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank">${escapeHtml(p.vacancy || 'вакансия')}</a>`
        : escapeHtml(p.vacancy || '');
      const trigsHtml = triggers.map(t => `<span class="trigger">${escapeHtml(t)}</span>`).join('');
      return `<tr data-id="${p.id}">
        <td class="muted">${fmtTime(p.ts)}</td>
        <td>${link}</td>
        <td>${trigsHtml}</td>
        <td class="preview"><b>${escapeHtml(desc.slice(0, 200))}</b></td>
        <td>
          <button class="btn-mini ok"      data-act="resolve" data-id="${p.id}">✓ отправлено</button>
          <button class="btn-mini dismiss" data-act="dismiss" data-id="${p.id}">× не подходит</button>
        </td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const act = btn.dataset.act, id = btn.dataset.id;
        const r = await fetch(`/api/pending/${id}/${act}`, { method: 'POST' });
        if (r.ok) await refreshPending();
        else alert('не удалось обновить статус');
      });
    });
    tbody.querySelectorAll('tr[data-id]').forEach(tr => {
      tr.addEventListener('click', () => {
        const id = parseInt(tr.dataset.id);
        const p = d.pending.find(x => x.id === id);
        if (p) openPendingModal(p);
      });
    });
  } catch(e){ /* ignore */ }
}

function openPendingModal(p){
  const url = (p.payload && p.payload.vacancy_url) || p.chat_url || '';
  const triggers = (p.payload && p.payload.triggers) || [];
  const desc = (p.payload && p.payload.description_preview) || '';
  const time = new Date(p.ts * 1000).toLocaleString('ru-RU');
  const linkHtml = url ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url)}</a>` : '<span class="muted">—</span>';
  const trigsHtml = triggers.map(t => `<span class="trigger">${escapeHtml(t)}</span>`).join(' ');
  const addendums = p.suggested_addendums || [];
  const addLabel = addendums.length ? `<span class="muted">(в шаблон добавлено: ${escapeHtml(addendums.join(', '))})</span>` : '';
  const meta = `
    <div><b>Триггеры:</b> ${trigsHtml || '<span class="muted">—</span>'}</div>
    <div><b>Время:</b> ${escapeHtml(time)} · <b>Ссылка:</b> ${linkHtml}</div>
  `;
  const draft = p.suggested_letter || '';
  const descBlock = desc
    ? `<details class="desc-fold"><summary>📄 Описание вакансии (превью)</summary><div>${escapeHtml(desc)}</div></details>`
    : '';
  const editor = `
    ${descBlock}
    <div class="muted" style="margin-bottom:6px">✍️ Текст письма ${addLabel}</div>
    <textarea class="editor-area" id="editor-textarea">${escapeHtml(draft)}</textarea>
    <div class="char-counter" id="char-counter">0 / 2000</div>
    <div class="editor-actions">
      <span class="editor-status" id="editor-status"></span>
      <button class="btn btn-mini" id="editor-reset">↺ сбросить</button>
      <button class="btn btn-mini dismiss" id="editor-dismiss">× не подходит</button>
      <button class="btn btn-send" id="editor-send">📤 отправить</button>
    </div>
  `;
  $('#modal-title').textContent = p.vacancy || 'вакансия';
  $('#modal-meta').innerHTML = meta;
  $('#modal-body').innerHTML = editor;
  $('#modal-body').classList.remove('body');  // снимаем стиль pre
  $('#modal').classList.add('show');

  const ta = document.getElementById('editor-textarea');
  const status = document.getElementById('editor-status');
  const counter = document.getElementById('char-counter');
  const sendBtn = () => document.getElementById('editor-send');
  function updateCounter(){
    const n = ta.value.length;
    counter.textContent = `${n} / 2000`;
    counter.className = 'char-counter' + (n > 2000 ? ' over' : (n > 1700 ? ' warn' : ''));
    if (sendBtn()) sendBtn().disabled = (n === 0 || n > 2000);
  }
  ta.addEventListener('input', updateCounter);
  updateCounter();
  ta.focus();

  document.getElementById('editor-reset').addEventListener('click', () => {
    ta.value = draft;
    updateCounter();
    status.textContent = 'сброшено';
    status.className = 'editor-status muted';
  });

  document.getElementById('editor-dismiss').addEventListener('click', async () => {
    if (!confirm('Удалить вакансию из очереди без отклика?')) return;
    const r = await fetch(`/api/pending/${p.id}/dismiss`, { method: 'POST' });
    if (r.ok) { closeModal(); await refreshPending(); }
    else { status.textContent = 'не удалось'; status.className = 'editor-status err'; }
  });

  document.getElementById('editor-send').addEventListener('click', async () => {
    const letter = ta.value.trim();
    if (!letter) { status.textContent = 'письмо пустое'; status.className = 'editor-status err'; return; }
    if (!confirm('Отправить отклик с этим текстом? Действие необратимо.')) return;
    document.getElementById('editor-send').disabled = true;
    document.getElementById('editor-dismiss').disabled = true;
    status.textContent = '⏳ открываю HH через Playwright, обычно 10-20 секунд...';
    status.className = 'editor-status muted';
    try {
      const r = await fetch(`/api/pending/${p.id}/send`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ letter }),
      });
      const d = await r.json();
      if (d.ok) {
        status.textContent = `✓ отправлено${d.reason ? ' · ' + d.reason : ''}`;
        status.className = 'editor-status ok';
        setTimeout(async () => { closeModal(); await Promise.all([refreshPending(), refreshEvents()]); }, 1200);
      } else {
        status.textContent = '✗ ' + (d.error || 'ошибка');
        status.className = 'editor-status err';
        document.getElementById('editor-send').disabled = false;
        document.getElementById('editor-dismiss').disabled = false;
      }
    } catch(e) {
      status.textContent = '✗ ' + e;
      status.className = 'editor-status err';
      document.getElementById('editor-send').disabled = false;
      document.getElementById('editor-dismiss').disabled = false;
    }
  });
}

const SIGNAL_LABEL = {
  interview: 'Интервью / созвон',
  contact_request: 'Просят контакт',
  under_review: 'На рассмотрении',
};

function lastHrMessage(historyTail){
  if (!historyTail) return '';
  const blocks = historyTail.split('\\n\\n').map(s => s.trim()).filter(Boolean);
  for (let i = blocks.length - 1; i >= 0; i--){
    if (!blocks[i].startsWith('[Я')) return blocks[i];
  }
  return blocks[blocks.length - 1] || '';
}

async function refreshPositive(){
  try {
    const r = await fetch('/api/positive'); const d = await r.json();
    const items = d.positive || [];
    $('#positive-count').textContent = '— ' + items.length;
    const tbody = $('#positive tbody');
    if (!items.length){
      tbody.innerHTML = '<tr><td colspan="6" class="muted">пока пусто</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(it => {
      const p = it.payload || {};
      const url = it.chat_url || '';
      const vac = it.vacancy || '(чат)';
      const sig = it.signal_type || 'positive';
      const label = SIGNAL_LABEL[sig] || sig;
      const trigger = p.trigger || '';
      const lastMsg = lastHrMessage(p.history_tail || '');
      const link = url
        ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank">${escapeHtml(vac)}</a>`
        : escapeHtml(vac);
      return `<tr data-id="${it.id}">
        <td class="muted">${fmtAgo(it.ts, Date.now()/1000)}</td>
        <td>${link}</td>
        <td><span class="sig-badge sig-${escapeHtml(sig)}">${escapeHtml(label)}</span></td>
        <td class="muted">${escapeHtml(trigger)}</td>
        <td class="preview"><b>${escapeHtml(lastMsg.slice(0, 240))}</b></td>
        <td>
          <button class="btn-mini ok" data-act="resolve-positive" data-id="${it.id}">✓ обработано</button>
        </td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('button[data-act="resolve-positive"]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const id = btn.dataset.id;
        const r = await fetch(`/api/positive/${id}/resolve`, { method: 'POST' });
        if (r.ok) await refreshPositive();
        else alert('не удалось обновить статус');
      });
    });
    tbody.querySelectorAll('tr[data-id]').forEach(tr => {
      tr.addEventListener('click', () => {
        const id = parseInt(tr.dataset.id);
        const it = items.find(x => x.id === id);
        if (it) openPositiveModal(it);
      });
    });
  } catch(e){ /* ignore */ }
}

function openPositiveModal(it){
  const p = it.payload || {};
  const url = it.chat_url || '';
  const time = new Date(it.ts * 1000).toLocaleString('ru-RU');
  const sig = it.signal_type || 'positive';
  const label = SIGNAL_LABEL[sig] || sig;
  const linkHtml = url ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url)}</a>` : '<span class="muted">—</span>';
  const meta = `
    <div><b>Сигнал:</b> <span class="sig-badge sig-${escapeHtml(sig)}">${escapeHtml(label)}</span> · <b>Триггер:</b> ${escapeHtml(p.trigger || '—')}</div>
    <div><b>Время:</b> ${escapeHtml(time)} · <b>Чат:</b> ${linkHtml}</div>
  `;
  $('#modal-title').textContent = it.vacancy || 'чат';
  $('#modal-meta').innerHTML = meta;
  $('#modal-body').classList.add('body');
  $('#modal-body').textContent = p.history_tail || '(история не сохранилась)';
  $('#modal').classList.add('show');
}

async function refreshRejections(){
  try {
    const r = await fetch('/api/rejections'); const d = await r.json();
    const items = d.rejections || [];
    $('#rejection-count').textContent = '— ' + items.length;
    const tbody = $('#rejections tbody');
    if (!items.length){
      tbody.innerHTML = '<tr><td colspan="3" class="muted">пока пусто</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(it => {
      const p = it.payload || {};
      const url = it.chat_url || '';
      const vac = it.vacancy || '(чат)';
      const lastMsg = lastHrMessage(p.history_tail || '');
      const link = url
        ? `<a class="row-link" href="${escapeHtml(url)}" target="_blank">${escapeHtml(vac)}</a>`
        : escapeHtml(vac);
      return `<tr data-id="${it.id}">
        <td class="muted">${fmtAgo(it.ts, Date.now()/1000)}</td>
        <td>${link}</td>
        <td class="preview"><b>${escapeHtml(lastMsg.slice(0, 280))}</b></td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('tr[data-id]').forEach(tr => {
      tr.addEventListener('click', () => {
        const id = parseInt(tr.dataset.id);
        const it = items.find(x => x.id === id);
        if (it) openRejectionModal(it);
      });
    });
  } catch(e){ /* ignore */ }
}

function openRejectionModal(it){
  const p = it.payload || {};
  const url = it.chat_url || '';
  const time = new Date(it.ts * 1000).toLocaleString('ru-RU');
  const linkHtml = url ? `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url)}</a>` : '<span class="muted">—</span>';
  $('#modal-title').textContent = it.vacancy || 'чат';
  $('#modal-meta').innerHTML = `<div><b>Время:</b> ${escapeHtml(time)} · <b>Чат:</b> ${linkHtml}</div>`;
  $('#modal-body').classList.add('body');
  $('#modal-body').textContent = p.history_tail || '(история не сохранилась)';
  $('#modal').classList.add('show');
}

async function tick(){
  await Promise.all([
    refreshStatus(),
    refreshPending(),
    refreshPositive(),
    refreshRejections(),
    refreshEvents(),
    refreshLog('apply'),
    refreshLog('reply'),
    refreshLog('boost'),
  ]);
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HH_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("HH_DASHBOARD_PORT", "8765"))
    print(f"\n🌐 Дашборд запущен: http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
