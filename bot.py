import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
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
from db_layer import db_init, db_fetch_all, db_execute
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


async def answer_forbidden(target: CallbackQuery | types.Message):
    text = "⛔ Немає доступу до цієї дії."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


# --- КЛАВІАТУРИ ТА МЕНЮ ---
def chairman_menu() -> InlineKeyboardMarkup:
    """Головне меню для вас (Суперадмін + повний функціонал)."""
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
    """Головне меню для звичайних адміністраторів / голів ОСББ."""
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
    """Мінімалістичне меню для переглядача (Viewer)."""
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

    # Якщо користувач не має жодних прав
    await m.answer("⛔ У вас немає доступу до цієї системи.")


# --- ОБРОБНИКИ ОСНОВНИХ КНОПОК МЕНЮ ---
@dp.callback_query(F.data == "menu_dashboard")
async def cb_dashboard(cb: CallbackQuery) -> None:
    if not user_allowed_osbbs(cb.from_user.id) and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    
    text = (
        "📊 <b>Зведений Дашборд ОСББ/ОК</b>\n\n"
        "• <b>ВП-16:</b> Активних актів: 0 | Чеки: 0 | Лічильники на перевірці: 0\n"
        "• <b>ВЮ3:</b> Активних актів: 0 | Чеки: 0 | Лічильники на перевірці: 0\n"
        "• <b>Е21:</b> Активних актів: 0 | Чеки: 0 | Лічильники на перевірці: 0\n"
        "• <b>ОКПТ:</b> Активних актів: 0 | Чеки: 0 | Лічильники на перевірці: 0\n"
    )
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


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
    await cb.message.answer("🗂️ <b>Робоче середовище:</b>\nОберіть потрібний розділ нижче:", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_residents")
async def cb_residents(cb: CallbackQuery) -> None:
    if not user_allowed_osbbs(cb.from_user.id) and not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏘️ Квартири та будинки", callback_data="res_apts")],
            [InlineKeyboardButton(text="📊 Показники на перевірку", callback_data="res_meters")],
            [InlineKeyboardButton(text="🔑 Ключі доступу до кабінетів", callback_data="res_keys")],
            [InlineKeyboardButton(text="🔙 Назад у меню", callback_data="main_menu_back")]
        ]
    )
    await cb.message.answer("🏘️ <b>Модуль «Мешканці та будинки»:</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_documents")
async def cb_documents(cb: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Створити заяву / акт", callback_data="doc_create")],
            [InlineKeyboardButton(text="⚙️ Визначити формат вихідного номера", callback_data="doc_format")],
            [InlineKeyboardButton(text="🔙 Назад у меню", callback_data="main_menu_back")]
        ]
    )
    await cb.message.answer("📝 <b>Конструктор документів / Написати документ:</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_superadmin")
async def superadmin_panel(cb: CallbackQuery) -> None:
    if not is_chairman(cb.from_user.id):
        return await answer_forbidden(cb)
    await cb.message.answer("🔐 <b>Панель Superadmin</b>\nГлобальні налаштування системи та резервні копії активні.", parse_mode="HTML")
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


# --- ЗАПУСК БОТА ---
async def main() -> None:
    await db_init()
    logger.info("Bot started successfully.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
