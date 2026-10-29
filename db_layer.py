"""SQLite storage and forward-only migrations for the OSBB bot."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(os.getenv("OSBB_DB_PATH", "data/osbb_acts.db"))
_WRITE_LOCK = asyncio.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with transaction() as conn:
        cur = conn.execute(sql, tuple(params))
        return int(cur.lastrowid or cur.rowcount or 0)


def execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with transaction() as conn:
        conn.executemany(sql, [tuple(row) for row in rows])


async def db_fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch_all, sql, tuple(params))


async def db_fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return await asyncio.to_thread(fetch_one, sql, tuple(params))


async def db_execute(sql: str, params: Iterable[Any] = ()) -> int:
    async with _WRITE_LOCK:
        return await asyncio.to_thread(execute, sql, tuple(params))


async def db_execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    async with _WRITE_LOCK:
        await asyncio.to_thread(execute_many, sql, [tuple(r) for r in rows])


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for column, declaration in definitions.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')


def init_db_sync(superadmin_id: int | None = None) -> None:
    """Create new tables and add fields without rewriting legacy business records."""
    with transaction() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS acts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT,
                file_id TEXT, status TEXT DEFAULT 'Не отримано', created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT,
                status TEXT DEFAULT 'Не отримано', created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS salaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT,
                amount REAL, osbb TEXT, status TEXT DEFAULT '⏳ Очікує'
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, osbb TEXT, month_year TEXT,
                task_text TEXT, status TEXT DEFAULT 'Створено', stages TEXT DEFAULT '',
                comments TEXT DEFAULT '', updated_at TEXT, created_at TEXT
            );
            """
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                owner_type TEXT NOT NULL DEFAULT 'admin' CHECK(owner_type IN ('admin','fop')),
                owner_user_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
                payment_status TEXT NOT NULL DEFAULT 'paid' CHECK(payment_status IN ('paid','unpaid','trial','blocked')),
                paid_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT 'osbb' CHECK(kind IN ('osbb','cooperative','fop')),
                active INTEGER NOT NULL DEFAULT 1,
                apartment_count INTEGER NOT NULL DEFAULT 0,
                meters_enabled INTEGER NOT NULL DEFAULT 1,
                resident_code TEXT,
                leadership_code TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL DEFAULT '',
                username TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                role TEXT NOT NULL CHECK(role IN ('admin','accountant','viewer','leadership','user','fop')),
                keywords TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id,organization_id,role)
            );
            CREATE TABLE IF NOT EXISTS apartments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                number TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                resident_pin TEXT,
                resident_pin_failures INTEGER NOT NULL DEFAULT 0,
                resident_pin_blocked_until TEXT,
                resident_info TEXT NOT NULL DEFAULT '',
                resident_count INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(organization_id,number)
            );
            CREATE TABLE IF NOT EXISTS apartment_members (
                apartment_id INTEGER NOT NULL REFERENCES apartments(id),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(apartment_id,telegram_id)
            );
            CREATE TABLE IF NOT EXISTS meter_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apartment_id INTEGER NOT NULL REFERENCES apartments(id),
                metric TEXT NOT NULL CHECK(metric IN ('electricity','cold_water','hot_water','heating','people')),
                mode TEXT NOT NULL DEFAULT 'meter' CHECK(mode IN ('meter','people')),
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(apartment_id,metric)
            );
            CREATE TABLE IF NOT EXISTS resident_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apartment_id INTEGER NOT NULL REFERENCES apartments(id),
                person_count INTEGER NOT NULL,
                starts_on TEXT NOT NULL,
                ends_on TEXT NOT NULL,
                act_file_id TEXT,
                notice_seen INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS period_reminder_logs (
                period_id INTEGER NOT NULL REFERENCES resident_periods(id) ON DELETE CASCADE,
                reminder_kind TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(period_id,reminder_kind,telegram_id)
            );
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apartment_id INTEGER NOT NULL REFERENCES apartments(id),
                metric TEXT NOT NULL,
                period TEXT NOT NULL,
                value TEXT NOT NULL,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
                decision_note TEXT NOT NULL DEFAULT '',
                submitted_by INTEGER NOT NULL,
                decided_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                UNIQUE(apartment_id,metric,period)
            );
            CREATE TABLE IF NOT EXISTS portal_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                apartment_id INTEGER REFERENCES apartments(id),
                name TEXT NOT NULL,
                file_id TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0,
                for_user INTEGER NOT NULL DEFAULT 0,
                for_leadership INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS portal_document_access (
                document_id INTEGER NOT NULL REFERENCES portal_documents(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(document_id,telegram_id)
            );
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS code_attempts (
                telegram_id INTEGER NOT NULL,
                code_kind TEXT NOT NULL,
                organization_id INTEGER NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT,
                PRIMARY KEY(telegram_id,code_kind,organization_id)
            );
            CREATE TABLE IF NOT EXISTS act_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                act_id INTEGER NOT NULL REFERENCES acts(id) ON DELETE CASCADE,
                amount REAL NOT NULL CHECK(amount > 0),
                comment TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS item_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL CHECK(item_type IN ('act','doc','job')),
                item_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                comment TEXT NOT NULL DEFAULT '',
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                osbb TEXT,
                old_value TEXT,
                new_value TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS salary_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                employee TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                seasonal_json TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(organization_id,employee)
            );
            CREATE TABLE IF NOT EXISTS code_attempts_osbb (
                telegram_id INTEGER NOT NULL,
                code_kind TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT,
                PRIMARY KEY(telegram_id,code_kind)
            );
            """
        )

        # The legacy tables remain in place and all existing columns and IDs are preserved.
        _add_columns(conn, "acts", {
            "performer": "TEXT NOT NULL DEFAULT ''", "amount": "REAL",
            "updated_at": "TEXT", "completed_at": "TEXT",
        })
        _add_columns(conn, "docs", {
            "published": "INTEGER NOT NULL DEFAULT 1", "for_user": "INTEGER NOT NULL DEFAULT 0",
            "for_leadership": "INTEGER NOT NULL DEFAULT 0", "updated_at": "TEXT", "completed_at": "TEXT",
        })
        _add_columns(conn, "jobs", {
            "priority": "TEXT NOT NULL DEFAULT 'Середня'", "deadline": "TEXT",
            "completed_at": "TEXT",
        })
        _add_columns(conn, "salaries", {"paid_at": "TEXT", "target_type": "TEXT NOT NULL DEFAULT 'organization'"})
        _add_columns(conn, "organizations", {"meters_enabled": "INTEGER NOT NULL DEFAULT 1"})

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(telegram_id,active,role);
            CREATE INDEX IF NOT EXISTS idx_memberships_org ON memberships(organization_id,active,role);
            CREATE INDEX IF NOT EXISTS idx_apartments_org ON apartments(organization_id,active,number);
            CREATE INDEX IF NOT EXISTS idx_readings_org_period ON readings(period,status);
            CREATE INDEX IF NOT EXISTS idx_portal_docs_org ON portal_documents(organization_id,published);
            CREATE INDEX IF NOT EXISTS idx_portal_doc_access_user ON portal_document_access(telegram_id,document_id);
            CREATE INDEX IF NOT EXISTS idx_act_payments_act ON act_payments(act_id,paid_at);
            CREATE INDEX IF NOT EXISTS idx_events_item ON item_events(item_type,item_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_acts_osbb_status_date ON acts(osbb,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_docs_osbb_status_date ON docs(osbb,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_osbb_status_date ON jobs(osbb,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_salaries_osbb_month ON salaries(osbb,month_year);
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (datetime.now(timezone.utc).isoformat(),))
        if superadmin_id:
            conn.execute(
                "INSERT INTO users(telegram_id,full_name) VALUES(?,?) ON CONFLICT(telegram_id) DO NOTHING",
                (int(superadmin_id), "Superadmin"),
            )
            conn.execute(
                "UPDATE memberships SET active=0 WHERE telegram_id=? AND role='accountant' AND active=1",
                (int(superadmin_id),),
            )


async def init_db(superadmin_id: int | None = None) -> None:
    await asyncio.to_thread(init_db_sync, superadmin_id)


def create_join_code() -> str:
    """Use non-ambiguous, random codes suitable for sharing in chat."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
