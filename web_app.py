import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import db_layer

app = FastAPI(title="ОСББ Вараш - CRM")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def startup_event():
    chairman_id = int(os.getenv("SUPERADMIN_ID", "0"))
    await db_layer.init_db(chairman_id)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    organizations = await db_layer.db_fetch_all("SELECT id, name, apartment_count FROM organizations WHERE active=1 ORDER BY name")
    acts = await db_layer.db_fetch_all("SELECT status, count(id) as count FROM acts GROUP BY status")
    return templates.TemplateResponse(
        "dashboard.html", 
        {
            "request": request, 
            "organizations": organizations,
            "acts": acts
        }
    )