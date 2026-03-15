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

ACCESS_MAP = {
    5178201242: ["ВП-16", "Е21"],
    1332732213: ["ОКПТ", "В19"]
}

STAFF_CONFIG = {
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання": 12000, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000},
    "В19": {"Голова": 6000, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500}
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СТАНИ ---
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
    conn = sqlite3.connect('osbb_acts.db'); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS acts (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано")')
    cursor.execute('CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано")')
    cursor.execute('CREATE TABLE IF NOT EXISTS salaries (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT DEFAULT "⏳ Очікує")')
    conn.commit(); conn.close()

def get_seasonal_salary():
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- МЕНЮ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки ОСББ")]]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

# --- ЛОГІКА КНОПОК ---
def get_item_kb(item_id, status, table, user_id):
    btns = []
    is_ch = (user_id == CHAIRMAN_ID)
    if table == "acts":
        if status == "Не отримано":
            if is_ch: btns.append([InlineKeyboardButton(text="❌ Видалити акт", callback_data=f"del_acts_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"proc_acts_{item_id}")])
        elif status == "В роботі" and not is_ch:
            btns.append([InlineKeyboardButton(text="💳 Оплачено", callback_data=f"pay_acts_{item_id}")])
        elif status == "Акт оплачений" and is_ch:
            btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"fin_acts_{item_id}")])
    else:
        if status == "Не отримано":
            if is_ch: btns.append([InlineKeyboardButton(text="❌ Видалити PDF", callback_data=f"del_docs_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти чек", callback_data=f"proc_docs_{item_id}")])
        elif status == "В роботі" and not is_ch:
            btns.append([InlineKeyboardButton(text="📝 Опрацьовано", callback_data=f"pay_docs_{item_id}")])
        elif status == "Опрацьовано" and is_ch:
            btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"fin_docs_{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns) if btns else None

# --- ОБРОБНИКИ ЗАРПЛАТ ---
@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"sal_v_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для зарплат:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_"))
async def handle_salaries(cb: CallbackQuery):
    parts = cb.data.split("_")
    action, osbb_or_id = parts[1], parts[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()

    if action == "v": # Перегляд
        c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb_or_id, m_y))
        rows = c.fetchall()
        if not rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати", callback_data=f"sal_g_{osbb_or_id}")]])
            await cb.message.edit_text(f"Немає списку для {osbb_or_id} за {m_y}", reply_markup=kb)
        else:
            text = f"💰 <b>{osbb_or_id} ({m_y})</b>\n\n"
            btns = []
            for r in rows:
                text += f"{r[3]} {r[1]}: {r[2]} грн\n"
                if "Очікує" in r[3]: btns.append([InlineKeyboardButton(text=f"Видати {r[1]}", callback_data=f"sal_p_{r[0]}")])
            btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")])
            await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")
    
    elif action == "g": # Генерація
        for emp, amo in STAFF_CONFIG[osbb_or_id].items():
            val = get_seasonal_salary() if amo == "seasonal" else amo
            c.execute("INSERT INTO salaries (month_year, employee, amount, osbb) VALUES (?,?,?,?)", (m_y, emp, val, osbb_or_id))
        conn.commit(); await cb.answer("Список створено"); # Оновити екран (викликати "v")
    
    elif action == "p": # Виплата
        c.execute("UPDATE salaries SET status='✅ Видано' WHERE id=?", (osbb_or_id,))
        conn.commit(); await cb.answer("Виплачено")
    
    conn.close()

# --- СТВОРЕННЯ АКТУ (FSM з виправленнями) ---
@dp.message(F.text == "➕ Створити Акт")
async def act_start(m: types.Message, state: FSMContext):
    if m.from_user.id != CHAIRMAN_ID: return
    await m.answer("📝 Крок 1/4: Введіть номер акту (наприклад: 12/03):")
    await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def act_num(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text)
    await m.answer("🏠 Крок 2/4: Оберіть ОСББ (ВП-16, Е21, ОКПТ або В19):")
    await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def act_osbb(m: types.Message, state: FSMContext):
    osbb_name = m.text.upper().strip()
    await state.update_data(o=osbb_name)
    await m.answer(f"✅ Обрано {osbb_name}. Крок 3/4: Введіть опис робіт:")
    await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def act_descr(m: types.Message, state: FSMContext):
    await state.update_data(d=m.text)
    await m.answer("📸 Крок 4/4: Надішліть ФОТО акту (як фото, не файл):")
    await state.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo)
async def act_file(m: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = m.photo[-1].file_id  # Беремо найкращу якість
    
    try:
        conn = sqlite3.connect('osbb_acts.db')
        c = conn.cursor()
        c.execute("INSERT INTO acts (number, osbb, descr, file_id, status) VALUES (?,?,?,?,?)", 
                  (data['n'], data['o'], data['d'], file_id, "Не отримано"))
        conn.commit()
        conn.close()
        await state.clear()
        await m.answer(f"✅ Акт №{data['n']} збережено і він з'явився у списку 'Поточні акти'.", 
                       reply_markup=get_main_menu(m.from_user.id))
    except Exception as e:
        await m.answer(f"❌ Помилка бази даних: {e}")

# --- СТВОРЕННЯ ЧЕКУ (FSM з виправленнями) ---
@dp.message(F.text == "➕ Додати PDF чек")
async def doc_start(m: types.Message, state: FSMContext):
    if m.from_user.id != CHAIRMAN_ID: return
    await m.answer("🧾 Крок 1/3: Назва чеку (наприклад: Оплата світла березень):")
    await state.set_state(DocForm.name)

@dp.message(DocForm.name)
async def doc_name(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text)
    await m.answer("🏠 Крок 2/3: Введіть ОСББ:")
    await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def doc_osbb(m: types.Message, state: FSMContext):
    await state.update_data(o=m.text.upper().strip())
    await m.answer("📂 Крок 3/3: Надішліть PDF файл (саме як файл/документ):")
    await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def doc_file(m: types.Message, state: FSMContext):
    data = await state.get_data()
    if not m.document.mime_type == 'application/pdf':
        return await m.answer("⚠️ Будь ласка, надішліть саме PDF файл.")
    
    try:
        conn = sqlite3.connect('osbb_acts.db')
        c = conn.cursor()
        c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?,?,?,?)", 
                  (data['n'], data['o'], m.document.file_id, "Не отримано"))
        conn.commit()
        conn.close()
        await state.clear()
        await m.answer("✅ Чек збережено!", reply_markup=get_main_menu(m.from_user.id))
    except Exception as e:
        await m.answer(f"❌ Помилка: {e}")


# --- ВІДОБРАЖЕННЯ СПИСКІВ ---
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message):
    uid = m.from_user.id
    is_arch = "Архів" in m.text
    is_acts = "акт" in m.text.lower()
    table = "acts" if is_acts else "docs"
    status_sql = "status IN ('Завершено!', 'Роботу завершено')" if is_arch else "status NOT IN ('Завершено!', 'Роботу завершено')"
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall(); conn.close()
    if not rows: return await m.answer("📭 Список порожній.")

    for r in rows:
        if is_acts:
            cap = f"📄 Акт №{r[1]} ({r[2]})\n📝 {r[3]}\n⏳ Статус: {r[5]}"
            kb = get_item_kb(r[0], r[5], "acts", uid) if not is_arch else None
            await bot.send_photo(m.chat.id, r[4], caption=cap, reply_markup=kb)
        else:
            cap = f"🧾 Чек: {r[1]} ({r[2]})\n⏳ Статус: {r[4]}"
            kb = get_item_kb(r[0], r[4], "docs", uid) if not is_arch else None
            await bot.send_document(m.chat.id, r[3], caption=cap, reply_markup=kb)

# --- CALLBACKS ---
@dp.callback_query(F.data.contains("_") & ~F.data.startswith("sal_"))
async def cb_items(cb: CallbackQuery):
    act, table, i_id = cb.data.split("_")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor(); new_s = None
    if act == "del": c.execute(f"DELETE FROM {table} WHERE id=?", (i_id,)); await cb.message.delete()
    elif act == "proc": new_s = "В роботі"
    elif act == "pay": new_s = "Акт оплачений" if table == "acts" else "Опрацьовано"
    elif act == "fin": new_s = "Завершено!" if table == "acts" else "Роботу завершено"; await cb.message.delete()
    
    if new_s:
        c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_s, i_id))
        if act != "fin":
            cap = cb.message.caption.split("⏳")[0] + f"⏳ Статус: {new_s}"
            await cb.message.edit_caption(caption=cap, reply_markup=get_item_kb(i_id, new_s, table, cb.from_user.id))
    conn.commit(); conn.close(); await cb.answer()

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("Система активована.", reply_markup=get_main_menu(m.from_user.id))

@dp.message(F.text == "📄 Акти")
async def m_acts(m): await m.answer("АКТИ", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")], [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="⬅️ Назад")]], resize_keyboard=True))

@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m): await m.answer("ЧЕКИ", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")], [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="⬅️ Назад")]], resize_keyboard=True))

@dp.message(F.text == "⬅️ Назад")
async def m_back(m): await m.answer("Головне меню:", reply_markup=get_main_menu(m.from_user.id))

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
