import os
import sqlite3
import asyncio
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

# ID бухгалтерів та їх зони відповідальності
ACC1_ID = 5178201242
ACC2_ID = 1332732213

ACCESS_MAP = {
    ACC1_ID: ["ВП-16", "Е21"],
    ACC2_ID: ["ОКПТ", "В19"]
}
ACCOUNTANTS = [ACC1_ID, ACC2_ID]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT)''')
    conn.commit()
    conn.close()

# --- КЛАВІАТУРИ ---
def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки")]
    ], resize_keyboard=True)

def get_acts_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
        [KeyboardButton(text="➡️ Перейти до Чеки")]
    ], resize_keyboard=True)

def get_docs_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
        [KeyboardButton(text="➡️ Перейти до Акти")]
    ], resize_keyboard=True)

class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

# --- СТАРТ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Ласкаво просимо до системи керування документами ОСББ.", reply_markup=get_main_menu())

# --- НАВІГАЦІЯ ---
@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu())

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ (PDF)", reply_markup=get_docs_menu())

# --- РЕЄСТРАЦІЯ АКТУ (ГОЛОВА) ---
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
              (data
