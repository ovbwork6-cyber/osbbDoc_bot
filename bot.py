import asyncio
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

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
import db_layer
from access import (
    SUPERADMIN_ID,
    can_access_osbb as _can_access_osbb,
    can_manage_osbb,
    can_process_osbb,
    is_chairman as _is_chairman,
    is_readonly_viewer as _is_readonly_viewer,
    is_superadmin,
    readonly_keywords as _readonly_keywords,
    readonly_name as _readonly_name,
    user_allowed_osbbs as _user_allowed_osbbs,
    roles_for,
)
from portal import main_menu_for_user, register_handlers as register_portal_handlers, resident_period_reminder_worker, upsert_user


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("osbb_bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAIRMAN_ID_RAW = os.getenv("SUPERADMIN_ID") or os.getenv("CHAIRMAN_ID", "")
DB_PATH = str(db_layer.DB_PATH)
PAGE_SIZE = int(os.getenv("BOT_PAGE_SIZE", "5"))
LOCAL_TZ = ZoneInfo("Europe/Kyiv")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAIRMAN_ID_RAW.isdigit():
    raise RuntimeError("SUPERADMIN_ID must be numeric")

CHAIRMAN_ID = int(CHAIRMAN_ID_RAW)

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
bot = Bot(token=TOKEN)
dp = Dispatcher()


class ActForm(StatesGroup):
    number = State()
    osbb = State()
    performer = State()
    amount = State()
    descr = State()
    file = State()


class ItemPaymentForm(StatesGroup):
    amount = State()
    comment = State()


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
    action: Literal["view", "add", "month", "priority", "act", "fin"]
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
    return 3500 if 4 <= month <= 10 else 4500


def today_date() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def current_month_year() -> str:
    return datetime.now(LOCAL_TZ).strftime("%m.%Y")


def user_allowed_osbbs(user_id: int) -> list[str]:
    return _user_allowed_osbbs(user_id)


def is_chairman(user_id: int) -> bool:
    return _is_chairman(user_id)


def is_readonly_viewer(user_id: int) -> bool:
    return _is_readonly_viewer(user_id)


def readonly_keywords(user_id: int, organization: str | None = None) -> list[str]:
    return _readonly_keywords(user_id, organization)


def readonly_name(user_id: int) -> str:
    return _readonly_name(user_id)


def can_access_osbb(user_id: int, osbb: str) -> bool:
    return _can_access_osbb(user_id, osbb)


def can_read_legacy_item(user_id: int, organization: str, table: str) -> bool:
    roles=roles_for(user_id,organization)
    if is_superadmin(user_id) or roles & {"admin","accountant","fop"}:
        return True
    if table=="acts" and "viewer" in roles:
        return bool(readonly_keywords(user_id,organization))
    return False


def can_report_osbb(user_id: int, organization: str) -> bool:
    return can_access_osbb(user_id,organization) and (is_superadmin(user_id) or bool(roles_for(user_id,organization) & {"admin","accountant","fop"}))


def can_view_jobs(user_id: int, organization: str) -> bool:
    return can_access_osbb(user_id,organization) and (is_superadmin(user_id) or bool(roles_for(user_id,organization) & {"admin","accountant","fop","leadership","user"}))


def can_view_salary(user_id: int, organization: str) -> bool:
    return can_access_osbb(user_id,organization) and (is_superadmin(user_id) or bool(roles_for(user_id,organization) & {"admin","accountant","fop"}))


def is_limited_viewer(user_id: int, organization: str) -> bool:
    roles = roles_for(user_id, organization)
    return "viewer" in roles and not bool(roles & {"admin", "accountant", "fop"}) and not is_superadmin(user_id)


async def answer_forbidden(target: CallbackQuery | types.Message):
    text = "⛔ Немає доступу до цієї дії."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


async def init_db() -> None:
    await db_layer.init_db(CHAIRMAN_ID)


db_fetch_all = db_layer.db_fetch_all
db_fetch_one = db_layer.db_fetch_one
db_execute = db_layer.db_execute
db_execute_many = db_layer.db_execute_many


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Dashboard"), KeyboardButton(text="📄 Акти")]], resize_keyboard=True)


def readonly_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔄 Оновити статус")]],
        resize_keyboard=True,
    )


def acts_menu(user_id: int) -> ReplyKeyboardMarkup:
    rows=[[KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")]]
    if is_chairman(user_id): rows.append([KeyboardButton(text="➕ Створити Акт")])
    rows.append([KeyboardButton(text="🔎 Пошук актів")])
    if is_chairman(user_id): rows.append([KeyboardButton(text="📦 ZIP Архів")])
    rows.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def docs_menu(user_id: int) -> ReplyKeyboardMarkup:
    rows=[[KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],[KeyboardButton(text="🔎 Пошук чеків")]]
    if is_chairman(user_id): rows.append([KeyboardButton(text="➕ Додати PDF чек")])
    rows.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def jobs_menu(user_id: int) -> ReplyKeyboardMarkup:
    rows=[]
    if is_chairman(user_id): rows.append([KeyboardButton(text="➕ Добавити роботу")])
    rows += [[KeyboardButton(text="📋 Поточні роботи")],[KeyboardButton(text="✅ Виконані роботи"),KeyboardButton(text="🔎 Пошук робіт")],[KeyboardButton(text="⬅️ Назад")]]
    return ReplyKeyboardMarkup(
        keyboard=rows,
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
    item = db_layer.fetch_one(f"SELECT osbb FROM {table} WHERE id=?", (item_id,))
    organization = item["osbb"] if item else ""
    roles = roles_for(user_id, organization) if organization else set()
    is_admin = bool(roles & {"admin", "fop"}) or is_superadmin(user_id)
    is_accountant = bool(roles & {"accountant"}) or is_superadmin(user_id)

    if table == "acts":
        if status == "Не отримано":
            if is_admin:
                rows.append([InlineKeyboardButton(text="❌ Видалити акт", callback_data=ItemCb(cmd="ask", action="del", table=table, item_id=item_id).pack())])
            if is_accountant:
                rows.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=ItemCb(cmd="ask", action="proc", table=table, item_id=item_id).pack())])
        elif status == "В роботі" and is_accountant:
            rows.append([InlineKeyboardButton(text="➕ Записати оплату", callback_data=ItemCb(cmd="ask", action="pay", table=table, item_id=item_id).pack())])
        elif status == "Акт оплачений" and is_admin:
            if has_photo:
                rows.append([InlineKeyboardButton(text="✅ Завершити", callback_data=ItemCb(cmd="ask", action="fin", table=table, item_id=item_id).pack())])
                rows.append([InlineKeyboardButton(text="🔄 Оновити фото акту", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
            else:
                rows.append([InlineKeyboardButton(text="📷 Додати фото акту (обов'язково)", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
        if not has_photo and status != "Акт оплачений" and is_admin:
            rows.append([InlineKeyboardButton(text="📷 Завантажити фото", callback_data=ItemCb(cmd="photo", action="attach", table=table, item_id=item_id).pack())])
    else:
        if status == "Не отримано":
            if is_admin:
                rows.append([InlineKeyboardButton(text="❌ Видалити PDF", callback_data=ItemCb(cmd="ask", action="del", table=table, item_id=item_id).pack())])
            if is_accountant:
                rows.append([InlineKeyboardButton(text="📥 Прийняти чек", callback_data=ItemCb(cmd="ask", action="proc", table=table, item_id=item_id).pack())])
        elif status == "В роботі" and is_accountant:
            rows.append([InlineKeyboardButton(text="📝 Опрацьовано", callback_data=ItemCb(cmd="ask", action="pay", table=table, item_id=item_id).pack())])
        elif status == "Опрацьовано" and is_admin:
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
        return await db_fetch_one("SELECT id, number AS title, osbb, descr, performer, amount, file_id, status, created_at FROM acts WHERE id=?", (item_id,))
    if table == "docs":
        return await db_fetch_one("SELECT id, name AS title, osbb, '' AS descr, file_id, status, created_at FROM docs WHERE id=?", (item_id,))
    return None


async def _paid_total(act_id: int) -> float:
    row=await db_fetch_one("SELECT COALESCE(SUM(amount),0) total FROM act_payments WHERE act_id=?",(act_id,))
    return float(row["total"] if row else 0)


def _record_item_event(kind: str,item_id: int,event: str,old: str|None,new: str|None,actor: int,comment: str="") -> None:
    db_layer.execute("INSERT INTO item_events(item_type,item_id,event_type,old_value,new_value,comment,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(kind,item_id,event,old,new,comment,actor,db_layer.utc_now()))
    table={"act":"acts","doc":"docs","job":"jobs"}.get(kind)
    organization=db_layer.fetch_one(f"SELECT osbb FROM {table} WHERE id=?",(item_id,)) if table else None
    db_layer.execute("INSERT INTO audit_log(user_id,action,entity_type,entity_id,osbb,old_value,new_value,details,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(actor,event,kind,item_id,organization["osbb"] if organization else None,old,new,comment,db_layer.utc_now()))


async def require_item_access(cb: CallbackQuery, table: str, item_id: int) -> dict[str, Any] | None:
    row = await get_item(table, item_id)
    if not row:
        await cb.answer("Запис не знайдено", show_alert=True)
        return None
    if not can_access_osbb(cb.from_user.id, row["osbb"]):
        await answer_forbidden(cb)
        return None
    if not can_read_legacy_item(cb.from_user.id,row["osbb"],table):
        await answer_forbidden(cb)
        return None
    if is_limited_viewer(cb.from_user.id,row["osbb"]):
        if table != "acts":
            await answer_forbidden(cb)
            return None
        keywords=[k.casefold() for k in readonly_keywords(cb.from_user.id, row["osbb"])]
        if not keywords or not any(k in f"{row.get('performer') or ''} {row.get('descr') or ''}".casefold() for k in keywords):
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
    columns = "id, number AS title, osbb, descr, performer, amount, file_id, status, created_at" if table == "acts" else "id, name AS title, osbb, '' AS descr, file_id, status, created_at"
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
    rows=await db_fetch_all(sql, params)
    rows=[r for r in rows if can_read_legacy_item(user_id,r["osbb"],table)]
    filtered=[]
    for row in rows:
        if is_limited_viewer(user_id,row["osbb"]):
            if table!="acts": continue
            if not any(k.casefold() in f"{row.get('performer') or ''} {row.get('descr') or ''}".casefold() for k in readonly_keywords(user_id,row["osbb"])): continue
        filtered.append(row)
    rows=filtered
    return rows


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
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}):
        return await answer_forbidden(m)
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
        amount=f"\n💵 Сума: {row['amount']} грн" if row.get("amount") is not None else ""
        if row.get("amount") is not None:
            paid=await _paid_total(int(row["id"]))
            amount+=f"\n💳 Оплачено: {paid:g} з {float(row['amount']):g} грн"
        caption = f"📄 Акт №{row['title']} ({row['osbb']}){photo_note}\n👷 Виконавець: {row.get('performer') or '-'}{amount}\n📝 Опис: {row.get('descr') or '-'}\n⏳ Статус: {row['status']}"
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
    upsert_user(m.from_user)
    if is_readonly_viewer(m.from_user.id):
        await m.answer(
            f"👋 Вітаємо, {readonly_name(m.from_user.id)}.\n"
            "Натисніть кнопку нижче, щоб побачити актуальний статус ваших актів.",
            reply_markup=readonly_menu(),
        )
        return
    await m.answer("👋 Система готова.", reply_markup=main_menu_for_user(m.from_user.id))


@dp.message(F.text == "🔄 Оновити статус")
async def readonly_refresh_status(m: types.Message, state: FSMContext) -> None:
    if not is_readonly_viewer(m.from_user.id):
        return
    await state.clear()
    keywords = [keyword.casefold() for keyword in readonly_keywords(m.from_user.id)]
    orgs = user_allowed_osbbs(m.from_user.id)
    if not keywords or not orgs:
        return await m.answer("Superadmin ще не призначив ОСББ та ключові слова.", reply_markup=readonly_menu())
    placeholders=",".join("?" for _ in orgs)
    candidates=await db_fetch_all(f"SELECT number,osbb,status,created_at,descr,performer,file_id,id FROM acts WHERE osbb IN ({placeholders}) ORDER BY osbb,id",orgs)
    rows=[r for r in candidates if any(k in f"{r.get('performer') or ''} {r.get('descr') or ''}".casefold() for k in keywords)]
    if not rows:
        await m.answer("📭 Актів з вашим описом наразі не знайдено.", reply_markup=readonly_menu())
        return
    text = "📄 <b>Актуальний статус ваших актів:</b>\n\n"
    for row in rows:
        text += f"№{row['number']} ({row['osbb']}) — {row['performer'] or '-'} — {row['status']} [{row['created_at']}]\n{row['descr'] or ''}\n"
    await m.answer(text, parse_mode="HTML", reply_markup=readonly_menu())
    for row in rows:
        if row.get("file_id") and row["file_id"]!="NO_FILE":
            try: await bot.send_photo(m.chat.id,row["file_id"],caption=f"Акт №{row['number']} · {row['osbb']}")
            except Exception: logger.exception("Viewer photo send failed for act %s",row["id"])


@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if is_readonly_viewer(m.from_user.id):
        await m.answer("Меню:", reply_markup=readonly_menu())
        return
    await m.answer("Головне меню:", reply_markup=main_menu_for_user(m.from_user.id))


@dp.callback_query(F.data == "main_menu_back")
async def back_to_menu_inline(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(cb.message, "Дію скасовано. Скористайтесь кнопками меню на клавіатурі.")
    await cb.answer()


@dp.message(F.text == "📄 Акти")
async def m_acts(m: types.Message, state: FSMContext) -> None:
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}): return await answer_forbidden(m)
    await state.clear()
    await m.answer("АКТИ", reply_markup=acts_menu(m.from_user.id))


@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message, state: FSMContext) -> None:
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}): return await answer_forbidden(m)
    await state.clear()
    await m.answer("ЧЕКИ", reply_markup=docs_menu(m.from_user.id))


@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message, state: FSMContext) -> None:
    table = "acts" if "акт" in (m.text or "").lower() else "docs"
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}): return await answer_forbidden(m)
    await state.clear()
    archive = "Архів" in (m.text or "")
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
async def ask_item_action(cb: CallbackQuery, callback_data: ItemCb, state: FSMContext) -> None:
    if callback_data.table not in VALID_TABLES:
        return await cb.answer("Некоректна дія", show_alert=True)
    if not await require_item_access(cb, callback_data.table, callback_data.item_id):
        return
    row=await get_item(callback_data.table,callback_data.item_id)
    if not row: return await cb.answer("Запис не знайдено.",show_alert=True)
    if callback_data.action in {"proc","pay"} and not (can_process_osbb(cb.from_user.id,row["osbb"]) or (is_superadmin(cb.from_user.id))):
        return await answer_forbidden(cb)
    if callback_data.action in {"del","fin"} and not can_manage_osbb(cb.from_user.id,row["osbb"]):
        return await answer_forbidden(cb)
    if callback_data.table=="acts" and callback_data.action=="pay":
        await state.clear(); await state.update_data(act_id=callback_data.item_id); await state.set_state(ItemPaymentForm.amount)
        await cb.message.answer(f"Акт №{row['title']} · сплачено раніше {await _paid_total(callback_data.item_id):g} грн з {row.get('amount') or 'сума не задана'} грн.\nВведіть суму цього платежу:")
        return await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=confirm_keyboard(callback_data.item_id, callback_data.action, callback_data.table))
    await cb.answer("Ви впевнені?")


@dp.message(ItemPaymentForm.amount)
async def payment_amount(m: types.Message, state: FSMContext) -> None:
    try:
        amount=float((m.text or "").strip().replace(",","."))
        if amount<=0: raise ValueError
    except ValueError:
        return await m.answer("Введіть суму платежу в гривнях, більше нуля.")
    data=await state.get_data(); act_id=int(data.get("act_id") or 0); row=await get_item("acts",act_id)
    if not row or not can_process_osbb(m.from_user.id,row["osbb"]):
        await state.clear(); return await m.answer("Немає доступу до цього акту.")
    total=row.get("amount")
    already=await _paid_total(act_id)
    if total is not None and already+amount>float(total)+0.005:
        return await m.answer(f"Сума перевищує залишок ({max(float(total)-already,0):.2f} грн). Введіть суму не більшу за залишок.")
    await state.update_data(payment_amount=amount); await state.set_state(ItemPaymentForm.comment)
    await m.answer("Додайте коментар бухгалтера про платіж (наприклад, реквізити або «часткова оплата»).")


@dp.message(ItemPaymentForm.comment)
async def payment_comment(m: types.Message,state: FSMContext) -> None:
    data=await state.get_data(); act_id=int(data.get("act_id") or 0); row=await get_item("acts",act_id)
    if not row or not can_process_osbb(m.from_user.id,row["osbb"]):
        await state.clear(); return await m.answer("Немає доступу до цього акту.")
    comment=(m.text or "").strip()
    if not comment: return await m.answer("Коментар не може бути порожнім.")
    if len(comment)>1000: return await m.answer("Коментар завеликий, скоротіть до 1000 символів.")
    amount=float(data["payment_amount"]); now=db_layer.utc_now(); paid_before=await _paid_total(act_id)
    with db_layer.transaction() as conn:
        conn.execute("INSERT INTO act_payments(act_id,amount,comment,paid_at,created_by,created_at) VALUES(?,?,?,?,?,?)",(act_id,amount,comment,now,m.from_user.id,now))
        paid=paid_before+amount; total=row.get("amount")
        new_status="Акт оплачений" if total is not None and paid+0.005>=float(total) else "В роботі"
        conn.execute("UPDATE acts SET status=?,updated_at=? WHERE id=?",(new_status,now,act_id))
        conn.execute("INSERT INTO item_events(item_type,item_id,event_type,old_value,new_value,comment,actor_id,created_at) VALUES('act',?,'payment',?,?,?, ?,?)",(act_id,str(paid_before),str(paid),f"{amount:.2f} грн · {comment}",m.from_user.id,now))
        conn.execute("INSERT INTO audit_log(user_id,action,entity_type,entity_id,osbb,old_value,new_value,details,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(m.from_user.id,"payment","act",act_id,row["osbb"],str(paid_before),str(paid),f"{amount:.2f} грн · {comment}",now))
    await state.clear()
    total_text=f"{paid:.2f} з {float(row['amount']):.2f} грн" if row.get("amount") is not None else f"{paid:.2f} грн записано; загальну суму акта ще не задано Admin"
    await m.answer(f"Оплату записано: {total_text}. Статус акта: {new_status}.",reply_markup=main_menu_for_user(m.from_user.id))


@dp.message(Command("actamount"))
async def set_legacy_act_amount(m: types.Message) -> None:
    try:
        _,rest=(m.text or "").split(maxsplit=1); act_raw,amount_raw=[p.strip() for p in rest.split("|",1)]; act_id=int(act_raw); amount=float(amount_raw.replace(",","."))
        if amount<=0: raise ValueError
    except Exception:
        return await m.answer("Формат: /actamount ID_АКТА | сума_грн")
    row=await get_item("acts",act_id)
    if not row or not can_manage_osbb(m.from_user.id,row["osbb"]): return await m.answer("Немає доступу або акт не знайдено.")
    if row["status"] in FINAL_STATUSES: return await m.answer("Завершений акт змінювати не можна.")
    paid=await _paid_total(act_id)
    if paid>amount+0.005: return await m.answer(f"Сума менша за вже записані платежі ({paid:.2f} грн).")
    await db_execute("UPDATE acts SET amount=?,updated_at=? WHERE id=?",(amount,db_layer.utc_now(),act_id))
    _record_item_event("act",act_id,"set_amount",str(row.get("amount")),str(amount),m.from_user.id)
    await m.answer(f"Суму акту №{row['title']} збережено: {amount:.2f} грн.")


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
    org= row["osbb"]
    admin_allowed=can_manage_osbb(user_id,org)
    accountant_allowed=can_process_osbb(user_id,org)
    if action in {"del", "fin"} and not admin_allowed:
        return await answer_forbidden(cb)
    if action in {"proc", "pay"} and not accountant_allowed:
        return await answer_forbidden(cb)
    if table=="acts" and action=="pay":
        return await cb.answer("Запис платежу потрібно почати кнопкою «Записати оплату».",show_alert=True)
    allowed_transition={
        ("acts","proc","Не отримано"),
        ("acts","fin","Акт оплачений"),
        ("docs","proc","Не отримано"),
        ("docs","pay","В роботі"),
        ("docs","fin","Опрацьовано"),
        ("acts","del","Не отримано"),
        ("docs","del","Не отримано"),
    }
    if (table,action,row["status"]) not in allowed_transition:
        return await cb.answer("Цей перехід статусу зараз неможливий.",show_alert=True)
    if action=="del" and row["status"] not in {"Не отримано"}:
        return await cb.answer("Видаляти можна лише новий, ще не прийнятий запис.",show_alert=True)
    if table == "acts" and action == "fin":
        paid=await _paid_total(callback_data.item_id)
        if row.get("amount") is None or paid+0.005 < float(row["amount"]):
            return await cb.answer("Акт можна завершити лише після повної оплати. Перевірте суму та платежі.", show_alert=True)
        if row.get("file_id") in ("", "NO_FILE", None):
            return await cb.answer("Перед завершенням акту потрібно додати фото.", show_alert=True)

    if action == "del":
        _record_item_event("act" if table=="acts" else "doc",callback_data.item_id,"deleted",row["status"],None,user_id)
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
    if table=="acts":
        await db_execute("UPDATE acts SET updated_at=?"+(",completed_at=?" if action=="fin" else "")+" WHERE id=?",(today_date(),today_date(),callback_data.item_id) if action=="fin" else (today_date(),callback_data.item_id))
    else:
        await db_execute("UPDATE docs SET updated_at=?"+(",completed_at=?" if action=="fin" else "")+" WHERE id=?",(today_date(),today_date(),callback_data.item_id) if action=="fin" else (today_date(),callback_data.item_id))
    _record_item_event("act" if table=="acts" else "doc",callback_data.item_id,action,row["status"],new_status,user_id)
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
    if not can_manage_osbb(cb.from_user.id,row["osbb"]):
        return await answer_forbidden(cb)
    if row["status"] in FINAL_STATUSES:
        return await cb.answer("Завершений акт більше не змінюється.",show_alert=True)
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
    if not row or not can_manage_osbb(m.from_user.id, row["osbb"]):
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
    await state.update_data(user_id=m.from_user.id)
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
    if not can_manage_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb)
    await state.set_state(ActForm.performer)
    await safe_edit_text(cb.message, "Введіть виконавця (це поле використовуватиметься для пошуку Viewer):")
    await cb.answer()


@dp.message(ActForm.performer)
async def act_performer(m: types.Message,state: FSMContext) -> None:
    performer=(m.text or "").strip()
    if not performer or len(performer)>200: return await m.answer("Вкажіть виконавця (до 200 символів).")
    await state.update_data(performer=performer); await state.set_state(ActForm.amount)
    await m.answer("Введіть повну суму акту в гривнях.")


@dp.message(ActForm.amount)
async def act_amount(m: types.Message,state: FSMContext) -> None:
    try:
        amount=float((m.text or "").strip().replace(",","."))
        if amount<=0 or amount>1_000_000_000: raise ValueError
    except ValueError: return await m.answer("Введіть суму більше нуля, у гривнях.")
    await state.update_data(amount=amount); await state.set_state(ActForm.descr)
    await m.answer("Введіть опис акту:")


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
    if not data.get("osbb") or not can_manage_osbb(int(data.get("user_id") or 0),data["osbb"]):
        raise PermissionError("Немає права створювати акти в цій організації або гілка не оплачена.")
    now=db_layer.utc_now()
    act_id=await db_execute(
        "INSERT INTO acts (number, osbb, descr, performer, amount, file_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (data["number"], data["osbb"], data["descr"],data["performer"],data["amount"], file_id, today_date(),now),
    )
    _record_item_event("act",act_id,"created",None,"Не отримано",int(data.get("user_id") or CHAIRMAN_ID))
    return act_id


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
        await m.answer("✅ Акт успішно зареєстровано з фото!", reply_markup=acts_menu(m.from_user.id))
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
    if not can_manage_osbb(cb.from_user.id, callback_data.osbb):
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
    if not can_manage_osbb(m.from_user.id,data.get("osbb", "")):
        await state.clear(); return await m.answer("Немає права додавати чеки в цій організації.")
    await db_execute(
        "INSERT INTO docs (name, osbb, file_id, created_at) VALUES (?,?,?,?)",
        (data["name"], data["osbb"], document.file_id, today_date()),
    )
    await state.clear()
    await m.answer("✅ PDF додано", reply_markup=docs_menu(m.from_user.id))


@dp.message(DocForm.file)
async def doc_file_wrong(m: types.Message) -> None:
    await m.answer("Будь ласка, завантажте PDF-файл.")


@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для зарплат:", reply_markup=osbb_keyboard("salary", m.from_user.id))


@dp.message(Command("salaryadd"))
async def salary_template_add(m: types.Message) -> None:
    try:
        _,rest=(m.text or "").split(maxsplit=1); osbb,employee,amount_raw=[x.strip() for x in rest.split("|",2)]; amount=float(amount_raw.replace(",","."))
        if amount<0: raise ValueError
    except Exception: return await m.answer("Формат: /salaryadd Назва_організації | працівник | сума_грн")
    if not can_manage_osbb(m.from_user.id,osbb): return await m.answer("Немає доступу до цієї організації.")
    org=await db_fetch_one("SELECT id FROM organizations WHERE name=?",(osbb,))
    await db_execute("INSERT INTO salary_templates(organization_id,employee,amount) VALUES(?,?,?) ON CONFLICT(organization_id,employee) DO UPDATE SET amount=excluded.amount,active=1",(org["id"],employee,amount))
    await m.answer(f"Працівника {employee} та ставку {amount:g} грн додано до списку {osbb}.")


@dp.message(Command("salaryseason"))
async def salary_template_season(m: types.Message) -> None:
    try:
        _,rest=(m.text or "").split(maxsplit=1); osbb,employee,terms=[x.strip() for x in rest.split("|",2)]
        periods={}
        for item in terms.split(","):
            months,amount_raw=[x.strip() for x in item.split("=",1)]; start,end=months.split("-",1); amount=float(amount_raw.replace(",",".")); periods[f"{int(start)}-{int(end)}"]=amount
            if int(start)<1 or int(start)>12 or int(end)<1 or int(end)>12 or amount<0: raise ValueError
    except Exception: return await m.answer("Формат: /salaryseason Назва_організації | працівник | 4-10=3500,11-3=4500")
    if not can_manage_osbb(m.from_user.id,osbb): return await m.answer("Немає доступу до цієї організації.")
    org=await db_fetch_one("SELECT id FROM organizations WHERE name=?",(osbb,))
    import json
    await db_execute("INSERT INTO salary_templates(organization_id,employee,amount,seasonal_json) VALUES(?,?,0,?) ON CONFLICT(organization_id,employee) DO UPDATE SET seasonal_json=excluded.seasonal_json,active=1",(org["id"],employee,json.dumps(periods,ensure_ascii=False)))
    await m.answer(f"Сезонні ставки для {employee} збережено.")


@dp.callback_query(OsbbCb.filter(F.flow == "salary"))
async def view_salaries_options(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_view_salary(cb.from_user.id,callback_data.osbb):
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
    await safe_edit_text(cb.message, f"Керування зарплатами: <b>{osbb}</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "back"))
async def salary_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.delete()
    await salary_menu(cb.message, state)
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "hist"))
async def view_salary_history(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not can_view_salary(cb.from_user.id,callback_data.osbb):
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
    if not can_view_salary(cb.from_user.id,callback_data.osbb):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    month_year = callback_data.month_year
    rows = await db_fetch_all("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=? ORDER BY id ASC", (osbb, month_year))
    if not rows:
        if month_year == current_month_year():
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати список", callback_data=SalaryCb(action="gen", osbb=osbb).pack())]]) if can_manage_osbb(cb.from_user.id,osbb) else None
            await safe_edit_text(cb.message, f"Нарахувань для {osbb} за {month_year} ще немає.", reply_markup=kb)
        else:
            await safe_edit_text(cb.message, "Дані відсутні.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=SalaryCb(action="hist", osbb=osbb).pack())]]))
        return await cb.answer()

    text = f"💰 <b>{osbb} | {month_year}</b>\n\n"
    buttons = []
    for row in rows:
        amount = int(row["amount"]) if float(row["amount"]).is_integer() else row["amount"]
        text += f"{row['status']} {row['employee']}: {amount} грн\n"
        if can_manage_osbb(cb.from_user.id,osbb):
            buttons.append([InlineKeyboardButton(text=f"Змінити: {row['employee']}", callback_data=SalaryCb(action="toggle", osbb=osbb, month_year=month_year, salary_id=row["id"]).pack())])
    back_target = SalaryCb(action="hist", osbb=osbb).pack() if month_year != current_month_year() else OsbbCb(flow="salary", osbb=osbb).pack()
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_target)])
    await safe_edit_text(cb.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "gen"))
async def gen_salaries(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not can_manage_osbb(cb.from_user.id,callback_data.osbb):
        return await answer_forbidden(cb)
    osbb = callback_data.osbb
    month_year = current_month_year()
    existing = await db_fetch_one("SELECT count(*) AS count FROM salaries WHERE osbb=? AND month_year=?", (osbb, month_year))
    if existing and int(existing["count"]) == 0:
        templates=await db_fetch_all("SELECT employee,amount,seasonal_json FROM salary_templates WHERE organization_id=(SELECT id FROM organizations WHERE name=?) AND active=1 ORDER BY employee",(osbb,))
        rows = []
        for item in templates:
            amount=float(item["amount"])
            if item.get("seasonal_json"):
                try:
                    import json
                    seasonal=json.loads(item["seasonal_json"])
                    month=int(datetime.now(LOCAL_TZ).month)
                    for key,value in seasonal.items():
                        start,end=map(int,key.split("-"))
                        in_season=(start<=month<=end) if start<=end else (month>=start or month<=end)
                        if in_season: amount=float(value)
                except Exception: logger.exception("Invalid seasonal salary for %s",item["employee"])
            rows.append((month_year,item["employee"],amount,osbb))
        if not rows:
            return await cb.answer("Спочатку додайте працівників і ставки командою /salaryadd.",show_alert=True)
        await db_execute_many("INSERT INTO salaries (month_year, employee, amount, osbb) VALUES (?,?,?,?)", rows)
    await show_salary_list(cb, SalaryCb(action="list", osbb=osbb, month_year=month_year))


@dp.callback_query(SalaryCb.filter(F.action == "toggle"))
async def toggle_salary(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not can_manage_osbb(cb.from_user.id,callback_data.osbb):
        return await answer_forbidden(cb)
    row = await db_fetch_one("SELECT status FROM salaries WHERE id=? AND osbb=?", (callback_data.salary_id, callback_data.osbb))
    if not row:
        return await cb.answer("Запис не знайдено", show_alert=True)
    new_status = "⏳ Очікує" if row["status"] == "✅ Видано" else "✅ Видано"
    await db_execute("UPDATE salaries SET status=?,paid_at=? WHERE id=?", (new_status,today_date() if new_status=="✅ Видано" else None, callback_data.salary_id))
    await show_salary_list(cb, SalaryCb(action="list", osbb=callback_data.osbb, month_year=callback_data.month_year))


@dp.callback_query(SalaryCb.filter(F.action == "rep_years"))
async def salary_report_years(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not can_view_salary(cb.from_user.id,callback_data.osbb):
        return await answer_forbidden(cb)
    current_year = datetime.now().year
    start_year = 2026
    rows = []
    for y in range(start_year, current_year + 1):
        rows.append([InlineKeyboardButton(text=f"📅 {y} рік", callback_data=SalaryCb(action="rep_gen", osbb=callback_data.osbb, year=str(y)).pack())])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=OsbbCb(flow="salary", osbb=callback_data.osbb).pack())])
    await safe_edit_text(cb.message, f"📊 <b>Звіт по зарплатам {callback_data.osbb}</b>\nОберіть рік:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(SalaryCb.filter(F.action == "rep_gen"))
async def salary_report_gen(cb: CallbackQuery, callback_data: SalaryCb) -> None:
    if not can_view_salary(cb.from_user.id,callback_data.osbb):
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
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}): return await answer_forbidden(m)
    await state.clear()
    await m.answer("🛠️ <b>План робіт:</b>", reply_markup=jobs_menu(m.from_user.id), parse_mode="HTML")


@dp.message(F.text == "➕ Добавити роботу")
async def job_add_start(m: types.Message, state: FSMContext) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await state.clear()
    await state.set_state(JobForm.osbb)
    await m.answer("Оберіть ОСББ для додавання завдання:", reply_markup=osbb_keyboard("job_add", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "job_add"), JobForm.osbb)
async def job_add_osbb(cb: CallbackQuery, callback_data: OsbbCb, state: FSMContext) -> None:
    if not can_manage_osbb(cb.from_user.id, callback_data.osbb):
        return await answer_forbidden(cb)
    await state.update_data(osbb=callback_data.osbb)
    await safe_edit_text(cb.message, "Оберіть місяць для планування завдання:", reply_markup=months_keyboard("job_add", callback_data.osbb))
    await cb.answer()


@dp.callback_query(JobCb.filter(F.action == "month"), JobForm.osbb)
async def job_add_month(cb: CallbackQuery, callback_data: JobCb, state: FSMContext) -> None:
    month_year = f"{callback_data.mode}.{datetime.now(LOCAL_TZ).year}"
    await state.update_data(month_year=month_year)
    await state.set_state(JobForm.text)
    await safe_edit_text(cb.message, f"Опис завдання для обраного періоду ({month_year}).\n✍️ <b>Введіть текст задачі:</b>", parse_mode="HTML")
    await cb.answer()


@dp.message(JobForm.text)
async def job_add_save(m: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    osbb = data.get("osbb")
    month_year = data.get("month_year")
    if not osbb or not month_year or not can_manage_osbb(m.from_user.id, osbb):
        await state.clear()
        return await m.answer("❌ Втрачено дані про період або ОСББ. Спробуйте створити завдання заново.")
    await state.update_data(task_text=(m.text or "").strip())
    await state.set_state(JobForm.priority)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=x,callback_data=JobCb(action="priority",mode=x).pack())] for x in ["Критична","Висока","Середня","Низька"]])
    await m.answer("Оберіть важливість роботи:",reply_markup=kb)


@dp.callback_query(JobCb.filter(F.action=="priority"),JobForm.priority)
async def job_priority(cb: CallbackQuery,callback_data: JobCb,state: FSMContext) -> None:
    if callback_data.mode not in {"Критична","Висока","Середня","Низька"}: return await cb.answer("Некоректний пріоритет.",show_alert=True)
    await state.update_data(priority=callback_data.mode); await state.set_state(JobForm.deadline)
    await cb.message.answer("Введіть дедлайн у форматі YYYY-MM-DD або «-», якщо без дедлайну."); await cb.answer()


@dp.message(JobForm.deadline)
async def job_deadline_save(m: types.Message,state: FSMContext) -> None:
    data=await state.get_data(); osbb=data.get("osbb"); month_year=data.get("month_year"); deadline=(m.text or "").strip()
    if deadline!="-":
        try: deadline=date.fromisoformat(deadline).isoformat()
        except ValueError: return await m.answer("Дедлайн має бути датою YYYY-MM-DD або «-».")
    if not osbb or not can_manage_osbb(m.from_user.id,osbb): await state.clear(); return await m.answer("Немає права додати роботу.")
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    job_id=await db_execute(
        "INSERT INTO jobs (osbb, month_year, task_text,priority,deadline,updated_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (osbb, month_year, data.get("task_text"),data.get("priority","Середня"),None if deadline=="-" else deadline,now,today_date()),
    )
    _record_item_event("job",job_id,"created",None,"Створено",m.from_user.id)
    await state.clear()
    await m.answer("✅ Задача успішно додана в план робіт!", reply_markup=jobs_menu(m.from_user.id))


@dp.message(F.text == "📋 Поточні роботи")
async def current_jobs_start(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для перегляду активних завдань:", reply_markup=osbb_keyboard("jobs_current", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "jobs_current"))
async def show_current_jobs(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_view_jobs(cb.from_user.id, callback_data.osbb):
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
    job=db_layer.fetch_one("SELECT osbb FROM jobs WHERE id=?",(job_id,)); ch=bool(job and can_manage_osbb(user_id,job["osbb"]))
    if not ch: return None
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
    row = await db_fetch_one("SELECT osbb, month_year, task_text, status, stages, comments,priority,deadline FROM jobs WHERE id=?", (job_id,))
    if not row or not can_view_jobs(user_id, row["osbb"]):
        return None, None
    text = (
        f"🛠️ <b>Завдання ОСББ {row['osbb']} ({row['month_year']})</b>\n"
        f"📝 <b>Задача:</b> {row['task_text']}\n"
        f"📊 <b>Статус:</b> <code>{row['status']}</code>\n"
        f"⚠️ <b>Важливість:</b> {row.get('priority') or 'Середня'}\n"
        f"📅 <b>Дедлайн:</b> {row.get('deadline') or '—'}\n"
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
    if not can_manage_osbb(cb.from_user.id, row["osbb"]):
        return await answer_forbidden(cb)
    if mode=="del" and row["status"]!="Створено": return await cb.answer("Видаляти можна лише нову роботу.",show_alert=True)

    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    if mode == "del":
        _record_item_event("job",job_id,"deleted",row["status"],None,cb.from_user.id)
        await db_execute("DELETE FROM jobs WHERE id=?", (job_id,))
        await cb.message.delete()
        return await cb.answer("Видалено")
    if mode == "proc":
        if row["status"]!="Створено": return await cb.answer("Робота вже змінена.",show_alert=True)
        await db_execute("UPDATE jobs SET status='В роботі', updated_at=? WHERE id=?", (now, job_id))
        _record_item_event("job",job_id,"status",row["status"],"В роботі",cb.from_user.id)
        text, kb = await render_job_text_and_kb(job_id, cb.from_user.id)
        if text:
            await safe_edit_text(cb.message, text, reply_markup=kb, parse_mode="HTML")
        return await cb.answer("Взято в роботу!")
    if mode == "fin":
        if row["status"]!="В роботі": return await cb.answer("Спершу прийміть роботу в роботу.",show_alert=True)
        await db_execute("UPDATE jobs SET status='Роботу закінчено', updated_at=? WHERE id=?", (now, job_id))
        await db_execute("UPDATE jobs SET completed_at=? WHERE id=?",(now,job_id))
        _record_item_event("job",job_id,"status",row["status"],"Роботу закінчено",cb.from_user.id)
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
    if not row or not mode or not can_manage_osbb(m.from_user.id, row["osbb"]):
        await state.clear()
        return await m.answer("❌ Втрачено зв'язок із карткою завдання. Спробуйте ще раз.")
    now = datetime.now(LOCAL_TZ).strftime("[%d.%m %H:%M]")
    if mode == "stage":
        new_value = (row.get("stages") or "") + f"• {now} {m.text}\n"
        await db_execute("UPDATE jobs SET stages=?, updated_at=? WHERE id=?", (new_value, datetime.now().strftime("%Y-%m-%d %H:%M"), job_id))
        _record_item_event("job",job_id,"stage",row.get("stages"),new_value,m.from_user.id,(m.text or ""))
    else:
        new_value = (row.get("comments") or "") + f"{now}: {m.text}\n"
        await db_execute("UPDATE jobs SET comments=?, updated_at=? WHERE id=?", (new_value, datetime.now().strftime("%Y-%m-%d %H:%M"), job_id))
        _record_item_event("job",job_id,"comment",row.get("comments"),new_value,m.from_user.id,(m.text or ""))
    await state.clear()
    await m.answer("✅ Дані оновлено в картці завдання.", reply_markup=jobs_menu(m.from_user.id))


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
        if not can_report_osbb(cb.from_user.id,callback_data.osbb):
            return await answer_forbidden(cb)
        await cb.answer("📦 Архів формується у фоні...")
        await cb.message.answer("📦 Почав формувати архів. Надішлю файл сюди, коли буде готово.")
        asyncio.create_task(send_filtered_zip(cb.message.chat.id, callback_data.osbb, callback_data.year, callback_data.period))
    elif callback_data.flow == "rep":
        if not can_report_osbb(cb.from_user.id,callback_data.osbb):
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
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}):
        return await answer_forbidden(m)
    await m.answer("Оберіть ОСББ для вивантаження ZIP-архіву:", reply_markup=osbb_keyboard("zip", m.from_user.id))


@dp.callback_query(OsbbCb.filter(F.flow == "zip"))
async def zip_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_report_osbb(cb.from_user.id,callback_data.osbb):
        return await answer_forbidden(cb)
    await safe_edit_text(cb.message, f"📦 <b>ZIP Архів для {callback_data.osbb}</b>. Оберіть рік:", reply_markup=period_year_keyboard("zip", callback_data.osbb), parse_mode="HTML")
    await cb.answer()


@dp.message(F.text == "📊 Прозвітувати")
async def report_main_menu(m: types.Message) -> None:
    if not is_superadmin(m.from_user.id) and not (roles_for(m.from_user.id) & {"admin","accountant","fop"}):
        return await answer_forbidden(m)
    await m.answer("📊 <b>Генерація фінансово-господарських звітів.</b>\nОберіть ОСББ:", reply_markup=osbb_keyboard("report", m.from_user.id), parse_mode="HTML")


@dp.callback_query(OsbbCb.filter(F.flow == "report"))
async def report_years(cb: CallbackQuery, callback_data: OsbbCb) -> None:
    if not can_report_osbb(cb.from_user.id,callback_data.osbb):
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
        acts = await db_fetch_all("SELECT id,number, file_id FROM acts WHERE osbb=? AND status='Завершено!' AND created_at LIKE ?", (osbb, date_pattern))
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
                safe_number=re.sub(r"[^\w.-]","_",str(row["number"] or "без_номера"))
                zip_file.writestr(f"Акти/Акт_{row['id']}_{safe_number}.jpg", data)
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
        acts = await db_fetch_all("SELECT id, number, performer, amount, descr, file_id, status, created_at FROM acts WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        docs = await db_fetch_all("SELECT name, file_id, status, created_at FROM docs WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
        jobs = await db_fetch_all("SELECT task_text, stages, comments, updated_at, month_year FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND created_at LIKE ?", (osbb, date_pattern))

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
            lines.append(f"  Виконавець: {row.get('performer') or '-'} | Сума: {row.get('amount') if row.get('amount') is not None else 'не задана'} грн")
            lines.append(f"  Опис: {row['descr']}")
            payments=db_layer.fetch_all("SELECT amount,comment,paid_at,created_by FROM act_payments WHERE act_id=? ORDER BY id",(row["id"],))
            if payments:
                lines.append("  Платежі:")
                for payment in payments: lines.append(f"    {payment['paid_at']} · {payment['amount']:.2f} грн · {payment['comment']} · бухгалтер {payment['created_by']}")
            history=db_layer.fetch_all("SELECT event_type,old_value,new_value,comment,actor_id,created_at FROM item_events WHERE item_type='act' AND item_id=? ORDER BY id",(row["id"],))
            if history:
                lines.append("  Історія змін:")
                for event in history: lines.append(f"    {event['created_at']} · {event['event_type']} · {event['old_value'] or ''} → {event['new_value'] or ''} · {event['comment'] or ''} · {event['actor_id']}")
            safe_number=re.sub(r"[^\w.-]","_",str(row["number"] or "без_номера"))
            lines.append("  📂 Файл в архіві: " + f"Акти/Акт_{row['id']}_{safe_number}.jpg" if row["file_id"] != "NO_FILE" else "  ⚠️ Файл акту відсутній у базі")
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


register_portal_handlers(dp, bot)


async def main() -> None:
    await init_db()
    logger.info("Bot started with DB_PATH=%s", DB_PATH)
    reminders=asyncio.create_task(resident_period_reminder_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        try: await reminders
        except asyncio.CancelledError: pass


if __name__ == "__main__":
    asyncio.run(main())
