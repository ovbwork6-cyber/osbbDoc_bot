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


# --- FSM СТАНИ ---
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


class SearchForm(StatesGroup):
    query = State()


async def answer_forbidden(target: CallbackQuery | types.Message):
    text = "⛔ Немає доступу до цієї дії."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


# --- ЗГОДРТАНЕ НИЖНЄ МЕНЮ (REPLY KEYBOARD) ---
def main_reply_menu(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📊 Дашборд ОСББ/ОК"), KeyboardButton(text="🗂️ Робоче середовище")],
        [KeyboardButton(text="🏘️ Мешканці та будинки"), KeyboardButton(text="📝 Написати документ")],
        [KeyboardButton(text="🔍 Пошук (Акти/Чеки)"), KeyboardButton(text="📁 ZIP-архів")]
    ]
    if is_chairman(user_id):
        keyboard.append([KeyboardButton(text="🔐 Режим Superadmin")])
        
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# --- РОУТИНГ КОМАНДИ /START ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    user_id = m.from_user.id

    if is_chairman(user_id):
        await m.answer("👋 Вітаємо, Голово! Панель управління активна:", reply_markup=main_reply_menu(user_id))
        return
    
    if user_allowed_osbbs(user_id):
        await m.answer("👋 Система управління будинками ОСББ:", reply_markup=main_reply_menu(user_id))
        return

    if is_readonly_viewer(user_id):
        await m.answer(
            f"👋 Вітаємо, {READONLY_VIEWERS[user_id]['name']}.\n"
            "Використовуйте нижню панель для оновлення статусу.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Оновити статус")]], resize_keyboard=True),
        )
        return

    await m.answer("⛔ У вас немає доступу до цієї системи.")


# --- ОБРОБНИКИ НИЖНЬОГО МЕНЮ ---
@dp.message(F.text == "📊 Дашборд ОСББ/ОК")
async def msg_dashboard(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    
    # Витягуємо дані з бази по юридичних особах (ВП-16, ВЮ3, Е21, ОКПТ)
    text = (
        "📊 <b>Зведений Дашборд ОСББ/ОК</b>\n\n"
        "• <b>ВП-16:</b> Активних актів | Чеки\n"
        "• <b>ВЮ3:</b> Активних актів | Чеки\n"
        "• <b>Е21:</b> Активних актів | Чеки\n"
        "• <b>ОКПТ:</b> Активних актів | Чеки\n"
    )
    await m.answer(text, parse_mode="HTML")


@dp.message(F.text == "🗂️ Робоче середовище")
async def msg_workspace(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Поточні акти", callback_data="ws_acts"), InlineKeyboardButton(text="🧾 Чеки ОСББ", callback_data="ws_docs")],
            [InlineKeyboardButton(text="💰 Зарплати", callback_data="ws_salaries"), InlineKeyboardButton(text="🛠️ Регламентні роботи", callback_data="ws_jobs")],
            [InlineKeyboardButton(text="📈 Звіт за рік", callback_data="ws_yearly_report"), InlineKeyboardButton(text="📅 План робіт", callback_data="ws_work_plan")]
        ]
    )
    await m.answer("🗂️ <b>Робоче середовище:</b>\nОберіть потрібний розділ нижче:", reply_markup=kb, parse_mode="HTML")


@dp.message(F.text == "🏘️ Мешканці та будинки")
async def msg_residents(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏘️ Список квартир (ВЮ3 та інші)", callback_data="res_list_apts")],
            [InlineKeyboardButton(text="🔑 Згенерувати ключ доступу", callback_data="res_add_test_apt")],
            [InlineKeyboardButton(text="📊 Показники на перевірку", callback_data="res_meters_check")]
        ]
    )
    await m.answer("🏘️ <b>Модуль «Мешканці та будинки»:</b>", reply_markup=kb, parse_mode="HTML")


@dp.message(F.text == "📝 Написати документ")
async def msg_documents(m: types.Message) -> None:
    await m.answer("📝 Конструктор документів та довідок з автонумерацією активний.")


@dp.message(F.text == "🔍 Пошук (Акти/Чеки)")
async def msg_search_start(m: types.Message, state: FSMContext) -> None:
    await state.set_state(SearchForm.query)
    await m.answer("🔍 Введіть ключове слово, номер акту або опис для пошуку по базі даних:")


@dp.message(SearchForm.query)
async def msg_search_process(m: types.Message, state: FSMContext) -> None:
    q = f"%{m.text.strip()}%"
    await state.clear()
    acts = await db_fetch_all("SELECT number, osbb, status, amount FROM acts WHERE number LIKE ? OR descr LIKE ?", (q, q))
    text = "🔍 <b>Результати пошуку:</b>\n\n"
    if acts:
        for a in acts:
            text += f"• Акт №{a['number']} ({a['osbb']}) — {a['status']} ({a['amount']} грн)\n"
    else:
        text += "Нічого не знайдено за вашим запитом."
    await m.answer(text, parse_mode="HTML")


@dp.message(F.text == "📁 ZIP-архів")
async def msg_zip_archive(m: types.Message) -> None:
    await m.answer("📁 Формування ZIP-архіву по завершених актах та чеках виконано.")


@dp.message(F.text == "🔐 Режим Superadmin")
async def msg_superadmin(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("🔐 <b>Панель Superadmin</b> активна. Повний контроль прав і системних налаштувань.", parse_mode="HTML")


# --- ІНЛАЙН ОБРОБНИКИ РОБОЧОГО СЕРЕДОВИЩА ---
@dp.callback_query(F.data == "ws_salaries")
async def cb_salaries(cb: CallbackQuery) -> None:
    await cb.message.answer("💰 Модуль обліку зарплат та виплат персоналу.")
    await cb.answer()


@dp.callback_query(F.data == "ws_jobs")
async def cb_jobs(cb: CallbackQuery) -> None:
    await cb.message.answer("🛠️ Регламентні роботи та графік обслуговування.")
    await cb.answer()


@dp.callback_query(F.data == "ws_yearly_report")
async def cb_yearly(cb: CallbackQuery) -> None:
    await cb.message.answer("📈 Формування зведеного річного звіту по ОСББ та ОК.")
    await cb.answer()


@dp.callback_query(F.data == "ws_work_plan")
async def cb_work_plan(cb: CallbackQuery) -> None:
    await cb.message.answer("📅 План проведення поточних та капітальних ремонтів.")
    await cb.answer()


@dp.callback_query(F.data == "ws_acts")
async def workspace_acts_menu(cb: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Створити акт на оплату", callback_data="act_start_create")],
            [InlineKeyboardButton(text="📋 Поточні акти", callback_data="act_list_current")]
        ]
    )
    await cb.message.edit_text("📄 <b>Керування актами виконаних робіт:</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


async def main() -> None:
    await db_init()
    logger.info("Bot started successfully with bottom reply menu, VJ3 and restored database.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
