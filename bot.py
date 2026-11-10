import asyncio
import io
import logging
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("osbb_bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAIRMAN_ID_RAW = os.getenv("CHAIRMAN_ID", "").strip()
DB_PATH = os.getenv("OSBB_DB_PATH", "osbb_acts.db")
PAGE_SIZE = int(os.getenv("BOT_PAGE_SIZE", "5"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAIRMAN_ID_RAW.isdigit():
    raise RuntimeError("CHAIRMAN_ID must be numeric")

CHAIRMAN_ID = int(CHAIRMAN_ID_RAW)

# Read-only viewers: each sees only the status of acts whose description contains
# their keyword, across all OSBB. None of them are part of ACCESS_MAP on purpose —
# this keeps them locked out of every other flow (acts list/create, docs, jobs,
# salaries) because those all gate on can_access_osbb()/user_allowed_osbbs(),
# which return False/[] for anyone not in ACCESS_MAP or CHAIRMAN_ID.
# Add a new person here (same pattern) to give them the same one-button view.
READONLY_VIEWERS: dict[int, dict[str, str]] = {
    5186498707: {"name": "Сокол Микола Миколайович", "keyword": "Сокол"},
    396484643: {"name": "Денисюк Станіслав Станіславович", "keyword": "Денисюк"},
}

ACCESS_MAP = {
    5178201242: ["ВП-16", "Е21"],
    1332732213: ["ОКПТ", "ВЮ3"],
}

STAFF_CONFIG = {
    "ВП-16": {
        "Голова": 6000,
        "Бухгалтер": 3000,
        "Прибирання (Марія)": 9500,
        "Прибирання (Олег)": 3000,
        "Сантехнік": 2800,
    },
    "Е21": {"Голова": 6000, "Бухгалтер": 3000, "Сантехнік": 1000, "Двірник": "seasonal"},
    "ОКПТ": {
        "Голова": 4000,
        "Бухгалтер": 1000,
        "Нарахування ВТВК": 1000,
        "Двірник": 2000,
        "Обхідник": 1000,
        "Баки": 1000,
    },
    "ВЮ3": {"Голова": 4820, "Сантехнік": 2500, "Бухгалтер": 2500, "Бухгалтер (ФОП)": 500},
}

MONTHS_UA = {
    "01": "Січень",
    "02": "Лютий",
    "03": "Березень",
    "04": "Квітень",
    "05": "Травень",
    "06": "Червень",
    "07": "Липень",
    "08": "Серпень",
    "09": "Вересень",
    "10": "Жовтень",
    "11": "Листопад",
    "12": "Грудень",
}

VALID_TABLES = {"acts", "docs"}
FINAL_STATUSES = ("Завершено!", "Роботу завершено")
DB_WRITE_LOCK = asyncio.Lock()

bot = Bot(token=TOKEN)
dp = Dispatcher()


class ActForm(StatesGroup):
    number = State()
    osbb = State()
    descr = State()
    file = State()


class ActAttachPhotoForm(StatesGroup):
    photo = State()


class DocForm(StatesGroup):
    name = State()
    osbb = State()
    file = State()


class JobForm(StatesGroup):
    osbb = State()
    text = State()


class JobCommentForm(StatesGroup):
    text = State()


class SearchForm(StatesGroup):
    query = State()


class OsbbCb(CallbackData, prefix="osbb"):
    flow: str
    osbb: str


class PeriodCb(CallbackData, prefix="per"):
    flow: str
    step: Literal["year", "period"]
    osbb: str
    year: str = ""
    period: str = ""


class ItemCb(CallbackData, prefix="item"):
    cmd: Literal["ask", "yes", "no", "photo"]
    action: str
    table: str
    item_id: int


class SalaryCb(CallbackData, prefix="sal"):
    action: Literal["view", "hist", "list", "gen", "toggle", "back"]
    osbb: str = ""
