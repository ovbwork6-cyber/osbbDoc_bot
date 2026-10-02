import asyncio
import html
import io
import logging
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("osbb_bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAIRMAN_ID_RAW = os.getenv("CHAIRMAN_ID", "").strip()
DB_PATH = os.getenv("OSBB_DB_PATH", "osbb_acts.db")
PAGE_SIZE = int(os.getenv("BOT_PAGE_SIZE", "5"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAIRMAN_ID_RAW.isdigit():
    raise RuntimeError("CHAIRMAN_ID must be numeric")

CHAIRMAN_ID = int(CHAIRMAN_ID_RAW)

READONLY_VIEWERS: dict[int, dict[str, Any]] = {
    5186498707: {"name": "Сокол Микола Миколайович", "keywords": ["Сокол"]},
    396484643: {"name": "Денисюк Станіслав Станіславович", "keywords": ["Денисюк", "ТО ІТП"]},
}

ACCESS_MAP = {
    5178201242: ["ВП-16", "Е21"],
    1332732213: ["ОКПТ", "В19"],
}

STAFF_CONFIG = {
    "ВП-16": {
        "Голова": 6000,
        "Бухгалтер": 3000,
        "Прибирання (Марія)": 9500,
        "Прибирання (Олег)": 3000,
        "Сантехнік": 2800,
    },
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {
        "Голова": 4000,
        "Бухгалтер": 1000,
        "Нарахування ВТВК": 1000,
        "Двірник": 2000,
        "Обхідник": 1000,
        "Баки": 1000,
    },
    "В19": {"Голова": 4820, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500},
}

MONTHS_UA = {
    "01": "Січень",
    "02": "Лютий",
    "03": "Березень",
    "04": "Квітень",
    "05": "Травень",
    "06": "Червень",
    "07": "Липень",
    "08": "Серпень",
    "09": "Вересень",
    "10": "Жовтень",
    "11": "Листопад",
    "12": "Грудень",
}

VALID_TABLES = {"acts", "docs"}
FINAL_STATUSES = ("Завершено!", "Роботу завершено")
DB_WRITE_LOCK = asyncio.Lock()

bot = Bot(token=TOKEN)
dp = Dispatcher()


class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    file = State()


class ActAttachPhotoForm(StatesGroup):
    photo = State()


class DocForm(StatesGroup):
    name = State()
    osbb = State()
    file = State()


class JobForm(StatesGroup):
    osbb = State()
    text = State()
    priority = State()
    deadline = State()


class JobCommentForm(StatesGroup):
    text = State()


class SearchForm(StatesGroup):
    query = State()
    year = State()
    status = State()


class SearchActCb(CallbackData, prefix="sact"):
    step: Literal["year", "status"]
    year: str = ""
    status: str = ""


class OsbbCb(CallbackData, prefix="osbb"):
    flow: str
    osbb: str


class PeriodCb(CallbackData, prefix="per"):
    flow: str
    step: Literal["year", "period"]
    osbb: str
    year: str = ""
    period: str = ""


class ItemCb(CallbackData, prefix="item"):
    cmd: Literal["ask", "yes", "no", "photo"]
    action: str
    table: str
    item_id: int


class SalaryCb(CallbackData, prefix="sal"):
    action: Literal["view", "hist", "list", "gen", "toggle", "back", "rep_years", "rep_gen"]
    osbb: str = ""
    month_year: str = ""
    salary_id: int = 0
    year: str = ""


class JobCb(CallbackData, prefix="job"):
    action: Literal["view", "add", "month", "priority", "act", "fin", "dash", "bundle"]
    osbb: str = ""
    job_id: int = 0
    mode: str = ""
    year: str = ""
    period: str = ""


class PageCb(CallbackData, prefix="page"):
    kind: Literal["items", "jobs", "finished"]
    table: str = "-"
    archive: int = 0
    osbb: str = "-"
    page: int = 0
    year: str = "-"
    period: str = "-"


class SearchCb(CallbackData, prefix="search"):
    section: Literal["acts", "docs", "jobs"]


@dataclass(frozen=True)
class ItemRow:
    id: int
    title: str
    osbb: str
    file_id: str
    status: str
    created_at: str
    descr: str = ""


def h(value: Any) -> str:
    """Escape dynamic values before inserting them into Telegram HTML."""
    return html.escape("" if value is None else str(value))


def now_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_seasonal_salary() -> int:
    month = datetime.now().month
    return 3500 if 4 <= month <= 10 else 4500


def today_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def current_month_year() -> str:
    return datetime.now().strftime("%m.%Y")


def user_allowed_osbbs(user_id: int) -> list[str]:
    if user_id == CHAIRMAN_ID:
        return list(STAFF_CONFIG.keys())
    return ACCESS_MAP.get(user_id, [])


def is_chairman(user_id: int) -> bool:
    return user_id == CHAIRMAN_ID


def is_readonly_viewer(user_id: int) -> bool:
    return user_id in READONLY_VIEWERS


def readonly_keywords(user_id: int) -> list[str]:
    return READONLY_VIEWERS[user_id]["keywords"]


def readonly_name(user_id: int) -> str:
    return READONLY_VIEWERS[user_id]["name"]


def can_access_osbb(user_id: int, osbb: str) -> bool:
    return osbb in user_allowed_osbbs(user_id)


async def answer_forbidden(target: CallbackQuery | types.Message):
    text = "⛔ Немає доступу до цієї дії."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _execute(sql: str, params: Iterable[Any] = ()) -> int:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or cur.rowcount or 0)
    finally:
        conn.close()


def _execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    conn = _connect()
    try:
        conn.executemany(sql, [tuple(row) for row in rows])
        conn.commit()
    finally:
        conn.close()


async def db_fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_fetch_all, sql, tuple(params))


async def db_fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return await asyncio.to_thread(_fetch_one, sql, tuple(params))


async def db_execute(sql: str, params: Iterable[Any] = ()) -> int:
    async with DB_WRITE_LOCK:
        return await asyncio.to_thread(_execute, sql, tuple(params))


async def db_execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    async with DB_WRITE_LOCK:
        return await asyncio.to_thread(_execute_many, sql, [tuple(row) for row in rows])


async def audit_action(
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    osbb: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    details: str | None = None,
) -> None:
    """Write an immutable business-action record without interrupting the main flow."""
    try:
        await db_execute(
            """INSERT INTO audit_log
               (user_id, action, entity_type, entity_id, osbb, old_value, new_value, details, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, action, entity_type, entity_id, osbb, old_value, new_value, details, now_timestamp()),
        )
    except Exception:
        logger.exception("Could not write audit event action=%s entity=%s id=%s", action, entity_type, entity_id)


def init_db_sync() -> None:
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS acts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT,
                osbb TEXT,
                descr TEXT,
                file_id TEXT,
                status TEXT DEFAULT "Не отримано",
                created_at TEXT
            )"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                osbb TEXT,
                file_id TEXT,
                status TEXT DEFAULT "Не отримано",
                created_at TEXT
            )"""
        )
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
                created_at TEXT,
                completed_at TEXT,
                priority TEXT DEFAULT "Середня",
                deadline TEXT
            )"""
        )
        for sql in (
            "ALTER TABLE acts ADD COLUMN created_at TEXT",
            "ALTER TABLE docs ADD COLUMN created_at TEXT",
            "ALTER TABLE jobs ADD COLUMN updated_at TEXT",
            "ALTER TABLE jobs ADD COLUMN created_at TEXT",
            "ALTER TABLE jobs ADD COLUMN completed_at TEXT",
            "ALTER TABLE jobs ADD COLUMN priority TEXT DEFAULT 'Середня'",
            "ALTER TABLE jobs ADD COLUMN deadline TEXT",
        ):
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_acts_osbb_status_date ON acts(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_docs_osbb_status_date ON docs(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_osbb_status_date ON jobs(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_salaries_osbb_month ON salaries(osbb, month_year)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_completed_at ON jobs(osbb, completed_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_deadline ON jobs(osbb, deadline, status)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS audit_log (
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
        )""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at)")
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(init_db_sync)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Dashboard"), KeyboardButton(text="📄 Акти")],
            [KeyboardButton(text="🧾 Чеки ОСББ"), KeyboardButton(text="🛠️ План робіт")],
            [KeyboardButton(text="💰 Зарплати"), KeyboardButton(text="📈 Звіт по ОСББ")],
        ], resize_keyboard=True,
    )


def readonly_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Оновити статус")]], resize_keyboard=True)


def acts_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Dashboard актів"), KeyboardButton(text="📋 Поточні акти")],
            [KeyboardButton(text="📂 Архів актів"), KeyboardButton(text="➕ Створити акт")],
            [KeyboardButton(text="🔎 Пошук актів")],
            [KeyboardButton(text="⬅️ Назад")],
        ], resize_keyboard=True,
    )


def docs_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Dashboard чеків"), KeyboardButton(text="📋 Поточні чеки")],
            [KeyboardButton(text="📂 Архів чеків"), KeyboardButton(text="➕ Додати PDF чек")],
            [KeyboardButton(text="⬅️ Назад")],
        ], resize_keyboard=True,
    )


def jobs_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Dashboard робіт"), KeyboardButton(text="➕ Додати роботу")],
            [KeyboardButton(text="📋 Поточні роботи"), KeyboardButton(text="✅ Архів робіт")],
            [KeyboardButton(text="⬅️ Назад")],
        ], resize_keyboard=True,
    )


def osbb_keyboard(flow: str, user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=osbb, callback_data=OsbbCb(flow=flow, osbb=osbb).pack())]
        for osbb in user_allowed_osbbs(user_id)
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def months_keyboard(flow: str, osbb: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for month, name in MONTHS_UA.items():
        row.append(InlineKeyboardButton(text=name, callback_data=JobCb(action="month", osbb=osbb, mode=month).pack()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def period_year_keyboard(flow: str, osbb: str) -> InlineKeyboardMarkup:
    year = datetime.now().year
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(year), callback_data=PeriodCb(flow=flow, step="year", osbb=osbb, year=str(year)).pack())],
            [InlineKeyboardButton(text=str(year - 1), callback_data=PeriodCb(flow=flow, step="year", osbb=osbb, year=str(year - 1)).pack())],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")],
        ]
    )


def period_month_keyboard(flow: str, osbb: str, year: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📅 Цілий рік", callback_data=PeriodCb(flow=flow, step="period", osbb=osbb, year=year, period="all").pack())]]
    row: list[InlineKeyboardButton] = []
    for month, name in MONTHS_UA.items():
        row.append(InlineKeyboardButton(text=name, callback_data=PeriodCb(flow=flow, step="period", osbb=osbb, year=year, period=month).pack()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=PeriodCb(flow=flow, step="year", osbb=osbb).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_keyboard(item_id: int, status: str, table: str, user_id: int, file_id: str = "") -> InlineKeyboardMarkup | None:
    if table not in VALID_TABLES:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    has_photo = bool(file_id and file_id != "NO_FILE")
    ch = is_chairman(user_id)

    if table == "acts":
        if status == "Не отримано":
            if ch:
                rows.append([InlineKeyboardButton(text="❌ Видалити акт", callback_data=ItemCb(cmd="ask", action="del", table=table, item_id=item_id).pack())])
            else:
                rows.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=ItemCb(cmd="ask", action="proc", table=table, item_id=item_id).pack())])
        elif status == "В роботі" and not ch:
            rows.append([InlineKeyboardButton(text="💳 Оплачено", callback_data=ItemCb(cmd="ask", action="pay", table=table, item_id=item_id).pack())])
        elif status == "Акт оплачений" and ch:
            if has_photo:
                rows.append([InlineKeyboardButton(text="✅ Завершити", callback_data=ItemCb(cmd="ask", action="fin", table=table, item_id=item_id).pack())])
                rows.append([InlineKeyboardButton(text="🔄 Оновити фото акту", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
            else:
                rows.append([InlineKeyboardButton(text="📷 Додати фото акту (обов'язково)", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
        if not has_photo and status != "Акт оплачений":
            rows.append([InlineKeyboardButton(text="📷 Завантажити фото", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
    else:
        if status == "Не отримано":
            if ch:
                rows.append([InlineKeyboardButton(text="❌ Видалити PDF", callback_data=ItemCb(cmd="ask", action="del", table=table, item_id=item_id).pack())])
            else:
                rows.append([InlineKeyboardButton(text="📥 Прийняти чек", callback_data=ItemCb(cmd="ask", action="proc", table=table, item_id=item_id).pack())])
        elif status == "В роботі" and not ch:
            rows.append([InlineKeyboardButton(text="📝 Опрацьовано", callback_data=ItemCb(cmd="ask", action="pay", table=table, item_id=item_id).pack())])
        elif status == "Опрацьовано" and ch:
            rows.append([InlineKeyboardButton(text="✅ Завершити", callback_data=ItemCb(cmd="ask", action="fin", table=table, item_id=item_id).pack())])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def confirm_keyboard(item_id: int, action: str, table: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Так", callback_data=ItemCb(cmd="yes", action=action, table=table, item_id=item_id).pack()),
                InlineKeyboardButton(text="❌ Ні", callback_data=ItemCb(cmd="no", action=action, table=table, item_id=item_id).pack()),
            ]
        ]
    )


def page_keyboard(kind: str, table: str, archive: bool, osbb: str, page: int, total: int) -> InlineKeyboardMarkup | None:
    max_page = max((total - 1) // PAGE_SIZE, 0)
    if max_page == 0:
        return None
    row = []
    cb_table = table or "-"
    cb_osbb = osbb or "-"
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=PageCb(kind=kind, table=cb_table, archive=int(archive), osbb=cb_osbb, page=page - 1).pack()))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="noop"))
    if page < max_page:
        row.append(InlineKeyboardButton(text="➡️", callback_data=PageCb(kind=kind, table=cb_table, archive=int(archive), osbb=cb_osbb, page=page + 1).pack()))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def get_item(table: str, item_id: int) -> dict[str, Any] | None:
    if table == "acts":
        return await db_fetch_one("SELECT id, number AS title, osbb, descr, file_id, status, created_at FROM acts WHERE id=?", (item_id,))
    if table == "docs":
        return await db_fetch_one("SELECT id, name AS title, osbb, '' AS descr, file_id, status, created_at FROM docs WHERE id=?", (item_id,))
    return None


async def require_item_access(cb: CallbackQuery, table: str, item_id: int) -> dict[str, Any] | None:
    row = await get_item(table, item_id)
    if not row:
        await cb.answer("Запис не знайдено", show_alert=True)
        return None
    if not can_access_osbb(cb.from_user.id, row["osbb"]):
        await answer_forbidden(cb)
        return None
    return row


def status_filter(archive: bool) -> str:
    if archive:
        return "status IN ('Завершено!', 'Роботу завершено')"
    return "status NOT IN ('Завершено!', 'Роботу завершено')"


async def load_items(table: str, archive: bool, user_id: int, osbb: str = "") -> list[dict[str, Any]]:
    if table not in VALID_TABLES:
        return []
    columns = "id, number AS title, osbb, descr, file_id, status, created_at" if table == "acts" else "id, name AS title, osbb, '' AS descr, file_id, status, created_at"
    allowed = user_allowed_osbbs(user_id)
    if not allowed:
        return []
    params: list[Any] = []
    where = [status_filter(archive)]
    if osbb:
        if osbb not in allowed:
            return []
        where.append("osbb=?")
        params.append(osbb)
    elif not is_chairman(user_id):
        placeholders = ",".join("?" for _ in allowed)
        where.append(f"osbb IN ({placeholders})")
        params.extend(allowed)
    sql = f"SELECT {columns} FROM {table} WHERE {' AND '.join(where)} ORDER BY id ASC"
    return await db_fetch_all(sql, params)


async def render_items_page(message: types.Message, table: str, archive: bool, user_id: int, page: int = 0, osbb: str = "") -> None:
    rows = await load_items(table, archive, user_id, osbb)
    if not rows:
        await message.answer("📭 Порожньо.")
        return
    start = page * PAGE_SIZE
    visible = rows[start : start + PAGE_SIZE]
    title = "Акти" if table == "acts" else "Чеки"
    mode = "архів" if archive else "поточні"
    await message.answer(f"📋 <b>{title}: {mode}</b> ({start + 1}-{start + len(visible)} з {len(rows)})", parse_mode="HTML")
    for row in visible:
        await send_item_card(message.chat.id, row, table, user_id, archive)
    markup = page_keyboard("items", table, archive, osbb, page, len(rows))
    if markup:
        await message.answer("Сторінки:", reply_markup=markup)


def section_from_search_text(text: str) -> str | None:
    if "акт" in text:
        return "acts"
    if "чек" in text:
        return "docs"
    if "роб" in text:
        return "jobs"
    return None


def search_title(section: str) -> str:
    return {"acts": "актах", "docs": "чеках", "jobs": "роботах"}[section]


async def search_records(section: str, query: str, user_id: int) -> list[dict[str, Any]]:
    allowed = user_allowed_osbbs(user_id)
    if not allowed:
        return []
    like = f"%{query}%"
    upper_like = f"%{query.upper()}%"
    limit = 25

    if section == "acts":
        where = "(number LIKE ? OR descr LIKE ? OR osbb LIKE ? OR status LIKE ?)"
        params: list[Any] = [like, like, upper_like, like]
        if not is_chairman(user_id):
            placeholders = ",".join("?" for _ in allowed)
            where += f" AND osbb IN ({placeholders})"
            params.extend(allowed)
        params.append(limit)
        return await db_fetch_all(
            f"SELECT id, number AS title, osbb, descr, file_id, status, created_at FROM acts WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
        )

    if section == "docs":
        where = "(name LIKE ? OR osbb LIKE ? OR status LIKE ?)"
        params = [like, upper_like, like]
        if not is_chairman(user_id):
            placeholders = ",".join("?" for _ in allowed)
            where += f" AND osbb IN ({placeholders})"
            params.extend(allowed)
        params.append(limit)
        return await db_fetch_all(
            f"SELECT id, name AS title, osbb, '' AS descr, file_id, status, created_at FROM docs WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
        )

    if section == "jobs":
        where = "(CAST(id AS TEXT) LIKE ? OR task_text LIKE ? OR osbb LIKE ? OR month_year LIKE ? OR status LIKE ? OR stages LIKE ? OR comments LIKE ?)"
        params = [like, like, upper_like, like, like, like, like]
        if not is_chairman(user_id):
            placeholders = ",".join("?" for _ in allowed)
            where += f" AND osbb IN ({placeholders})"
            params.extend(allowed)
        params.append(limit)
        return await db_fetch_all(
            f"SELECT id, osbb, month_year, task_text, status, stages, comments, updated_at, created_at FROM jobs WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
        )
    return []


@dp.message(F.text == "🔎 Пошук актів")
async def start_search(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if m.text != "🔎 Пошук актів": return
    await state.update_data(section="acts")
    await state.set_state(SearchForm.query)
    await m.answer("🔎 Введіть номер, опис або ОСББ для пошуку актів:")


@dp.message(SearchForm.query)
async def run_search(m: types.Message, state: FSMContext) -> None:
    query = (m.text or "").strip()
    if len(query) < 2: return await m.answer("Введіть мінімум 2 символи.")
    await state.update_data(query=query)
    await state.set_state(SearchForm.year)
    year = datetime.now().year
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поточний рік", callback_data=SearchActCb(step="year", year=str(year)).pack())],
        [InlineKeyboardButton(text=f"{year-1} рік", callback_data=SearchActCb(step="year", year=str(year-1)).pack())],
        [InlineKeyboardButton(text="Всі роки", callback_data=SearchActCb(step="year", year="all").pack())],
    ])
    await m.answer("📅 За який рік шукати?", reply_markup=kb)


@dp.callback_query(SearchActCb.filter(F.step == "year"))
async def search_act_year(cb: CallbackQuery, callback_data: SearchActCb, state: FSMContext) -> None:
    await state.update_data(year=callback_data.year)
    await state.set_state(SearchForm.status)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Всі статуси", callback_data=SearchActCb(step="status", year=callback_data.year, status="all").pack())],
        [InlineKeyboardButton(text="Не отримано", callback_data=SearchActCb(step="status", year=callback_data.year, status="Не отримано").pack())],
        [InlineKeyboardButton(text="В роботі", callback_data=SearchActCb(step="status", year=callback_data.year, status="В роботі").pack())],
        [InlineKeyboardButton(text="Акт оплачений", callback_data=SearchActCb(step="status", year=callback_data.year, status="Акт оплачений").pack())],
        [InlineKeyboardButton(text="Завершено", callback_data=SearchActCb(step="status", year=callback_data.year, status="Завершено!").pack())],
    ])
    await safe_edit_text(cb.message, "📌 Який статус актів шукати?", reply_markup=kb)
    await cb.answer()


@dp.callback_query(SearchActCb.filter(F.step == "status"))
async def search_act_status(cb: CallbackQuery, callback_data: SearchActCb, state: FSMContext) -> None:
    data = await state.get_data(); query=data.get("query", "")
    allowed=user_allowed_osbbs(cb.from_user.id)
    clauses=["(number LIKE ? OR descr LIKE ? OR osbb LIKE ?)"]; params=[f"%{query}%"]*3
    if callback_data.year != "all": clauses.append("created_at LIKE ?"); params.append(f"{callback_data.year}-%")
    if callback_data.status != "all": clauses.append("status=?"); params.append(callback_data.status)
    if not is_chairman(cb.from_user.id): clauses.append("osbb IN ("+",".join("?" for _ in allowed)+")"); params.extend(allowed)
    rows=await db_fetch_all("SELECT id, number AS title, osbb, descr, file_id, status, created_at FROM acts WHERE "+" AND ".join(clauses)+" ORDER BY id DESC LIMIT 50", params)
    await state.clear()
    await safe_edit_text(cb.message, f"🔎 Акти: <b>{h(query)}</b> | {h(callback_data.year)} | {h(callback_data.status)}", parse_mode="HTML")
    if not rows: return await cb.message.answer("📭 Нічого не знайдено.")
    for row in rows: await send_item_card(cb.message.chat.id, row, "acts", cb.from_user.id, archive=(row["status"] in FINAL_STATUSES))
    await cb.answer()

async def send_item_card(chat_id: int, row: dict[str, Any], table: str, user_id: int, archive: bool = False) -> None:
    markup = None if archive else item_keyboard(int(row["id"]), row["status"], table, user_id, row.get("file_id", ""))
    if table == "acts":
        photo_note = "\n⚠️ Фото ще не додано" if row.get("file_id") == "NO_FILE" else ""
        caption = (
            f"📄 Акт №{h(row['title'])} ({h(row['osbb'])}){photo_note}"
            f"\n📝 Опис: {h(row.get('descr') or '-')}\n⏳ Статус: {h(row['status'])}"
        )
        if row.get("file_id") and row["file_id"] != "NO_FILE":
            try:
                await bot.send_photo(chat_id, row["file_id"], caption=caption, reply_markup=markup)
                return
            except Exception:
                logger.exception("Could not send act photo id=%s", row["id"])
        await bot.send_message(chat_id, caption, reply_markup=markup)
    else:
        caption = f"🧾 Чек: {h(row['title'])} ({h(row['osbb'])})\n⏳ Статус: {h(row['status'])}"
        try:
            await bot.send_document(chat_id, row["file_id"], caption=caption, reply_markup=markup)
        except Exception:
            logger.exception("Could not send doc id=%s", row["id"])
            await bot.send_message(chat_id, caption + "\n⚠️ Файл не вдалося відкрити.", reply_markup=markup)


async def safe_edit_text(message: types.Message, text: str, **kwargs: Any) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        await message.answer(text, **kwargs)


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if is_readonly_viewer(m.from_user.id):
        await m.answer(
            f"👋 Вітаємо, {readonly_name(m.from_user.id)}.\n"
            "Натисніть кнопку нижче, щоб побачити актуальний статус ваших актів.",
            reply_markup=readonly_menu(),
        )
        return
    await m.answer("👋 Система готова.", reply_markup=main_menu())


@dp.message(F.text == "🔄 Оновити статус")
async def readonly_refresh_status(m: types.Message, state: FSMContext) -> None:
    if not is_readonly_viewer(m.from_user.id):
        return
    await state.clear()
    keywords = readonly_keywords(m.from_user.id)
    where = " OR ".join("descr LIKE ?" for _ in keywords)
    params = [f"%{kw}%" for kw in keywords]
    rows = await db_fetch_all(
        f"SELECT number, osbb, status, created_at FROM acts WHERE {where} ORDER BY osbb ASC, id ASC",
        params,
    )
    if not rows:
        await m.answer("📭 Актів з вашим описом наразі не знайдено.", reply_markup=readonly_menu())
        return
    text = "📄 <b>Актуальний статус ваших актів:</b>\n\n"
    for row in rows:
        text += f"№{h(row['number'])} ({h(row['osbb'])}) — {h(row['status'])} [{h(row['created_at'])}]\n"
    await m.answer(text, parse_mode="HTML", reply_markup=readonly_menu())


@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if is_readonly_viewer(m.from_user.id):
        await m.answer("Меню:", reply_markup=readonly_menu())
        return
    await m.answer("Головне меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "main_menu_back")
async def back_to_menu_inline(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(cb.message, "Дію скасовано. Скористайтесь кнопками меню на клавіатурі.")
    await cb.answer()


async def dashboard_text(osbb: str) -> str:
    acts = await db_fetch_one("SELECT COUNT(*) AS n FROM acts WHERE osbb=? AND status NOT IN ('Завершено!', 'Роботу завершено')", (osbb,))
    docs = await db_fetch_one("SELECT COUNT(*) AS n FROM docs WHERE osbb=? AND status NOT IN ('Завершено!', 'Роботу завершено')", (osbb,))
    jobs = await db_fetch_one("SELECT COUNT(*) AS n FROM jobs WHERE osbb=? AND status != 'Роботу закінчено'", (osbb,))
    overdue = await db_fetch_one("SELECT COUNT(*) AS n FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' AND deadline IS NOT NULL AND deadline < date('now')", (osbb,))
    due = await db_fetch_one("SELECT COUNT(*) AS n FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' AND deadline BETWEEN date('now') AND date('now','+7 day')", (osbb,))
    return (f"📊 <b>{h(osbb)}</b>\n\n"
            f"📄 Незавершені акти: <b>{acts['n']}</b>\n"
            f"🧾 Неопрацьовані чеки: <b>{docs['n']}</b>\n"
            f"🛠️ Активні роботи: <b>{jobs['n']}</b>\n"
            f"🔴 Прострочені дедлайни: <b>{overdue['n']}</b>\n"
            f"🟡 Дедлайн протягом 7 днів: <b>{due['n']}</b>")


def dashboard_kb(osbb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Акти", callback_data=OsbbCb(flow="dash_acts", osbb=osbb).pack()), InlineKeyboardButton(text="🧾 Чеки", callback_data=OsbbCb(flow="dash_docs", osbb=osbb).pack())],
        [InlineKeyboardButton(text="🛠️ План робіт", callback_data=OsbbCb(flow="jobs_dashboard", osbb=osbb).pack())],
        [InlineKeyboardButton(text="📦 Повний звіт + ZIP", callback_data=OsbbCb(flow="bundle", osbb=osbb).pack())],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")],
    ])


@dp.message(F.text.in_({"📊 Dashboard", "📊 Dashboard актів", "📊 Dashboard чеків", "📊 Dashboard робіт"}))
async def dashboard_menu(m: types.Message, state: FSMContext) -> None:
    if is_readonly_viewer(m.from_user.id): return
    await state.clear()
    await m.answer("📊 Оберіть ОСББ або ОКПТ:", reply_markup=osbb_keyboard("dashboard", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "dashboard"))
async def show_dashboard(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb): return await answer_forbidden(cb)
    await safe_edit_text(cb.message, await dashboard_text(callback_data.osbb), reply_markup=dashboard_kb(callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(OsbbCb.filter(F.flow.in_({"dash_acts", "dash_docs"})))
async def dashboard_documents(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb): return await answer_forbidden(cb)
    table = "acts" if callback_data.flow == "dash_acts" else "docs"
    await cb.message.delete()
    await render_items_page(cb.message, table, False, cb.from_user.id, osbb=callback_data.osbb)
    await cb.answer()


@dp.message(F.text == "📄 Акти")
async def m_acts(m: types.Message, state: FSMContext) -> None:
    if is_readonly_viewer(m.from_user.id):
        return
    await state.clear()
    await m.answer("АКТИ", reply_markup=acts_menu())


@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message, state: FSMContext) -> None:
    if is_readonly_viewer(m.from_user.id):
        return
    await state.clear()
    await m.answer("ЧЕКИ", reply_markup=docs_menu())


@dp.message(F.text.in_(["📋 Поточні акти", "📋 Поточні чеки"]))
async def show_items(m: types.Message, state: FSMContext) -> None:
    if is_readonly_viewer(m.from_user.id):
        return
    await state.clear()
    archive = "Архів" in (m.text or "")
    table = "acts" if "акт" in (m.text or "").lower() else "docs"
    await render_items_page(m, table, archive, m.from_user.id)


@dp.callback_query(PageCb.filter())
async def paginate_items(cb: CallbackQuery, callback_data: PageCb) -> None:
    await cb.message.delete()
    osbb = "" if callback_data.osbb == "-" else callback_data.osbb
    if callback_data.kind == "items":
        table = "" if callback_data.table == "-" else callback_data.table
        await render_items_page(cb.message, table, bool(callback_data.archive), cb.from_user.id, callback_data.page, osbb)
    elif callback_data.kind == "jobs":
        await render_jobs_page(cb.message, cb.from_user.id, osbb, callback_data.page)
    else:
        await cb.answer("Некоректна сторінка", show_alert=True)
        return
    await cb.answer()


@dp.callback_query(ItemCb.filter(F.cmd == "ask"))
async def ask_item_action(cb: CallbackQuery, callback_data: ItemCb) -> None:
    if callback_data.table not in VALID_TABLES:
        return await cb.answer("Некоректна дія", show_alert=True)
    if not await require_item_access(cb, callback_data.table, callback_data.item_id):
        return
    await cb.message.edit_reply_markup(reply_markup=confirm_keyboard(callback_data.item_id, callback_data.action, callback_data.table))
    await cb.answer("Ви впевнені?")


@dp.callback_query(ItemCb.filter(F.cmd == "no"))
async def cancel_item_action(cb: CallbackQuery, callback_data: ItemCb) -> None:
    row = await require_item_access(cb, callback_data.table, callback_data.item_id)
    if not row:
        return
    await cb.message.edit_reply_markup(reply_markup=item_keyboard(callback_data.item_id, row["status"], callback_data.table, cb.from_user.id, row.get("file_id", "")))
    await cb.answer("Скасовано")


@dp.callback_query(ItemCb.filter(F.cmd == "yes"))
async def confirm_item_action(cb: CallbackQuery, callback_data: ItemCb) -> None:
    row = await require_item_access(cb, callback_data.table, callback_data.item_id)
    if not row:
        return
    table = callback_data.table
    action = callback_data.action
    user_id = cb.from_user.id
    ch = is_chairman(user_id)

    if action in {"del", "fin"} and not ch:
        return await answer_forbidden(cb)
    if action in {"proc", "pay"} and ch:
        return await answer_forbidden(cb)
    if table == "acts" and action == "fin" and row.get("file_id") in ("", "NO_FILE", None):
        return await cb.answer("Перед завершенням акту потрібно додати фото.", show_alert=True)

    if action == "del":
        await db_execute(f"DELETE FROM {table} WHERE id=?", (callback_data.item_id,))
        await audit_action(user_id, "delete", table, callback_data.item_id, osbb=row["osbb"], old_value=row["status"])
        await cb.message.delete()
        return await cb.answer("Видалено")

    new_status = None
    if action == "proc":
        new_status = "В роботі"
    elif action == "pay":
        new_status = "Акт оплачений" if table == "acts" else "Опрацьовано"
    elif action == "fin":
        new_status = "Завершено!" if table == "acts" else "Роботу завершено"
    if not new_status:
        return await cb.answer("Некоректна дія", show_alert=True)

    await db_execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, callback_data.item_id))
    await audit_action(user_id, "complete" if action == "fin" else "status_change", table, callback_data.item_id, osbb=row["osbb"], old_value=row["status"], new_value=new_status)
    if action == "fin":
        await cb.message.delete()
        return await cb.answer("Завершено")

    updated = await get_item(table, callback_data.item_id)
    if not updated:
        return await cb.answer("Оновлено")
    prefix = (cb.message.caption or cb.message.text or "").split("⏳")[0]
    caption = prefix + f"⏳ Статус: {new_status}"
    markup = item_keyboard(callback_data.item_id, new_status, table, user_id, updated.get("file_id", ""))
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=caption, reply_markup=markup)
        else:
            await cb.message.edit_text(caption, reply_markup=markup)
    except Exception:
        logger.exception("Could not update message for item %s", callback_data.item_id)
        await cb.message.answer(caption, reply_markup=markup)
    await cb.answer("Оновлено")


@dp.callback_query(ItemCb.filter(F.cmd == "photo"))
async def start_attach_photo(cb: CallbackQuery, callback_data: ItemCb, state: FSMContext) -> None:
    row = await require_item_access(cb, "acts", callback_data.item_id)
    if not row:
        return
    await state.clear()
    await state.update_data(act_id=callback_data.item_id, mode="attach_existing")
    await state.set_state(ActAttachPhotoForm.photo)
    await cb.message.answer(
        f"📸 <b>Надішліть фото саме для акту №{row['title']} ({row['osbb']}).</b>\n"
        "Якщо це не той акт, натисніть /start і почніть дію заново.",
        parse_mode="HTML",
    )
    await cb.answer()


@dp.message(ActAttachPhotoForm.photo, F.photo)
async def save_attached_photo(m: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    act_id = int(data.get("act_id") or 0)
    if data.get("mode") != "attach_existing":
        await state.clear()
        return await m.answer("❌ Втрачено прив'язку до акту. Натисніть кнопку фото під потрібним актом ще раз.")
    row = await get_item("acts", act_id)
    if not row or not can_access_osbb(m.from_user.id, row["osbb"]):
        await state.clear()
        return await m.answer("⛔ Немає доступу або акт не знайдено.")
    file_id = m.photo[-1].file_id
    await db_execute("UPDATE acts SET file_id=? WHERE id=?", (file_id, act_id))
    await audit_action(m.from_user.id, "attach_photo", "acts", act_id, osbb=row["osbb"], details="Фото акту додано/оновлено")
    await state.clear()
    await m.answer(f"✅ Фото додано до акту №{row['title']} ({row['osbb']}).")
    row["file_id"] = file_id
    await send_item_card(m.chat.id, row, "acts", m.from_user.id)


@dp.message(ActAttachPhotoForm.photo)
async def save_attached_photo_wrong_type(m: types.Message) -> None:
    await m.answer("Будь ласка, надішліть саме фото акту.")


@dp.message(F.text == "➕ Створити Акт")
async def start_act(m: types.Message, state: FSMContext) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await state.clear()
    await m.answer("Введіть номер акту:")
    await state.set_state(ActForm.number)


@dp.message(ActForm.number)
async def act_number(m: types.Message, state: FSMContext) -> None:
    number = (m.text or "").strip()
    if not number:
        return await m.answer("Номер акту не може бути порожнім.")
    await state.update_data(number=number)
    await state.set_state(ActForm.osbb)
    await m.answer("Оберіть ОСББ:", reply_markup=osbb_keyboard("act_create", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "act_create"), ActForm.osbb)
async def act_osbb(cb: CallbackQuery, callback_data: OsbbCb, state: FSMContext) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb)
    await state.set_state(ActForm.descr)
    await safe_edit_text(cb.message, "Введіть опис акту:")
    await cb.answer()


@dp.message(ActForm.descr)
async def act_descr(m: types.Message, state: FSMContext) -> None:
    descr = (m.text or "").strip()
    if not descr:
        return await m.answer("Опис не може бути порожнім.")
    await state.update_data(descr=descr)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📷 Додати фото зараз", callback_data="add_act_photo_now")],
            [InlineKeyboardButton(text="💾 Зберегти без фото", callback_data="skip_act_photo")],
        ]
    )
    await m.answer("Оберіть, як зберегти акт:", reply_markup=kb)
    await state.set_state(ActForm.file)


async def create_act_from_state(state: FSMContext, file_id: str) -> int:
    data = await state.get_data()
    return await db_execute(
        "INSERT INTO acts (number, osbb, descr, file_id, created_at) VALUES (?,?,?,?,?)",
        (data["number"], data["osbb"], data["descr"], file_id, today_date()),
    )


@dp.callback_query(F.data == "add_act_photo_now", ActForm.file)
async def act_file_prompt(cb: CallbackQuery) -> None:
    await safe_edit_text(cb.message, "📷 Надішліть фото для нового акту.")
    await cb.answer()


@dp.callback_query(F.data == "skip_act_photo", ActForm.file)
async def act_file_skip(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        act_id = await create_act_from_state(state, "NO_FILE")
        data = await state.get_data()
        await audit_action(cb.from_user.id, "create", "acts", act_id, osbb=data.get("osbb"), new_value="Не отримано", details=f"Акт №{data.get('number')}")
        await state.clear()
        row = await get_item("acts", act_id)
        await safe_edit_text(cb.message, "✅ Акт зареєстровано без фото. Його можна додати пізніше перед закриттям.")
        if row:
            await send_item_card(cb.message.chat.id, row, "acts", cb.from_user.id)
    except Exception as exc:
        logger.exception("Could not create act without photo")
        await cb.message.answer(f"❌ Помилка реєстрації акту: {exc}")
    await cb.answer()


@dp.message(ActForm.file, F.photo)
async def act_file(m: types.Message, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        act_id = await create_act_from_state(state, m.photo[-1].file_id)
        await audit_action(m.from_user.id, "create", "acts", act_id, osbb=data.get("osbb"), new_value="Не отримано", details=f"Акт №{data.get('number')}")
        await state.clear()
        await m.answer("✅ Акт успішно зареєстровано з фото!", reply_markup=acts_menu())
        row = await get_item("acts", act_id)
        if row:
            await send_item_card(m.chat.id, row, "acts", m.from_user.id)
    except Exception as exc:
        logger.exception("Could not create act with photo")
        await m.answer(f"❌ Помилка реєстрації акту: {exc}")


@dp.message(ActForm.file)
async def act_file_wrong(m: types.Message) -> None:
    await m.answer("Надішліть фото акту або натисніть кнопку пропуску.")


@dp.message(F.text == "➕ Додати PDF чек")
async def start_doc(m: types.Message, state: FSMContext) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await state.clear()
    await m.answer("Введіть назву чеку:")
    await state.set_state(DocForm.name)


@dp.message(DocForm.name)
async def doc_name(m: types.Message, state: FSMContext) -> None:
    name = (m.text or "").strip()
    if not name:
        return await m.answer("Назва чеку не може бути порожньою.")
    await state.update_data(name=name)
    await state.set_state(DocForm.osbb)
    await m.answer("Оберіть ОСББ:", reply_markup=osbb_keyboard("doc_create", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "doc_create"), DocForm.osbb)
async def doc_osbb(cb: CallbackQuery, callback_data: OsbbCb, state: FSMContext) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb)
    await state.set_state(DocForm.file)
    await safe_edit_text(cb.message, "Завантажте PDF чек:")
    await cb.answer()


@dp.message(DocForm.file, F.document)
async def doc_file(m: types.Message, state: FSMContext) -> None:
    document = m.document
    filename = (document.file_name or "").lower()
    if document.mime_type != "application/pdf" and not filename.endswith(".pdf"):
        return await m.answer("Будь ласка, завантажте саме PDF-файл.")
    data = await state.get_data()
    doc_id = await db_execute(
        "INSERT INTO docs (name, osbb, file_id, created_at) VALUES (?,?,?,?)",
        (data["name"], data["osbb"], document.file_id, today_date()),
    )
    await audit_action(m.from_user.id, "create", "docs", doc_id, osbb=data["osbb"], new_value="Не отримано", details=data["name"])
    await state.clear()
    await m.answer("✅ PDF додано", reply_markup=docs_menu())


@dp.message(DocForm.file)
async def doc_file_wrong(m: types.Message) -> None:
    await m.answer("Будь ласка, завантажте PDF-файл.")


@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для зарплат:", reply_markup=osbb_keyboard("salary", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "salary"))
async def view_salaries_options(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Поточний місяць", callback_data=SalaryCb(action="list", osbb=osbb, month_year=current_month_year()).pack())],
            [InlineKeyboardButton(text="📂 Архів виплат", callback_data=SalaryCb(action="hist", osbb=osbb).pack())],
            [InlineKeyboardButton(text="📊 Звіт за рік", callback_data=SalaryCb(action="rep_years", osbb=osbb).pack())],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=SalaryCb(action="back").pack())],
        ]
    )
    await safe_edit_text(cb.message, f"Керування зарплатами: <b>{h(osbb)}</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "back"))
async def salary_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.delete()
    await salary_menu(cb.message, state)
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "hist"))
async def view_salary_history(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    months = await db_fetch_all("SELECT DISTINCT month_year FROM salaries WHERE osbb=? ORDER BY id DESC", (callback_data.osbb,))
    if not months:
        return await cb.answer("Історія порожня", show_alert=True)
    rows = [[InlineKeyboardButton(text=row["month_year"], callback_data=SalaryCb(action="list", osbb=callback_data.osbb, month_year=row["month_year"]).pack())] for row in months]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=OsbbCb(flow="salary", osbb=callback_data.osbb).pack())])
    await safe_edit_text(cb.message, f"Архів <b>{h(callback_data.osbb)}</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "list"))
async def show_salary_list(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    month_year = callback_data.month_year
    rows = await db_fetch_all("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=? ORDER BY id ASC", (osbb, month_year))
    if not rows:
        if month_year == current_month_year():
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати список", callback_data=SalaryCb(action="gen", osbb=osbb).pack())]])
            await safe_edit_text(cb.message, f"Нарахувань для {osbb} за {month_year} ще немає.", reply_markup=kb)
        else:
            await safe_edit_text(cb.message, "Дані відсутні.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=SalaryCb(action="hist", osbb=osbb).pack())]]))
        return await cb.answer()

    text = f"💰 <b>{osbb} | {month_year}</b>\n\n"
    buttons = []
    for row in rows:
        amount = int(row["amount"]) if float(row["amount"]).is_integer() else row["amount"]
        text += f"{h(row['status'])} {h(row['employee'])}: {amount} грн\n"
        buttons.append([InlineKeyboardButton(text=f"Змінити: {row['employee']}", callback_data=SalaryCb(action="toggle", osbb=osbb, month_year=month_year, salary_id=row["id"]).pack())])
    back_target = SalaryCb(action="hist", osbb=osbb).pack() if month_year != current_month_year() else OsbbCb(flow="salary", osbb=osbb).pack()
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_target)])
    await safe_edit_text(cb.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "gen"))
async def gen_salaries(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    month_year = current_month_year()
    existing = await db_fetch_one("SELECT count(*) AS count FROM salaries WHERE osbb=? AND month_year=?", (osbb, month_year))
    if existing and int(existing["count"]) == 0:
        rows = []
        for employee, amount in STAFF_CONFIG[osbb].items():
            rows.append((month_year, employee, get_seasonal_salary() if amount == "seasonal" else amount, osbb))
        await db_execute_many("INSERT INTO salaries (month_year, employee, amount, osbb) VALUES (?,?,?,?)", rows)
    await show_salary_list(cb, SalaryCb(action="list", osbb=osbb, month_year=month_year))


@dp.callback_query(SalaryCb.filter(F.action == "toggle"))
async def toggle_salary(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    row = await db_fetch_one("SELECT status FROM salaries WHERE id=? AND osbb=?", (callback_data.salary_id, callback_data.osbb))
    if not row:
        return await cb.answer("Запис не знайдено", show_alert=True)
    new_status = "⏳ Очікує" if row["status"] == "✅ Видано" else "✅ Видано"
    await db_execute("UPDATE salaries SET status=? WHERE id=?", (new_status, callback_data.salary_id))
    await audit_action(cb.from_user.id, "salary_status_change", "salaries", callback_data.salary_id, osbb=callback_data.osbb, old_value=row["status"], new_value=new_status)
    await show_salary_list(cb, SalaryCb(action="list", osbb=callback_data.osbb, month_year=callback_data.month_year))


@dp.callback_query(SalaryCb.filter(F.action == "rep_years"))
async def salary_report_years(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    current_year = datetime.now().year
    start_year = 2026
    rows = []
    for y in range(start_year, current_year + 1):
        rows.append([InlineKeyboardButton(text=f"📅 {y} рік", callback_data=SalaryCb(action="rep_gen", osbb=callback_data.osbb, year=str(y)).pack())])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=OsbbCb(flow="salary", osbb=callback_data.osbb).pack())])
    await safe_edit_text(cb.message, f"📊 <b>Звіт по зарплатам {h(callback_data.osbb)}</b>\nОберіть рік:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "rep_gen"))
async def salary_report_gen(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    year = callback_data.year
    await cb.answer("📈 Формую звіт по зарплатам...")
    
    salary_pattern = f"%.{year}"
    salaries = await db_fetch_all("SELECT month_year, employee, amount, status FROM salaries WHERE osbb=? AND month_year LIKE ? ORDER BY id ASC", (osbb, salary_pattern))
    
    lines = [
        "=" * 50,
        f"     ЗВІТ ПО ЗАРПЛАТАМ ДЛЯ {osbb}",
        f"     РІК: {year}",
        f"     Дата генерації: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 50,
        ""
    ]
    
    if not salaries:
        lines.append(f"Дані про виплату заробітної плати за {year} рік відсутні.\n")
    else:
        total_salary = 0.0
        for row in salaries:
            amount = float(row["amount"] or 0)
            lines.append(f"• [{row['month_year']}] {row['employee']}: {amount:g} грн — {row['status']}")
            if "Видано" in row["status"]:
                total_salary += amount
        lines.append(f"\n👉 Усього фактично виплачено (статус 'Видано') за {year} рік: {total_salary:g} грн\n")
    
    lines.extend(["=" * 50, "Кінець звіту."])
    report_text = "\n".join(lines)
    
    report_file = io.BytesIO(report_text.encode("utf-8"))
    txt_document = types.BufferedInputFile(report_file.read(), filename=f"Salary_Report_{osbb}_{year}.txt")
    await bot.send_document(cb.message.chat.id, txt_document, caption=f"💰 Звіт по зарплатам {osbb} за {year} рік")


@dp.message(F.text == "🛠️ План робіт")
async def jobs_main_menu(m: types.Message, state: FSMContext) -> None:
    if is_readonly_viewer(m.from_user.id): return
    await state.clear()
    await m.answer("🛠️ <b>Dashboard плану робіт</b>", reply_markup=jobs_menu(), parse_mode="HTML")
    await m.answer("Оберіть ОСББ або ОКПТ для перегляду:", reply_markup=osbb_keyboard("jobs_dashboard", m.from_user.id))


@dp.message(F.text == "📊 Dashboard робіт")
async def jobs_dashboard_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear(); await m.answer("🛠️ Оберіть ОСББ або ОКПТ:", reply_markup=osbb_keyboard("jobs_dashboard", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow.in_({"dashboard", "jobs_dashboard"})))
async def show_jobs_dashboard(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb): return await answer_forbidden(cb)
    text=await dashboard_text(callback_data.osbb)
    rows=await db_fetch_all("SELECT id, task_text, priority, deadline, status FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' ORDER BY CASE priority WHEN 'Критична' THEN 1 WHEN 'Висока' THEN 2 WHEN 'Середня' THEN 3 ELSE 4 END, deadline IS NULL, deadline", (callback_data.osbb,))
    text += "\n\n<b>Активні роботи:</b>"
    for r in rows[:10]:
        marker="🔴" if r["deadline"] and r["deadline"] < today_date() else ("🟡" if r["deadline"] and r["deadline"] <= (datetime.now()+timedelta(days=7)).strftime('%Y-%m-%d') else "🟢")
        text += f"\n{marker} #{r['id']} {h(r['priority'])} | {h(r['deadline'] or 'без дедлайну')} | {h(r['task_text'])}"
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати роботу", callback_data=JobCb(action="add", osbb=callback_data.osbb).pack())],
        [InlineKeyboardButton(text="📋 Відкрити всі активні", callback_data=JobCb(action="view", osbb=callback_data.osbb).pack())],
        [InlineKeyboardButton(text="📊 Повний звіт + ZIP", callback_data=OsbbCb(flow="bundle", osbb=callback_data.osbb).pack())],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")],
    ])
    await safe_edit_text(cb.message,text,reply_markup=kb,parse_mode="HTML"); await cb.answer()


@dp.message(F.text == "➕ Додати роботу")
async def job_add_start(m: types.Message, state: FSMContext) -> None:
    if not is_chairman(m.from_user.id): return await answer_forbidden(m)
    await state.clear(); await state.set_state(JobForm.osbb)
    await m.answer("Оберіть ОСББ або ОКПТ:", reply_markup=osbb_keyboard("job_add", m.from_user.id))


@dp.callback_query(JobCb.filter(F.action == "add"))
async def job_add_from_dashboard(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    if not is_chairman(cb.from_user.id): return await answer_forbidden(cb)
    await state.clear(); await state.update_data(osbb=callback_data.osbb); await state.set_state(JobForm.text)
    await safe_edit_text(cb.message, f"ОСББ: <b>{h(callback_data.osbb)}</b>\nВведіть опис роботи:", parse_mode="HTML"); await cb.answer()


@dp.callback_query(OsbbCb.filter(F.flow == "job_add"), JobForm.osbb)
async def job_add_osbb(cb: CallbackQuery, callback_data: OsbbCb, state: FSMContext) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb): return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb); await state.set_state(JobForm.text)
    await safe_edit_text(cb.message, f"ОСББ: <b>{h(callback_data.osbb)}</b>\nВведіть опис роботи:", parse_mode="HTML"); await cb.answer()


@dp.message(JobForm.text)
async def job_add_text(m: types.Message, state: FSMContext) -> None:
    text=(m.text or '').strip()
    if len(text)<3: return await m.answer("Опишіть роботу детальніше (мінімум 3 символи).")
    await state.update_data(text=text); await state.set_state(JobForm.priority)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=x,callback_data=JobCb(action="priority",mode=x).pack())] for x in ["Критична","Висока","Середня","Низька"]])
    await m.answer("Оберіть важливість роботи:",reply_markup=kb)


@dp.callback_query(JobCb.filter(F.action == "priority"), JobForm.priority)
async def job_add_priority(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    await state.update_data(priority=callback_data.mode); await state.set_state(JobForm.deadline)
    await safe_edit_text(cb.message,"Введіть дедлайн у форматі <b>РРРР-ММ-ДД</b> або натисніть /skip:",parse_mode="HTML"); await cb.answer()


@dp.message(JobForm.deadline)
async def job_add_deadline(m: types.Message, state: FSMContext) -> None:
    value=(m.text or '').strip()
    deadline=None if value=="/skip" else value
    if deadline:
        try: datetime.strptime(deadline,"%Y-%m-%d")
        except ValueError: return await m.answer("Невірний формат. Введіть РРРР-ММ-ДД або /skip.")
    data=await state.get_data(); now=datetime.now().strftime("%Y-%m-%d %H:%M")
    job_id=await db_execute("INSERT INTO jobs (osbb,month_year,task_text,updated_at,created_at,completed_at,priority,deadline) VALUES (?,?,?,?,?,?,?,?)",(data["osbb"],current_month_year(),data["text"],now,today_date(),None,data.get("priority","Середня"),deadline))
    await audit_action(m.from_user.id,"create","job",job_id,osbb=data["osbb"],new_value="Створено",details=f"{data['priority']} | дедлайн {deadline or 'без дедлайну'} | {data['text']}")
    await state.clear(); await m.answer("✅ Роботу додано до плану.",reply_markup=jobs_menu())


@dp.message(F.text == "📋 Поточні роботи")
async def current_jobs_start(m: types.Message) -> None:
    await m.answer("Оберіть ОСББ або ОКПТ:", reply_markup=osbb_keyboard("jobs_dashboard", m.from_user.id))


@dp.callback_query(JobCb.filter(F.action == "view"))
async def show_jobs_from_dashboard(cb: CallbackQuery, callback_data: JobCb) -> None:
    await cb.message.delete(); await render_jobs_page(cb.message,cb.from_user.id,callback_data.osbb,0); await cb.answer()


async def render_jobs_page(message: types.Message, user_id: int, osbb: str, page: int) -> None:
    rows=await db_fetch_all("SELECT id FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' ORDER BY CASE priority WHEN 'Критична' THEN 1 WHEN 'Висока' THEN 2 WHEN 'Середня' THEN 3 ELSE 4 END, deadline IS NULL, deadline, id DESC",(osbb,))
    if not rows: return await message.answer(f"📭 Активних робіт по {h(osbb)} немає.")
    start=page*PAGE_SIZE; visible=rows[start:start+PAGE_SIZE]
    await message.answer(f"🛠️ <b>План робіт {h(osbb)}</b> ({start+1}-{start+len(visible)} з {len(rows)})",parse_mode="HTML")
    for row in visible:
        text,kb=await render_job_text_and_kb(row["id"],user_id)
        if text: await message.answer(text,reply_markup=kb,parse_mode="HTML")
    markup=page_keyboard("jobs","",False,osbb,page,len(rows))
    if markup: await message.answer("Сторінки:",reply_markup=markup)


def job_card_markup(job_id: int, status: str, user_id: int) -> InlineKeyboardMarkup | None:
    if status == "Створено":
        rows = [[InlineKeyboardButton(text="📥 Прийняти в роботу",callback_data=JobCb(action="act",job_id=job_id,mode="proc").pack())]]
        if is_chairman(user_id):
            rows.append([InlineKeyboardButton(text="❌ Видалити роботу",callback_data=JobCb(action="act",job_id=job_id,mode="del").pack())])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if status == "В роботі": return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧱 Етап",callback_data=JobCb(action="act",job_id=job_id,mode="stage").pack()),InlineKeyboardButton(text="💬 Коментар",callback_data=JobCb(action="act",job_id=job_id,mode="comm").pack())],[InlineKeyboardButton(text="📅 Перенести дедлайн",callback_data=JobCb(action="act",job_id=job_id,mode="deadline").pack())],[InlineKeyboardButton(text="🏁 Закрити роботу",callback_data=JobCb(action="act",job_id=job_id,mode="fin").pack())]])
    return None


async def render_job_text_and_kb(job_id: int, user_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    row=await db_fetch_one("SELECT osbb,month_year,task_text,status,stages,comments,priority,deadline FROM jobs WHERE id=?",(job_id,))
    if not row or not can_access_osbb(user_id,row["osbb"]): return None,None
    deadline=row.get("deadline") or "без дедлайну"
    marker="🔴 ПРОСТРОЧЕНО" if row.get("deadline") and row["deadline"]<today_date() and row["status"]!="Роботу закінчено" else ("🟡 скоро дедлайн" if row.get("deadline") and row["deadline"] <= (datetime.now()+timedelta(days=7)).strftime('%Y-%m-%d') else "🟢")
    text=f"🛠️ <b>#{job_id} {h(row['osbb'])}</b>\n📝 {h(row['task_text'])}\n⭐ Важливість: <b>{h(row.get('priority') or 'Середня')}</b>\n📅 Дедлайн: <b>{h(deadline)}</b> {marker}\n📊 Статус: <code>{h(row['status'])}</code>"
    if row.get("stages"): text+=f"\n\n🧱 <b>Етапи:</b>\n{h(row['stages'])}"
    if row.get("comments"): text+=f"\n💬 <b>Коментарі:</b>\n{h(row['comments'])}"
    return text,job_card_markup(job_id,row["status"],user_id)


@dp.callback_query(JobCb.filter(F.action == "act"))
async def handle_job_action(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    row=await db_fetch_one("SELECT osbb,status,deadline FROM jobs WHERE id=?",(callback_data.job_id,))
    if not row or not can_access_osbb(cb.from_user.id,row["osbb"]): return await answer_forbidden(cb)
    mode=callback_data.mode; now=datetime.now().strftime("%Y-%m-%d %H:%M")
    if mode=="del":
        if not is_chairman(cb.from_user.id): return await answer_forbidden(cb)
        await db_execute("DELETE FROM jobs WHERE id=?",(callback_data.job_id,))
        await audit_action(cb.from_user.id,"delete","job",callback_data.job_id,osbb=row["osbb"],old_value=row["status"])
        await cb.message.delete(); return await cb.answer("Роботу видалено")
    if mode=="proc":
        await db_execute("UPDATE jobs SET status='В роботі',updated_at=? WHERE id=?",(now,callback_data.job_id)); await audit_action(cb.from_user.id,"status_change","job",callback_data.job_id,osbb=row["osbb"],old_value=row["status"],new_value="В роботі")
    elif mode=="fin":
        await db_execute("UPDATE jobs SET status='Роботу закінчено',updated_at=?,completed_at=? WHERE id=?",(now,now,callback_data.job_id)); await audit_action(cb.from_user.id,"complete","job",callback_data.job_id,osbb=row["osbb"],old_value=row["status"],new_value="Роботу закінчено"); await cb.message.delete(); return await cb.answer("Роботу закрито та перенесено в архів")
    elif mode in {"stage","comm","deadline"}:
        await state.update_data(job_id=callback_data.job_id,mode=mode); await state.set_state(JobCommentForm.text)
        prompt={"stage":"Введіть етап виконання:","comm":"Введіть коментар:","deadline":"Введіть новий дедлайн РРРР-ММ-ДД:"}[mode]
        await cb.message.answer(prompt); return await cb.answer()
    text,kb=await render_job_text_and_kb(callback_data.job_id,cb.from_user.id)
    if text: await safe_edit_text(cb.message,text,reply_markup=kb,parse_mode="HTML")
    await cb.answer("Оновлено")


@dp.message(JobCommentForm.text)
async def save_job_stage_or_comment(m: types.Message,state: FSMContext) -> None:
    data=await state.get_data(); job_id=int(data.get("job_id") or 0); mode=data.get("mode")
    row=await db_fetch_one("SELECT osbb,stages,comments,deadline FROM jobs WHERE id=?",(job_id,))
    if not row or not can_access_osbb(m.from_user.id,row["osbb"]): await state.clear(); return await m.answer("❌ Роботу не знайдено.")
    value=(m.text or '').strip(); now=datetime.now().strftime("%Y-%m-%d %H:%M")
    if mode=="deadline":
        try: datetime.strptime(value,"%Y-%m-%d")
        except ValueError: return await m.answer("Невірний формат. Введіть РРРР-ММ-ДД.")
        await db_execute("UPDATE jobs SET deadline=?,updated_at=? WHERE id=?",(value,now,job_id)); await audit_action(m.from_user.id,"change_deadline","job",job_id,osbb=row["osbb"],old_value=row.get("deadline"),new_value=value)
    elif mode=="stage":
        await db_execute("UPDATE jobs SET stages=?,updated_at=? WHERE id=?",((row.get("stages") or '')+f"• [{datetime.now().strftime('%d.%m %H:%M')}] {value}\n",now,job_id)); await audit_action(m.from_user.id,"add_stage","job",job_id,osbb=row["osbb"],details=value)
    else:
        await db_execute("UPDATE jobs SET comments=?,updated_at=? WHERE id=?",((row.get("comments") or '')+f"[{datetime.now().strftime('%d.%m %H:%M')}] {value}\n",now,job_id)); await audit_action(m.from_user.id,"add_comment","job",job_id,osbb=row["osbb"],details=value)
    await state.clear(); text,kb=await render_job_text_and_kb(job_id,m.from_user.id); await m.answer("✅ Оновлено.",reply_markup=jobs_menu());
    if text: await m.answer(text,reply_markup=kb,parse_mode="HTML")


@dp.message(F.text.in_(["✅ Виконані роботи", "✅ Архів робіт"]))
async def finished_jobs_menu(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для перегляду історії виконаних завдань:", reply_markup=osbb_keyboard("jobs_finished", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "jobs_finished"))
async def finished_jobs_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"✅ <b>Архів виконаних робіт {h(callback_data.osbb)}</b>. Оберіть рік:", reply_markup=period_year_keyboard("jfin", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(PeriodCb.filter())
async def period_router(cb: CallbackQuery, callback_data: PeriodCb) -> None:
    if callback_data.flow not in {"zip", "rep", "jfin", "docs"}:
        return await cb.answer("Некоректний період", show_alert=True)
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    if callback_data.step == "year":
        if not callback_data.year:
            return await safe_edit_text(cb.message, "Оберіть рік:", reply_markup=period_year_keyboard(callback_data.flow, callback_data.osbb))
        title = {"zip": "📦 ZIP Архів", "rep": "📊 Звітність", "jfin": "✅ Архів робіт", "docs": "📂 Архів чеків"}[callback_data.flow]
        await safe_edit_text(cb.message, f"{title} для <b>{h(callback_data.osbb)}</b> за {h(callback_data.year)} рік. Оберіть період:", reply_markup=period_month_keyboard(callback_data.flow, callback_data.osbb, callback_data.year), parse_mode="HTML")
        return await cb.answer()

    if callback_data.flow == "jfin":
        await show_finished_jobs_results(cb, callback_data.osbb, callback_data.year, callback_data.period)
    elif callback_data.flow == "docs":
        await show_docs_archive(cb, callback_data.osbb, callback_data.year, callback_data.period)
    elif callback_data.flow == "zip":
        if not is_chairman(cb.from_user.id):
            return await answer_forbidden(cb)
        await cb.answer("📦 Архів формується у фоні...")
        await cb.message.answer("📦 Почав формувати архів. Надішлю файл сюди, коли буде готово.")
        asyncio.create_task(send_filtered_zip(cb.message.chat.id, callback_data.osbb, callback_data.year, callback_data.period))
    elif callback_data.flow == "rep":
        if not is_chairman(cb.from_user.id):
            return await answer_forbidden(cb)
        await cb.answer("📈 Звіт формується у фоні...")
        await cb.message.answer("📈 Почав формувати звіт. Надішлю документи сюди, коли буде готово.")
        asyncio.create_task(generate_and_send_report_file(cb.message.chat.id, callback_data.osbb, callback_data.year, callback_data.period))


async def show_finished_jobs_results(cb: CallbackQuery, osbb: str, year: str, period: str) -> None:
    date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
    title = f"Всі виконані роботи {osbb} за {year} рік" if period == "all" else f"Виконані роботи {osbb} за {MONTHS_UA[period]} {year}"
    rows = await db_fetch_all(
        "SELECT month_year, task_text, stages, comments, updated_at, completed_at FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND COALESCE(completed_at, updated_at, created_at) LIKE ? ORDER BY id DESC",
        (osbb, date_pattern),
    )
    if not rows:
        return await cb.message.answer(f"📭 {title} не знайдені.")
    await cb.message.answer(f"🏁 <b>{h(title)}:</b>", parse_mode="HTML")
    for row in rows[:30]:
        text = f"📋 <b>Період планування:</b> {h(row['month_year'])}\n✅ <b>Задача:</b> {h(row['task_text'])}\n📆 <b>Дата закриття:</b> {h(row.get('completed_at') or row.get('updated_at'))}\n"
        if row.get("stages"):
            text += f"🧱 <b>Етапи виконання:</b>\n{h(row['stages'])}\n"
        if row.get("comments"):
            text += f"💬 <b>Коментарі/архів нотаток:</b>\n{h(row['comments'])}"
        await cb.message.answer(text, parse_mode="HTML")
    if len(rows) > 30:
        await cb.message.answer(f"Показано перші 30 записів із {len(rows)}. Для повного списку сформуйте звіт.")


@dp.message(F.text == "📂 Архів чеків")
async def docs_archive_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear(); await m.answer("Оберіть ОСББ або ОКПТ:", reply_markup=osbb_keyboard("docs_archive", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "docs_archive"))
async def docs_archive_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id,callback_data.osbb): return await answer_forbidden(cb)
    await safe_edit_text(cb.message,f"📂 Архів чеків <b>{h(callback_data.osbb)}</b>. Оберіть рік:",reply_markup=period_year_keyboard("docs",callback_data.osbb),parse_mode="HTML"); await cb.answer()


async def show_docs_archive(cb: CallbackQuery, osbb: str, year: str, period: str) -> None:
    pattern=f"{year}-%" if period=="all" else f"{year}-{period}-%"
    rows=await db_fetch_all("SELECT id,name AS title,osbb,file_id,status,created_at,'' AS descr FROM docs WHERE osbb=? AND created_at LIKE ? ORDER BY id DESC",(osbb,pattern))
    if not rows: return await cb.message.answer("📭 Чеків за цей період немає.")
    await cb.message.answer(f"📂 <b>Архів чеків {h(osbb)} — {h(year)} {h(period)}</b>",parse_mode="HTML")
    for row in rows: await send_item_card(cb.message.chat.id,row,"docs",cb.from_user.id,archive=True)


@dp.message(F.text == "📦 ZIP Архів")
async def zip_report_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для вивантаження ZIP-архіву:", reply_markup=osbb_keyboard("zip", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "zip"))
async def zip_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"📦 <b>ZIP Архів для {h(callback_data.osbb)}</b>. Оберіть рік:", reply_markup=period_year_keyboard("zip", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.message(F.text.in_(["📊 Прозвітувати", "📈 Звіт по ОСББ"]))
async def report_main_menu(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("📊 <b>Генерація фінансово-господарських звітів.</b>\nОберіть ОСББ:", reply_markup=osbb_keyboard("report", m.from_user.id), parse_mode="HTML")


@dp.callback_query(OsbbCb.filter(F.flow == "report"))
async def report_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"📊 <b>Звітність для {h(callback_data.osbb)}</b>. Оберіть рік:", reply_markup=period_year_keyboard("rep", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


async def download_to_bytes(file_id: str) -> bytes | None:
    try:
        file = await bot.get_file(file_id)
        downloaded = await bot.download_file(file.file_path)
        return downloaded.read()
    except Exception:
        logger.exception("Could not download Telegram file %s", file_id)
        return None


async def send_filtered_zip(chat_id: int, osbb: str, year: str, period: str) -> None:
    try:
        date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
        period_title = year if period == "all" else f"{MONTHS_UA[period]}_{year}"
        acts = await db_fetch_all("SELECT number, file_id FROM acts WHERE osbb=? AND status='Завершено!' AND created_at LIKE ?", (osbb, date_pattern))
        docs = await db_fetch_all("SELECT name, file_id FROM docs WHERE osbb=? AND status='Роботу завершено' AND created_at LIKE ?", (osbb, date_pattern))
        if not acts and not docs:
            return await bot.send_message(chat_id, f"❌ За період {period_title} для {osbb} немає закритих документів.")

        zip_buffer = io.BytesIO()
        missed = 0
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for row in acts:
                if row["file_id"] == "NO_FILE":
                    continue
                data = await download_to_bytes(row["file_id"])
                if data is None:
                    missed += 1
                    continue
                zip_file.writestr(f"Акти/Акт_{row['number']}.jpg", data)
            for row in docs:
                data = await download_to_bytes(row["file_id"])
                if data is None:
                    missed += 1
                    continue
                safe_name = str(row["name"]).replace("/", "_").replace("\\", "_")
                zip_file.writestr(f"Чеки/{safe_name}.pdf", data)
        zip_buffer.seek(0)
        document = types.BufferedInputFile(zip_buffer.read(), filename=f"Archive_{osbb}_{period_title}.zip")
        caption = f"✅ Згенеровано архів {osbb} за період: {period_title}"
        if missed:
            caption += f"\n⚠️ Не вдалося додати файлів: {missed}"
        await bot.send_document(chat_id, document, caption=caption)
    except Exception:
        logger.exception("ZIP generation failed")
        await bot.send_message(chat_id, "❌ Помилка під час формування ZIP-архіву. Деталі записані в лог.")


def report_period_title(year: str, period: str) -> str:
    return f"{year} рік" if period == "all" else f"{MONTHS_UA[period]} {year}"


async def generate_and_send_report_file(chat_id: int, osbb: str, year: str, period: str) -> None:
    try:
        date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
        title = report_period_title(year, period)
        acts = await db_fetch_all("SELECT number, descr, file_id, status, created_at FROM acts WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        docs = await db_fetch_all("SELECT name, file_id, status, created_at FROM docs WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        jobs = await db_fetch_all("SELECT task_text, stages, comments, updated_at, completed_at, month_year FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND COALESCE(completed_at, updated_at, created_at) LIKE ?", (osbb, date_pattern))

        report = build_report_text(osbb, title, acts, docs, jobs)
        report_file = io.BytesIO(report.encode("utf-8"))
        txt_document = types.BufferedInputFile(report_file.read(), filename=f"Report_{osbb}_{period}_{year}.txt")
        await bot.send_document(chat_id, txt_document, caption=f"📄 Фінансовий звіт {osbb} за {title}")
        if acts or docs:
            await send_filtered_zip(chat_id, osbb, year, period)
    except Exception:
        logger.exception("Report generation failed")
        await bot.send_message(chat_id, "❌ Помилка під час формування звіту. Деталі записані в лог.")


def build_report_text(osbb: str, title: str, acts: list[dict[str, Any]], docs: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> str:
    lines = [
        "=" * 50,
        f"     ФІНАНСОВО-ГОСПОДАРСЬКИЙ ЗВІТ ДЛЯ {osbb}",
        f"     ПЕРІОД: {title.upper()}",
        f"     Дата генерації: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 50,
        "",
        f"📋 1. АКТИ ВИКОНАНИХ РОБІТ (Всього знайдено: {len(acts)})",
        "-" * 50,
    ]
    if acts:
        for row in acts:
            lines.append(f"• Акт №{row['number']} від [{row['created_at']}] | Status: {row['status']}")
            lines.append(f"  Опис: {row['descr']}")
            lines.append("  📂 Файл в архіві: Акти/" + f"Акт_{row['number']}.jpg" if row["file_id"] != "NO_FILE" else "  ⚠️ Файл акту відсутній у базі")
            lines.append("")
    else:
        lines.append("Записів за вказаний період немає.\n")

    lines.extend([f"🧾 2. ДОКУМЕНТИ ТА ЧЕКИ ВИТРАТ (Всього знайдено: {len(docs)})", "-" * 50])
    if docs:
        for row in docs:
            lines.append(f"• Документ: {row['name']} від [{row['created_at']}] | Status: {row['status']}")
            lines.append(f"  📂 Файл в архіві: Чеки/{row['name']}.pdf\n")
    else:
        lines.append("Чеки за вказаний період відсутні.\n")

    lines.extend(["🛠️ 3. ГОСПОДАРСЬКІ РОБОТИ (ЗАКРИТІ ЗАДАЧІ ЗА ПЕРІОД)", "-" * 50])
    if jobs:
        for row in jobs:
            lines.append(f"• Задача (план на {row['month_year']}): {row['task_text']}")
            lines.append(f"  📆 Дата фінального закриття: {row.get('completed_at') or row.get('updated_at')}")
            if row.get("stages"):
                lines.append(f"  🧱 Пройдені технічні етапи:\n{row['stages']}")
            if row.get("comments"):
                lines.append(f"  💬 Лог коментарів/нотаток:\n{row['comments']}")
            lines.append("-" * 50)
    else:
        lines.append("У звітному періоді виконаних завдань немає.\n")
    lines.extend(["", "=" * 50, "Кінець звіту. Документ сформовано автоматично."])
    return "\n".join(lines)


@dp.callback_query(OsbbCb.filter(F.flow == "bundle"))
async def osbb_bundle(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not is_chairman(cb.from_user.id) or not can_access_osbb(cb.from_user.id,callback_data.osbb): return await answer_forbidden(cb)
    year=str(datetime.now().year); await cb.answer("📊 Формую повний звіт..."); await cb.message.answer(f"📊 Формую звіт для {h(callback_data.osbb)} за весь {year} рік. Надішлю файли сюди.",parse_mode="HTML")
    asyncio.create_task(generate_and_send_report_file(cb.message.chat.id,callback_data.osbb,year,"all"))


@dp.errors()
async def global_error_handler(event: types.ErrorEvent) -> bool:
    logger.exception("Unhandled update error", exc_info=event.exception)
    return True


async def run_polling_forever() -> None:
    delay=3
    while True:
        try:
            logger.info("Starting polling attempt")
            await dp.start_polling(bot, polling_timeout=30, handle_as_tasks=True, tasks_concurrency_limit=100)
            delay=3
        except asyncio.CancelledError:
            raise
        except TelegramNetworkError:
            logger.exception("Telegram network error; restarting polling in %s seconds",delay)
            await asyncio.sleep(delay); delay=min(delay*2,60)
        except Exception:
            logger.exception("Polling crashed; restarting in %s seconds",delay)
            await asyncio.sleep(delay); delay=min(delay*2,60)


async def main() -> None:
    await init_db()
    logger.info("Bot started with DB_PATH=%s", DB_PATH)
    await run_polling_forever()


if __name__ == "__main__":
    asyncio.run(main())
