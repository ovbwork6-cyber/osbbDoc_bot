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

# 1. ЗАВАНТАЖЕННЯ НАЛАШТУВАНЬ
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

# 2. БАЗА ДАНИХ
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    # Оновлена таблиця актів (додано file_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS acts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT,
            osbb TEXT,
            descr TEXT,
            file_id TEXT,
            status TEXT,
            history TEXT
        )
    ''')
    # Таблиця для загальних документів ОСББ
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            file_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

# 3. КЛАВІАТУРИ
def get_main_menu():
    buttons = [
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів")],
        [KeyboardButton(text="📁 Документи ОСББ")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    file = State()

class DocForm(StatesGroup):
    name = State()
    file = State()

# 4. ОБРОБКА /START
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == CHAIRMAN_ID or message.from_user.id in ACCOUNTANTS:
        await message.answer("👋 Бот ОСББ готовий. Оберіть дію:", reply_markup=get_main_menu())

# 5. СТВОРЕННЯ АКТУ (З ПІДТРИМКОЮ PDF/ФОТО)
@dp.message(Command("new_act"), F.from_user.id == CHAIRMAN_ID)
async def start_new_act(message: types.Message, state: FSMContext):
    await message.answer("📝 Введіть номер акту (наприклад, 45-А):")
    await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def process_num(message: types.Message, state: FSMContext):
    await state.update_data(number=message.text)
    await message.answer("🏢 Вкажіть ОСББ (ВП-16, Е21, ОКПТ, В19):")
    await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def process_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip())
    await message.answer("📋 Введіть опис робіт:")
    await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def process_descr(message: types.Message, state: FSMContext):
    await state.update_data(descr=message.text)
    await message.answer("📎 Надішліть ФОТО або PDF-файл акту (або натисніть /skip):")
    await state.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo | F.document | Command("skip"))
async def process_file(message: types.Message, state: FSMContext):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    
    data = await state.get_data()
    now = datetime.now().strftime("%d.%m %H:%M")
    history_entry = f"🆕 {now} - Створено."

    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status, history) VALUES (?, ?, ?, ?, ?, ?)",
              (data['number'], data['osbb'], data['descr'], file_id, "В роботі", history_entry))
    db_id = c.lastrowid
    conn.commit()
    conn.close()

    await message.answer(f"✅ Акт №{data['number']} збережено!")
    await state.clear()

    # Сповіщення бухгалтера
    for acc_id, allowed_osbb in ACCESS_MAP.items():
        if data['osbb'] in allowed_osbb:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Отримано", callback_data=f"rec_{db_id}")]])
            caption = f"📄 Акт №{data['number']}\n🏢 ОСББ: {data['osbb']}\nℹ️ Опис: {data['descr']}"
            try:
                if file_id:
                    await bot.send_document(acc_id, file_id, caption=caption, reply_markup=kb)
                else:
                    await bot.send_message(acc_id, caption, reply_markup=kb)
            except Exception as e: print(f"Error: {e}")

# 6. ТАБЛИЦІ (ПОТОЧНІ ТА АРХІВ)
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів"]))
async def show_table(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено'" if is_archive else "status != 'Завершено'"
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    uid = message.from_user.id
    
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        placeholders = ','.join(['?'] * len(allowed))
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} AND osbb IN ({placeholders}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall()
    conn.close()

    if not rows: return await message.answer("📭 Порожньо.")

    await message.answer(f"<b>{'📂 Архів' if is_archive else '📋 Поточні акти'}:</b>", parse_mode="HTML")

    for db_id, num, osbb, status, desc, f_id in rows:
        text = f"🔹 <b>Акт №{num}</b> ({osbb})\n📝 {desc}\n⏳ Статус: <i>{status}</i>"
        btns = []
        if uid == CHAIRMAN_ID and not is_archive:
            btns.append(InlineKeyboardButton(text="🗑 Видалити", callback_data=f"del_{db_id}"))
        if f_id:
            btns.append(InlineKeyboardButton(text="📄 Файл", callback_data=f"view_{db_id}"))
        
        kb = InlineKeyboardMarkup(inline_keyboard=[btns]) if btns else None
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

# 7. ЗАГАЛЬНІ ДОКУМЕНТИ (СХОВИЩЕ)
@dp.message(F.text == "📁 Документи ОСББ")
async def list_docs(message: types.Message):
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT id, name FROM docs")
    rows = c.fetchall()
    conn.close()

    if not rows:
        kb = None
        if message.from_user.id == CHAIRMAN_ID:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати документ", callback_data="add_doc")]])
        return await message.answer("🗄 Сховище документів порожнє.", reply_markup=kb)

    kb_list = []
    for d_id, name in rows:
        kb_list.append([InlineKeyboardButton(text=f"📥 {name}", callback_data=f"getdoc_{d_id}")])
    
    if message.from_user.id == CHAIRMAN_ID:
        kb_list.append([InlineKeyboardButton(text="➕ Додати ще", callback_data="add_doc")])
    
    await message.answer("🗄 Документи ОСББ (Статути, Протоколи):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list))

@dp.callback_query(F.data == "add_doc", F.from_user.id == CHAIRMAN_ID)
async def start_add_doc(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Введіть назву документа (напр. Статут ВП-16):")
    await state.set_state(DocForm.name)
    await callback.answer()

@dp.message(DocForm.name)
async def process_doc_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📎 Тепер надішліть PDF-файл документа:")
    await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def process_doc_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO docs (name, file_id) VALUES (?, ?)", (data['name'], message.document.file_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Документ '{data['name']}' збережено!")

# 8. ОБРОБНИКИ CALLBACK КНОПОК
@dp.callback_query(F.data.startswith("view_"))
async def view_act_file(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT file_id FROM acts WHERE id=?", (db_id,))
    res = c.fetchone()
    conn.close()
    if res and res[0]:
        await callback.message.answer_document(res[0])
        await callback.answer()
    else: await callback.answer("Файл відсутній", show_alert=True)

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

@dp.callback_query(F.data.startswith("del_"), F.from_user.id == CHAIRMAN_ID)
async def delete_act(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("DELETE FROM acts WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    await callback.answer("Видалено", show_alert=True)
    await callback.message.delete()

# ЛОГІКА БУХГАЛТЕРА (REC, PAID, FINISH) - залиште аналогічно вашому коду, але з id з бази
@dp.callback_query(F.data.startswith("rec_"))
async def acc_rec(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='Отримано' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Оплачено", callback_data=f"paid_{db_id}")]])
    await callback.message.edit_caption(caption=callback.message.caption + "\n✅ ОТРИМАНО", reply_markup=kb)

@dp.callback_query(F.data.startswith("paid_"))
async def acc_paid(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='Оплачено' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_caption(caption=callback.message.caption + "\n💰 ОПЛАЧЕНО", reply_markup=None)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏁 Завершити", callback_data=f"finish_{db_id}")]])
    await bot.send_message(CHAIRMAN_ID, f"💰 Акт в базі (ID {db_id}) оплачено. Закрити?", reply_markup=kb)

@dp.callback_query(F.data.startswith("finish_"))
async def finish_act(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("UPDATE acts SET status='Завершено' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("🏁 Справу закрито та перенесено в архів.")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
