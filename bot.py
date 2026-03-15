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
    buttons = [
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів")],
        [KeyboardButton(text="📁 Чеки ОСББ")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == CHAIRMAN_ID or message.from_user.id in ACCOUNTANTS:
        await message.answer("👋 Система ОСББ готова до роботи.", reply_markup=get_main_menu())

# --- ЛОГІКА АКТІВ (ГОЛОВА) ---
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
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?, ?, ?, ?, ?)",
              (data['number'], data['osbb'], data['descr'], f_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit()
    conn.close()

    await message.answer(f"✅ Акт №{data['number']} зареєстровано (Статус: Не отримано)")
    await state.clear()

    # Сповіщення бухгалтера
    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"acc_accept_{db_id}")]])
            caption = f"📄 <b>Новий Акт №{data['number']}</b>\n🏢 ОСББ: {data['osbb']}\n📝 Опис: {data['descr']}\n⏳ Статус: Не отримано"
            try:
                await bot.send_photo(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            except:
                await bot.send_document(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")

# --- ЛОГІКА ЧЕКІВ (ГОЛОВА) ---
@dp.callback_query(F.data == "add_doc", F.from_user.id == CHAIRMAN_ID)
async def start_add_doc(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Назва чеків (напр. Чеки Березень):")
    await state.set_state(DocForm.name)
    await callback.answer()

@dp.message(DocForm.name)
async def process_doc_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("🏢 Вкажіть ОСББ:")
    await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def process_doc_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip())
    await message.answer("📎 Завантажте ПДФ файл з чеками:")
    await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def process_doc_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?, ?, ?, ?)", 
              (data['name'], data['osbb'], message.document.file_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Файл '{data['name']}' збережено.")

    # Повідомлення бухгалтеру про чек
    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти чек", callback_data=f"chk_accept_{db_id}")]])
            await bot.send_document(acc_id, message.document.file_id, 
                                    caption=f"🧾 <b>Нові чеки: {data['name']}</b>\n🏢 {data['osbb']}\n⏳ Статус: Не отримано", 
                                    reply_markup=kb, parse_mode="HTML")

# --- ОБРОБНИКИ КНОПОК БУХГАЛТЕРА (АКТИ) ---
@dp.callback_query(F.data.startswith("acc_accept_"))
async def acc_accept_act(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='В роботі' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Оплачено", callback_data=f"acc_paid_{db_id}")]])
    new_caption = callback.message.caption.replace("⏳ Статус: Не отримано", "⏳ Статус: В роботі")
    await callback.message.edit_caption(caption=new_caption, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Акт прийнято в роботу")

@dp.callback_query(F.data.startswith("acc_paid_"))
async def acc_paid_act(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='Акт оплачений' WHERE id=?", (db_id,))
    c.execute("SELECT number, osbb FROM acts WHERE id=?", (db_id,))
    res = c.fetchone()
    conn.commit()
    conn.close()
    
    await callback.message.edit_caption(caption=callback.message.caption.replace("⏳ Статус: В роботі", "✅ Статус: Акт оплачений"), reply_markup=None)
    
    # Кнопка для Голови
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏁 Завершити", callback_data=f"finish_{db_id}")]])
    await bot.send_message(CHAIRMAN_ID, f"💰 Акт №{res[0]} ({res[1]}) оплачений. Можна закривати:", reply_markup=kb)
    await callback.answer("Статус змінено на: Оплачено")

# --- ОБРОБНИКИ КНОПОК БУХГАЛТЕРА (ЧЕКИ) ---
@dp.callback_query(F.data.startswith("chk_accept_"))
async def chk_accept(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE docs SET status='В роботі' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Опрацьовано", callback_data=f"chk_done_{db_id}")]])
    await callback.message.edit_caption(caption=callback.message.caption.replace("⏳ Статус: Не отримано", "⏳ Статус: В роботі"), reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("chk_done_"))
async def chk_done(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE docs SET status='Роботу завершено' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_caption(caption=callback.message.caption.replace("⏳ Статус: В роботі", "✅ Статус: Роботу завершено"), reply_markup=None)
    await callback.answer("Чеки опрацьовано")

# --- ЗАВЕРШЕННЯ ТА АРХІВ ---
@dp.callback_query(F.data.startswith("finish_"))
async def finish_act(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='Завершено' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("🏁 Акт перенесено в архів.")

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів"]))
async def show_table(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено'" if is_archive else "status != 'Завершено'"
    uid = message.from_user.id
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall()
    conn.close()
    if not rows: return await message.answer("📭 Порожньо.")

    for db_id, num, osbb, status, desc, f_id in rows:
        text = f"🔹 <b>Акт №{num}</b> ({osbb})\n📝 {desc}\n⏳ Статус: <i>{status}</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Файл", callback_data=f"view_{db_id}")]])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

# --- ПЕРЕГЛЯД ЧЕКІВ ---
@dp.message(F.text == "📁 Чеки ОСББ")
async def list_docs(message: types.Message):
    uid = message.from_user.id
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute("SELECT id, name, osbb, status FROM docs ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, name, osbb, status FROM docs WHERE osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall()
    conn.close()

    kb_list = []
    for d_id, name, osbb, status in rows:
        kb_list.append([InlineKeyboardButton(text=f"📥 {name} ({osbb}) - {status}", callback_data=f"getdoc_{d_id}")])
    
    if uid == CHAIRMAN_ID:
        kb_list.append([InlineKeyboardButton(text="➕ Додати чеки (PDF)", callback_data="add_doc")])
    
    await message.answer("🗄 Чеки ОСББ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list))

@dp.callback_query(F.data.startswith("getdoc_"))
async def get_general_doc(callback: CallbackQuery):
    d_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT file_id FROM docs WHERE id=?", (d_id,))
    res = c.fetchone()
    conn.close()
    if res: await callback.message.answer_document(res[0])
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_act_file(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT file_id FROM acts WHERE id=?", (db_id,))
    res = c.fetchone()
    conn.close()
    if res:
        try: await callback.message.answer_photo(res[0])
        except: await callback.message.answer_document(res[0])
    await callback.answer()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
