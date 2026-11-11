from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import sqlite3
import os

app = FastAPI(title="osbbDoc_bot Web Panel")
templates = Jinja2Templates(directory="templates")

DB_PATH = os.getenv("OSBB_DB_PATH", "osbb_acts.db")


def get_db_stats():
    """Збирає базову статистику для відображення на веб-панелі."""
    if not os.path.exists(DB_PATH):
        return {"acts_in_progress": 0, "acts_completed": 0, "osbb_count": 4}
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur_in_prog = conn.execute("SELECT count(*) FROM acts WHERE status NOT IN ('Завершено!', 'Роботу завершено')").fetchone()[0]
        cur_comp = conn.execute("SELECT count(*) FROM acts WHERE status IN ('Завершено!', 'Роботу завершено')").fetchone()[0]
        return {
            "acts_in_progress": cur_in_prog,
            "acts_completed": cur_comp,
            "osbb_count": 4  # ВП-16, ВЮ3, Е21, ОКПТ
        }
    except Exception:
        return {"acts_in_progress": 0, "acts_completed": 0, "osbb_count": 4}
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    stats = get_db_stats()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats
    })
