import os
import sqlite3
import asyncioimport os
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

# ID бухгалтерів (з вашого попереднього коду)
ACC1_ID = 5178201242
ACC2_ID = 1332732213

ACCESS_MAP = {
    ACC1_ID: ["ВП-16", "Е21"],
    ACC2_ID: ["ОКПТ", "В19"]
}
ACCOUNTANTS = [ACC1_ID, ACC2_ID]

# Дані працівників для авто-нарахування
STAFF_CONFIG = {
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання": 12000, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000}
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СТАНИ FSM ---
class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

class SalaryEdit(StatesGroup):
    waiting_for_amount = State()
    waiting_for_comment = State()

# --- БАЗА ДАНИХ ТА МІГРАЦІЯ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    # Акти
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT)''')
    # Чеки
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT)''')
    # Зарплати
    cursor.execute('''CREATE TABLE IF NOT EXISTS salaries 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT)''')
    # Історія змін зарплат
    cursor.execute('''CREATE TABLE IF NOT EXISTS salary_history 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, salary_id INTEGER, old_amount REAL, new_amount REAL, comment TEXT, date TEXT)''')
    conn.commit()
    conn.close()

def migrate_db():
    """Синхронізація статусів при оновленні"""
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE acts SET status = 'Завершено!' WHERE status = 'Завершено'")
    cursor.execute("UPDATE docs SET status = 'Завершено!' WHERE status = 'Завершено'")
    conn.commit()
    conn.close()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def get_seasonal_salary():
    """Двірник Е21: Квітень-Вересень 4500, інакше 3500"""
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- КЛАВІАТУРИ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки")]]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

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

# --- СТАРТ ТА НАВІГАЦІЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Система ОСББ готова до роботи. Оберіть розділ:", 
                         reply_markup=get_main_menu(message.from_user.id))

@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu())

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ (PDF)", reply_markup=get_docs_menu())

# --- РОЗДІЛ: ЗАРПЛАТИ (ТІЛЬКИ ГОЛОВА) ---
@dp.message(F.text == "💰 Зарплати", F.from_user.id == CHAIRMAN_ID)
async def salary_selection(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")]
    ])
    await message.answer("Оберіть ОСББ для контролю виплат:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_view_"))
async def view_salaries(callback: CallbackQuery):
    osbb = callback.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    rows = c.fetchall()

    if not rows:
        # Автоматичне нарахування з 20-го числа
        if datetime.now().day >= 20:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати нарахування", callback_data=f"sal_gen_{osbb}")]])
            await callback.message.edit_text(f"Нарахувань для {osbb} за {m_y} ще немає. Сформувати?", reply_markup=kb)
        else:
            await callback.answer(f"Нарахування будуть доступні з 20-го числа.", show_alert=True)
        return

    total = sum(r[2] for r in rows)
    text = f"💰 <b>{osbb} ({m_y})</b>\nРазом до виплати: <b>{total} грн</b>\n\n"
    
    kb_list = []
    all_paid = True
    for s_id, emp, amo, stat in rows:
        is_paid = stat == "Видано"
        if not is_paid: all_paid = False
        text += f"{'✅' if is_paid else '⏳'} {emp}: {amo} грн\n"
        
        if not is_paid:
            kb_list.append([
                InlineKeyboardButton(text=f"💵 Видати {emp}", callback_data=f"sal_pay_{s_id}"),
                InlineKeyboardButton(text="📝 Ред.", callback_data=f"sal_edit_{s_id}")
            ])
    
    if all_paid: text += "\n🔒 <b>Місяць закритий.</b> Всі виплати здійснено."
    kb_list.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_gen_"))
async def process_gen_salaries(callback: CallbackQuery):
    osbb = callback.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    for emp, amo in STAFF_CONFIG[osbb].items():
        val = get_seasonal_salary() if amo == "seasonal" else amo
        c.execute("INSERT INTO salaries (month_year, employee, amount, osbb, status) VALUES (?,?,?,?,?)",
                  (m_y, emp, val, osbb, "Очікує"))
    conn.commit(); conn.close()
    await view_salaries(callback)

@dp.callback_query(F.data.startswith("sal_pay_"))
async def process_pay_salary(callback: CallbackQuery):
    s_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE salaries SET status='Видано' WHERE id=?", (s_id,))
    c.execute("SELECT osbb FROM salaries WHERE id=?", (s_id,))
    osbb = c.fetchone()[0]
    conn.commit(); conn.close()
    await callback.answer("Виплату відмічено")
    callback.data = f"sal_view_{osbb}"
    await view_salaries(callback)

@dp.callback_query(F.data.startswith("sal_edit_"))
async def edit_salary_init(callback: CallbackQuery, state: FSMContext):
    await state.update_data(sid=callback.data.split("_")[2])
    await callback.message.answer("Введіть НОВУ суму:")
    await state.set_state(SalaryEdit.waiting_for_amount)
    await callback.answer()

@dp.message(SalaryEdit.waiting_for_amount)
async def edit_salary_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        await state.update_data(new_amo=amount)
        await message.answer("Напишіть коментар (причина зміни):")
        await state.set_state(SalaryEdit.waiting_for_comment)
    except ValueError:
        await message.answer("Будь ласка, введіть число.")

@dp.message(SalaryEdit.waiting_for_comment)
async def edit_salary_final(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT amount, osbb FROM salaries WHERE id=?", (data['sid'],))
    old_amo, osbb = c.fetchone()
    
    c.execute("UPDATE salaries SET amount=? WHERE id=?", (data['new_amo'], data['sid']))
    c.execute("INSERT INTO salary_history (salary_id, old_amount, new_amount, comment, date) VALUES (?,?,?,?,?)",
              (data['sid'], old_amo, data['new_amo'], message.text, datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit(); conn.close()
    await state.clear()
    await message.answer(f"✅ Зміни збережено для {osbb}", reply_markup=get_main_menu(CHAIRMAN_ID))

@dp.callback_query(F.data == "sal_back")
async def process_sal_back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")]
    ])
    await callback.message.edit_text("Оберіть ОСББ для контролю зарплат:", reply_markup=kb)

# --- РОЗДІЛ: АКТИ (ПОПЕРЕДНЯ ЛОГІКА) ---
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
              (data['number'], data['osbb'], data['descr'], f_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit(); conn.close()

    await message.answer(f"✅ Акт №{data['number']} зареєстровано.")
    await state.clear()

    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")]])
            caption = f"🔔 <b>Новий Акт №{data['number']}</b>\n🏢 ОСББ: {data['osbb']}\n📝 Опис: {data['descr']}"
            try: await bot.send_photo(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            except: await bot.send_document(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів"]))
async def show_acts(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено!'" if is_archive else "status != 'Завершено!'"
    uid = message.from_user.id
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()

    if not rows: return await message.answer(f"📭 {'Архів' if is_archive else 'Поточних'} актів порожній.")

    for db_id, num, osbb, status, desc, f_id in rows:
        text = f"📄 <b>Акт №{num}</b> ({osbb})\n📝 {desc}\n⏳ Статус: <b>{status}</b>"
        btns = []
        if not is_archive:
            if uid in ACCOUNTANTS:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")])
                elif status == "В роботі": btns.append([InlineKeyboardButton(text="💰 Оплачено", callback_data=f"act_paid_{db_id}")])
            if uid == CHAIRMAN_ID:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"act_del_{db_id}")])
                elif status == "Акт оплачений": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"act_fin_{db_id}")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        try: await bot.send_photo(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")
        except: await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")

# --- РОЗДІЛ: ЧЕКИ (PDF) ---
@dp.message(F.text.in_(["📋 Поточні чеки", "📂 Архів чеків"]))
async def show_docs_list(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено!'" if is_archive else "status != 'Завершено!'"
    uid = message.from_user.id
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID and not is_archive:
        kb_add = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати pdf-файл", callback_data="add_doc")]])
        await message.answer("Керування поточними чеками:", reply_markup=kb_add)

    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, name, osbb, status, file_id FROM docs WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, name, osbb, status, file_id FROM docs WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()

    if not rows: return await message.answer(f"📭 {'Архів' if is_archive else 'Поточних'} чеків порожній.")

    for d_id, name, osbb, status, f_id in rows:
        text = f"🧾 <b>{name}</b> ({osbb})\n⏳ Статус: <b>{status}</b>"
        btns = []
        if not is_archive:
            if uid in ACCOUNTANTS:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"ch_acc_{d_id}")])
                elif status == "В роботі": btns.append([InlineKeyboardButton(text="⚙️ Опрацьовано", callback_data=f"ch_done_{d_id}")])
            if uid == CHAIRMAN_ID:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"ch_del_{d_id}")])
                elif status == "Роботу завершено": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"ch_fin_{d_id}")])
        
        await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None, parse_mode="HTML")

@dp.callback_query(F.data == "add_doc")
async def add_doc_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Назва (напр. Світло Березень):"); await state.set_state(DocForm.name); await callback.answer()

@dp.message(DocForm.name)
async def doc_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text); await message.answer("🏢 ОСББ:"); await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def doc_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip()); await message.answer("📎 PDF-файл:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def doc_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?, ?, ?, ?)", (data['name'], data['osbb'], message.document.file_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit(); conn.close(); await state.clear(); await message.answer("✅ PDF завантажено.")

    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"ch_acc_{db_id}")]])
            await bot.send_document(acc_id, message.document.file_id, caption=f"🧾 <b>Нові чеки: {data['name']}</b> ({data['osbb']})", reply_markup=kb, parse_mode="HTML")

# --- ОБРОБНИКИ КОЛБЕКІВ (АКТИ/ЧЕКИ) ---
@dp.callback_query(F.data.startswith(("act_acc_", "act_paid_", "act_fin_", "act_del_", "ch_acc_", "ch_done_", "ch_fin_", "ch_del_")))
async def global_callbacks(callback: CallbackQuery):
    action, _, db_id = callback.data.rpartition("_")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if action == "act_acc":
        c.execute("UPDATE acts SET status='В роботі' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "🔔 Бухгалтер прийняв Акт у роботу.")
    elif action == "act_paid":
        c.execute("UPDATE acts SET status='Акт оплачений' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "💰 Акт ОПЛАЧЕНО.")
    elif action == "act_fin": c.execute("UPDATE acts SET status='Завершено!' WHERE id=?", (db_id,))
    elif action == "act_del": c.execute("DELETE FROM acts WHERE id=?", (db_id,))
    elif action == "ch_acc":
        c.execute("UPDATE docs SET status='В роботі' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "🔔 Бухгалтер прийняв Чеки в роботу.")
    elif action == "ch_done":
        c.execute("UPDATE docs SET status='Роботу завершено' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "⚙️ Чеки ОПРАЦЬОВАНО.")
    elif action == "ch_fin": c.execute("UPDATE docs SET status='Завершено!' WHERE id=?", (db_id,))
    elif action == "ch_del": c.execute("DELETE FROM docs WHERE id=?", (db_id,))

    conn.commit(); conn.close()
    await callback.answer("Виконано"); await callback.message.delete()

# --- ЗАПУСК ---
async def main():
    init_db()
    migrate_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
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

# ID бухгалтерів (з вашого попереднього коду)
ACC1_ID = 5178201242
ACC2_ID = 1332732213

ACCESS_MAP = {
    ACC1_ID: ["ВП-16", "Е21"],
    ACC2_ID: ["ОКПТ", "В19"]
}
ACCOUNTANTS = [ACC1_ID, ACC2_ID]

# Дані працівників для авто-нарахування
STAFF_CONFIG = {
    "ВП-16": {"Голова": 6000, "Бухгалтер": 3000, "Прибирання": 12000, "Сантехнік": 2800},
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {"Голова": 4000, "Бухгалтер": 1000, "Нарахування ВТВК": 1000, "Двірник": 2000, "Обхідник": 1000}
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СТАНИ FSM ---
class ActForm(StatesGroup):
    number, osbb, descr, file = State(), State(), State(), State()

class DocForm(StatesGroup):
    name, osbb, file = State(), State(), State()

class SalaryEdit(StatesGroup):
    waiting_for_amount = State()
    waiting_for_comment = State()

# --- БАЗА ДАНИХ ТА МІГРАЦІЯ ---
def init_db():
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    # Акти
    cursor.execute('''CREATE TABLE IF NOT EXISTS acts 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, osbb TEXT, descr TEXT, file_id TEXT, status TEXT)''')
    # Чеки
    cursor.execute('''CREATE TABLE IF NOT EXISTS docs 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, osbb TEXT, file_id TEXT, status TEXT)''')
    # Зарплати
    cursor.execute('''CREATE TABLE IF NOT EXISTS salaries 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, month_year TEXT, employee TEXT, amount REAL, osbb TEXT, status TEXT)''')
    # Історія змін зарплат
    cursor.execute('''CREATE TABLE IF NOT EXISTS salary_history 
        (id INTEGER PRIMARY KEY AUTOINCREMENT, salary_id INTEGER, old_amount REAL, new_amount REAL, comment TEXT, date TEXT)''')
    conn.commit()
    conn.close()

def migrate_db():
    """Синхронізація статусів при оновленні"""
    conn = sqlite3.connect('osbb_acts.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE acts SET status = 'Завершено!' WHERE status = 'Завершено'")
    cursor.execute("UPDATE docs SET status = 'Завершено!' WHERE status = 'Завершено'")
    conn.commit()
    conn.close()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def get_seasonal_salary():
    """Двірник Е21: Квітень-Вересень 4500, інакше 3500"""
    month = datetime.now().month
    return 4500 if 4 <= month <= 9 else 3500

# --- КЛАВІАТУРИ ---
def get_main_menu(uid):
    btns = [[KeyboardButton(text="📄 Акти"), KeyboardButton(text="🧾 Чеки")]]
    if uid == CHAIRMAN_ID:
        btns.append([KeyboardButton(text="💰 Зарплати")])
    return ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)

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

# --- СТАРТ ТА НАВІГАЦІЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Система ОСББ готова до роботи. Оберіть розділ:", 
                         reply_markup=get_main_menu(message.from_user.id))

@dp.message(F.text.in_(["📄 Акти", "➡️ Перейти до Акти"]))
async def menu_acts(message: types.Message):
    await message.answer("📂 Розділ: АКТИ", reply_markup=get_acts_menu())

@dp.message(F.text.in_(["🧾 Чеки", "➡️ Перейти до Чеки"]))
async def menu_docs(message: types.Message):
    await message.answer("📂 Розділ: ЧЕКИ (PDF)", reply_markup=get_docs_menu())

# --- РОЗДІЛ: ЗАРПЛАТИ (ТІЛЬКИ ГОЛОВА) ---
@dp.message(F.text == "💰 Зарплати", F.from_user.id == CHAIRMAN_ID)
async def salary_selection(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")]
    ])
    await message.answer("Оберіть ОСББ для контролю виплат:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sal_view_"))
async def view_salaries(callback: CallbackQuery):
    osbb = callback.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT id, employee, amount, status FROM salaries WHERE osbb=? AND month_year=?", (osbb, m_y))
    rows = c.fetchall()

    if not rows:
        # Автоматичне нарахування з 20-го числа
        if datetime.now().day >= 20:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Сформувати нарахування", callback_data=f"sal_gen_{osbb}")]])
            await callback.message.edit_text(f"Нарахувань для {osbb} за {m_y} ще немає. Сформувати?", reply_markup=kb)
        else:
            await callback.answer(f"Нарахування будуть доступні з 20-го числа.", show_alert=True)
        return

    total = sum(r[2] for r in rows)
    text = f"💰 <b>{osbb} ({m_y})</b>\nРазом до виплати: <b>{total} грн</b>\n\n"
    
    kb_list = []
    all_paid = True
    for s_id, emp, amo, stat in rows:
        is_paid = stat == "Видано"
        if not is_paid: all_paid = False
        text += f"{'✅' if is_paid else '⏳'} {emp}: {amo} грн\n"
        
        if not is_paid:
            kb_list.append([
                InlineKeyboardButton(text=f"💵 Видати {emp}", callback_data=f"sal_pay_{s_id}"),
                InlineKeyboardButton(text="📝 Ред.", callback_data=f"sal_edit_{s_id}")
            ])
    
    if all_paid: text += "\n🔒 <b>Місяць закритий.</b> Всі виплати здійснено."
    kb_list.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sal_back")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list), parse_mode="HTML")

@dp.callback_query(F.data.startswith("sal_gen_"))
async def process_gen_salaries(callback: CallbackQuery):
    osbb = callback.data.split("_")[2]
    m_y = datetime.now().strftime("%m.%Y")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    for emp, amo in STAFF_CONFIG[osbb].items():
        val = get_seasonal_salary() if amo == "seasonal" else amo
        c.execute("INSERT INTO salaries (month_year, employee, amount, osbb, status) VALUES (?,?,?,?,?)",
                  (m_y, emp, val, osbb, "Очікує"))
    conn.commit(); conn.close()
    await view_salaries(callback)

@dp.callback_query(F.data.startswith("sal_pay_"))
async def process_pay_salary(callback: CallbackQuery):
    s_id = callback.data.split("_")[2]
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("UPDATE salaries SET status='Видано' WHERE id=?", (s_id,))
    c.execute("SELECT osbb FROM salaries WHERE id=?", (s_id,))
    osbb = c.fetchone()[0]
    conn.commit(); conn.close()
    await callback.answer("Виплату відмічено")
    callback.data = f"sal_view_{osbb}"
    await view_salaries(callback)

@dp.callback_query(F.data.startswith("sal_edit_"))
async def edit_salary_init(callback: CallbackQuery, state: FSMContext):
    await state.update_data(sid=callback.data.split("_")[2])
    await callback.message.answer("Введіть НОВУ суму:")
    await state.set_state(SalaryEdit.waiting_for_amount)
    await callback.answer()

@dp.message(SalaryEdit.waiting_for_amount)
async def edit_salary_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        await state.update_data(new_amo=amount)
        await message.answer("Напишіть коментар (причина зміни):")
        await state.set_state(SalaryEdit.waiting_for_comment)
    except ValueError:
        await message.answer("Будь ласка, введіть число.")

@dp.message(SalaryEdit.waiting_for_comment)
async def edit_salary_final(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("SELECT amount, osbb FROM salaries WHERE id=?", (data['sid'],))
    old_amo, osbb = c.fetchone()
    
    c.execute("UPDATE salaries SET amount=? WHERE id=?", (data['new_amo'], data['sid']))
    c.execute("INSERT INTO salary_history (salary_id, old_amount, new_amount, comment, date) VALUES (?,?,?,?,?)",
              (data['sid'], old_amo, data['new_amo'], message.text, datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit(); conn.close()
    await state.clear()
    await message.answer(f"✅ Зміни збережено для {osbb}", reply_markup=get_main_menu(CHAIRMAN_ID))

@dp.callback_query(F.data == "sal_back")
async def process_sal_back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 ВП-16", callback_data="sal_view_ВП-16")],
        [InlineKeyboardButton(text="🏢 Е21", callback_data="sal_view_Е21")],
        [InlineKeyboardButton(text="🏢 ОКПТ", callback_data="sal_view_ОКПТ")]
    ])
    await callback.message.edit_text("Оберіть ОСББ для контролю зарплат:", reply_markup=kb)

# --- РОЗДІЛ: АКТИ (ПОПЕРЕДНЯ ЛОГІКА) ---
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
              (data['number'], data['osbb'], data['descr'], f_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit(); conn.close()

    await message.answer(f"✅ Акт №{data['number']} зареєстровано.")
    await state.clear()

    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")]])
            caption = f"🔔 <b>Новий Акт №{data['number']}</b>\n🏢 ОСББ: {data['osbb']}\n📝 Опис: {data['descr']}"
            try: await bot.send_photo(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            except: await bot.send_document(acc_id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів актів"]))
async def show_acts(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено!'" if is_archive else "status != 'Завершено!'"
    uid = message.from_user.id
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, number, osbb, status, descr, file_id FROM acts WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()

    if not rows: return await message.answer(f"📭 {'Архів' if is_archive else 'Поточних'} актів порожній.")

    for db_id, num, osbb, status, desc, f_id in rows:
        text = f"📄 <b>Акт №{num}</b> ({osbb})\n📝 {desc}\n⏳ Статус: <b>{status}</b>"
        btns = []
        if not is_archive:
            if uid in ACCOUNTANTS:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"act_acc_{db_id}")])
                elif status == "В роботі": btns.append([InlineKeyboardButton(text="💰 Оплачено", callback_data=f"act_paid_{db_id}")])
            if uid == CHAIRMAN_ID:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"act_del_{db_id}")])
                elif status == "Акт оплачений": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"act_fin_{db_id}")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        try: await bot.send_photo(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")
        except: await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=kb, parse_mode="HTML")

# --- РОЗДІЛ: ЧЕКИ (PDF) ---
@dp.message(F.text.in_(["📋 Поточні чеки", "📂 Архів чеків"]))
async def show_docs_list(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено!'" if is_archive else "status != 'Завершено!'"
    uid = message.from_user.id
    
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    if uid == CHAIRMAN_ID and not is_archive:
        kb_add = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Додати pdf-файл", callback_data="add_doc")]])
        await message.answer("Керування поточними чеками:", reply_markup=kb_add)

    if uid == CHAIRMAN_ID:
        c.execute(f"SELECT id, name, osbb, status, file_id FROM docs WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(uid, [])
        c.execute(f"SELECT id, name, osbb, status, file_id FROM docs WHERE {status_filter} AND osbb IN ({','.join(['?']*len(allowed))}) ORDER BY id DESC", allowed)
    rows = c.fetchall(); conn.close()

    if not rows: return await message.answer(f"📭 {'Архів' if is_archive else 'Поточних'} чеків порожній.")

    for d_id, name, osbb, status, f_id in rows:
        text = f"🧾 <b>{name}</b> ({osbb})\n⏳ Статус: <b>{status}</b>"
        btns = []
        if not is_archive:
            if uid in ACCOUNTANTS:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"ch_acc_{d_id}")])
                elif status == "В роботі": btns.append([InlineKeyboardButton(text="⚙️ Опрацьовано", callback_data=f"ch_done_{d_id}")])
            if uid == CHAIRMAN_ID:
                if status == "Не отримано": btns.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"ch_del_{d_id}")])
                elif status == "Роботу завершено": btns.append([InlineKeyboardButton(text="🏁 Завершити", callback_data=f"ch_fin_{d_id}")])
        
        await bot.send_document(message.chat.id, f_id, caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None, parse_mode="HTML")

@dp.callback_query(F.data == "add_doc")
async def add_doc_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Назва (напр. Світло Березень):"); await state.set_state(DocForm.name); await callback.answer()

@dp.message(DocForm.name)
async def doc_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text); await message.answer("🏢 ОСББ:"); await state.set_state(DocForm.osbb)

@dp.message(DocForm.osbb)
async def doc_osbb(message: types.Message, state: FSMContext):
    await state.update_data(osbb=message.text.strip()); await message.answer("📎 PDF-файл:"); await state.set_state(DocForm.file)

@dp.message(DocForm.file, F.document)
async def doc_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    c.execute("INSERT INTO docs (name, osbb, file_id, status) VALUES (?, ?, ?, ?)", (data['name'], data['osbb'], message.document.file_id, "Не отримано"))
    db_id = c.lastrowid
    conn.commit(); conn.close(); await state.clear(); await message.answer("✅ PDF завантажено.")

    for acc_id, osbbs in ACCESS_MAP.items():
        if data['osbb'] in osbbs:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Прийняти в роботу", callback_data=f"ch_acc_{db_id}")]])
            await bot.send_document(acc_id, message.document.file_id, caption=f"🧾 <b>Нові чеки: {data['name']}</b> ({data['osbb']})", reply_markup=kb, parse_mode="HTML")

# --- ОБРОБНИКИ КОЛБЕКІВ (АКТИ/ЧЕКИ) ---
@dp.callback_query(F.data.startswith(("act_acc_", "act_paid_", "act_fin_", "act_del_", "ch_acc_", "ch_done_", "ch_fin_", "ch_del_")))
async def global_callbacks(callback: CallbackQuery):
    action, _, db_id = callback.data.rpartition("_")
    conn = sqlite3.connect('osbb_acts.db'); c = conn.cursor()
    
    if action == "act_acc":
        c.execute("UPDATE acts SET status='В роботі' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "🔔 Бухгалтер прийняв Акт у роботу.")
    elif action == "act_paid":
        c.execute("UPDATE acts SET status='Акт оплачений' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "💰 Акт ОПЛАЧЕНО.")
    elif action == "act_fin": c.execute("UPDATE acts SET status='Завершено!' WHERE id=?", (db_id,))
    elif action == "act_del": c.execute("DELETE FROM acts WHERE id=?", (db_id,))
    elif action == "ch_acc":
        c.execute("UPDATE docs SET status='В роботі' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "🔔 Бухгалтер прийняв Чеки в роботу.")
    elif action == "ch_done":
        c.execute("UPDATE docs SET status='Роботу завершено' WHERE id=?", (db_id,))
        await bot.send_message(CHAIRMAN_ID, "⚙️ Чеки ОПРАЦЬОВАНО.")
    elif action == "ch_fin": c.execute("UPDATE docs SET status='Завершено!' WHERE id=?", (db_id,))
    elif action == "ch_del": c.execute("DELETE FROM docs WHERE id=?", (db_id,))

    conn.commit(); conn.close()
    await callback.answer("Виконано"); await callback.message.delete()

# --- ЗАПУСК ---
async def main():
    init_db()
    migrate_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

