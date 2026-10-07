# -*- coding: utf-8 -*-
"""
database.py — база данных бота (SQLite) и функции работы с ней.
Файл anon_bot.db создаётся автоматически рядом с этим файлом.
Кладите database.py В ОДНУ ПАПКУ с anon_bot.py.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anon_bot.db")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.RLock()


def execute(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def one(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchone()


def many(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def init_db():
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id          INTEGER PRIMARY KEY,
            first_name  TEXT DEFAULT '',
            last_name   TEXT DEFAULT '',
            created_at  INTEGER,
            accepting   INTEGER DEFAULT 1,
            banned      INTEGER DEFAULT 0,
            state       TEXT,
            state_data  TEXT DEFAULT '{}',
            last_ask_ts INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS questions(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            to_id       INTEGER NOT NULL,
            from_id     INTEGER NOT NULL,
            text        TEXT NOT NULL,
            answer      TEXT,
            status      TEXT DEFAULT 'new',      -- new | answered | deleted
            reported    INTEGER DEFAULT 0,
            delivered   INTEGER DEFAULT 1,
            created_at  INTEGER,
            answered_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS blocks(
            owner_id   INTEGER,
            blocked_id INTEGER,
            PRIMARY KEY(owner_id, blocked_id)
        );
        CREATE TABLE IF NOT EXISTS broadcasts(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT,
            created_at INTEGER,
            total      INTEGER DEFAULT 0,
            sent       INTEGER DEFAULT 0,
            failed     INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'running'    -- running | done
        );
        CREATE INDEX IF NOT EXISTS ix_q_to   ON questions(to_id, status);
        CREATE INDEX IF NOT EXISTS ix_q_time ON questions(created_at);
        """)
        _conn.commit()


def now():
    return int(time.time())


# ───────────────────────────── ПОЛЬЗОВАТЕЛИ ─────────────────────────────
def get_user(uid):
    return one("SELECT * FROM users WHERE id=?", (uid,))


def uname(uid):
    u = get_user(uid)
    return f"{u['first_name']} {u['last_name']}".strip() if u else f"id{uid}"


def set_state(uid, state=None, data=None):
    execute("UPDATE users SET state=?, state_data=? WHERE id=?",
            (state, json.dumps(data or {}, ensure_ascii=False), uid))


def state_data(user):
    try:
        return json.loads(user["state_data"] or "{}")
    except Exception:
        return {}


# ───────────────────────────── СТАТИСТИКА ─────────────────────────────
def day_start(offset=0):
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=offset)
    return int(d.timestamp())


def get_stats():
    s = {}
    s["users"] = one("SELECT COUNT(*) c FROM users")["c"]
    s["banned"] = one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"]
    s["new_today"] = one("SELECT COUNT(*) c FROM users WHERE created_at>=?", (day_start(),))["c"]
    s["questions"] = one("SELECT COUNT(*) c FROM questions WHERE status!='deleted'")["c"]
    s["answered"] = one("SELECT COUNT(*) c FROM questions WHERE status='answered'")["c"]
    s["reported"] = one("SELECT COUNT(*) c FROM questions WHERE reported=1 AND status!='deleted'")["c"]
    s["today"] = one("SELECT COUNT(*) c FROM questions WHERE created_at>=?", (day_start(),))["c"]
    s["rate"] = round(s["answered"] * 100 / s["questions"]) if s["questions"] else 0
    chart = []
    for i in range(13, -1, -1):
        a, b = day_start(i), day_start(i - 1)
        n = one("SELECT COUNT(*) c FROM questions WHERE created_at>=? AND created_at<?", (a, b))["c"]
        chart.append({"label": datetime.fromtimestamp(a).strftime("%d.%m"), "n": n})
    mx = max([c["n"] for c in chart] + [1])
    for c in chart:
        c["pct"] = max(3, round(c["n"] * 100 / mx)) if c["n"] else 0
    s["chart"] = chart
    return s
