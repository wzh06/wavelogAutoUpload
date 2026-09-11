from contextlib import asynccontextmanager
import logging
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import DEFAULT_SCAN_INTERVAL, connect, init_db
from .service import service

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def configure_console_encoding() -> None:
    """Keep Python console output readable in Windows CMD and other terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # Some redirected or embedded streams do not allow reconfiguration.
                pass


configure_console_encoding()


class _AsciiConsoleFilter(logging.Filter):
    """Keep Windows CMD output free of localized/non-ASCII error text."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        exception_text = ""
        if record.exc_info and record.exc_info[1] is not None:
            exception_text = str(record.exc_info[1])
        if any(ord(char) > 127 for char in message) or any(ord(char) > 127 for char in exception_text):
            if "10048" in message:
                message = "Port 10086 is already in use (Windows error 10048)."
            else:
                message = message.encode("ascii", errors="replace").decode("ascii")
            record.msg = message
            record.args = ()
            # Avoid a localized exception traceback being appended by the formatter.
            record.exc_info = None
            record.exc_text = None
        return True


def configure_console_logging() -> None:
    """Prevent localized Windows errors from leaking into the CMD window."""
    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _AsciiConsoleFilter) for item in logger.filters):
            logger.addFilter(_AsciiConsoleFilter())


configure_console_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="Wavelog ADI Auto Upload", lifespan=lifespan)


async def _query_stations(server_url: str, api_key: str) -> JSONResponse:
    """Return station profiles without exposing the API key to the UI."""
    try:
        stations = await service.list_stations(server_url, api_key)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"stations": stations})


@app.get("/api/stations")
async def query_stations_get(server_url: str, api_key: str):
    return await _query_stations(server_url, api_key)


@app.post("/api/stations")
async def query_stations_post(server_url: str = Form(...), api_key: str = Form(...)):
    return await _query_stations(server_url, api_key)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with connect() as conn:
        users = conn.execute(
            "SELECT u.*, COUNT(f.id) total, SUM(CASE WHEN f.status='success' THEN 1 ELSE 0 END) success, SUM(CASE WHEN f.status IN ('retry','failed') THEN 1 ELSE 0 END) failed FROM users u LEFT JOIN files f ON f.user_id=u.id GROUP BY u.id ORDER BY u.id DESC"
        ).fetchall()
        files = conn.execute("SELECT f.*,u.name user_name FROM files f JOIN users u ON u.id=f.user_id ORDER BY f.updated_at DESC LIMIT 100").fetchall()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"users": users, "files": files})


@app.post("/users")
def create_user(name: str = Form(...), directory: str = Form(...), server_url: str = Form(...), api_key: str = Form(...), station_profile_id: str = Form(""), scan_interval: int = Form(DEFAULT_SCAN_INTERVAL)):
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
def edit_user(user_id: int, name: str = Form(...), directory: str = Form(...), server_url: str = Form(...), api_key: str = Form(""), station_profile_id: str = Form(""), scan_interval: int = Form(DEFAULT_SCAN_INTERVAL)):
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
