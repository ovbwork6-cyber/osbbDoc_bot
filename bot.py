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

# Доступ для бухгалтерів
ACCESS_MAP = {
    5178201242: ["ВП-16", "Е21"],
    1332732213: ["ОКПТ", "В19"]
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- КЛАСИ СТАНІВ ---
class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    file = State()

class DocForm(StatesGroup):
    name = State()
    osbb = State()
    file = State()

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    # Таблиця актів (з описом)
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT DEFAULT "Очікує")''')
    # Таблиця чеків
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT DEFAULT "Очікує")''')
    conn.commit()
    conn.close()

# --- КЛАВІАТУРИ ---
def get_acts_menu(uid):
    btns = [
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
        [KeyboardButton(text="➕ Створити Акт")]
    ]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="➡️ Перейти до Чеки")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

def get_docs_menu(uid):
    btns = [
        [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
        [KeyboardButton(text="➕ Додати PDF чек")]
    ]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="➡️ Перейти до Акти")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

# --- ОБРОБНИКИ МЕНЮ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Вітаю! Оберіть розділ:", reply_markup=get_acts_menu(message.from_user.id))

@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu(message.from_user.id))

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ", reply_markup=get_docs_menu(message.from_user.id))

# --- СТВОРЕННЯ АКТУ (ФОТО) ---
@dp.message(F.text == "➕ Створити Акт")
async def start_act(message: types.Message, state: FSMContext):
    await message.answer("📝 Введіть номер акту:")
    await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def act_num(message: types.Message, state: FSMContext):
    await state.update_data(number=message.text)
    await message.answer("🏢 Введіть назву ОСББ (напр. ВП-16):")
    await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def act_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.upper())
    await message.answer("🗒 Введіть опис робіт:")
    await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def act_descr(message: types.Message, state: FSMContext):
    await state.update_data(descr=message.text)
    await message.answer("📸 Надішліть ФОТО акту:")
    await state.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo)
async def act_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?,?,?,?,?)",
              (data['number'], data['osbb'], data['descr'], file_id, "Очікує"))
    conn.commit(); conn.close()
    await state.clear()
    await message.answer("✅ Акт успішно створено!", reply_markup=get_acts_menu(message.from_user.id))

# --- ВІДОБРАЖЕННЯ (АКТИ ТА ЧЕКИ) ---
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(message: types.Message):
    is_archive = "Архів" in message.text
    is_acts = "акт" in message.text.lower()
    table = "acts" if is_acts else "docs"
    status_filter = "status = 'Завершено'" if is_archive else "status != 'Завершено'"
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if message.from_user.id == CHAIRMAN_ID:
        c.execute(f"SELECT * FROM {table} WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(message.from_user.id, [])
        c.execute(f"SELECT * FROM {table} WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall(); conn.close()
    if not rows: return await message.answer("📭 Порожньо.")

    for r in rows:
        if is_acts:
            # r[1]:номер, r[2]:осбб, r[3]:опис, r[4]:file_id, r[5]:status
            caption = f"📄 Акт №{r[1]} ({r[2]})\n📝 {r[3]}\n⏳ Статус: {r[5]}"
            f_id = r[4]
            try:
                await bot.send_photo(message.chat.id, f_id, caption=caption)
            except:
                await message.answer(f"⚠️ Фото не знайдено:\n{caption}")
        else:
            # r[1]:назва, r[2]:осбб, r[3]:file_id, r[4]:status
            caption = f"🧾 Чек: {r[1]} ({r[2]})\n⏳ Статус: {r[4]}"
            f_id = r[3]
            try:
                await bot.send_document(message.chat.id, f_id, caption=caption)
            except:
                await message.answer(f"⚠️ PDF не знайдено:\n{caption}")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
