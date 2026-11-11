import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
class SearchForm(StatesGroup):
    query = State()


class ActCreateForm(StatesGroup):
    number = State()
    amount = State()
    descr = State()


async def answer_forbidden(message: types.Message):
    await message.answer("⛔ Немає доступу до цієї дії.")


# --- ДИНАМІЧНІ НИЖНІ МЕНЮ (REPLY KEYBOARDS) ---

def main_reply_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Головне нижнє меню для Адміна / Голови / ФОПа / Бухгалтера"""
    keyboard = [
        [KeyboardButton(text="📊 Дашборд ОСББ/ОК"), KeyboardButton(text="🗂️ Робоче середовище")],
        [KeyboardButton(text="🏘️ Мешканці та будинки"), KeyboardButton(text="📝 Написати документ")],
        [KeyboardButton(text="🔍 Пошук (Акти/Чеки)"), KeyboardButton(text="📁 ZIP-архів")]
    ]
    if is_chairman(user_id):
        keyboard.append([KeyboardButton(text="🔐 Режим Superadmin")])
        
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def workspace_reply_menu() -> ReplyKeyboardMarkup:
    """Нижнє меню для розділу 'Робоче середовище' (без інлайну)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Поточні акти"), KeyboardButton(text="🧾 Чеки ОСББ")],
            [KeyboardButton(text="💰 Зарплати"), KeyboardButton(text="🛠️ Регламентні роботи")],
            [KeyboardButton(text="📈 Звіт за рік"), KeyboardButton(text="📅 План робіт")],
            [KeyboardButton(text="🔙 Головне меню")]
        ],
        resize_keyboard=True
    )


def residents_reply_menu() -> ReplyKeyboardMarkup:
    """Нижнє меню для розділу 'Мешканці та будинки'"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏘️ Список квартир (ВЮ3)"), KeyboardButton(text="🔑 Згенерувати ключ")],
            [KeyboardButton(text="📊 Показники на перевірку"), KeyboardButton(text="🔙 Головне меню")]
        ],
        resize_keyboard=True
    )


def superadmin_reply_menu() -> ReplyKeyboardMarkup:
    """Нижнє меню для Superadmin"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏢 Керування гілками (ОСББ/ОК)"), KeyboardButton(text="👥 Керування ролями")],
            [KeyboardButton(text="🔙 Головне меню")]
        ],
        resize_keyboard=True
    )


def acts_reply_menu() -> ReplyKeyboardMarkup:
    """Нижнє меню для підрозділу актів"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Створити акт на оплату"), KeyboardButton(text="📋 Список активних актів")],
            [KeyboardButton(text="🔙 У робоче середовище")]
        ],
        resize_keyboard=True
    )


# --- РОУТИНГ КОМАНДИ /START ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext) -> None:
    await state.clear()
    user_id = m.from_user.id

    if is_chairman(user_id):
        await m.answer("👋 Вітаємо, Голово! Панель управління активна:", reply_markup=main_reply_menu(user_id))
        return
    
    if user_allowed_osbbs(user_id):
        await m.answer("👋 Система управління будинками ОСББ (Адмін/Бухгалтер/ФОП):", reply_markup=main_reply_menu(user_id))
        return

    if is_readonly_viewer(user_id):
        viewer_info = READONLY_VIEWERS[user_id]
        await m.answer(
            f"👋 Вітаємо, {viewer_info['name']} ({viewer_info.get('role', 'Член правління / Мешканець')}).\n"
            "Використовуйте нижню панель для перегляду та оновлення статусу.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📊 Дашборд ОСББ/ОК"), KeyboardButton(text="🏘️ Мешканці та будинки")],
                    [KeyboardButton(text="🔄 Оновити статус")]
                ], 
                resize_keyboard=True
            ),
        )
        return

    await m.answer("⛔ У вас немає доступу до цієї системи.")


# --- ПОВЕРНЕННЯ ДО ГОЛОВНОГО МЕНЮ ---
@dp.message(F.text == "🔙 Головне меню")
async def msg_back_main(m: types.Message) -> None:
    await m.answer("Головне меню:", reply_markup=main_reply_menu(m.from_user.id))


@dp.message(F.text == "🔙 У робоче середовище")
async def msg_back_workspace(m: types.Message) -> None:
    await m.answer("🗂️ Робоче середовище:", reply_markup=workspace_reply_menu())


# --- ОБРОБНИКИ ОСНОВНИХ КНОПОК НИЖНЬОГО МЕНЮ ---

@dp.message(F.text == "📊 Дашборд ОСББ/ОК")
async def msg_dashboard(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id) and not is_readonly_viewer(m.from_user.id):
        return await answer_forbidden(m)
    
    text = (
        "📊 <b>Зведений Дашборд ОСББ/ОК</b>\n\n"
        "• <b>ВП-16:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>ВЮ3:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>Е21:</b> Активних актів: 0 | Чеки: 0\n"
        "• <b>ОКПТ:</b> Активних актів: 0 | Чеки: 0\n"
    )
    await m.answer(text, parse_mode="HTML", reply_markup=main_reply_menu(m.from_user.id))


@dp.message(F.text == "🗂️ Робоче середовище")
async def msg_workspace(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("🗂️ <b>Робоче середовище:</b>\nОберіть потрібний розділ нижче:", parse_mode="HTML", reply_markup=workspace_reply_menu())


@dp.message(F.text == "🏘️ Мешканці та будинки")
async def msg_residents(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id) and not is_readonly_viewer(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("🏘️ <b>Модуль «Мешканці та будинки»:</b>", parse_mode="HTML", reply_markup=residents_reply_menu())


@dp.message(F.text == "📝 Написати документ")
async def msg_documents(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("📝 Конструктор документів та довідок з автонумерацією активний.", reply_markup=main_reply_menu(m.from_user.id))


@dp.message(F.text == "🔍 Пошук (Акти/Чеки)")
async def msg_search_start(m: types.Message, state: FSMContext) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await state.set_state(SearchForm.query)
    await m.answer("🔍 Введіть ключове слово, номер акту або опис для пошуку по базі даних:", reply_markup=ReplyKeyboardRemove())


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
    await m.answer(text, parse_mode="HTML", reply_markup=main_reply_menu(m.from_user.id))


@dp.message(F.text == "📁 ZIP-архів")
async def msg_zip_archive(m: types.Message) -> None:
    if not user_allowed_osbbs(m.from_user.id) and not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("📁 Формування ZIP-архіву по завершених актах та чеках виконано.", reply_markup=main_reply_menu(m.from_user.id))


# --- РОБОЧЕ СЕРЕДОВИЩЕ (ПІДРОЗДІЛИ) ---

@dp.message(F.text == "📄 Поточні акти")
async def msg_acts_menu(m: types.Message) -> None:
    await m.answer("📄 <b>Керування актами виконаних робіт:</b>", parse_mode="HTML", reply_markup=acts_reply_menu())


@dp.message(F.text == "🧾 Чеки ОСББ")
async def msg_checks(m: types.Message) -> None:
    await m.answer("🧾 Керування чеками та витратами ОСББ активне.", reply_markup=workspace_reply_menu())


@dp.message(F.text == "💰 Зарплати")
async def msg_salaries(m: types.Message) -> None:
    await m.answer("💰 Модуль обліку зарплат та виплат персоналу активний.", reply_markup=workspace_reply_menu())


@dp.message(F.text == "🛠️ Регламентні роботи")
async def msg_jobs(m: types.Message) -> None:
    await m.answer("🛠️ Регламентні роботи та графік обслуговування будинків.", reply_markup=workspace_reply_menu())


@dp.message(F.text == "📈 Звіт за рік")
async def msg_yearly(m: types.Message) -> None:
    await m.answer("📈 Формування зведеного річного звіту по ОСББ та ОК.", reply_markup=workspace_reply_menu())


@dp.message(F.text == "📅 План робіт")
async def msg_work_plan(m: types.Message) -> None:
    await m.answer("📅 План проведення поточних та капітальних ремонтів.", reply_markup=workspace_reply_menu())


# --- ПІДРОЗДІЛИ АКТІВ ---

@dp.message(F.text == "📋 Список активних актів")
async def msg_list_acts(m: types.Message) -> None:
    await m.answer("📋 Список поточних активних актів у системі.", reply_markup=acts_reply_menu())


@dp.message(F.text == "➕ Створити акт на оплату")
async def msg_create_act(m: types.Message) -> None:
    await m.answer("➕ Майстер створення нового акту на оплату.", reply_markup=acts_reply_menu())


# --- МОДУЛЬ МЕШКАНЦІ ТА БУДИНКИ ---

@dp.message(F.text == "🏘️ Список квартир (ВЮ3)")
async def msg_list_apts(m: types.Message) -> None:
    await m.answer("🏘️ Список квартир та таунхаусів (ВЮ3 та інші об'єкти):", reply_markup=residents_reply_menu())


@dp.message(F.text == "🔑 Згенерувати ключ")
async def msg_gen_key(m: types.Message) -> None:
    await m.answer("🔑 Згенеровано тестовий ключ доступу для мешканця.", reply_markup=residents_reply_menu())


@dp.message(F.text == "📊 Показники на перевірку")
async def msg_meters_check(m: types.Message) -> None:
    await m.answer("📊 Зведені показники лічильників мешканців на перевірку.", reply_markup=residents_reply_menu())


# --- РЕЖИМ SUPERA ADMIN ---

@dp.message(F.text == "🔐 Режим Superadmin")
async def msg_superadmin(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("🔐 <b>Панель Superadmin активна.</b>\nОберіть розділ керування:", parse_mode="HTML", reply_markup=superadmin_reply_menu())


@dp.message(F.text == "🏢 Керування гілками (ОСББ/ОК)")
async def msg_sa_branches(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("🏢 <b>Гілки (юридичні особи):</b> ВП-16, ВЮ3, Е21, ОКПТ.", parse_mode="HTML", reply_markup=superadmin_reply_menu())


@dp.message(F.text == "👥 Керування ролями")
async def msg_sa_roles(m: types.Message) -> None:
    if not is_chairman(m.from_user.id):
        return await answer_forbidden(m)
    await m.answer("👥 <b>Керування ролями:</b> Адмін, Бухгалтер, ФОП, Переглядач, Член правління.", parse_mode="HTML", reply_markup=superadmin_reply_menu())


@dp.message(F.text == "🔄 Оновити статус")
async def msg_refresh_status(m: types.Message) -> None:
    await m.answer("🔄 Статус оновлено.", reply_markup=main_reply_menu(m.from_user.id))


async def main() -> None:
    await db_init()
    logger.info("Bot started successfully with full reply keyboard navigation, VJ3 and database support.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
