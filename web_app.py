import os
import io
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from docx import Document
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


@app.get("/generate/appeal/{org_id}")
async def generate_appeal(org_id: int):
    org = await db_layer.db_fetch_one(
        "SELECT name, address, edrpou, head_name FROM organizations WHERE id=?", 
        (org_id,)
    )
    if not org:
        return {"error": "Організацію не знайдено"}
    
    # Якщо поля в базі пусті, ставимо заглушки, щоб документ не зламався
    address = org['address'] or "[Адреса не вказана]"
    head_name = org['head_name'] or "[ПІБ Голови не вказано]"
    
    doc = Document()
    doc.add_paragraph("ОБ’ЄДНАННЯ СПІВВЛАСНИКІВ БАГАТОКВАРТИРНОГО")
    doc.add_paragraph(f"БУДИНКУ «{org['name']}»")
    doc.add_paragraph(address)
    
    doc.add_paragraph(f"Вих. № ... від {datetime.now().strftime('%d.%m.%Y')} р.")
    doc.add_paragraph("Міському голові м. Вараш\nМензул Олександру Павловичу")
    
    doc.add_paragraph("\nПрошу провести санітарну обрізку дерев на прибудинковій території...")
    
    doc.add_paragraph(f"\nЗ повагою голова ОСББ «{org['name']}»                {head_name}")
    
    file_stream = io.BytesIO()
    doc.save(file_stream)
    file_stream.seek(0)
    
    return FileResponse(
        file_stream, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", 
        filename=f"Звернення_{org['name']}.docx"
    )


@app.get("/generate/act/{fop_id}/{osbb_id}")
async def generate_act(fop_id: int, osbb_id: int):
    # Тягнемо реквізити обох організацій
    fop = await db_layer.db_fetch_one("SELECT name, edrpou, address, iban, head_name FROM organizations WHERE id=?", (fop_id,))
    osbb = await db_layer.db_fetch_one("SELECT name, edrpou, address, head_name FROM organizations WHERE id=?", (osbb_id,))
    
    if not fop or not osbb:
        return {"error": "Організації не знайдено"}
        
    fop_address = fop['address'] or "[Адреса ФОП]"
    fop_iban = fop['iban'] or "[IBAN ФОП]"
    fop_head = fop['head_name'] or fop['name']
    
    osbb_address = osbb['address'] or "[Адреса ОСББ]"
    osbb_head = osbb['head_name'] or "[Голова ОСББ]"
        
    doc = Document()
    doc.add_heading(f"АКТ НАДАННЯ ПОСЛУГ № 1", 1)
    doc.add_paragraph(f"від {datetime.now().strftime('%d.%m.%Y')} р.")
    
    # Реквізити сторін
    doc.add_paragraph(f"Виконавець: ФОП {fop['name']}\nІПН/ЄДРПОУ: {fop['edrpou']}\nАдреса: {fop_address}\nIBAN: {fop_iban}")
    doc.add_paragraph(f"Замовник: ОСББ «{osbb['name']}»\nЄДРПОУ: {osbb['edrpou']}\nАдреса: {osbb_address}")
    
    doc.add_paragraph("\nМи, що нижче підписалися, склали цей акт про те, що Виконавець надав, а Замовник прийняв наступні послуги:")
    
    # Створення таблиці послуг
    table = doc.add_table(rows=2, cols=3)
    table.style = 'Table Grid'
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = '№'
    hdr_cells[1].text = 'Назва послуги'
    hdr_cells[2].text = 'Сума'
    
    row_cells = table.rows[1].cells
    row_cells[0].text = '1'
    row_cells[1].text = 'Технічне обслуговування згідно договору'
    row_cells[2].text = '1000.00 грн'
    
    # Підписи
    doc.add_paragraph(f"\nВиконавець: _________________ {fop_head}")
    doc.add_paragraph(f"Замовник: _________________ {osbb_head}")
    
    file_stream = io.BytesIO()
    doc.save(file_stream)
    file_stream.seek(0)
    
    return FileResponse(
        file_stream, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", 
        filename=f"Акт_{fop['name']}_{osbb['name']}.docx"
    )