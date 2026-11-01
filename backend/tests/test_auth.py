from fastapi.testclient import TestClient

from app.main import app


def client():
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))


def test_open_when_no_password(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    c = client()
    assert c.get("/api/auth/status").json() == {"required": False, "authenticated": True}
    assert c.get("/api/jobs").status_code == 200


def test_password_gates_api_and_media(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse")
    c = client()
    assert c.get("/api/auth/status").json() == {"required": True, "authenticated": False}
    assert c.get("/api/jobs").status_code == 401
    assert c.get("/media/abc/x.mp4").status_code == 401
    assert c.get("/api/health").status_code == 200                         # health checks stay open
    assert c.get("/api/publish/oauth/instagram/callback?code=x&state=y").status_code == 400   # reachable, state enforced
    assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert c.get("/api/jobs").status_code == 200
    assert c.get("/api/auth/status").json()["authenticated"] is True
    monkeypatch.setenv("APP_PASSWORD", "rotated")                          # changing the password ends sessions
    assert c.get("/api/jobs").status_code == 401
    c.post("/api/auth/logout")
