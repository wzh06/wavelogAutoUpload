import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .db import connect

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()


class UploadService:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.scan_task: asyncio.Task | None = None
        self.upload_task: asyncio.Task | None = None
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.queued: set[int] = set()
        self.next_scans: dict[int, float] = {}

    async def start(self) -> None:
        self.stop_event.clear()
        self.scan_task = asyncio.create_task(self.scan_loop())
        self.upload_task = asyncio.create_task(self.upload_loop())

    async def stop(self) -> None:
        self.stop_event.set()
        for task in (self.scan_task, self.upload_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.scan_task, self.upload_task) if t), return_exceptions=True)

    async def scan_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.scan_all()
            except Exception:
                logger.exception("scan failed")
            await asyncio.sleep(5)

    def scan_all(self, force: bool = False) -> None:
        with connect() as conn:
            users = conn.execute("SELECT * FROM users WHERE enabled=1").fetchall()
            for user in users:
                current_time = time.time()
                if not force and self.next_scans.get(user["id"], 0) > current_time:
                    continue
                self.next_scans[user["id"]] = current_time + max(5, int(user["scan_interval"] or 60))
                directory = Path(user["directory"])
                if not directory.exists():
                    conn.execute(
                        "UPDATE users SET last_scan_at=?,last_scan_error=? WHERE id=?",
                        (now_iso(), "目录不存在或无法访问", user["id"]),
                    )
                    continue
                try:
                    files = [path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".adi"]
                except OSError as exc:
                    conn.execute("UPDATE users SET last_scan_at=?,last_scan_error=? WHERE id=?", (now_iso(), str(exc), user["id"]))
                    continue
                conn.execute("UPDATE users SET last_scan_at=?,last_scan_error=NULL WHERE id=?", (now_iso(), user["id"]))
                for path in files:
                    try:
                        stat = path.stat()
                        fp = fingerprint(path)
                    except OSError:
                        continue
                    row = conn.execute("SELECT * FROM files WHERE user_id=? AND path=?", (user["id"], str(path))).fetchone()
                    if row is None:
                        cur = conn.execute(
                            "INSERT INTO files(user_id,path,size,mtime,fingerprint,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (user["id"], str(path), stat.st_size, stat.st_mtime, fp, "pending", now_iso()),
                        )
                        file_id = cur.lastrowid
                        self.enqueue(file_id)
                    elif row["fingerprint"] != fp or row["status"] in ("success", "pending", "uploading"):
                        conn.execute(
                            "UPDATE files SET size=?,mtime=?,fingerprint=?,status='pending',attempts=0,last_error=NULL,next_retry=NULL,updated_at=? WHERE id=?",
                            (stat.st_size, stat.st_mtime, fp, now_iso(), row["id"]),
                        )
                        self.enqueue(row["id"])
                    elif row["status"] == "retry" and (row["next_retry"] or 0) <= current_time:
                        self.enqueue(row["id"])

    def enqueue(self, file_id: int) -> None:
        if file_id not in self.queued:
            self.queued.add(file_id)
            self.queue.put_nowait(file_id)

    async def upload_loop(self) -> None:
        while not self.stop_event.is_set():
            file_id = await self.queue.get()
            try:
                await self.upload_file(file_id)
            except Exception:
                logger.exception("upload failed for file %s", file_id)
            finally:
                self.queued.discard(file_id)
                self.queue.task_done()

    async def upload_file(self, file_id: int) -> None:
        with connect() as conn:
            row = conn.execute(
                "SELECT f.*, u.server_url,u.api_key,u.station_profile_id,u.enabled FROM files f JOIN users u ON u.id=f.user_id WHERE f.id=?",
                (file_id,),
            ).fetchone()
        if not row or not row["enabled"]:
            return
        path = Path(row["path"])
        if not path.exists():
            self.mark_failed(file_id, "文件不存在")
            return
        self.mark_status(file_id, "uploading")
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                station_id = row["station_profile_id"] or await self.resolve_station_id(client, row["server_url"], row["api_key"])
                payload = {
                    "key": row["api_key"],
                    "station_profile_id": station_id,
                    "type": "adif",
                    "string": path.read_text(encoding="utf-8-sig", errors="replace"),
                }
                url = row["server_url"].rstrip("/") + "/index.php/api/qso"
                response = await client.post(url, json=payload)
            if response.is_success:
                with connect() as conn:
                    conn.execute("UPDATE files SET status='success',attempts=0,last_error=NULL,next_retry=NULL,upload_count=upload_count+1,last_uploaded_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), file_id))
            else:
                self.mark_failed(file_id, f"HTTP {response.status_code}: {response.text[:500]}")
        except Exception as exc:
            self.mark_failed(file_id, str(exc))

    async def resolve_station_id(self, client: httpx.AsyncClient, server_url: str, api_key: str) -> str:
        url = server_url.rstrip("/") + "/index.php/api/station_info/" + quote(api_key, safe="")
        response = await client.get(url)
        response.raise_for_status()
        stations = response.json()
        if not isinstance(stations, list) or not stations:
            raise ValueError("API key 下没有可用的电台位置")
        active = [station for station in stations if str(station.get("station_active", "")) == "1"]
        selected = active[0] if active else stations[0]
        station_id = selected.get("station_id")
        if station_id is None:
            raise ValueError("Wavelog 返回的电台位置缺少 station_id")
        return str(station_id)

    def mark_status(self, file_id: int, status: str) -> None:
        with connect() as conn:
            conn.execute("UPDATE files SET status=?,updated_at=? WHERE id=?", (status, now_iso(), file_id))

    def mark_failed(self, file_id: int, error: str) -> None:
        with connect() as conn:
            row = conn.execute("SELECT attempts FROM files WHERE id=?", (file_id,)).fetchone()
            attempts = int(row["attempts"] if row else 0) + 1
            if attempts >= 5:
                conn.execute(
                    "UPDATE files SET status='failed',attempts=?,last_error=?,next_retry=NULL,updated_at=? WHERE id=?",
                    (attempts, error[:1000], now_iso(), file_id),
                )
                return
            delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
            conn.execute("UPDATE files SET status='retry',attempts=?,last_error=?,next_retry=?,updated_at=? WHERE id=?", (attempts, error[:1000], time.time() + delay, now_iso(), file_id))

    async def retry(self, file_id: int) -> None:
        with connect() as conn:
            conn.execute("UPDATE files SET status='pending',attempts=0,last_error=NULL,next_retry=NULL,updated_at=? WHERE id=?", (now_iso(), file_id))
        self.enqueue(file_id)


service = UploadService()
