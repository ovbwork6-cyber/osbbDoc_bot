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

# Мапа доступу бухгалтерів
ACCESS_MAP = {
    5178201242: ["ВП-16", "Е21"],
    1332732213: ["ОКПТ", "В19"]
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- КЛАСИ СТАНІВ ---
class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано")''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано")''')
    conn.commit()
    conn.close()

# --- КЛАВІАТУРИ МЕНЮ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки ОСББ")]]
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

# --- ГЕНЕРАЦІЯ КНОПОК ДЛЯ ЗАПИСІВ ---
def get_item_buttons(item_id, status, table, user_id):
    btns = []
    is_chairman = (user_id == CHAIRMAN_ID)
    
    if table == "acts":
        if status == "Не отримано":
            if is_chairman: btns.append([InlineKeyboardButton(text="❌ Видалити акт", callback_data=f"del_acts_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"proc_acts_{item_id}")])
        elif status == "В роботу" or status == "В роботі":
            if not is_chairman: btns.append([InlineKeyboardButton(text="💳 Оплачено", callback_data=f"pay_acts_{item_id}")])
        elif status == "Акт оплачений":
            if is_chairman: btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"fin_acts_{item_id}")])
            
    else: # Для таблиці docs (Чеки PDF)
        if status == "Не отримано":
            if is_chairman: btns.append([InlineKeyboardButton(text="❌ Видалити PDF", callback_data=f"del_docs_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти чек", callback_data=f"proc_docs_{item_id}")])
        elif status == "В роботі":
            if not is_chairman: btns.append([InlineKeyboardButton(text="📝 Опрацьовано", callback_data=f"pay_docs_{item_id}")])
        elif status == "Опрацьовано":
            if is_chairman: btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"fin_docs_{item_id}")])

    return InlineKeyboardMarkup(inline_keyboard=btns) if btns else None

# --- ОБРОБНИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 Вітаю! Оберіть розділ:", reply_markup=get_main_menu(message.from_user.id))

@dp.message(F.text == "📄 Акти")
async def menu_acts(message: types.Message):
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
        [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await message.answer("Розділ АКТИ:", reply_markup=kb)

@dp.message(F.text == "🧾 Чеки ОСББ")
async def menu_docs(message: types.Message):
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
        [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await message.answer("Розділ ЧЕКИ ОСББ:", reply_markup=kb)

@dp.message(F.text == "⬅️ Назад")
async def cmd_back(message: types.Message):
    await message.answer("Головне меню:", reply_markup=get_main_menu(message.from_user.id))

# --- ВІДОБРАЖЕННЯ СПИСКІВ ---
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(message: types.Message):
    uid = message.from_user.id
    is_archive = "Архів" in message.text
    is_acts = "акт" in message.text.lower()
    table = "acts" if is_acts else "docs"
    
    # Визначаємо фільтр статусу
    if is_archive:
        status_sql = "status IN ('Завершено!', 'Завершено', 'Роботу завершено')"
    else:
        status_sql = "status NOT IN ('Завершено!', 'Завершено', 'Роботу завершено')"

    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        if not allowed: return await message.answer("У вас немає доступу до жодного ОСББ.")
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall(); conn.close()
    if not rows: return await message.answer("📭 Порожньо.")

    for r in rows:
        # Для актів: r[1] номер, r[2] осбб, r[3] опис, r[4] file_id, r[5] статус
        # Для чеків: r[1] назва, r[2] осбб, r[3] file_id, r[4] статус
        if is_acts:
            cap = f"📄 Акт №{r[1]} ({r[2]})\n📝 Опис: {r[3]}\n⏳ Статус: {r[5]}"
            f_id, cur_status = r[4], r[5]
            kb = get_item_buttons(r[0], cur_status, "acts", uid) if not is_archive else None
            await bot.send_photo(message.chat.id, f_id, caption=cap, reply_markup=kb)
        else:
            cap = f"🧾 Чек: {r[1]} ({r[2]})\n⏳ Статус: {r[4]}"
            f_id, cur_status = r[3], r[4]
            kb = get_item_buttons(r[0], cur_status, "docs", uid) if not is_archive else None
            await bot.send_document(message.chat.id, f_id, caption=cap, reply_markup=kb)

# --- ОБРОБКА КНОПОК (CALLBACKS) ---
@dp.callback_query(F.data.contains("_"))
async def handle_buttons(callback: CallbackQuery):
    action, table, item_id = callback.data.split("_")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if action == "del": # Видалити
        c.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        await callback.message.delete()
        await callback.answer("Видалено")
    
    elif action == "proc": # Прийняти
        new_status = "В роботі"
        c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        await callback.answer("Прийнято в роботу")
        
    elif action == "pay": # Оплачено / Опрацьовано
        new_status = "Акт оплачений" if table == "acts" else "Опрацьовано"
        c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        await callback.answer("Оновлено")
        
    elif action == "fin": # Завершити
        new_status = "Завершено!" if table == "acts" else "Роботу завершено"
        c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        await callback.answer("Перенесено в архів")
        await callback.message.delete()

    conn.commit(); conn.close()
    # Якщо не видалили повідомлення, оновлюємо текст (тільки для не-видалення)
    if action != "del" and action != "fin":
        await callback.message.edit_caption(caption=callback.message.caption.split("⏳")[0] + f"⏳ Статус: {new_status}", 
                                           reply_markup=get_item_buttons(item_id, new_status, table, callback.from_user.id))

# --- РЕЄСТРАЦІЯ НОВИХ (АКТИ ТА PDF) ---
@dp.message(F.text == "➕ Створити Акт")
async def start_act(message: types.Message, state: FSMContext):
    if message.from_user.id != CHAIRMAN_ID: return
    await message.answer("Введіть номер акту:"); await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def act_n(m: types.Message, s: FSMContext):
    await s.update_data(n=m.text); await m.answer("ОСББ:"); await s.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def act_o(m: types.Message, s: FSMContext):
    await s.update_data(o=m.text.upper()); await m.answer("Опис:"); await s.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def act_d(m: types.Message, s: FSMContext):
    await s.update_data(d=m.text); await m.answer("Фото:"); await s.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo)
async def act_f(m: types.Message, s: FSMContext):
    d = await s.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?,?,?,?,?)", 
              (d['n'], d['o'], d['d'], m.photo[-1].file_id, "Не отримано"))
    conn.commit(); conn.close(); await s.clear()
    await m.answer("✅ Акт зареєстровано", reply_markup=get_acts_menu(m.from_user.id))

# --- РЕЄСТРАЦІЯ PDF ---
@dp.message(F.text == "➕ Додати PDF чек")
async def start_doc(message: types.Message, state: FSMContext):
    if message.from_user.id != CHAIRMAN_ID: return
    await message.answer("Назва чеку (опис):"); await state.set_state(DocForm.name)

@dp.message(DocForm.name)
async def doc_n(m: types.Message, s: FSMContext):
    await s.update_data(n=m.text); await m.answer("ОСББ:"); await s.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def doc_o(m: types.Message, s: FSMContext):
    await s.update_data(o=m.text.upper()); await m.answer("Завантажте PDF:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def doc_f(m: types.Message, s: FSMContext):
    d = await s.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?,?,?,?)", 
              (d['n'], d['o'], m.document.file_id, "Не отримано"))
    conn.commit(); conn.close(); await s.clear()
    await m.answer("✅ PDF файл додано", reply_markup=get_docs_menu(m.from_user.id))

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
