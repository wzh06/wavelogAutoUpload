import logging

from fastapi.testclient import TestClient

from app import db
from app.main import _AsciiConsoleFilter, app, configure_console_encoding


def test_user_configuration_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Wavelog ADI 自动上传" in response.text
        assert 'name="scan_interval"' in response.text
        assert 'value="1800"' in response.text
        assert "变更校验间隔（秒）" in response.text

        response = client.post(
            "/users",
            data={
                "name": "Alice",
                "directory": str(tmp_path / "logs"),
                "server_url": "https://radio.example",
                "api_key": "top-secret",
                "station_profile_id": "12",
                "scan_interval": "60",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Alice" in response.text
        assert "top-secret" not in response.text

        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE name='Alice'").fetchone()["id"]

        response = client.post(f"/users/{user_id}/toggle", follow_redirects=True)
        assert "已停用" in response.text

        response = client.post(f"/users/{user_id}/delete", follow_redirects=True)
        assert response.status_code == 200
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (user_id,)).fetchone()[0] == 0


def test_console_encoding_reconfigure_is_safe(monkeypatch):
    class Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    stdout = Stream()
    stderr = Stream()
    monkeypatch.setattr("app.main.sys.stdout", stdout)
    monkeypatch.setattr("app.main.sys.stderr", stderr)

    configure_console_encoding()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_console_log_filter_removes_localized_port_error():
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="[Errno 10048] error while attempting to bind: 通常每个套接字地址只允许使用一次",
        args=(),
        exc_info=None,
    )

    assert _AsciiConsoleFilter().filter(record) is True
    assert record.getMessage() == "Port 10086 is already in use (Windows error 10048)."
    assert all(ord(char) < 128 for char in record.getMessage())
