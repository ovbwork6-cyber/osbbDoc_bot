import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

# Імпортуємо наші шари даних та прав
from db_layer import db_init, db_fetch_all, db_fetch_one, db_execute
from access import CHAIRMAN_ID, is_chairman, is_readonly_viewer, user_allowed_osbbs, READONLY_VIEWERS

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("osbb_bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- FSM СТАНИ ДЛЯ АКТІВ ТА ЧЕКІВ ---
class ActCreateForm(StatesGroup):
    number = State()
    osbb = State()
    viewer_keyword = State()
    amount = State()
    descr = State()
    file = State()


class ActPaymentForm(StatesGroup):
    payment = State()


class CheckCreateForm(StatesGroup):
    osbb = State()
    purpose = State()
    file = State()


async def answer_forbidden(target: CallbackQuery | types.Message):
    text = "⛔ Немає доступу до цієї дії."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


# --- КЛАВІАТУРИ ТА МЕНЮ ---
def chairman_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Дашборд ОСББ/ОК", callback_data="menu_dashboard")],
            [
                InlineKeyboardButton(text="🗂️ Робоче середовище", callback_data="menu_workspace"),
                InlineKeyboardButton(text="🏘️ Мешканці та будинки", callback_data="menu_residents"),
            ],
            [
                InlineKeyboardButton(text="📝 Написати документ", callback_data="menu_documents"),
                InlineKeyboardButton(text="🌐 Веб-панель", web_app=WebAppInfo(url="http://104.248.21.75:8000")),
            ],
            [InlineKeyboardButton(text="🔐 Режим Superadmin", callback_data="menu_superadmin")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Дашборд ОСББ/ОК", callback_data="menu_dashboard")],
            [
                InlineKeyboardButton(text="🗂️ Робоче середовище", callback_data="menu_workspace"),
                InlineKeyboardButton(text="🏘️ Мешканці та будинки", callback_data="menu_residents"),
            ],
            [
                InlineKeyboardButton(text="📝 Написати документ", callback_data="menu_documents"),
                InlineKeyboardButton(text="🌐 Веб-панель", web_app=WebAppInfo(url="http://104.248.21.75:8000")),
            ],
        ]
    )


def viewer_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Оновити статус")],
            [KeyboardButton(text="🌐 Веб-панель")]
        ],
        resize_keyboard=True,
    )


# --- РОУТИНГ КОМАНДИ /START ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    user_id = m.from_user.id

    if is_chairman(user_id):
        await m.answer("👋 Вітаємо, Голово! Панель керування Superadmin активна:", reply_markup=chairman_menu())
        return
    
    if user_allowed_osbbs(user_id):
        await m.answer("👋 Система управління будинками ОСББ:", reply_markup=admin_menu())
        return

    if is_readonly_viewer(user_id):
        await m.answer(
            f"👋 Вітаємо, {READONLY_VIEWERS[user_id]['name']}.\n"
            "Натисніть кнопку нижче, щоб оновити статуси ваших актів.",
            reply_markup=viewer_menu(),
        )
        return

    await m.answer("⛔ У вас немає доступу до цієї системи.")


# --- МЕНЮ РОБОЧОГО СЕРЕДОВИЩА ---
@dp.callback_query(F.data == "menu_workspace")
async def cb_workspace(cb: CallbackQuery) -> None:
    if not user_allowed_osbbs(cb.from_user.id) and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Поточні акти", callback_data="ws_acts"), InlineKeyboardButton(text="🧾 Чеки ОСББ", callback_data="ws_docs")],
            [InlineKeyboardButton(text="💰 Зарплати", callback_data="ws_salaries"), InlineKeyboardButton(text="🛠️ Регламентні роботи", callback_data="ws_jobs")],
            [InlineKeyboardButton(text="🔙 Назад у меню", callback_data="main_menu_back")]
        ]
    )
    await cb.message.edit_text("🗂️ <b>Робоче середовище:</b>\nОберіть потрібний розділ нижче:", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# --- МОДУЛЬ АКТІВ ---
@dp.callback_query(F.data == "ws_acts")
async def workspace_acts_menu(cb: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Створити акт на оплату", callback_data="act_start_create")],
            [InlineKeyboardButton(text="📋 Поточні акти", callback_data="act_list_current")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_workspace")]
        ]
    )
    await cb.message.edit_text("📄 <b>Керування актами виконаних робіт:</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "act_start_create")
async def act_start_create(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ActCreateForm.number)
    await cb.message.answer("✍️ Введіть номер акту:")
    await cb.answer()


@dp.message(ActCreateForm.number)
async def act_got_number(m: types.Message, state: FSMContext) -> None:
    await state.update_data(number=m.text.strip())
    await state.set_state(ActCreateForm.osbb)
    
    allowed = user_allowed_osbbs(m.from_user.id)
    rows = [[InlineKeyboardButton(text=org, callback_data=f"act_osbb_{org}")] for org in allowed]
    await m.answer("Оберіть ОСББ або ОК:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("act_osbb_"), ActCreateForm.osbb)
async def act_got_osbb(cb: CallbackQuery, state: FSMContext) -> None:
    osbb = cb.data.replace("act_osbb_", "")
    await state.update_data(osbb=osbb)
    await state.set_state(ActCreateForm.viewer_keyword)
    await cb.message.edit_text(f"ОСББ: <b>{osbb}</b>\n\n👤 Введіть ключове слово для переглядача (Viewer) або напишіть 'немає':", parse_mode="HTML")
    await cb.answer()


@dp.message(ActCreateForm.viewer_keyword)
async def act_got_keyword(m: types.Message, state: FSMContext) -> None:
    kw = m.text.strip()
    if kw.lower() == "немає":
        kw = ""
    await state.update_data(viewer_keyword=kw)
    await state.set_state(ActCreateForm.amount)
    await m.answer("💰 Введіть повну суму акту до оплати (тільки цифри, наприклад: 12500):")


@dp.message(ActCreateForm.amount)
async def act_got_amount(m: types.Message, state: FSMContext) -> None:
    try:
        amount = float(m.text.strip().replace(",", "."))
    except ValueError:
        return await m.answer("❌ Будь ласка, введіть числове значення суми.")
    
    await state.update_data(amount=amount)
    await state.set_state(ActCreateForm.descr)
    await m.answer("📝 Введіть опис робіт за актом:")


@dp.message(ActCreateForm.descr)
async def act_got_descr(m: types.Message, state: FSMContext) -> None:
    await state.update_data(descr=m.text.strip())
    await state.set_state(ActCreateForm.file)
    
    kb = InlineKeyboardMarkup(
        inline_keyword=[[InlineKeyboardButton(text="💾 Зберегти без фото", callback_data="act_save_nofile")]]
    )
    await m.answer("📷 Надішліть фото акту (або натисніть кнопку нижче, щоб зберегти без фото):", reply_markup=kb)


@dp.callback_query(F.data == "act_save_nofile", ActCreateForm.file)
async def act_save_without_photo(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await db_execute(
        "INSERT INTO acts (number, osbb, descr, file_id, status, amount, paid_amount, viewer_keyword, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, date('now'))",
        (data["number"], data["osbb"], data["descr"], "NO_FILE", "Не отримано", data["amount"], 0.0, data["viewer_keyword"])
    )
    await state.clear()
    await cb.message.edit_text("✅ Акт успішно створено (без фото). Очікує прийняття бухгалтером.")
    await cb.answer()


@dp.message(ActCreateForm.file, F.photo)
async def act_save_with_photo(m: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = m.photo[-1].file_id
    await db_execute(
        "INSERT INTO acts (number, osbb, descr, file_id, status, amount, paid_amount, viewer_keyword, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, date('now'))",
        (data["number"], data["osbb"], data["descr"], file_id, "Не отримано", data["amount"], 0.0, data["viewer_keyword"])
    )
    await state.clear()
    await m.answer("✅ Акт успішно створено з фото! Очікує прийняття бухгалтером.")


@dp.callback_query(F.data == "act_list_current")
async def act_list_current(cb: CallbackQuery) -> None:
    acts = await db_fetch_all("SELECT id, number, osbb, status, amount, paid_amount FROM acts WHERE status != 'Завершено!'")
    if not acts:
        return await cb.message.edit_text("📭 Поточних актів немає.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="ws_acts")]]))
    
    text = "📋 <b>Поточні акти в системі:</b>\n\n"
    rows = []
    for a in acts:
        text += f"• №{a['number']} ({a['osbb']}) | Статус: <b>{a['status']}</b> | Сума: {a['amount']} грн (Оплачено: {a['paid_amount']} грн)\n"
        rows.append([InlineKeyboardButton(text=f"Переглянути акт №{a['number']}", callback_data=f"act_view_{a['id']}")])
    
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="ws_acts")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


# --- ІНШІ БАЗОВІ КНОПКИ МЕНЮ ---
@dp.callback_query(F.data == "menu_dashboard")
async def cb_dashboard(cb: CallbackQuery) -> None:
    if not user_allowed_osbbs(cb.from_user.id) and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    text = (
        "📊 <b>Зведений Дашборд ОСББ/ОК</b>\n\n"
        "• <b>ВП-16:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>ВЮ3:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>Е21:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>ОКПТ:</b> Активних актів: 0 | Чеки: 0\n"
    )
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_residents")
async def cb_residents(cb: CallbackQuery) -> None:
    if not user_allowed_osbbs(cb.from_user.id) and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await cb.message.answer("🏘️ Модуль «Мешканці та будинки» в процесі розробки.", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_documents")
async def cb_documents(cb: CallbackQuery) -> None:
    await cb.message.answer("📝 Конструктор документів в процесі розробки.", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_superadmin")
async def superadmin_panel(cb: CallbackQuery) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await cb.message.answer("🔐 <b>Панель Superadmin</b> активна.", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "main_menu_back")
async def cb_back_to_menu(cb: CallbackQuery) -> None:
    user_id = cb.from_user.id
    if is_chairman(user_id):
        await cb.message.answer("Головне меню:", reply_markup=chairman_menu())
    elif user_allowed_osbbs(user_id):
        await cb.message.answer("Головне меню:", reply_markup=admin_menu())
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


async def main() -> None:
    await db_init()
    logger.info("Bot started successfully with full act cycle.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
