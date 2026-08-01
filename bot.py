import asyncio
import io
import logging
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal

from aiogram import Bot, Dispatcher, F, types
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


class JobCommentForm(StatesGroup):
    text = State()


class SearchForm(StatesGroup):
    query = State()


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
    action: Literal["view", "hist", "list", "gen", "toggle", "back"]
    osbb: str = ""
    month_year: str = ""
    salary_id: int = 0


class JobCb(CallbackData, prefix="job"):
    action: Literal["view", "add", "month", "act", "fin"]
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


def get_seasonal_salary() -> int:
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500


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
    with _connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]


def _fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with _connect() as conn:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row else None


def _execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _connect() as conn:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or cur.rowcount or 0)


def _execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _connect() as conn:
        conn.executemany(sql, [tuple(row) for row in rows])
        conn.commit()


async def db_fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_fetch_all, sql, tuple(params))


async def db_fetch_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    return await asyncio.to_thread(_fetch_one, sql, tuple(params))


async def db_execute(sql: str, params: Iterable[Any] = ()) -> int:
    async with DB_WRITE_LOCK:
        return await asyncio.to_thread(_execute, sql, tuple(params))


async def db_execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    async with DB_WRITE_LOCK:
        await asyncio.to_thread(_execute_many, sql, [tuple(row) for row in rows])


def init_db_sync() -> None:
    with _connect() as conn:
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
                created_at TEXT
            )"""
        )
        for sql in (
            "ALTER TABLE acts ADD COLUMN created_at TEXT",
            "ALTER TABLE docs ADD COLUMN created_at TEXT",
            "ALTER TABLE jobs ADD COLUMN updated_at TEXT",
            "ALTER TABLE jobs ADD COLUMN created_at TEXT",
        ):
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_acts_osbb_status_date ON acts(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_docs_osbb_status_date ON docs(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_osbb_status_date ON jobs(osbb, status, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_salaries_osbb_month ON salaries(osbb, month_year)")
        conn.commit()


async def init_db() -> None:
    await asyncio.to_thread(init_db_sync)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки ОСББ")],
            [KeyboardButton(text="🛠️ План робіт"), KeyboardButton(text="📊 Прозвітувати")],
            [KeyboardButton(text="💰 Зарплати")],
        ],
        resize_keyboard=True,
    )


def acts_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
            [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="🔎 Пошук актів")],
            [KeyboardButton(text="📦 ZIP Архів")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def docs_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
            [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="🔎 Пошук чеків")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def jobs_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавити роботу"), KeyboardButton(text="📋 Поточні роботи")],
            [KeyboardButton(text="✅ Виконані роботи"), KeyboardButton(text="🔎 Пошук робіт")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
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


@dp.message(F.text.in_(["🔎 Пошук актів", "🔎 Пошук чеків", "🔎 Пошук робіт"]))
async def start_search(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    section = section_from_search_text(m.text or "")
    if not section:
        return await m.answer("Не вдалося визначити розділ пошуку.")
    await state.update_data(section=section)
    await state.set_state(SearchForm.query)
    await m.answer(
        f"🔎 Введіть запит для пошуку в {search_title(section)}.\n"
        "Можна шукати за номером/ID, описом, назвою, ОСББ або статусом."
    )


@dp.message(SearchForm.query)
async def run_search(m: types.Message, state: FSMContext) -> None:
    query = (m.text or "").strip()
    data = await state.get_data()
    section = data.get("section")
    if not section:
        await state.clear()
        return await m.answer("Пошук скинуто. Оберіть розділ ще раз.")
    if len(query) < 2:
        return await m.answer("Введіть мінімум 2 символи для пошуку.")

    rows = await search_records(section, query, m.from_user.id)
    await state.clear()
    if not rows:
        return await m.answer(f"📭 Нічого не знайдено за запитом: {query}")

    await m.answer(f"🔎 Знайдено {len(rows)} результат(ів) за запитом: <b>{query}</b>", parse_mode="HTML")
    if section in {"acts", "docs"}:
        for row in rows:
            await send_item_card(m.chat.id, row, section, m.from_user.id, archive=False)
        return

    for row in rows:
        text, markup = await render_job_text_and_kb(int(row["id"]), m.from_user.id)
        if text:
            await m.answer(text, reply_markup=markup, parse_mode="HTML")


async def send_item_card(chat_id: int, row: dict[str, Any], table: str, user_id: int, archive: bool = False) -> None:
    markup = None if archive else item_keyboard(int(row["id"]), row["status"], table, user_id, row.get("file_id", ""))
    if table == "acts":
        photo_note = "\n⚠️ Фото ще не додано" if row.get("file_id") == "NO_FILE" else ""
        caption = f"📄 Акт №{row['title']} ({row['osbb']}){photo_note}\n📝 Опис: {row.get('descr') or '-'}\n⏳ Статус: {row['status']}"
        if row.get("file_id") and row["file_id"] != "NO_FILE":
            try:
                await bot.send_photo(chat_id, row["file_id"], caption=caption, reply_markup=markup)
                return
            except Exception:
                logger.exception("Could not send act photo id=%s", row["id"])
        await bot.send_message(chat_id, caption, reply_markup=markup)
    else:
        caption = f"🧾 Чек: {row['title']} ({row['osbb']})\n⏳ Статус: {row['status']}"
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
    await m.answer("👋 Система готова.", reply_markup=main_menu())


@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Головне меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "main_menu_back")
async def back_to_menu_inline(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(cb.message, "Дію скасовано. Скористайтесь кнопками меню на клавіатурі.")
    await cb.answer()


@dp.message(F.text == "📄 Акти")
async def m_acts(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("АКТИ", reply_markup=acts_menu())


@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("ЧЕКИ", reply_markup=docs_menu())


@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message, state: FSMContext) -> None:
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
        act_id = await create_act_from_state(state, m.photo[-1].file_id)
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
    await db_execute(
        "INSERT INTO docs (name, osbb, file_id, created_at) VALUES (?,?,?,?)",
        (data["name"], data["osbb"], document.file_id, today_date()),
    )
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
            [InlineKeyboardButton(text="🔙 Назад", callback_data=SalaryCb(action="back").pack())],
        ]
    )
    await safe_edit_text(cb.message, f"Керування зарплатами: <b>{osbb}</b>", reply_markup=kb, parse_mode="HTML")
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
    await safe_edit_text(cb.message, f"Архів <b>{callback_data.osbb}</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
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
        text += f"{row['status']} {row['employee']}: {amount} грн\n"
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
    await show_salary_list(cb, SalaryCb(action="list", osbb=callback_data.osbb, month_year=callback_data.month_year))


@dp.message(F.text == "🛠️ План робіт")
async def jobs_main_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("🛠️ <b>Керування планом робіт по ОСББ:</b>", reply_markup=jobs_menu(), parse_mode="HTML")


@dp.message(F.text == "➕ Добавити роботу")
async def job_add_start(m: types.Message, state: FSMContext) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await state.clear()
    await state.set_state(JobForm.osbb)
    await m.answer("Оберіть ОСББ для додавання завдання:", reply_markup=osbb_keyboard("job_add", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "job_add"), JobForm.osbb)
async def job_add_osbb(cb: CallbackQuery, callback_data: OsbbCb, state: FSMContext) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb)
    await safe_edit_text(cb.message, "Оберіть місяць для планування завдання:", reply_markup=months_keyboard("job_add", callback_data.osbb))
    await cb.answer()


@dp.callback_query(JobCb.filter(F.action == "month"), JobForm.osbb)
async def job_add_month(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    month_year = f"{callback_data.mode}.{datetime.now().year}"
    await state.update_data(month_year=month_year)
    await state.set_state(JobForm.text)
    await safe_edit_text(cb.message, f"Опис завдання для обраного періоду ({month_year}).\n✍️ <b>Введіть текст задачі:</b>", parse_mode="HTML")
    await cb.answer()


@dp.message(JobForm.text)
async def job_add_save(m: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    osbb = data.get("osbb")
    month_year = data.get("month_year")
    if not osbb or not month_year or not can_access_osbb(m.from_user.id, osbb):
        await state.clear()
        return await m.answer("❌ Втрачено дані про період або ОСББ. Спробуйте створити завдання заново.")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    await db_execute(
        "INSERT INTO jobs (osbb, month_year, task_text, updated_at, created_at) VALUES (?,?,?,?,?)",
        (osbb, month_year, m.text, now, today_date()),
    )
    await state.clear()
    await m.answer("✅ Задача успішно додана в план робіт!", reply_markup=jobs_menu())


@dp.message(F.text == "📋 Поточні роботи")
async def current_jobs_start(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для перегляду активних завдань:", reply_markup=osbb_keyboard("jobs_current", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "jobs_current"))
async def show_current_jobs(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await cb.message.delete()
    await render_jobs_page(cb.message, cb.from_user.id, callback_data.osbb, 0)
    await cb.answer()


async def render_jobs_page(message: types.Message, user_id: int, osbb: str, page: int) -> None:
    rows = await db_fetch_all("SELECT id FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' ORDER BY id DESC", (osbb,))
    if not rows:
        return await message.answer(f"📭 Активних робіт по {osbb} немає.")
    start = page * PAGE_SIZE
    visible = rows[start : start + PAGE_SIZE]
    await message.answer(f"🛠️ <b>Поточні роботи {osbb}</b> ({start + 1}-{start + len(visible)} з {len(rows)})", parse_mode="HTML")
    for row in visible:
        text, kb = await render_job_text_and_kb(row["id"], user_id)
        if text:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
    markup = page_keyboard("jobs", "", False, osbb, page, len(rows))
    if markup:
        await message.answer("Сторінки:", reply_markup=markup)


def job_card_markup(job_id: int, status: str, user_id: int) -> InlineKeyboardMarkup | None:
    rows = []
    ch = is_chairman(user_id)
    if status == "Створено":
        rows.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=JobCb(action="act", job_id=job_id, mode="proc").pack())])
        if ch:
            rows.append([InlineKeyboardButton(text="❌ Видалити задачу", callback_data=JobCb(action="act", job_id=job_id, mode="del").pack())])
    elif status == "В роботі":
        rows.append(
            [
                InlineKeyboardButton(text="🧱 Додати етап", callback_data=JobCb(action="act", job_id=job_id, mode="stage").pack()),
                InlineKeyboardButton(text="💬 Коментар", callback_data=JobCb(action="act", job_id=job_id, mode="comm").pack()),
            ]
        )
        rows.append([InlineKeyboardButton(text="🏁 Роботу закінчено", callback_data=JobCb(action="act", job_id=job_id, mode="fin").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def render_job_text_and_kb(job_id: int, user_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    row = await db_fetch_one("SELECT osbb, month_year, task_text, status, stages, comments FROM jobs WHERE id=?", (job_id,))
    if not row or not can_access_osbb(user_id, row["osbb"]):
        return None, None
    text = (
        f"🛠️ <b>Завдання ОСББ {row['osbb']} ({row['month_year']})</b>\n"
        f"📝 <b>Задача:</b> {row['task_text']}\n"
        f"📊 <b>Статус:</b> <code>{row['status']}</code>\n"
    )
    if row.get("stages"):
        text += f"\n🧱 <b>Етапи виконання:</b>\n{row['stages']}"
    if row.get("comments"):
        text += f"\n💬 <b>Коментарі/нотатки:</b>\n{row['comments']}"
    return text, job_card_markup(job_id, row["status"], user_id)


@dp.callback_query(JobCb.filter(F.action == "act"))
async def handle_job_action(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    job_id = callback_data.job_id
    mode = callback_data.mode
    row = await db_fetch_one("SELECT osbb, status FROM jobs WHERE id=?", (job_id,))
    if not row:
        return await cb.answer("Задачу не знайдено", show_alert=True)
    if not can_access_osbb(cb.from_user.id, row["osbb"]):
        return await answer_forbidden(cb)
    if mode == "del" and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if mode == "del":
        await db_execute("DELETE FROM jobs WHERE id=?", (job_id,))
        await cb.message.delete()
        return await cb.answer("Видалено")
    if mode == "proc":
        await db_execute("UPDATE jobs SET status='В роботі', updated_at=? WHERE id=?", (now, job_id))
        text, kb = await render_job_text_and_kb(job_id, cb.from_user.id)
        if text:
            await safe_edit_text(cb.message, text, reply_markup=kb, parse_mode="HTML")
        return await cb.answer("Взято в роботу!")
    if mode == "fin":
        await db_execute("UPDATE jobs SET status='Роботу закінчено', updated_at=? WHERE id=?", (now, job_id))
        await cb.message.delete()
        return await cb.answer("Роботу закрито!")
    if mode in {"stage", "comm"}:
        await state.update_data(job_id=job_id, mode=mode)
        await state.set_state(JobCommentForm.text)
        prompt = "Введіть назву етапу виконання:" if mode == "stage" else "Введіть ваш коментар/зауваження до роботи:"
        await cb.message.answer(f"✍️ <b>{prompt}</b>", parse_mode="HTML")
        return await cb.answer()
    await cb.answer("Некоректна дія", show_alert=True)


@dp.message(JobCommentForm.text)
async def save_job_stage_or_comment(m: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    job_id = int(data.get("job_id") or 0)
    mode = data.get("mode")
    row = await db_fetch_one("SELECT osbb, stages, comments FROM jobs WHERE id=?", (job_id,))
    if not row or not mode or not can_access_osbb(m.from_user.id, row["osbb"]):
        await state.clear()
        return await m.answer("❌ Втрачено зв'язок із карткою завдання. Спробуйте ще раз.")
    now = datetime.now().strftime("[%d.%m %H:%M]")
    if mode == "stage":
        new_value = (row.get("stages") or "") + f"• {now} {m.text}\n"
        await db_execute("UPDATE jobs SET stages=?, updated_at=? WHERE id=?", (new_value, datetime.now().strftime("%Y-%m-%d %H:%M"), job_id))
    else:
        new_value = (row.get("comments") or "") + f"{now}: {m.text}\n"
        await db_execute("UPDATE jobs SET comments=?, updated_at=? WHERE id=?", (new_value, datetime.now().strftime("%Y-%m-%d %H:%M"), job_id))
    await state.clear()
    await m.answer("✅ Дані оновлено в картці завдання.", reply_markup=jobs_menu())


@dp.message(F.text == "✅ Виконані роботи")
async def finished_jobs_menu(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для перегляду історії виконаних завдань:", reply_markup=osbb_keyboard("jobs_finished", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "jobs_finished"))
async def finished_jobs_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"✅ <b>Архів виконаних робіт {callback_data.osbb}</b>. Оберіть рік:", reply_markup=period_year_keyboard("jfin", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(PeriodCb.filter())
async def period_router(cb: CallbackQuery, callback_data: PeriodCb) -> None:
    if callback_data.flow not in {"zip", "rep", "jfin"}:
        return await cb.answer("Некоректний період", show_alert=True)
    if not can_access_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    if callback_data.step == "year":
        if not callback_data.year:
            return await safe_edit_text(cb.message, "Оберіть рік:", reply_markup=period_year_keyboard(callback_data.flow, callback_data.osbb))
        title = {"zip": "📦 ZIP Архів", "rep": "📊 Звітність", "jfin": "✅ Архів робіт"}[callback_data.flow]
        await safe_edit_text(cb.message, f"{title} для <b>{callback_data.osbb}</b> за {callback_data.year} рік. Оберіть період:", reply_markup=period_month_keyboard(callback_data.flow, callback_data.osbb, callback_data.year), parse_mode="HTML")
        return await cb.answer()

    if callback_data.flow == "jfin":
        await show_finished_jobs_results(cb, callback_data.osbb, callback_data.year, callback_data.period)
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
        "SELECT month_year, task_text, stages, comments, updated_at FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND created_at LIKE ? ORDER BY id DESC",
        (osbb, date_pattern),
    )
    if not rows:
        return await cb.message.answer(f"📭 {title} не знайдені.")
    await cb.message.answer(f"🏁 <b>{title}:</b>", parse_mode="HTML")
    for row in rows[:30]:
        text = f"📋 <b>Період планування:</b> {row['month_year']}\n✅ <b>Задача:</b> {row['task_text']}\n📆 <b>Дата закриття:</b> {row['updated_at']}\n"
        if row.get("stages"):
            text += f"🧱 <b>Етапи виконання:</b>\n{row['stages']}\n"
        if row.get("comments"):
            text += f"💬 <b>Коментарі/архів нотаток:</b>\n{row['comments']}"
        await cb.message.answer(text, parse_mode="HTML")
    if len(rows) > 30:
        await cb.message.answer(f"Показано перші 30 записів із {len(rows)}. Для повного списку сформуйте звіт.")


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
    await safe_edit_text(cb.message, f"📦 <b>ZIP Архів для {callback_data.osbb}</b>. Оберіть рік:", reply_markup=period_year_keyboard("zip", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.message(F.text == "📊 Прозвітувати")
async def report_main_menu(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("📊 <b>Генерація фінансово-господарських звітів.</b>\nОберіть ОСББ:", reply_markup=osbb_keyboard("report", m.from_user.id), parse_mode="HTML")


@dp.callback_query(OsbbCb.filter(F.flow == "report"))
async def report_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"📊 <b>Звітність для {callback_data.osbb}</b>. Оберіть рік:", reply_markup=period_year_keyboard("rep", callback_data.osbb), parse_mode="HTML")
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
        salary_pattern = f"%.{year}" if period == "all" else f"{period}.{year}"
        title = report_period_title(year, period)
        acts = await db_fetch_all("SELECT number, descr, file_id, status, created_at FROM acts WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        docs = await db_fetch_all("SELECT name, file_id, status, created_at FROM docs WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        salaries = await db_fetch_all("SELECT month_year, employee, amount, status FROM salaries WHERE osbb=? AND month_year LIKE ?", (osbb, salary_pattern))
        jobs = await db_fetch_all("SELECT task_text, stages, comments, updated_at, month_year FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND created_at LIKE ?", (osbb, date_pattern))

        report = build_report_text(osbb, title, acts, docs, salaries, jobs)
        report_file = io.BytesIO(report.encode("utf-8"))
        txt_document = types.BufferedInputFile(report_file.read(), filename=f"Report_{osbb}_{period}_{year}.txt")
        await bot.send_document(chat_id, txt_document, caption=f"📄 Фінансовий звіт {osbb} за {title}")
        if acts or docs:
            await send_filtered_zip(chat_id, osbb, year, period)
    except Exception:
        logger.exception("Report generation failed")
        await bot.send_message(chat_id, "❌ Помилка під час формування звіту. Деталі записані в лог.")


def build_report_text(osbb: str, title: str, acts: list[dict[str, Any]], docs: list[dict[str, Any]], salaries: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> str:
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

    lines.extend(["💰 3. ВІДОМІСТЬ НАРАХУВАННЯ ТА ВИПЛАТИ ЗАРПЛАТ", "-" * 50])
    total_salary = 0.0
    if salaries:
        for row in salaries:
            amount = float(row["amount"] or 0)
            lines.append(f"• [{row['month_year']}] {row['employee']}: {amount:g} грн — {row['status']}")
            if "Видано" in row["status"]:
                total_salary += amount
        lines.append(f"\n👉 Усього виплачено за відомостями: {total_salary:g} грн\n")
    else:
        lines.append("Дані про виплату заробітної плати відсутні.\n")

    lines.extend(["🛠️ 4. ГОСПОДАРСЬКІ РОБОТИ (ЗАКРИТІ ЗАДАЧІ ЗА ПЕРІОД)", "-" * 50])
    if jobs:
        for row in jobs:
            lines.append(f"• Задача (план на {row['month_year']}): {row['task_text']}")
            lines.append(f"  📆 Дата фінального закриття: {row['updated_at']}")
            if row.get("stages"):
                lines.append(f"  🧱 Пройдені технічні етапи:\n{row['stages']}")
            if row.get("comments"):
                lines.append(f"  💬 Лог коментарів/нотаток:\n{row['comments']}")
            lines.append("-" * 50)
    else:
        lines.append("У звітному періоді виконаних завдань немає.\n")
    lines.extend(["", "=" * 50, "Кінець звіту. Документ сформовано автоматично."])
    return "\n".join(lines)


async def main() -> None:
    await init_db()
    logger.info("Bot started with DB_PATH=%s", DB_PATH)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
