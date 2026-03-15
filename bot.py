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
                            CallbackQuery, ReplyKeyboardMarkup, KeyboardButton)

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

STAFF_CONFIG = {
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання": 12000, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000},
    "В19": {"Голова": 6000, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500}
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- КЛАСИ СТАНІВ (ВИПРАВЛЕНО СИНТАКСИС) ---
class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    file = State()

class DocForm(StatesGroup):
    name = State()
    osbb = State()
    file = State()

class SalaryEdit(StatesGroup):
    waiting_for_amount = State()
    waiting_for_comment = State()

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db'); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS acts (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT DEFAULT "Очікує")')
    cursor.execute('CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT DEFAULT "Очікує")')
    cursor.execute('CREATE TABLE IF NOT EXISTS salaries (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT DEFAULT "Очікує")')
    cursor.execute('CREATE TABLE IF NOT EXISTS salary_history (id INTEGER PRIMARY KEY AUTOINCREMENT, salary_id INTEGER, old_amount REAL, new_amount REAL, comment TEXT, date TEXT)')
    conn.commit(); conn.close()

def get_seasonal_salary():
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- КЛАВІАТУРИ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки")]]
    if uid == CHAIRMAN_ID: btns.append([KeyboardButton(text="💰 Зарплати")])
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
    await message.answer("👋 Система ОСББ готова.", reply_markup=get_main_menu(message.from_user.id))

@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu(message.from_user.id))

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ (PDF)", reply_markup=get_docs_menu(message.from_user.id))

# --- ЗАРПЛАТИ ---
@dp.message(F.text == "💰 Зарплати", F.from_user.id == CHAIRMAN_ID)
async def salary_selection(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")],
        [InlineKeyboardButton(text="🏢 В19", callback_data="sal_view_В19")]
    ])
    await message.answer("Оберіть ОСББ:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_view_"))
async def view_salaries(callback: CallbackQuery):
    osbb = callback.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    rows = c.fetchall()
    if not rows:
        if datetime.now().day >= 15:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати", callback_data=f"sal_gen_{osbb}")]])
            await callback.message.edit_text(f"Нарахувань за {m_y} ще немає.", reply_markup=kb)
        else:
            await callback.answer("Нарахування доступні з 20-го числа.", show_alert=True)
        return
    
    text = f"💰 <b>{osbb} ({m_y})</b>\n\n"
    kb_list = []
    for s_id, emp, amo, stat in rows:
        text += f"{'✅' if stat == 'Видано' else '⏳'} {emp}: {amo} грн\n"
        if stat != "Видано":
            kb_list.append([InlineKeyboardButton(text=f"💵 {emp}", callback_data=f"sal_pay_{s_id}")])
    kb_list.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

# --- ПЕРЕГЛЯД ЕЛЕМЕНТІВ (ВИПРАВЛЕНО) ---
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(message: types.Message):
    is_archive = "Архів" in message.text
    is_acts = "акт" in message.text.lower()
    table = "acts" if is_acts else "docs"
    
    # Фільтр: в архіві тільки 'Завершено', в поточних - ВСЕ ІНШЕ
    status_filter = "status = 'Завершено'" if is_archive else "status != 'Завершено'"
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if not is_archive and not is_acts and message.from_user.id == CHAIRMAN_ID:
        kb_add = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати pdf-файл", callback_data="add_doc")]])
        await message.answer("Керування чеками:", reply_markup=kb_add)

    if message.from_user.id == CHAIRMAN_ID:
        c.execute(f"SELECT * FROM {table} WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(message.from_user.id, [])
        placeholders = ','.join(['?'] * len(allowed))
        c.execute(f"SELECT * FROM {table} WHERE {status_filter} AND osbb IN ({placeholders}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall(); conn.close()
    if not rows: return await message.answer("📭 Порожньо.")
    
    for r in rows:
        # r[1] - номер/назва, r[2] - ОСББ, r[3] - опис (тільки для актів)
        # r[4] - file_id, r[5] - статус
        
        if is_acts:
            caption = f"📄 {r[1]} ({r[2]})\n📝 {r[3]}\n⏳ Статус: {r[5]}"
            f_id = r[4]
        else:
            caption = f"📄 {r[1]} ({r[2]})\n⏳ Статус: {r[4]}" # для чеків статус в r[4]
            f_id = r[3] # для чеків file_id в r[3]
        
        try:
            if is_acts:
                await bot.send_photo(message.chat.id, f_id, caption=caption)
            else:
                await bot.send_document(message.chat.id, f_id, caption=caption)
        except Exception:
            await message.answer(f"⚠️ Файл №{r[0]} не знайдено:\n{caption}")

@dp.callback_query(F.data == "add_doc")
async def add_doc_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Назва чеку:"); await state.set_state(DocForm.name); await callback.answer()

@dp.message(DocForm.name)
async def doc_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text); await message.answer("🏢 ОСББ:"); await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def doc_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip().upper()); await message.answer("📎 Надішліть PDF:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def doc_file_proc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?, ?, ?, ?)", (data['name'], data['osbb'], message.document.file_id, "Очікує"))
    conn.commit(); conn.close(); await state.clear(); await message.answer("✅ PDF додано.")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
