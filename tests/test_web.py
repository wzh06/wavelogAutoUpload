from fastapi.testclient import TestClient

from app import db
from app.main import app, configure_console_encoding


def test_user_configuration_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Wavelog ADI 自动上传" in response.text

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
