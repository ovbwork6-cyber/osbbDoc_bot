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
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?, ?, ?, ?, ?)",
              (data['number'], data['osbb'], data['descr'], f_id, "Не отримано"))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Акт №{data['number']} зареєстровано.")
    await state.clear()

# --- ВІДОБРАЖЕННЯ АКТІВ ---
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів"]))
async def show_acts(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено!'" if is_archive else "status != 'Завершено!'"
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
        text = f"📄 <b>Акт №{num}</b> ({osbb})\n📝 {desc}\n⏳ Статус: <b>{status}</b>"
        btns = []
        
        if uid in ACCOUNTANTS and not is_archive:
            if status == "Не отримано":
                btns.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"act_acc_{db_id}")])
            elif status == "В роботі":
                btns.append([InlineKeyboardButton(text="💰 Оплачено", callback_data=f"act_paid_{db_id}")])
        
        if uid == CHAIRMAN_ID and not is_archive:
            if status == "Не отримано":
                btns.append([InlineKeyboardButton(text="🗑 Видалити акт", callback_data=f"act_del_{db_id}")])
            elif status == "Акт оплачений":
                btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"act_fin_{db_id}")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        try:
            await bot.send_photo(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")
        except:
            await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")

# --- ОБРОБНИКИ АКТІВ ---
@dp.callback_query(F.data.startswith("act_acc_"))
async def act_acc(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE acts SET status='В роботі' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Акт в роботі"); await callback.message.delete()

@dp.callback_query(F.data.startswith("act_paid_"))
async def act_paid_cb(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE acts SET status='Акт оплачений' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Оплачено"); await callback.message.delete()

@dp.callback_query(F.data.startswith("act_fin_"))
async def act_fin(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE acts SET status='Завершено!' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("В архіві"); await callback.message.delete()

@dp.callback_query(F.data.startswith("act_del_"))
async def act_del_cb(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("DELETE FROM acts WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Видалено"); await callback.message.delete()

# --- ЧЕКИ ОСББ ---
@dp.message(F.text == "📁 Чеки ОСББ")
async def show_docs(message: types.Message):
    uid = message.from_user.id
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute("SELECT id, name, osbb, status, file_id FROM docs WHERE status != 'Завершено!' ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, name, osbb, status, file_id FROM docs WHERE status != 'Завершено!' AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()

    if uid == CHAIRMAN_ID:
        kb_add = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати pdf-файл", callback_data="add_doc")]])
        await message.answer("Керування чеками:", reply_markup=kb_add)

    if not rows: return await message.answer("📭 Нових чеків немає.")

    for d_id, name, osbb, status, f_id in rows:
        text = f"🧾 <b>{name}</b> ({osbb})\n⏳ Статус: <b>{status}</b>"
        btns = []
        if uid in ACCOUNTANTS:
            if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"ch_acc_{d_id}")])
            elif status == "В роботі": btns.append([InlineKeyboardButton(text="⚙️ Опрацьовано", callback_data=f"ch_done_{d_id}")])
        if uid == CHAIRMAN_ID:
            if status == "Не отримано": btns.append([InlineKeyboardButton(text="🗑 Видалити pdf-файл", callback_data=f"ch_del_{d_id}")])
            elif status == "Роботу завершено": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"ch_fin_{d_id}")])
        
        await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None, parse_mode="HTML")

@dp.callback_query(F.data == "add_doc")
async def doc_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Назва:"); await state.set_state(DocForm.name); await callback.answer()

@dp.message(DocForm.name)
async def d_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text); await message.answer("🏢 ОСББ:"); await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def d_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip()); await message.answer("📎 PDF:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def d_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?, ?, ?, ?)", (data['name'], data['osbb'], message.document.file_id, "Не отримано"))
    conn.commit(); conn.close(); await state.clear(); await message.answer("✅ PDF збережено.")

@dp.callback_query(F.data.startswith("ch_acc_"))
async def ch_acc(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE docs SET status='В роботі' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Прийнято"); await callback.message.delete()

@dp.callback_query(F.data.startswith("ch_done_"))
async def ch_done(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE docs SET status='Роботу завершено' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Опрацьовано"); await callback.message.delete()

@dp.callback_query(F.data.startswith("ch_fin_"))
async def ch_fin(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE docs SET status='Завершено!' WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Завершено"); await callback.message.delete()

@dp.callback_query(F.data.startswith("ch_del_"))
async def ch_del(callback: CallbackQuery):
    db_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("DELETE FROM docs WHERE id=?", (db_id,)); conn.commit(); conn.close()
    await callback.answer("Видалено"); await callback.message.delete()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
