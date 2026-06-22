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
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000, "Баки": 1000},
    "В19": {"Голова": 4820, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500}
}

MONTHS_UA = {
    "01": "Січень", "02": "Лютий", "03": "Березень", "04": "Квітень",
    "05": "Травень", "06": "Червень", "07": "Липень", "08": "Серпень",
    "09": "Вересень", "10": "Жовтень", "11": "Листопад", "12": "Грудень"
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СТАНІ FSM ---
class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

class JobForm(StatesGroup):
    osbb, month, text = State(), State(), State()

class JobCommentForm(StatesGroup):
    job_id, text = State(), State()

# --- ІНІЦІАЛІЗАЦІЯ БАЗИ ДАНИХ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db'); cursor = conn.cursor()
    # Додано created_at для фільтрації за періодами
    cursor.execute('CREATE TABLE IF NOT EXISTS acts (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано", created_at TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT DEFAULT "Не отримано", created_at TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS salaries (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT DEFAULT "⏳ Очікує")')
    
    # Таблиця для Плану Робіт
    cursor.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        osbb TEXT, 
        month_year TEXT, 
        task_text TEXT, 
        status TEXT DEFAULT "Створено", 
        stages TEXT DEFAULT "", 
        comments TEXT DEFAULT "",
        updated_at TEXT,
        created_at TEXT
    )''')
    
    # Міграція: перевірка наявності колонки created_at у старих базах
    try:
        cursor.execute("ALTER TABLE acts ADD COLUMN created_at TEXT")
    except: pass
    try:
        cursor.execute("ALTER TABLE docs ADD COLUMN created_at TEXT")
    except: pass
    
    conn.commit(); conn.close()

def get_seasonal_salary():
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- ГОЛОВНЕ МЕНЮ ---
def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки ОСББ")],
        [KeyboardButton(text="🛠️ План робіт"), KeyboardButton(text="📊 Прозвітувати")],
        [KeyboardButton(text="💰 Зарплати")]
    ], resize_keyboard=True)

# --- ГЕНЕРАТОР КНОПОК ПЕРІОДІВ ---
def get_period_keyboard(prefix: str, osbb: str, year: str = None):
    kb = []
    if not year:
        # Вибір року (поточний та попередній)
        cy = datetime.now().year
        kb.append([InlineKeyboardButton(text=str(cy), callback_data=f"{prefix}_yr_{osbb}_{cy}")])
        kb.append([InlineKeyboardButton(text=str(cy-1), callback_data=f"{prefix}_yr_{osbb}_{cy-1}")])
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu_back")])
    else:
        # Вибір періоду в межах року
        kb.append([InlineKeyboardButton(text="📅 Цілий рік", callback_data=f"{prefix}_fin_{osbb}_{year}_all")])
        # Рядки з місяцями (по 2 в ряд)
        row = []
        for m_num, m_name in MONTHS_UA.items():
            row.append(InlineKeyboardButton(text=m_name, callback_data=f"{prefix}_fin_{osbb}_{year}_{m_num}"))
            if len(row) == 2:
                kb.append(row); row = []
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"{prefix}_v_{osbb}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

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

# --- МЕНЮ ЗАРПЛАТ ---
@dp.message(F.text == "💰 Зарплати")
async def salary_menu(m: types.Message, state: FSMContext):
    await state.clear()
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
            cap = cb.message.caption.split("⏳")[0] + f"⏳ Status: {new_status}"
            await cb.message.edit_caption(caption=cap, reply_markup=get_item_kb(item_id, new_status, table, cb.from_user.id))

# --- ОНОВЛЕНЕ МЕНЮ АРХІВУ АКТІВ ТА КНОПКА З БОРТУ ---
@dp.callback_query(F.data.startswith("arch_acts_v_"))
async def act_arch_years(cb: CallbackQuery):
    osbb = cb.data.split("_")[3]
    await cb.message.edit_text(f"📁 <b>Архів актів {osbb}</b>. Оберіть рік:", reply_markup=get_period_keyboard("arch_acts", osbb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("arch_acts_yr_"))
async def act_arch_months(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year = p[3], p[4]
    await cb.message.edit_text(f"📁 <b>Архів актів {osbb} за {year} рік</b>. Оберіть період:", reply_markup=get_period_keyboard("arch_acts", osbb, year), parse_mode="HTML")

@dp.callback_query(F.data.startswith("arch_acts_fin_"))
async def show_archived_acts_by_period(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year, period = p[3], p[4], p[5]
    await cb.answer("🔍 Шукаю акти...")

    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if period == "all":
        c.execute("SELECT * FROM acts WHERE osbb=? AND status='Завершено!' AND created_at LIKE ?", (osbb, f"{year}-%"))
        title = f"Всі закриті акти {osbb} за {year} рік"
    else:
        c.execute("SELECT * FROM acts WHERE osbb=? AND status='Завершено!' AND created_at LIKE ?", (osbb, f"{year}-{period}-%"))
        title = f"Закриті акти {osbb} за {MONTHS_UA[period]} {year}"
    rows = c.fetchall(); conn.close()

    if not rows:
        return await cb.message.answer(f"📭 {title} відсутні в базі.")

    await cb.message.answer(f"⬇️ <b>{title}:</b>", parse_mode="HTML")
    for r in rows:
        cap = f"📄 Акт №{r[1]} ({r[2]})\n📝 Опис: {r[3]}\n📅 Дата: {r[6]}\n⏳ Статус: {r[5]}"
        try: await bot.send_photo(cb.message.chat.id, r[4], caption=cap)
        except: pass

# --- ОНОВЛЕНИЙ ZIP АРХІВАТОР З КОНКРЕТНИМИ ПЕРІОДАМИ ---
@dp.message(F.text == "📦 ZIP Архів")
async def zip_report_menu(m: types.Message, state: FSMContext):
    await state.clear()
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"zip_v_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для вивантаження ZIP-архіву:", reply_markup=kb)

@dp.callback_query(F.data.startswith("zip_v_"))
async def zip_arch_years(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    await cb.message.edit_text(f"📦 <b>ZIP Архів для {osbb}</b>. Оберіть рік:", reply_markup=get_period_keyboard("zip", osbb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("zip_yr_"))
async def zip_arch_months(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year = p[2], p[3]
    await cb.message.edit_text(f"📦 <b>ZIP Архів для {osbb} ({year})</b>. Оберіть період:", reply_markup=get_period_keyboard("zip", osbb, year), parse_mode="HTML")

@dp.callback_query(F.data.startswith("zip_fin_"))
async def send_filtered_zip(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year, period = p[2], p[3], p[4]
    await cb.answer("📦 Збираю файли...")
    
    date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
    period_title = f"{year}" if period == "all" else f"{MONTHS_UA[period]}_{year}"

    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT number, file_id FROM acts WHERE osbb=? AND status='Завершено!' AND created_at LIKE ?", (osbb, date_pattern))
    acts = c.fetchall()
    c.execute("SELECT name, file_id FROM docs WHERE osbb=? AND status='Роботу завершено' AND created_at LIKE ?", (osbb, date_pattern))
    docs = c.fetchall()
    conn.close()

    if not acts and not docs:
        return await cb.message.answer(f"❌ За період {period_title} для {osbb} немає закритих документів.")

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
    document = types.BufferedInputFile(zip_buffer.read(), filename=f"Archive_{osbb}_{period_title}.zip")
    await bot.send_document(cb.message.chat.id, document, caption=f"✅ Згенеровано архів {osbb} за період: {period_title}")


# --- НОВИЙ МОДУЛЬ: ПЛАН РОБІТ ---
@dp.message(F.text == "🛠️ План робіт")
async def jobs_main_menu(m: types.Message, state: FSMContext):
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавити роботу"), KeyboardButton(text="📋 Поточні роботи")],
        [KeyboardButton(text="✅ Виконані роботи"), KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await m.answer("🛠️ <b>Керування планом робіт по ОСББ:</b>", reply_markup=kb, parse_mode="HTML")

# Додавання роботи
@dp.message(F.text == "➕ Добавити роботу")
async def job_add_start(m: types.Message, state: FSMContext):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"jadd_osbb_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("Оберіть ОСББ для додавання завдання:", reply_markup=kb)

@dp.callback_query(F.data.startswith("jadd_osbb_"))
async def job_add_osbb(cb: CallbackQuery, state: FSMContext):
    osbb = cb.data.split("_")[2]
    await state.update_data(osbb=osbb)
    # Кнопки вибору місяця
    kb = []
    row = []
    for m_num, m_name in MONTHS_UA.items():
        row.append(InlineKeyboardButton(text=m_name, callback_data=f"jadd_m_{m_num}"))
        if len(row) == 3:
            kb.append(row); row = []
    await cb.message.edit_text("Оберіть місяць для планування завдання:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("jadd_m_"))
async def job_add_month(cb: CallbackQuery, state: FSMContext):
    m_num = cb.data.split("_")[2]
    cy = datetime.now().year
    m_y = f"{m_num}.{cy}"
    await state.update_data(m_y=m_y)
    await state.set_state(JobForm.text)
    await cb.message.edit_text(f"Опис завдання для обраного періоду ({m_y}).\n✍️ <b>Введіть текст задачі:</b>", parse_mode="HTML")

@dp.message(JobForm.text)
async def job_add_save(m: types.Message, state: FSMContext):
    d = await state.get_data()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO jobs (osbb, month_year, task_text, updated_at, created_at) VALUES (?,?,?,?,?)",
              (d['osbb'], d['m_y'], m.text, now_str, datetime.now().strftime("%Y-%m-%d")))
    conn.commit(); conn.close()
    
    await state.clear()
    await m.answer("✅ Задача успішно додана в план робіт!", reply_markup=get_main_menu())

# Перегляд поточних робіт
@dp.message(F.text == "📋 Поточні роботи")
async def current_jobs_start(m: types.Message):
    # Доступ мають адмін або з ACCESS_MAP
    allowed_osbb = list(STAFF_CONFIG.keys()) if m.from_user.id == CHAIRMAN_ID else ACCESS_MAP.get(m.from_user.id, [])
    if not allowed_osbb: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o, callback_data=f"jview_curr_{o}")] for o in allowed_osbb])
    await m.answer("Оберіть ОСББ для перегляду активних завдань:", reply_markup=kb)

@dp.callback_query(F.data.startswith("jview_curr_"))
async def show_current_jobs(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, month_year, task_text, status, stages, comments FROM jobs WHERE osbb=? AND status != 'Роботу закінчено' ORDER BY id DESC", (osbb,))
    rows = c.fetchall(); conn.close()
    
    if not rows:
        return await cb.message.edit_text(f"📭 Активних (поточних) робіт по {osbb} немає.")
        
    await cb.message.delete()
    for j_id, m_y, text, stat, stages, comm in rows:
        msg_text = f"🛠️ <b>Завдання ОСББ {osbb} ({m_y})</b>\n" \
                   f"📝 <b>Задача:</b> {text}\n" \
                   f"📊 <b>Статус:</b> <code>{stat}</code>\n"
        if stages: msg_text += f"🧱 <b>Етапи виконання:</b>\n{stages}\n"
        if comm: msg_text += f"💬 <b>Коментарі/нотатки:</b>\n{comm}"
        
        # Кнопки взаємодії
        kb = []
        is_ch = (cb.from_user.id == CHAIRMAN_ID)
        
        if stat == "Створено":
            if not is_ch: kb.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"jact_proc_{j_id}")])
            if is_ch: kb.append([InlineKeyboardButton(text="❌ Видалити задачу", callback_data=f"jact_del_{j_id}")])
        elif stat == "В роботі":
            kb.append([InlineKeyboardButton(text="🧱 Додати етап", callback_data=f"jact_stage_{j_id}")])
            kb.append([InlineKeyboardButton(text="💬 Написати коментар", callback_data=f"jact_comm_{j_id}")])
            if not is_ch: kb.append([InlineKeyboardButton(text="🏁 Роботу закінчено", callback_data=f"jact_fin_{j_id}")])
            
        await cb.message.answer(msg_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None, parse_mode="HTML")

# Обробка дій з завданнями
@dp.callback_query(F.data.startswith("jact_"))
async def handle_job_action(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split("_")
    action, j_id = p[1], int(p[2])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if action == "del":
        c.execute("DELETE FROM jobs WHERE id=?", (j_id,))
        conn.commit(); conn.close(); await cb.message.delete(); await cb.answer("Видалено")
    elif action == "proc":
        c.execute("UPDATE jobs SET status='В роботі', updated_at=? WHERE id=?", (now_str, j_id))
        conn.commit(); conn.close(); await cb.answer("Взято в роботу!"); await current_jobs_start(cb.message)
    elif action == "fin":
        c.execute("UPDATE jobs SET status='Роботу закінчено', updated_at=? WHERE id=?", (now_str, j_id))
        conn.commit(); conn.close(); await cb.answer("Завершено!"); await cb.message.delete()
    elif action in ["stage", "comm"]:
        await state.update_data(j_id=j_id, mode=action)
        await state.set_state(JobCommentForm.text)
        txt = "Введіть назву етапу виконання:" if action == "stage" else "Введіть ваш коментар/зауваження до роботи:"
        await cb.message.answer(f"✍️ {txt}")
        conn.close()

@dp.message(JobCommentForm.text)
async def save_job_stage_or_comment(m: types.Message, state: FSMContext):
    d = await state.get_data()
    j_id, mode = d['j_id'], d['mode']
    now_str = datetime.now().strftime("[%d.%m %H:%M]")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if mode == "stage":
        c.execute("SELECT stages FROM jobs WHERE id=?", (j_id,))
        old = c.fetchone()[0] or ""
        new_val = old + f"• {now_str} {m.text}\n"
        c.execute("UPDATE jobs SET stages=? WHERE id=?", (new_val, j_id))
    else:
        c.execute("SELECT comments FROM jobs WHERE id=?", (j_id,))
        old = c.fetchone()[0] or ""
        new_val = old + f"{now_str}: {m.text}\n"
        c.execute("UPDATE jobs SET comments=? WHERE id=?", (new_val, j_id))
        
    conn.commit(); conn.close()
    await state.clear()
    await m.answer("✅ Дані оновлено в картці завдання.", reply_markup=get_main_menu())

# Виконані роботи з періодами
@dp.message(F.text == "✅ Виконані роботи")
async def finished_jobs_menu(m: types.Message):
    allowed_osbb = list(STAFF_CONFIG.keys()) if m.from_user.id == CHAIRMAN_ID else ACCESS_MAP.get(m.from_user.id, [])
    if not allowed_osbb: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o, callback_data=f"jfin_v_{o}")] for o in allowed_osbb])
    await m.answer("Оберіть ОСББ для перегляду історії виконаних завдань:", reply_markup=kb)

@dp.callback_query(F.data.startswith("jfin_v_"))
async def finished_jobs_years(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    await cb.message.edit_text(f"✅ <b>Архів виконаних робіт {osbb}</b>. Оберіть рік:", reply_markup=get_period_keyboard("jfin", osbb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("jfin_yr_"))
async def finished_jobs_months(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year = p[2], p[3]
    await cb.message.edit_text(f"✅ <b>Архів робіт {osbb} ({year})</b>. Оберіть період:", reply_markup=get_period_keyboard("jfin", osbb, year), parse_mode="HTML")

@dp.callback_query(F.data.startswith("jfin_fin_"))
async def show_finished_jobs_results(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year, period = p[2], p[3], p[4]
    
    date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
    title = f"Всі виконані роботи {osbb} за {year} рік" if period == "all" else f"Виконані роботи {osbb} за {MONTHS_UA[period]} {year}"

    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT month_year, task_text, stages, comments, updated_at FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND created_at LIKE ? ORDER BY id DESC", (osbb, date_pattern))
    rows = c.fetchall(); conn.close()

    if not rows:
        return await cb.message.answer(f"📭 {title} не знайдені.")

    await cb.message.answer(f"🏁 <b>{title}:</b>", parse_mode="HTML")
    for m_y, text, stages, comm, u_at in rows:
        m_txt = f"📋 <b>Період планування:</b> {m_y}\n" \
                f"✅ <b>Задача:</b> {text}\n" \
                f"📆 <b>Дата закриття:</b> {u_at}\n"
        if stages: m_txt += f"🧱 <b>Етапи виконання:</b>\n{stages}\n"
        if comm: m_txt += f"💬 <b>Коментарі/архів нотаток:</b>\n{comm}"
        await cb.message.answer(m_txt, parse_mode="HTML")


# --- НОВИЙ МОДУЛЬ: АВТОМАТИЧНІ ЗВІТИ (ПРОЗВІТУВАТИ) ---
@dp.message(F.text == "📊 Прозвітувати")
async def report_main_menu(m: types.Message):
    if m.from_user.id != CHAIRMAN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=osbb, callback_data=f"rep_v_{osbb}")] for osbb in STAFF_CONFIG.keys()])
    await m.answer("📊 <b>Генерація фінансово-господарських звітів.</b>\nОберіть ОСББ для формування документа:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("rep_v_"))
async def report_years(cb: CallbackQuery):
    osbb = cb.data.split("_")[2]
    await cb.message.edit_text(f"📊 <b>Звітність для {osbb}</b>. Оберіть рік:", reply_markup=get_period_keyboard("rep", osbb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("rep_yr_"))
async def report_months(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year = p[2], p[3]
    await cb.message.edit_text(f"📊 <b>Звітність для {osbb} за {year} рік</b>. Оберіть період:", reply_markup=get_period_keyboard("rep", osbb, year), parse_mode="HTML")

@dp.callback_query(F.data.startswith("rep_fin_"))
async def generate_and_send_report_file(cb: CallbackQuery):
    p = cb.data.split("_")
    osbb, year, period = p[2], p[3], p[4]
    await cb.answer("📈 Формую звіт...")

    date_pattern = f"{year}-%" if period == "all" else f"{year}-{period}-%"
    p_title = f"{year} рік" if period == "all" else f"{MONTHS_UA[period]} {year}"

    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    # 1. АКТИ ЗА ПЕРІОД
    c.execute("SELECT number, descr, file_id, status, created_at FROM acts WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
    acts = c.fetchall()
    
    # 2. ЧЕКИ ЗА ПЕРІОД
    c.execute("SELECT name, file_id, status, created_at FROM docs WHERE osbb=? AND created_at LIKE ?", (osbb, date_pattern))
    docs = c.fetchall()
    
    # 3. ЗАРПЛАТИ (Фільтр по назві місяця)
    sal_pattern = f"%.{year}" if period == "all" else f"{period}.{year}"
    c.execute("SELECT month_year, employee, amount, status FROM salaries WHERE osbb=? AND month_year LIKE ?", (osbb, sal_pattern))
    salaries = c.fetchall()
    
    # 4. ВИКОНАНІ РОБОТИ ЗА ПЕРІОД
    c.execute("SELECT task_text, stages, comments, updated_at FROM jobs WHERE osbb=? AND status='Роботу закінчено' AND created_at LIKE ?", (osbb, date_pattern))
    jobs = c.fetchall()
    
    conn.close()

    # Генерація тексту звіту
    report = f"==================================================\n"
    report += f"     ФІНАНСОВО-ГОСПОДАРСЬКИЙ ЗВІТ ДЛЯ {osbb}\n"
    report += f"     ПЕРІОД: {p_title.upper()}\n"
    report += f"     Дата генерації: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    report += f"==================================================\n\n"

    # Секція 1: Акти виконаних робіт
    report += f"📋 1. АКТИ ВИКОНАНИХ РОБІТ (Всього знайдено: {len(acts)})\n"
    report += f"--------------------------------------------------\n"
    if acts:
        for num, descr, f_id, stat, date in acts:
            report += f"• Акт №{num} від [{date}] | Статус: {stat}\n"
            report += f"  Опис: {descr}\n"
            report += f"  🔗 Посилання на документ (Telegram ID): {f_id}\n\n"
    else:
        report += "Записів за вказаний період немає.\n\n"

    # Секція 2: Чеки та ПДФ документи
    report += f"🧾 2. ДОКУМЕНТИ ТА ЧЕКИ ВИТРАТ (Всього знайдено: {len(docs)})\n"
    report += f"--------------------------------------------------\n"
    if docs:
        for name, f_id, stat, date in docs:
            report += f"• Документ: {name} від [{date}] | Статус: {stat}\n"
            report += f"  🔗 Посилання на файл (Telegram ID): {f_id}\n\n"
    else:
        report += "Чеки за вказаний період відсутні.\n\n"

    # Секція 3: Виплати заробітної плати
    report += f"💰 3. ВІДОМІСТЬ НАРАХУВАННЯ ТА ВИПЛАТИ ЗАРПЛАТ\n"
    report += f"--------------------------------------------------\n"
    total_sal = 0
    if salaries:
        for m_y, emp, amo, stat in salaries:
            report += f"• [{m_y}] {emp}: {amo} грн — {stat}\n"
            if "Видано" in stat or stat == "✅ Видано":
                total_sal += amo
        report += f"\n👉 Усього виплачено за відомостями: {total_sal} грн\n\n"
    else:
        report += "Дані про виплату заробітної плати відсутні.\n\n"

    # Секція 4: Виконані господарські роботи
    report += f"🛠️ 4. ГОСПОДАРСЬКІ РОБОТИ (ЗАКРИТІ ЗАДАЧІ)\n"
    report += f"--------------------------------------------------\n"
    if jobs:
        for text, stages, comm, u_at in jobs:
            report += f"• Задача: {text}\n"
            report += f"  Дата завершення: {u_at}\n"
            if stages: report += f"  🧱 Пройдені етапи:\n{stages}"
            if comm: report += f"  💬 Фінальні коментарі:\n{comm}"
            report += f"--------------------------------------------------\n"
    else:
        report += "У звітному періоді виконаних завдань немає.\n\n"

    report += f"\n==================================================\n"
    report += f"Кінець звіту. Документ сформовано автоматично.\n"

    # Конвертація в байт-файл та надсилання
    report_file = io.BytesIO(report.encode('utf-8'))
    document = types.BufferedInputFile(report_file.read(), filename=f"Report_{osbb}_{period}_{year}.txt")
    
    await bot.send_document(cb.message.chat.id, document, caption=f"📄 Готовий фінансовий звіт {osbb} за {p_title}")


# --- РЕШТА СТАРИХ ФУНКЦІЙ МЕНЮ (З ОЧИЩЕННЯМ СТАНУ) ---
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext): 
    await state.clear(); await m.answer("👋 Система готова.", reply_markup=get_main_menu())

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів", "📋 Поточні чеки", "📂 Архів чеків"]))
async def show_items(m: types.Message, state: FSMContext):
    await state.clear()
    is_arch = "Архів" in m.text; is_acts = "акт" in m.text.lower(); table = "acts" if is_acts else "docs"
    
    # Якщо це архів актів — відкриваємо нове гнучке меню замість старого виведення всього списку
    if is_arch and is_acts:
        allowed_osbb = list(STAFF_CONFIG.keys()) if m.from_user.id == CHAIRMAN_ID : else ACCESS_MAP.get(m.from_user.id, [])
        if not allowed_osbb: return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o, callback_data=f"arch_acts_v_{o}")] for o in allowed_osbb])
        return await m.answer("Оберіть ОСББ для перегляду архіву актів:", reply_markup=kb)

    status_sql = "status IN ('Завершено!', 'Роботу завершено')" if is_arch else "status NOT IN ('Завершено!', 'Роботу завершено')"
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
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
async def m_acts(m: types.Message, state: FSMContext): 
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів актів")],
        [KeyboardButton(text="➕ Створити Акт"), KeyboardButton(text="📦 ZIP Архів")],
        [KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await m.answer("АКТИ", reply_markup=kb)

@dp.message(F.text == "🧾 Чеки ОСББ")
async def m_docs(m: types.Message, state: FSMContext): 
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Поточні чеки"), KeyboardButton(text="📂 Архів чеків")],
        [KeyboardButton(text="➕ Додати PDF чек"), KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)
    await m.answer("ЧЕКИ", reply_markup=kb)

@dp.message(F.text == "⬅️ Назад")
async def m_back(m: types.Message, state: FSMContext): 
    await state.clear()
    await m.answer("Головне меню:", reply_markup=get_main_menu())

@dp.callback_query(F.data == "sal_back")
async def s_back(cb: CallbackQuery): 
    await salary_menu(cb.message, FSMContext); await cb.message.delete()

@dp.callback_query(F.data == "main_menu_back")
async def back_to_menu_inline(cb: CallbackQuery):
    await cb.message.edit_text("Дію скасовано. Скористайтесь кнопками меню на клавіатурі.")

# --- РЕЄСТРАЦІЯ АКТІВ ТА ЧЕКІВ (FSM) З АВТО-ДАТОЮ ---
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
    d = await state.get_data()
    today = datetime.now().strftime("%Y-%m-%d") # Автоматичний запис дати створення
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, file_id, created_at) VALUES (?,?,?,?,?)", (d['n'], d['o'], d['d'], m.photo[-1].file_id, today))
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
    d = await state.get_data()
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, created_at) VALUES (?,?,?,?)", (d['n'], d['o'], m.document.file_id, today))
    conn.commit(); conn.close(); await state.clear(); await m.answer("✅ PDF додано")

async def main():
    init_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
