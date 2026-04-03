import os
import sqlite3
import asyncio
import zipfile
import io
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
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання (Марія)": 9500, "Прибирання (Олег)": 2500, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000, "Сміттєві баки": 1000},
    "В19": {"Голова": 4820, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500}
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

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
def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки ОСББ")], [KeyboardButton(text="💰 Зарплати")]], resize_keyboard=True)

# --- ПІДТВЕРДЖЕННЯ ТА КНОПКИ ---
def get_confirm_kb(item_id, action, table):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так", callback_data=f"yes_{action}_{table}_{item_id}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"no_cancel_{table}_{item_id}")
    ]])

def get_item_kb(item_id, status, table, user_id):
    btns = []
    is_ch = (user_id == CHAIRMAN_ID)
    if table == "acts":
        if status == "Не отримано":
            if is_ch: btns.append([InlineKeyboardButton(text="❌ Видалити акт", callback_data=f"conf_del_acts_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти акт", callback_data=f"conf_proc_acts_{item_id}")])
        elif status == "В роботі" and not is_ch:
            btns.append([InlineKeyboardButton(text="💳 Оплачено", callback_data=f"conf_pay_acts_{item_id}")])
        elif status == "Акт оплачений" and is_ch:
            btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"conf_fin_acts_{item_id}")])
    else:
        if status == "Не отримано":
            if is_ch: btns.append([InlineKeyboardButton(text="❌ Видалити PDF", callback_data=f"conf_del_docs_{item_id}")])
            else: btns.append([InlineKeyboardButton(text="📥 Прийняти чек", callback_data=f"conf_proc_docs_{item_id}")])
        elif status == "В роботі" and not is_ch:
            btns.append([InlineKeyboardButton(text="📝 Опрацьовано", callback_data=f"conf_pay_docs_{item_id}")])
        elif status == "Опрацьовано" and is_ch:
            btns.append([InlineKeyboardButton(text="✅ Завершити", callback_data=f"conf_fin_docs_{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns) if btns else None

# --- ЗАРПЛАТИ (З АРХІВОМ ТА TOGGLE) ---
@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"sal_v_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для зарплат:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_v_"))
async def view_salaries_options(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Поточний місяць", callback_data=f"sal_list_{osbb}_{datetime.now().strftime('%m.%Y')}")],
        [InlineKeyboardButton(text="📂 Архів виплат", callback_data=f"sal_hist_{osbb}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")]
    ])
    await cb.message.edit_text(f"Керування зарплатами: <b>{osbb}</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_hist_"))
async def view_salary_history(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT DISTINCT month_year FROM salaries WHERE osbb=? ORDER BY id ASC", (osbb,))
    months = c.fetchall(); conn.close()
    if not months: return await cb.answer("Історія порожня", show_alert=True)
    btns = [[InlineKeyboardButton(text=m[0], callback_data=f"sal_list_{osbb}_{m[0]}")] for m in months]
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"sal_v_{osbb}")])
    await cb.message.edit_text(f"Архів <b>{osbb}</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_list_"))
async def show_salary_list(cb: CallbackQuery, osbb: str = None, m_y: str = None):
    if not osbb or not m_y:
        parts = cb.data.split("_")
        osbb, m_y = parts[2], parts[3]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    rows = c.fetchall(); conn.close()
    if not rows:
        if m_y == datetime.now().strftime("%m.%Y"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати список", callback_data=f"sal_g_{osbb}")]])
            return await cb.message.edit_text(f"Нарахувань для {osbb} за {m_y} ще немає.", reply_markup=kb)
        return await cb.message.edit_text("Дані відсутні.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"sal_hist_{osbb}")]]))

    text = f"💰 <b>{osbb} | {m_y}</b>\n\n"
    btns = []
    for s_id, emp, amo, stat in rows:
        text += f"{stat} {emp}: {amo} грн\n"
        btns.append([InlineKeyboardButton(text=f"Змінити: {emp}", callback_data=f"sal_p_{s_id}_{osbb}_{m_y}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"sal_hist_{osbb}" if m_y != datetime.now().strftime("%m.%Y") else f"sal_v_{osbb}")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_g_"))
async def gen_salaries(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT count(*) FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    if c.fetchone()[0] == 0:
        for emp, amo in STAFF_CONFIG[osbb].items():
            val = get_seasonal_salary() if amo == "seasonal" else amo
            c.execute("INSERT INTO salaries (month_year, employee, amount, osbb) VALUES (?,?,?,?)", (m_y, emp, val, osbb))
        conn.commit()
    conn.close(); await show_salary_list(cb, osbb, m_y)

@dp.callback_query(F.data.startswith("sal_p_"))
async def toggle_salary(cb: CallbackQuery):
    p = cb.data.split("_")
    s_id, osbb, m_y = p[2], p[3], p[4]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT status FROM salaries WHERE id=?", (s_id,))
    new_stat = "⏳ Очікує" if c.fetchone()[0] == "✅ Видано" else "✅ Видано"
    c.execute("UPDATE salaries SET status=? WHERE id=?", (new_stat, s_id))
    conn.commit(); conn.close(); await show_salary_list(cb, osbb, m_y)

# --- ПІДТВЕРДЖЕННЯ ДІЙ (АКТИ/ЧЕКИ) ---
@dp.callback_query(F.data.startswith(("conf_", "yes_", "no_")))
async def handle_items_confirmed(cb: CallbackQuery):
    parts = cb.data.split("_")
    cmd_type, action, table, item_id = parts[0], parts[1], parts[2], parts[3]
    
    if cmd_type == "conf":
        await cb.message.edit_reply_markup(reply_markup=get_confirm_kb(item_id, action, table))
        return await cb.answer("Ви впевнені?")

    if cmd_type == "no":
        conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
        c.execute(f"SELECT status FROM {table} WHERE id=?", (item_id,))
        stat = c.fetchone()[0]; conn.close()
        return await cb.message.edit_reply_markup(reply_markup=get_item_kb(item_id, stat, table, cb.from_user.id))

    if cmd_type == "yes":
        conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
        new_status = None
        if action == "del": c.execute(f"DELETE FROM {table} WHERE id=?", (item_id,)); await cb.message.delete()
        elif action == "proc": new_status = "В роботі"; c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        elif action == "pay":
            new_status = "Акт оплачений" if table == "acts" else "Опрацьовано"
            c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        elif action == "fin":
            new_status = "Завершено!" if table == "acts" else "Роботу завершено"
            c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id)); await cb.message.delete()
        conn.commit(); conn.close()
        if new_status and action != "fin":
            cap = cb.message.caption.split("⏳")[0] + f"⏳ Статус: {new_status}"
            await cb.message.edit_caption(caption=cap, reply_markup=get_item_kb(item_id, new_status, table, cb.from_user.id))

# --- ZIP АРХІВАТОР ---
@dp.message(F.text == "📦 ZIP Архів")
async def zip_report_menu(m: types.Message):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"zip_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для вивантаження архіву 2026:", reply_markup=kb)

@dp.callback_query(F.data.startswith("zip_"))
async def send_zip(cb: CallbackQuery):
    osbb = cb.data.split("_")[1]
    await cb.answer("📦 Готую архів...")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT number, file_id FROM acts WHERE osbb=? AND status='Завершено!'", (osbb,))
    acts = c.fetchall()
    c.execute("SELECT name, file_id FROM docs WHERE osbb=? AND status='Роботу завершено'", (osbb,))
    docs = c.fetchall()
    conn.close()

    if not acts and not docs:
        return await cb.message.answer(f"❌ В архіві {osbb} немає закритих документів.")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'a', zipfile.ZIP_DEFLATED) as zip_file:
        for num, f_id in acts:
            try:
                file = await bot.get_file(f_id)
                file_data = await bot.download_file(file.file_path)
                zip_file.writestr(f"Акти/Акт_№{num}.jpg", file_data.read())
            except: continue
        for name, f_id in docs:
            try:
                file = await bot.get_file(f_id)
                file_data = await bot.download_file(file.file_path)
                zip_file.writestr(f"Чеки/{name}.pdf", file_data.read())
            except: continue

    zip_buffer.seek(0)
    document = types.BufferedInputFile(zip_buffer.read(), filename=f"Archive_{osbb}_2026.zip")
    await bot.send_document(cb.message.chat.id, document, caption=f"✅ Повний архів {osbb}")

# --- РЕШТА ФУНКЦІЙ ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext): 
    await state.clear(); await m.answer("👋 Система готова.", reply_markup=get_main_menu())

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message):
    is_arch = "Архів" in m.text; is_acts = "акт" in m.text.lower(); table = "acts" if is_acts else "docs"
    status_sql = "status IN ('Завершено!', 'Роботу завершено')" if is_arch else "status NOT IN ('Завершено!', 'Роботу завершено')"
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    # Сортування ASC - старі спочатку, нові в кінці списку
    if m.from_user.id == CHAIRMAN_ID: c.execute(f"SELECT * FROM {table} WHERE {status_sql} ORDER BY id ASC")
    else:
        allowed = ACCESS_MAP.get(m.from_user.id, [])
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id ASC", allowed)
    rows = c.fetchall(); conn.close()
    if not rows: return await m.answer("📭 Порожньо.")
    for r in rows:
        if is_acts:
            cap = f"📄 Акт №{r[1]} ({r[2]})\n📝 Опис: {r[3]}\n⏳ Статус: {r[5]}"
            kb = get_item_kb(r[0], r[5], "acts", m.from_user.id) if not is_arch else None
            try: await bot.send_photo(m.chat.id, r[4], caption=cap, reply_markup=kb)
            except: pass
        else:
            cap = f"🧾 Чек: {r[1]} ({r[2]})\n⏳ Статус: {r[4]}"
            kb = get_item_kb(r[0], r[4], "docs", m.from_user.id) if not is_arch else None
            try: await bot.send_document(m.chat.id, r[3], caption=cap, reply_markup=kb)
            except: pass

@dp.message(F.text == "📄 Акти")
async def m_acts(m: types.Message): 
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
        [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="📦 ZIP Архів")],
        [KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await m.answer("АКТИ", reply_markup=kb)

@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message): 
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
        [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await m.answer("ЧЕКИ", reply_markup=kb)

@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message): await m.answer("Головне меню:", reply_markup=get_main_menu())
@dp.callback_query(F.data == "sal_back")
async def s_back(cb: CallbackQuery): await salary_menu(cb.message); await cb.message.delete()

# --- РЕЄСТРАЦІЯ (FSM) ---
@dp.message(F.text == "➕ Створити Акт")
async def start_a(m: types.Message, state: FSMContext): 
    if m.from_user.id == CHAIRMAN_ID: 
        await state.clear(); await m.answer("Введіть номер акту:"); await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def a_n(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text); await m.answer("Введіть ОСББ:"); await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def a_o(m: types.Message, state: FSMContext):
    await state.update_data(o=m.text.upper()); await m.answer("Введіть опис:"); await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def a_d(m: types.Message, state: FSMContext):
    await state.update_data(d=m.text); await m.answer("Завантажте фото:"); await state.set_state(ActForm.file)

@dp.message(ActForm.file, F.photo)
async def a_f(m: types.Message, state: FSMContext):
    d = await state.get_data(); conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id) VALUES (?,?,?,?)", (d['n'], d['o'], d['d'], m.photo[-1].file_id))
    conn.commit(); conn.close(); await state.clear(); await m.answer("✅ Акт зареєстровано")

@dp.message(F.text == "➕ Додати PDF чек")
async def start_d(m: types.Message, state: FSMContext):
    if m.from_user.id == CHAIRMAN_ID: 
        await state.clear(); await m.answer("Введіть назву чеку:"); await state.set_state(DocForm.name)

@dp.message(DocForm.name)
async def d_n(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text); await m.answer("Введіть ОСББ:"); await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def d_o(m: types.Message, state: FSMContext):
    await state.update_data(o=m.text.upper()); await m.answer("Завантажте PDF:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def d_f(m: types.Message, state: FSMContext):
    d = await state.get_data(); conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id) VALUES (?,?,?)", (d['n'], d['o'], m.document.file_id))
    conn.commit(); conn.close(); await state.clear(); await m.answer("✅ PDF додано")

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
