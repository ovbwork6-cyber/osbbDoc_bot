from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import sqlite3
import os

app = FastAPI(title="osbbDoc_bot Web Panel")
templates = Jinja2Templates(directory="templates")

DB_PATH = os.getenv("OSBB_DB_PATH", "osbb_acts.db")


def get_dashboard_data():
    """Витягує дані про організації (ОСББ/ОК) та зведені статуси актів із бази даних."""
    if not os.path.exists(DB_PATH):
        return [], []
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Отримуємо список ОСББ/ОК (якщо у вас є окрема таблиця для них, або беремо унікальні з актів/квартир)
        # Примітка: якщо таблиця організацій називається інакше, змініть запит під вашу структуру
        organizations = []
        try:
            cur_orgs = conn.execute("SELECT id, name, apartment_count FROM organizations").fetchall()
            organizations = [dict(row) for row in cur_orgs]
        except Exception:
            # Запасний варіант, якщо таблиці організацій ще немає — витягуємо унікальні ОСББ із табл. acts або apartments
            try:
                cur_osbb = conn.execute("SELECT DISTINCT osbb FROM acts").fetchall()
                organizations = [{"id": i+1, "name": row["osbb"], "apartment_count": 0} for i, row in enumerate(cur_osbb)]
            except Exception:
                organizations = []

        # Збираємо статуси актів та їх кількість
        acts = []
        try:
            cur_acts = conn.execute("SELECT status, count(*) as count FROM acts GROUP BY status").fetchall()
            acts = [dict(row) for row in cur_acts]
        except Exception:
            acts = []

        return organizations, acts
    except Exception:
        return [], []
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    organizations, acts = get_dashboard_data()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "organizations": organizations,
        "acts": acts
    })
