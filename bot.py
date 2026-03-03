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

# Словник доступу: які ОСББ бачить кожен бухгалтер
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS acts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT,
            osbb TEXT,
            descr TEXT,
            photo_id TEXT,
            status TEXT,
            history TEXT
        )
    ''')
    conn.commit()
    conn.close()

# 3. КЛАВІАТУРА МЕНЮ (ПОСТІЙНА)
def get_main_menu():
    buttons = [
        [KeyboardButton(text="📋 Поточні акти"), KeyboardButton(text="📂 Архів")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    photo = State()

# 4. ОБРОБКА КОМАНДИ /START
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id == CHAIRMAN_ID or user_id in ACCOUNTANTS:
        await message.answer("👋 Бот готовий до роботи. Використовуйте меню нижче.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Доступ обмежено.")

# 5. СЦЕНАРІЙ СТВОРЕННЯ АКТУ
@dp.message(Command("new_act"), F.from_user.id == CHAIRMAN_ID)
async def start_new_act(message: types.Message, state: FSMContext):
    await message.answer("📝 Введіть номер акту (наприклад, 45-А):")
    await state.set_state(ActForm.number)

@dp.message(ActForm.number)
async def process_num(message: types.Message, state: FSMContext):
    await state.update_data(number=message.text)
    await message.answer("🏢 Вкажіть абревіатуру ОСББ (ВП-16, Е21, ОКПТ, В19):")
    await state.set_state(ActForm.osbb)

@dp.message(ActForm.osbb)
async def process_osbb(message: types.Message, state: FSMContext):
    osbb_name = message.text.strip()
    await state.update_data(osbb=osbb_name)
    await message.answer(f"📋 Опис робіт для {osbb_name}:")
    await state.set_state(ActForm.descr)

@dp.message(ActForm.descr)
async def process_descr(message: types.Message, state: FSMContext):
    await state.update_data(descr=message.text)
    await message.answer("📸 Надішліть ФОТО акту:")
    await state.set_state(ActForm.photo)

@dp.message(ActForm.photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id
    now = datetime.now().strftime("%d.%m %H:%M")
    history_entry = f"🆕 {now} - {message.from_user.full_name}: Створено."

    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("INSERT INTO acts (number, osbb, descr, photo_id, status, history) VALUES (?, ?, ?, ?, ?, ?)",
              (data['number'], data['osbb'], data['descr'], photo_id, "Очікує підтвердження", history_entry))
    db_id = c.lastrowid # Це технічний ID для кнопок
    conn.commit()
    conn.close()

    await message.answer(f"✅ Акт №{data['number']} збережено.")
    await state.clear()

    # ВІДПРАВКА ТІЛЬКИ ВІДПОВІДНОМУ БУХГАЛТЕРУ
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Отримала фото", callback_data=f"rec_{db_id}")
    ]])
    
    caption_text = f"📄 Акт №{data['number']}\n🏢 ОСББ: {data['osbb']}\nℹ️ Опис: {data['descr']}"

    for acc_id, allowed_osbb in ACCESS_MAP.items():
        if data['osbb'] in allowed_osbb:
            try:
                await bot.send_photo(acc_id, photo_id, caption=caption_text, reply_markup=kb)
            except Exception as e:
                print(f"Помилка відправки: {e}")

# 6. ЛОГІКА КНОПОК БУХГАЛТЕРА
@dp.callback_query(F.data.startswith("rec_"))
async def accountant_received(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    name = callback.from_user.full_name
    now = datetime.now().strftime("%d.%m %H:%M")
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT number FROM acts WHERE id=?", (db_id,))
    act_num = c.fetchone()[0] # Отримуємо ваш номер акту
    
    c.execute("UPDATE acts SET status='Фото отримано', history=history || ? WHERE id=?", 
              (f"\n✅ {now} - {name}: Фото отримано.", db_id))
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 Оплачено", callback_data=f"paid_{db_id}")
    ]])
    
    await callback.message.edit_caption(caption=callback.message.caption + f"\n\n📥 Отримано: {name}", reply_markup=kb)
    await bot.send_message(CHAIRMAN_ID, f"🔔 Бухгалтер {name} отримала фото акту №{act_num}")

@dp.callback_query(F.data.startswith("paid_"))
async def accountant_paid(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    name = callback.from_user.full_name
    now = datetime.now().strftime("%d.%m %H:%M")
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT number FROM acts WHERE id=?", (db_id,))
    act_num = c.fetchone()[0]
    
    c.execute("UPDATE acts SET status='Оплачено', history=history || ? WHERE id=?", 
              (f"\n💰 {now} - {name}: Оплачено.", db_id))
    conn.commit()
    conn.close()

    await callback.message.edit_caption(caption=callback.message.caption + f"\n✅ ОПЛАЧЕНО", reply_markup=None)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏁 Завершити роботу", callback_data=f"finish_{db_id}")
    ]])
    await bot.send_message(CHAIRMAN_ID, f"💰 Акт №{act_num} ОПЛАЧЕНО.\nЗакрити справу?", reply_markup=kb)

@dp.callback_query(F.data.startswith("finish_"))
async def finish_work(callback: CallbackQuery):
    db_id = callback.data.split("_")[1]
    now = datetime.now().strftime("%d.%m %H:%M")
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    c.execute("SELECT number, history FROM acts WHERE id=?", (db_id,))
    res = c.fetchone()
    final_history = res[1] + f"\n🏁 {now}: Завершено."
    
    c.execute("UPDATE acts SET status='Завершено', history=? WHERE id=?", (final_history, db_id))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text(f"🏁 Справу №{res[0]} закрито.")

# 7. ТАБЛИЦІ (ПОТОЧНІ ТА АРХІВ)
@dp.message(F.text.in_(["📋 Поточні акти", "📂 Архів"]))
async def show_table(message: types.Message):
    is_archive = "Архів" in message.text
    status_filter = "status = 'Завершено'" if is_archive else "status != 'Завершено'"
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    
    if message.from_user.id == CHAIRMAN_ID:
        c.execute(f"SELECT id, number, osbb, status FROM acts WHERE {status_filter} ORDER BY id DESC")
    else:
        allowed = ACCESS_MAP.get(message.from_user.id, [])
        placeholders = ','.join(['?'] * len(allowed))
        c.execute(f"SELECT id, number, osbb, status FROM acts WHERE {status_filter} AND osbb IN ({placeholders}) ORDER BY id DESC", allowed)
    
    rows = c.fetchall()
    conn.close()

    if not rows:
        return await message.answer("📭 Список порожній.")

    await message.answer(f"<b>{'📂 Архів' if is_archive else '📋 Поточні акти'}:</b>", parse_mode="HTML")

    for db_id, num, osbb, status in rows:
        kb = None
        # Якщо це Голова і це поточні акти — додаємо кнопку видалення
        if message.from_user.id == CHAIRMAN_ID and not is_archive:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Видалити", callback_data=f"del_{db_id}")
            ]])
        
        await message.answer(
            f"🔹 <b>№{num}</b> ({osbb})\nСтатус: <i>{status}</i>", 
            parse_mode="HTML", 
            reply_markup=kb
        )

# Обробник кнопки видалення
@dp.callback_query(F.data.startswith("del_"), F.from_user.id == CHAIRMAN_ID)
async def delete_act(callback: types.CallbackQuery):
    db_id = callback.data.split("_")[1]
    
    conn = sqlite3.connect('osbb_acts.db')
    c = conn.cursor()
    # Отримуємо номер для сповіщення перед видаленням
    c.execute("SELECT number FROM acts WHERE id=?", (db_id,))
    res = c.fetchone()
    
    if res:
        act_num = res[0]
        c.execute("DELETE FROM acts WHERE id=?", (db_id,))
        conn.commit()
        await callback.answer(f"❌ Акт №{act_num} видалено", show_alert=True)
        await callback.message.delete() # Видаляємо повідомлення зі списком
    
    conn.close()
  
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
