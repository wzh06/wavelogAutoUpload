from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import connect, init_db
from .service import service

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="Wavelog ADI Auto Upload", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with connect() as conn:
        users = conn.execute(
            "SELECT u.*, COUNT(f.id) total, SUM(CASE WHEN f.status='success' THEN 1 ELSE 0 END) success, SUM(CASE WHEN f.status IN ('retry','failed') THEN 1 ELSE 0 END) failed FROM users u LEFT JOIN files f ON f.user_id=u.id GROUP BY u.id ORDER BY u.id DESC"
        ).fetchall()
        files = conn.execute("SELECT f.*,u.name user_name FROM files f JOIN users u ON u.id=f.user_id ORDER BY f.updated_at DESC LIMIT 100").fetchall()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"users": users, "files": files})


@app.post("/users")
def create_user(name: str = Form(...), directory: str = Form(...), server_url: str = Form(...), api_key: str = Form(...), station_profile_id: str = Form(""), scan_interval: int = Form(60)):
    with connect() as conn:
        conn.execute("INSERT INTO users(name,directory,server_url,api_key,station_profile_id,scan_interval) VALUES(?,?,?,?,?,?)", (name.strip(), directory.strip(), server_url.strip(), api_key.strip(), station_profile_id.strip(), max(5, scan_interval)))
    return RedirectResponse("/", status_code=303)


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(request: Request, user_id: int):
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="edit_user.html", context={"user": user})


@app.post("/users/{user_id}/edit")
def edit_user(user_id: int, name: str = Form(...), directory: str = Form(...), server_url: str = Form(...), api_key: str = Form(""), station_profile_id: str = Form(""), scan_interval: int = Form(60)):
    with connect() as conn:
        if api_key.strip():
            conn.execute("UPDATE users SET name=?,directory=?,server_url=?,api_key=?,station_profile_id=?,scan_interval=? WHERE id=?", (name.strip(), directory.strip(), server_url.strip(), api_key.strip(), station_profile_id.strip(), max(5, scan_interval), user_id))
        else:
            conn.execute("UPDATE users SET name=?,directory=?,server_url=?,station_profile_id=?,scan_interval=? WHERE id=?", (name.strip(), directory.strip(), server_url.strip(), station_profile_id.strip(), max(5, scan_interval), user_id))
    service.next_scans.pop(user_id, None)
    return RedirectResponse("/", status_code=303)


@app.post("/users/{user_id}/toggle")
def toggle_user(user_id: int):
    with connect() as conn:
        conn.execute("UPDATE users SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (user_id,))
    service.next_scans.pop(user_id, None)
    return RedirectResponse("/", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    service.next_scans.pop(user_id, None)
    return RedirectResponse("/", status_code=303)


@app.post("/files/{file_id}/retry")
async def retry_file(file_id: int):
    await service.retry(file_id)
    return RedirectResponse("/", status_code=303)


@app.post("/scan")
async def scan_now():
    service.scan_all(force=True)
    return RedirectResponse("/", status_code=303)
