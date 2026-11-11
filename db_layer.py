import asyncio
import sqlite3
from typing import Any, Iterable

DB_PATH = "osbb_acts.db"
DB_WRITE_LOCK = asyncio.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db_sync() -> None:
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        
        # 1. Акти (з частковими платежами, сумами та ключовим словом для переглядача)
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS acts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT,
                osbb TEXT,
                descr TEXT,
                file_id TEXT,
                status TEXT DEFAULT "Не отримано",
                amount REAL DEFAULT 0.0,
                paid_amount REAL DEFAULT 0.0,
                viewer_keyword TEXT,
                created_at TEXT
            )"""
        )
        
        # 2. Чеки / Документи витрат (з ціллю покупців)
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                osbb TEXT,
                file_id TEXT,
                status TEXT DEFAULT "Не отримано",
                purpose TEXT,
                created_at TEXT
            )"""
        )
        
        # 3. Зарплати
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS salaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_year TEXT,
                employee TEXT,
                amount REAL,
                osbb TEXT,
                status TEXT DEFAULT "⏳ Очікує"
            )"""
        )
        
        # 4. План робіт та регламентні задачі
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                osbb TEXT,
                month_year TEXT,
                task_text TEXT,
                status TEXT DEFAULT "Створено",
                stages TEXT DEFAULT "",
                comments TEXT DEFAULT "",
                updated_at TEXT,
                created_at TEXT
            )"""
        )
        
        # 5. Квартири та кабінети мешканців
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS apartments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                osbb TEXT,
                apt_number TEXT,
                account_number TEXT,
                residents_count INTEGER DEFAULT 1,
                access_key TEXT,
                meters_config TEXT DEFAULT "ХВП,ГВП,Опалення,Електроенергія"
            )"""
        )
        
        # 6. Показники лічильників
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS meter_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apt_id INTEGER,
                month_year TEXT,
                meter_type TEXT,
                value TEXT,
                photo_id TEXT,
                status TEXT DEFAULT "На перевірці",
                comment TEXT,
                created_at TEXT
            )"""
        )
        
        conn.commit()
    finally:
        conn.close()


def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or cur.rowcount or 0)
    finally:
        conn.close()


async def db_init() -> None:
    await asyncio.to_thread(init_db_sync)


async def db_fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch_all, sql, tuple(params))


async def db_fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return await asyncio.to_thread(fetch_one, sql, tuple(params))


async def db_execute(sql: str, params: Iterable[Any] = ()) -> int:
    async with DB_WRITE_LOCK:
        return await asyncio.to_thread(execute, sql, tuple(params))
