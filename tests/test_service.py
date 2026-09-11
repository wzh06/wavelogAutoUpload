import asyncio

import httpx

from app import db
from app.service import UploadService, fingerprint


def test_fingerprint_changes(tmp_path):
    path = tmp_path / "a.adi"
    path.write_text("QSO: one")
    first = fingerprint(path)
    path.write_text("QSO: two")
    assert fingerprint(path) != first


def test_active_station_is_selected():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/index.php/api/station_info/secret" in str(request.url)
        return httpx.Response(200, json=[
            {"station_id": "10", "station_active": "0"},
            {"station_id": "20", "station_active": "1"},
        ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UploadService().resolve_station_id(client, "https://radio.example", "secret")

    station_id = asyncio.run(run())
    assert station_id == "20"


def test_station_falls_back_to_first():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"station_id": 42, "station_active": 0}])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UploadService().resolve_station_id(client, "https://radio.example", "secret")

    station_id = asyncio.run(run())
    assert station_id == "42"


def test_upload_uses_wavelog_qso_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "upload.db")
    db.init_db()
    adi_path = tmp_path / "log.adi"
    adi_path.write_text("<CALL:5>BA1AA<EOR>", encoding="utf-8")
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users(name,directory,server_url,api_key,station_profile_id) VALUES(?,?,?,?,?)",
            ("Alice", str(tmp_path), "https://radio.example/", "secret", "7"),
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files(user_id,path,fingerprint) VALUES(?,?,?)",
            (user_id, str(adi_path), fingerprint(adi_path)),
        ).lastrowid

    sent = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            sent.update(url=url, payload=json)
            return httpx.Response(201, json={"status": "created"})

    monkeypatch.setattr("app.service.httpx.AsyncClient", FakeClient)
    asyncio.run(UploadService().upload_file(file_id))

    assert sent["url"] == "https://radio.example/index.php/api/qso"
    assert sent["payload"] == {
        "key": "secret",
        "station_profile_id": "7",
        "type": "adif",
        "string": "<CALL:5>BA1AA<EOR>",
    }
    with db.connect() as conn:
        uploaded = conn.execute("SELECT status,upload_count FROM files WHERE id=?", (file_id,)).fetchone()
    assert tuple(uploaded) == ("success", 1)


def test_duplicate_qso_is_not_shown_as_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duplicate.db")
    db.init_db()
    adi_path = tmp_path / "log.adi"
    adi_path.write_text("<CALL:5>BA1AA<EOR>", encoding="utf-8")
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users(name,directory,server_url,api_key,station_profile_id) VALUES(?,?,?,?,?)",
            ("Alice", str(tmp_path), "https://radio.example", "secret", "7"),
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files(user_id,path,fingerprint) VALUES(?,?,?)",
            (user_id, str(adi_path), fingerprint(adi_path)),
        ).lastrowid

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            return httpx.Response(400, json={"status": "error", "message": "Duplicate QSO"})

    monkeypatch.setattr("app.service.httpx.AsyncClient", FakeClient)
    asyncio.run(UploadService().upload_file(file_id))

    with db.connect() as conn:
        uploaded = conn.execute("SELECT status,last_error,attempts FROM files WHERE id=?", (file_id,)).fetchone()
    assert tuple(uploaded) == ("success", None, 0)


def test_non_duplicate_api_error_is_still_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api-error.db")
    db.init_db()
    adi_path = tmp_path / "log.adi"
    adi_path.write_text("<CALL:5>BA1AA<EOR>", encoding="utf-8")
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users(name,directory,server_url,api_key,station_profile_id) VALUES(?,?,?,?,?)",
            ("Alice", str(tmp_path), "https://radio.example", "secret", "7"),
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files(user_id,path,fingerprint) VALUES(?,?,?)",
            (user_id, str(adi_path), fingerprint(adi_path)),
        ).lastrowid

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            return httpx.Response(400, json={"status": "error", "message": "Invalid API key"})

    monkeypatch.setattr("app.service.httpx.AsyncClient", FakeClient)
    asyncio.run(UploadService().upload_file(file_id))

    with db.connect() as conn:
        failed = conn.execute("SELECT status,last_error,attempts FROM files WHERE id=?", (file_id,)).fetchone()
    assert failed[0] == "retry"
    assert "Invalid API key" in failed[1]
    assert failed[2] == 1


def test_old_duplicate_errors_are_cleared_but_other_errors_remain(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old-errors.db")
    db.init_db()
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users(name,directory,server_url,api_key) VALUES(?,?,?,?)",
            ("Alice", str(tmp_path), "https://radio.example", "secret"),
        ).lastrowid
        duplicate_id = conn.execute(
            "INSERT INTO files(user_id,path,status,attempts,last_error) VALUES(?,?,'failed',5,?)",
            (user_id, str(tmp_path / "duplicate.adi"), "HTTP 400: Duplicate QSO"),
        ).lastrowid
        other_id = conn.execute(
            "INSERT INTO files(user_id,path,status,attempts,last_error) VALUES(?,?,'failed',5,?)",
            (user_id, str(tmp_path / "invalid.adi"), "HTTP 401: Invalid API key"),
        ).lastrowid

    UploadService().clear_duplicate_failures()

    with db.connect() as conn:
        duplicate = conn.execute("SELECT status,attempts,last_error FROM files WHERE id=?", (duplicate_id,)).fetchone()
        other = conn.execute("SELECT status,attempts,last_error FROM files WHERE id=?", (other_id,)).fetchone()
    assert tuple(duplicate) == ("success", 0, None)
    assert tuple(other) == ("failed", 5, "HTTP 401: Invalid API key")


def test_scan_recovers_pending_but_not_final_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scan.db")
    db.init_db()
    adi_path = tmp_path / "log.adi"
    failed_path = tmp_path / "failed.adi"
    adi_path.write_text("<CALL:5>BA1AA<EOR>", encoding="utf-8")
    failed_path.write_text("<CALL:5>BA2BB<EOR>", encoding="utf-8")
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users(name,directory,server_url,api_key) VALUES(?,?,?,?)",
            ("Alice", str(tmp_path), "https://radio.example", "secret"),
        ).lastrowid
        pending_id = conn.execute(
            "INSERT INTO files(user_id,path,fingerprint,status) VALUES(?,?,?,'pending')",
            (user_id, str(adi_path), fingerprint(adi_path)),
        ).lastrowid
        failed_id = conn.execute(
            "INSERT INTO files(user_id,path,fingerprint,status,attempts) VALUES(?,?,?,?,?)",
            (user_id, str(failed_path), fingerprint(failed_path), "failed", 5),
        ).lastrowid

    service = UploadService()
    service.scan_all(force=True)
    assert pending_id in service.queued
    assert failed_id not in service.queued
