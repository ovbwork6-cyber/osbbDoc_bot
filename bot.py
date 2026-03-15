import os
import sqlite3
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, 
                            CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, FSInputFile)

# --- НАЛАШТУВАННЯ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
CHAIRMAN_ID = int(os.getenv("CHAIRMAN_ID"))

ACC1_ID = 5178201242
ACC2_ID = 1332732213

ACCESS_MAP = {
    ACC1_ID: ["ВП-16", "Е21"],
    ACC2_ID: ["ОКПТ", "В19"]
}
ACCOUNTANTS = [ACC1_ID, ACC2_ID]

STAFF_CONFIG = {
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання": 12000, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000},
    "В19": {
        "Голова": 6000, 
        "Сантехнік": 2500, 
        "Бухгалтер": 2500, 
        "Бухгалтер (ФОП)": 500
    }
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СТАНИ FSM ---
class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

class SalaryEdit(StatesGroup):
    waiting_for_amount = State()
    waiting_for_comment = State()

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS salaries 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS salary_history 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, salary_id INTEGER, old_amount REAL, new_amount REAL, comment TEXT, date TEXT)''')
    conn.commit()
    conn.close()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def get_seasonal_salary():
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- КЛАВІАТУРИ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки")]]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

def get_acts_menu(uid):
    btns = [[KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")], [KeyboardButton(text="➡️ Перейти до Чеки")]]
    if uid == CHAIRMAN_ID: btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

def get_docs_menu(uid):
    btns = [[KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")], [KeyboardButton(text="➡️ Перейти до Акти")]]
    if uid == CHAIRMAN_ID: btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

# --- ОБРОБНИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Система ОСББ готова до роботи.", reply_markup=get_main_menu(message.from_user.id))

@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu(message.from_user.id))

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ (PDF)", reply_markup=get_docs_menu(message.from_user.id))

@dp.message(F.text == "💰 Зарплати", F.from_user.id == CHAIRMAN_ID)
async def salary_selection(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")],
        [InlineKeyboardButton(text="🏢 В19", callback_data="sal_view_В19")]
    ])
    await message.answer("Оберіть ОСББ для контролю виплат:", reply_markup=kb)

# --- ЛОГІКА АКТІВ ---
@dp.message(Command("new_act"), F.from_user.id == CHAIRMAN_ID)
async def start_new_act(message: types.Message, state: FSMContext):
    await message.answer("📝 Введіть номер акту:")
    await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def process_num(message: types.Message, state: FSMContext):
    await state.update_data(number=message.text)
    await message.answer("🏢 Вкажіть ОСББ (ВП-16, Е21, ОКПТ, В19):")
    await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def process_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip())
    await message.answer("📋 Опис робіт:")
    await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def process_descr(message: types.Message, state: FSMContext):
    await state.update_data(descr=message.text)
    await message.answer("📎 Надішліть ФОТО акту:")
    await state.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo | F.document)
async def process_act_file(message: types.Message, state: FSMContext):
    f_id = message.photo[-1].file_id if message.photo else message.document.file_id
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?, ?, ?, ?, ?)",
              (data['number'], data['osbb'], data['descr'], f_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit(); conn.close()
    await message.answer(f"✅ Акт №{data['number']} зареєстровано.")
    await state.clear()
    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")]])
            caption = f"🔔 <b>Новий Акт №{data['number']}</b>\n🏢 ОСББ: {data['osbb']}"
            await bot.send_photo(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів"]))
async def show_acts(message: types.Message):
    is_archive = "Архів" in message.text
    # Гнучкий фільтр статусів для Архіву
    status_filter = "status LIKE 'Завершено%'" if is_archive else "status NOT LIKE 'Завершено%'"
    uid = message.from_user.id
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()
    if not rows: return await message.answer("📭 Порожньо.")

    for db_id, num, osbb, status, desc, f_id in rows:
        text = f"📄 <b>Акт №{num}</b> ({osbb})\n⏳ Статус: <b>{status}</b>"
        btns = []
        if not is_archive:
            if uid in ACCOUNTANTS:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")])
                elif status == "В роботі": btns.append([InlineKeyboardButton(text="💰 Оплачено", callback_data=f"act_paid_{db_id}")])
            if uid == CHAIRMAN_ID:
                if status == "Акт оплачений": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"act_fin_{db_id}")])
        await bot.send_photo(message.chat.id, f_id, caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None, parse_mode="HTML")

# --- КОЛБЕКИ ---
@dp.callback_query(F.data.startswith(("act_acc_", "act_paid_", "act_fin_", "sal_view_")))
async def global_callbacks(callback: CallbackQuery):
    action, _, db_id = callback.data.rpartition("_")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if action == "act_acc":
        c.execute("UPDATE acts SET status='В роботі' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "🔔 Бухгалтер прийняв Акт у роботу.")
    elif action == "act_paid":
        c.execute("UPDATE acts SET status='Акт оплачений' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "💰 Акт ОПЛАЧЕНО.")
    elif action == "act_fin":
        c.execute("UPDATE acts SET status='Завершено!' WHERE id=?", (db_id,))
    
    conn.commit(); conn.close()
    await callback.answer("Виконано")
    if "act_" in callback.data: await callback.message.delete()

# --- ЗАПУСК ---
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
