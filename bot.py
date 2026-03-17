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
    "В19": {"Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500}
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

# --- ЗАРПЛАТИ (НОВА ЛОГІКА) ---
@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"sal_v_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для зарплат:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_v_"))
async def view_salaries(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    rows = c.fetchall(); conn.close()
    
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати список", callback_data=f"sal_g_{osbb}")]])
        await cb.message.edit_text(f"Нарахувань для {osbb} за {m_y} ще немає.", reply_markup=kb)
        return

    text = f"💰 <b>{osbb} ({m_y})</b>\n\n"
    btns = []
    for s_id, emp, amo, stat in rows:
        text += f"{stat} {emp}: {amo} грн\n"
        # Кнопка тепер доступна завжди для зміни статусу туди-сюди
        btns.append([InlineKeyboardButton(text=f"Змінити: {emp}", callback_data=f"sal_p_{s_id}_{osbb}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_g_"))
async def gen_salaries(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    # Подвійна перевірка на дублікат перед записом
    c.execute("SELECT count(*) FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    if c.fetchone()[0] == 0:
        for emp, amo in STAFF_CONFIG[osbb].items():
            val = get_seasonal_salary() if amo == "seasonal" else amo
            c.execute("INSERT INTO salaries (month_year, employee, amount, osbb) VALUES (?,?,?,?)", (m_y, emp, val, osbb))
        conn.commit()
    conn.close(); await view_salaries(cb)

@dp.callback_query(F.data.startswith("sal_p_"))
async def toggle_salary(cb: CallbackQuery):
    s_id = cb.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT status FROM salaries WHERE id=?", (s_id,))
    current = c.fetchone()[0]
    # Логіка Toggle: якщо видано -> очікує, якщо очікує -> видано
    new_stat = "⏳ Очікує" if current == "✅ Видано" else "✅ Видано"
    c.execute("UPDATE salaries SET status=? WHERE id=?", (new_stat, s_id))
    conn.commit(); conn.close(); await view_salaries(cb)

# --- ОБРОБНИК КНОПОК АКТІВ ТА ЧЕКІВ (З ПІДТВЕРДЖЕННЯМ) ---
@dp.callback_query(F.data.contains("_") & ~F.data.startswith("sal_"))
async def handle_items(cb: CallbackQuery):
    parts = cb.data.split("_")
    cmd_type, action, table, item_id = parts[0], parts[1], parts[2], parts[3]
    
    if cmd_type == "conf":
        await cb.message.edit_reply_markup(reply_markup=get_confirm_kb(item_id, action, table))
        await cb.answer("Підтвердіть дію")
        return

    if cmd_type == "no": # Скасування
        conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
        c.execute(f"SELECT status FROM {table} WHERE id=?", (item_id,))
        stat = c.fetchone()[0]; conn.close()
        await cb.message.edit_reply_markup(reply_markup=get_item_kb(item_id, stat, table, cb.from_user.id))
        return

    if cmd_type == "yes": # Виконання
        conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
        new_status = None
        if action == "del": c.execute(f"DELETE FROM {table} WHERE id=?", (item_id,)); await cb.message.delete()
        elif action == "proc": new_status = "В роботі"; c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        elif action == "pay":
            new_status = "Акт оплачений" if table == "acts" else "Опрацьовано"
            c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
        elif action == "fin":
            new_status = "Завершено!" if table == "acts" else "Роботу завершено"
            c.execute(f"UPDATE {table} SET status=? WHERE id=?", (new_status, item_id))
            await cb.message.delete()
        conn.commit(); conn.close()
        if new_status and action != "fin":
            cap = cb.message.caption.split("⏳")[0] + f"⏳ Статус: {new_status}"
            await cb.message.edit_caption(caption=cap, reply_markup=get_item_kb(item_id, new_status, table, cb.from_user.id))

# --- РЕШТА КОДУ (СТАРТ, СПИСКИ, РЕЄСТРАЦІЯ) ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message): await m.answer("👋 Система готова.", reply_markup=get_main_menu())

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message):
    is_arch = "Архів" in m.text; is_acts = "акт" in m.text.lower(); table = "acts" if is_acts else "docs"
    status_sql = "status IN ('Завершено!', 'Роботу завершено')" if is_arch else "status NOT IN ('Завершено!', 'Роботу завершено')"
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if m.from_user.id == CHAIRMAN_ID: c.execute(f"SELECT * FROM {table} WHERE {status_sql} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(m.from_user.id, [])
        c.execute(f"SELECT * FROM {table} WHERE {status_sql} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()
    if not rows: return await m.answer("📭 Порожньо.")
    for r in rows:
        if is_acts:
            cap = f"📄 Акт №{r[1]} ({r[2]})\n📝 Опис: {r[3]}\n⏳ Статус: {r[5]}"
            kb = get_item_kb(r[0], r[5], "acts", m.from_user.id) if not is_arch else None
            try: await bot.send_photo(m.chat.id, r[4], caption=cap, reply_markup=kb)
            except: await m.answer(f"⚠️ Фото помилка")
        else:
            cap = f"🧾 Чек: {r[1]} ({r[2]})\n⏳ Статус: {r[4]}"
            kb = get_item_kb(r[0], r[4], "docs", m.from_user.id) if not is_arch else None
            try: await bot.send_document(m.chat.id, r[3], caption=cap, reply_markup=kb)
            except: await m.answer(f"⚠️ PDF помилка")

@dp.message(F.text == "📄 Акти")
async def m_acts(m: types.Message): await m.answer("АКТИ", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")], [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="⬅️ Назад")]], resize_keyboard=True))
@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message): await m.answer("ЧЕКИ", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")], [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="⬅️ Назад")]], resize_keyboard=True))
@dp.message(F.text == "💰 Зарплати")
async def m_sal(m: types.Message): await salary_menu(m)
@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message): await m.answer("Меню:", reply_markup=get_main_menu())

@dp.callback_query(F.data == "sal_back")
async def s_back(cb: CallbackQuery): await salary_menu(cb.message); await cb.message.delete()

# --- Реєстрація (Голова) ---
@dp.message(F.text == "➕ Створити Акт")
async def start_a(m, s: FSMContext): 
    if m.from_user.id == CHAIRMAN_ID: await m.answer("Номер акту:"); await s.set_state(ActForm.number)
@dp.message(ActForm.number)
async def a_n(m, s): await s.update_data(n=m.text); await m.answer("ОСББ:"); await s.set_state(ActForm.osbb)
@dp.message(ActForm.osbb)
async def a_o(m, s): await s.update_data(o=m.text.upper()); await m.answer("Опис:"); await s.set_state(ActForm.descr)
@dp.message(ActForm.descr)
async def a_d(m, s): await s.update_data(d=m.text); await m.answer("Фото:"); await s.set_state(ActForm.file)
@dp.message(ActForm.file, F.photo)
async def a_f(m, s):
    d = await s.get_data(); conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id) VALUES (?,?,?,?)", (d['n'], d['o'], d['d'], m.photo[-1].file_id))
    conn.commit(); conn.close(); await s.clear(); await m.answer("✅ Зареєстровано")

@dp.message(F.text == "➕ Додати PDF чек")
async def start_d(m, s: FSMContext):
    if m.from_user.id == CHAIRMAN_ID: await m.answer("Назва чеку:"); await s.set_state(DocForm.name)
@dp.message(DocForm.name)
async def d_n(m, s): await s.update_data(n=m.text); await m.answer("ОСББ:"); await s.set_state(DocForm.osbb)
@dp.message(DocForm.osbb)
async def d_o(m, s): await s.update_data(o=m.text.upper()); await m.answer("Завантажте PDF:"); await s.set_state(DocForm.file)
@dp.message(DocForm.file, F.document)
async def d_f(m, s):
    d = await s.get_data(); conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id) VALUES (?,?,?)", (d['n'], d['o'], m.document.file_id))
    conn.commit(); conn.close(); await s.clear(); await m.answer("✅ PDF додано")

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
