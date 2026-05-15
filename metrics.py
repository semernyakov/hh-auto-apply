"""Лог событий и метрик для HH-бота.

Хранит SQLite в ~/.n8n-files/hh_metrics.sqlite. Пишет каждое LLM-обращение
с tokens_in, tokens_out, моделью и контекстом. Дешбоард читает оттуда.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

DB_PATH = os.path.join(
    os.getenv("N8N_FILES_DIR", os.path.expanduser("~/.n8n-files")),
    "hh_metrics.sqlite",
)

# Тарифы Anthropic, USD за 1M токенов.
# https://www.anthropic.com/pricing
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6-20251022": {"input": 3.00, "output": 15.00},
    "claude-opus-4-7-20251101": {"input": 15.00, "output": 75.00},
}
DEFAULT_PRICING = {"input": 1.00, "output": 5.00}


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def _conn():
    _ensure_dir()
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,           -- 'reply', 'apply', 'skip', 'error', 'run_start', 'run_end'
            model TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            vacancy TEXT,
            chat_url TEXT,
            payload_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts DESC);
        """)


def calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICING.get(model, DEFAULT_PRICING)
    return tokens_in * p["input"] / 1_000_000 + tokens_out * p["output"] / 1_000_000


def log_event(
    kind: str,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    vacancy: str = "",
    chat_url: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    cost = calc_cost(model, tokens_in, tokens_out) if tokens_in or tokens_out else 0.0
    with _conn() as c:
        c.execute(
            """INSERT INTO events
               (ts, kind, model, tokens_in, tokens_out, cost_usd, vacancy, chat_url, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                kind,
                model,
                tokens_in,
                tokens_out,
                cost,
                vacancy,
                chat_url,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )


def get_summary() -> dict[str, Any]:
    with _conn() as c:
        row = c.execute(
            """SELECT
                 COUNT(*)                             AS total_calls,
                 COALESCE(SUM(tokens_in), 0)          AS tokens_in,
                 COALESCE(SUM(tokens_out), 0)         AS tokens_out,
                 COALESCE(SUM(cost_usd), 0)           AS cost_usd,
                 SUM(CASE WHEN kind='reply'  THEN 1 ELSE 0 END) AS replies,
                 SUM(CASE WHEN kind='apply'  THEN 1 ELSE 0 END) AS applies,
                 SUM(CASE WHEN kind='skip'   THEN 1 ELSE 0 END) AS skips,
                 SUM(CASE WHEN kind='error'  THEN 1 ELSE 0 END) AS errors
               FROM events"""
        ).fetchone()
        today = c.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) AS today_cost,
                      COALESCE(SUM(tokens_in), 0) AS today_tokens_in,
                      COALESCE(SUM(tokens_out), 0) AS today_tokens_out
               FROM events
               WHERE ts >= strftime('%s', 'now', 'start of day')"""
        ).fetchone()
    return {
        "total_calls": row["total_calls"] or 0,
        "tokens_in": row["tokens_in"] or 0,
        "tokens_out": row["tokens_out"] or 0,
        "cost_usd": float(row["cost_usd"] or 0),
        "replies": row["replies"] or 0,
        "applies": row["applies"] or 0,
        "skips": row["skips"] or 0,
        "errors": row["errors"] or 0,
        "today_cost_usd": float(today["today_cost"] or 0),
        "today_tokens_in": today["today_tokens_in"] or 0,
        "today_tokens_out": today["today_tokens_out"] or 0,
    }


def get_rejections(limit: int = 100) -> list[dict[str, Any]]:
    """События 'rejection' (отказы работодателей) с дедупом по chat_url."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, kind, vacancy, chat_url, payload_json FROM events
               WHERE kind = 'rejection'
                 AND id IN (
                   SELECT MAX(id) FROM events
                   WHERE kind = 'rejection'
                   GROUP BY COALESCE(NULLIF(chat_url, ''), CAST(id AS TEXT))
                 )
               ORDER BY ts DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else {}
        out.append(d)
    return out


def get_positive_signals(limit: int = 100) -> list[dict[str, Any]]:
    """Все события 'positive_signal' за всё время, дедупликация по chat_url
    (берём самое свежее на чат, чтобы не плодить дубли при повторных watch-обходах).

    Возвращает уже разобранный payload и поле signal_type для удобства фронта.
    """
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, kind, vacancy, chat_url, payload_json FROM events
               WHERE kind = 'positive_signal'
                 AND id IN (
                   SELECT MAX(id) FROM events
                   WHERE kind = 'positive_signal'
                   GROUP BY COALESCE(NULLIF(chat_url, ''), CAST(id AS TEXT))
                 )
               ORDER BY ts DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else {}
        d["signal_type"] = (d["payload"] or {}).get("signal_type", "")
        out.append(d)
    return out


def get_robot_questionnaires(limit: int = 200) -> list[dict[str, Any]]:
    """События 'robot_questionnaire' (анкеты автоботов — требуют ручного ответа).
    Дедуп по chat_url: берём самое свежее на чат, чтобы серия из 5 вопросов
    подряд от одного робота не плодила 5 одинаковых записей."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, kind, vacancy, chat_url, payload_json FROM events
               WHERE kind = 'robot_questionnaire'
                 AND id IN (
                   SELECT MAX(id) FROM events
                   WHERE kind = 'robot_questionnaire'
                   GROUP BY COALESCE(NULLIF(chat_url, ''), CAST(id AS TEXT))
                 )
               ORDER BY ts DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else {}
        out.append(d)
    return out


def get_pending_manual() -> list[dict[str, Any]]:
    """Возвращает все события вида 'pending_manual', которые ещё не обработаны."""
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, kind, vacancy, chat_url, payload_json
               FROM events
               WHERE kind = 'pending_manual'
               ORDER BY ts DESC"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else {}
        out.append(d)
    return out


def resolve_event(event_id: int, new_kind: str, expected_kind: str = "pending_manual") -> bool:
    """Меняет kind у события только если текущий совпадает с expected_kind."""
    with _conn() as c:
        # читаем-меняем-пишем payload через python, чтобы не зависеть от json_set
        row = c.execute("SELECT payload_json FROM events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return False
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        payload["resolved_at"] = time.time()
        cur = c.execute(
            "UPDATE events SET kind = ?, payload_json = ? WHERE id = ? AND kind = ?",
            (new_kind, json.dumps(payload, ensure_ascii=False), event_id, expected_kind),
        )
        return cur.rowcount > 0


def get_recent(limit: int = 50, kinds: Iterable[str] | None = None, offset: int = 0) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if kinds:
        ks = list(kinds)
        where = "WHERE kind IN (" + ",".join(["?"] * len(ks)) + ")"
        params.extend(ks)
    params.extend([limit, max(0, offset)])
    with _conn() as c:
        rows = c.execute(
            f"""SELECT id, ts, kind, model, tokens_in, tokens_out, cost_usd,
                       vacancy, chat_url, payload_json
                FROM events
                {where}
                ORDER BY ts DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
        out.append(d)
    return out


def count_events(kinds: Iterable[str] | None = None) -> int:
    where = ""
    params: list[Any] = []
    if kinds:
        ks = list(kinds)
        where = "WHERE kind IN (" + ",".join(["?"] * len(ks)) + ")"
        params.extend(ks)
    with _conn() as c:
        row = c.execute(f"SELECT COUNT(*) AS n FROM events {where}", params).fetchone()
    return int(row["n"]) if row else 0


def get_chat_history(chat_url: str) -> list[dict[str, Any]]:
    """Все события (reply/skip) для одного chat_url, по возрастанию времени."""
    if not chat_url:
        return []
    with _conn() as c:
        rows = c.execute(
            """SELECT id, ts, kind, model, tokens_in, tokens_out, cost_usd,
                      vacancy, chat_url, payload_json
               FROM events
               WHERE chat_url = ? AND kind IN ('reply','skip')
               ORDER BY ts ASC""",
            (chat_url,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
        out.append(d)
    return out


def get_reply_conversion(days: int = 60) -> dict[str, Any]:
    """Конверсия наших ответов в чатах: сколько привели к положительному сигналу
    (приглашение/контакт/на рассмотрении) vs к отказу vs остались без ответа.

    Окно = последние `days` дней. Под «привели» понимается: для chat_url, где
    был хотя бы один наш reply, ищем positive_signal / rejection с ts > времени
    первого reply в этом чате. Один chat — одна запись в знаменателе.
    """
    since = time.time() - days * 86400
    with _conn() as c:
        row = c.execute(
            """WITH replied AS (
                 SELECT chat_url, MIN(ts) AS first_reply_ts
                 FROM events
                 WHERE kind = 'reply' AND chat_url <> '' AND ts >= ?
                 GROUP BY chat_url
               ),
               followup AS (
                 SELECT r.chat_url,
                        MAX(CASE WHEN e.kind = 'positive_signal' AND e.ts > r.first_reply_ts THEN 1 ELSE 0 END) AS pos,
                        MAX(CASE WHEN e.kind = 'rejection'       AND e.ts > r.first_reply_ts THEN 1 ELSE 0 END) AS rej
                 FROM replied r
                 LEFT JOIN events e ON e.chat_url = r.chat_url
                 GROUP BY r.chat_url
               )
               SELECT
                 COUNT(*)                                              AS total,
                 COALESCE(SUM(pos), 0)                                 AS to_positive,
                 COALESCE(SUM(rej), 0)                                 AS to_rejection,
                 COALESCE(SUM(CASE WHEN pos=0 AND rej=0 THEN 1 END), 0) AS pending
               FROM followup""",
            (since,),
        ).fetchone()
    total = int(row["total"] or 0)
    pos = int(row["to_positive"] or 0)
    rej = int(row["to_rejection"] or 0)
    pending = int(row["pending"] or 0)
    return {
        "days": days,
        "total_chats_replied": total,
        "to_positive": pos,
        "to_rejection": rej,
        "pending": pending,
        "positive_pct": round(pos * 100.0 / total, 1) if total else 0.0,
        "rejection_pct": round(rej * 100.0 / total, 1) if total else 0.0,
    }


def get_rate_stats() -> dict[str, Any]:
    """Активность по временным окнам + последние запуски воркеров.

    Возвращает:
      now: серверное время (epoch)
      windows: {'1h': {...}, '24h': {...}, '7d': {...}} — счётчики событий
      last_runs: {'apply_watch': {ts, interval_sec}, 'watch': {ts, interval_sec}}
    """
    now = time.time()
    windows_def = [("1h", 3600), ("24h", 86_400), ("7d", 7 * 86_400)]
    out: dict[str, Any] = {"now": now, "windows": {}, "last_runs": {}}

    with _conn() as c:
        for label, sec in windows_def:
            since = now - sec
            row = c.execute(
                """SELECT
                    SUM(CASE WHEN kind='apply'           THEN 1 ELSE 0 END) AS apply,
                    SUM(CASE WHEN kind='reply'           THEN 1 ELSE 0 END) AS reply,
                    SUM(CASE WHEN kind='skip'            THEN 1 ELSE 0 END) AS skip,
                    SUM(CASE WHEN kind='error'           THEN 1 ELSE 0 END) AS error,
                    SUM(CASE WHEN kind='manual_applied'  THEN 1 ELSE 0 END) AS manual_applied,
                    SUM(CASE WHEN kind='pending_manual'  THEN 1 ELSE 0 END) AS pending_manual,
                    SUM(CASE WHEN kind='boost'           THEN 1 ELSE 0 END) AS boost,
                    SUM(CASE WHEN kind='rejection'       THEN 1 ELSE 0 END) AS rejection,
                    SUM(CASE WHEN kind='positive_signal' THEN 1 ELSE 0 END) AS positive_signal,
                    SUM(CASE WHEN kind='robot_questionnaire' THEN 1 ELSE 0 END) AS robot_questionnaire
                   FROM events WHERE ts >= ?""",
                (since,),
            ).fetchone()
            dup = c.execute(
                """SELECT COUNT(*) AS c FROM events
                   WHERE kind='skip' AND ts >= ?
                     AND payload_json LIKE '%"reason": "duplicate"%'""",
                (since,),
            ).fetchone()
            out["windows"][label] = {
                "apply": row["apply"] or 0,
                "reply": row["reply"] or 0,
                "skip": row["skip"] or 0,
                "error": row["error"] or 0,
                "manual_applied": row["manual_applied"] or 0,
                "pending_manual": row["pending_manual"] or 0,
                "duplicate": dup["c"] or 0,
                "boost": row["boost"] or 0,
                "rejection": row["rejection"] or 0,
                "positive_signal": row["positive_signal"] or 0,
                "robot_questionnaire": row["robot_questionnaire"] or 0,
            }

        # последние run_start для каждого воркера
        for mode in ("apply_watch", "watch", "boost_watch"):
            row = c.execute(
                """SELECT ts, payload_json FROM events
                   WHERE kind='run_start' AND payload_json LIKE ?
                   ORDER BY ts DESC LIMIT 1""",
                (f'%"mode": "{mode}"%',),
            ).fetchone()
            if row:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
                out["last_runs"][mode] = {
                    "ts": row["ts"],
                    "interval_sec": int(payload.get("interval_sec") or 0),
                }
    return out


_VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)|[?&]vacancyId=(\d+)")
_PARENS_RE = re.compile(r"[\(\[\{][^()\[\]{}]*[\)\]\}]")
_WS_RE = re.compile(r"\s+")


def extract_vacancy_id(url: str) -> str:
    """Возвращает числовой id вакансии из URL hh.ru (или '' если не нашли).

    Понимает два формата:
      - /vacancy/132788815                              (страница вакансии)
      - /applicant/vacancy_response?vacancyId=132788815 (форма отклика)
    """
    if not url:
        return ""
    m = _VACANCY_ID_RE.search(url)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def _norm(s: str) -> str:
    """Нормализация для сравнения: lower, без скобочного содержимого, без лишних пробелов."""
    if not s:
        return ""
    s = s.lower()
    s = _PARENS_RE.sub(" ", s)
    s = s.replace("ё", "е")
    s = _WS_RE.sub(" ", s).strip()
    return s


def load_applied_index(days: int = 60) -> dict[str, Any]:
    """Pre-load всех apply / pending_manual / manual_applied / manual_dismissed
    событий за окно в индекс для быстрых проверок in-memory. Учитывает и
    фактически отправленные отклики, и вакансии в ручной очереди, и явно
    отклонённые вручную (чтобы не плодить дубли при перезапусках бота).
    """
    since = time.time() - days * 86400
    by_vacancy_id: dict[str, dict[str, Any]] = {}
    by_employer_title: dict[tuple[str, str], dict[str, Any]] = {}
    with _conn() as c:
        rows = c.execute(
            """SELECT ts, kind, vacancy, chat_url, payload_json
               FROM events
               WHERE kind IN ('apply', 'pending_manual', 'manual_applied', 'manual_dismissed')
                 AND ts >= ?
               ORDER BY ts DESC""",
            (since,),
        ).fetchall()
    for r in rows:
        d = dict(r)
        vid = extract_vacancy_id(d["chat_url"] or "")
        if vid and vid not in by_vacancy_id:
            by_vacancy_id[vid] = d
        if d["payload_json"]:
            p = json.loads(d["payload_json"])
            key = (_norm(p.get("employer", "")), _norm(p.get("title", "")))
            if key[0] and key[1] and key not in by_employer_title:
                by_employer_title[key] = d
    return {"by_vacancy_id": by_vacancy_id, "by_employer_title": by_employer_title}


def check_applied(
    index: dict[str, Any],
    url: str,
    employer: str = "",
    title: str = "",
) -> dict[str, Any] | None:
    """In-memory проверка по индексу из load_applied_index. Стоимость — O(1)."""
    vid = extract_vacancy_id(url)
    if vid:
        hit = index["by_vacancy_id"].get(vid)
        if hit:
            return {
                "match": "vacancy_id",
                "vacancy_id": vid,
                "ts": hit["ts"],
                "vacancy": hit["vacancy"],
                "chat_url": hit["chat_url"],
            }
    employer_n = _norm(employer)
    title_n = _norm(title)
    if employer_n and title_n:
        hit = index["by_employer_title"].get((employer_n, title_n))
        if hit:
            return {
                "match": "employer_title",
                "employer": employer,
                "title": title,
                "ts": hit["ts"],
                "vacancy": hit["vacancy"],
                "chat_url": hit["chat_url"],
            }
    return None


def is_already_applied(
    url: str,
    employer: str = "",
    title: str = "",
    days: int = 60,
) -> dict[str, Any] | None:
    """Совместимость со старыми вызовами. Внутри грузит индекс заново — O(N).
    Для горячего пути используй load_applied_index + check_applied.
    """
    return check_applied(load_applied_index(days), url, employer, title)


if __name__ == "__main__":
    init_db()
    s = get_summary()
    print("DB:", DB_PATH)
    print("summary:", s)
