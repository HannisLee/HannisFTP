from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app


@pytest.fixture
def client(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MINISFTP_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("MINISFTP_DATA_PATH", str(tmp_path_factory.mktemp("app-data")))
    monkeypatch.setenv("MINISFTP_SSH_CONFIG_PATH", str(tmp_path / "ssh-config"))
    monkeypatch.setenv("MINISFTP_TRANSFER_CONCURRENCY", "1")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        state = app.state.state
        test_client.headers.update({"X-MiniSFTP-Token": state.security.token})
        yield test_client


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "LOCAL" in response.text and "REMOTE" in response.text


def test_local_api_lists_root(client, tmp_path):
    (tmp_path / "file.txt").write_text("hello")
    response = client.get("/api/files/local", params={"path": str(tmp_path)})
    assert response.status_code == 200
    data = response.json()
    assert data["entries"][0]["name"] == "file.txt"
    assert data["total_files"] == 1


def test_modification_requires_token(client, tmp_path):
    token = client.headers["X-MiniSFTP-Token"]
    response = client.post("/api/files/local/mkdir", json={"path": f"{tmp_path}/new"}, headers={"X-MiniSFTP-Token": "invalid"})
    assert response.status_code == 401
    response = client.post(
        "/api/files/local/mkdir",
        json={"path": f"{tmp_path}/new"},
        headers={"X-MiniSFTP-Token": token},
    )
    assert response.status_code == 201


def test_ssh_hosts_endpoint(client):
    response = client.get("/api/ssh-hosts")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_windows_native_mkdir_and_invalid_names(client, tmp_path):
    response = client.post("/api/files/local/mkdir", json={"path": str(tmp_path / "native folder")})
    assert response.status_code == 201
    assert (tmp_path / "native folder").is_dir()
    response = client.post("/api/files/local/rename", json={"path": str(tmp_path / "native folder"), "new_name": ".."})
    assert response.status_code == 400
    assert (tmp_path / "native folder").is_dir()


def test_invalid_profile_update_and_missing_profile(client):
    profile = client.post("/api/profiles", json={"name": "server", "host": "example.com"}).json()
    response = client.patch(f"/api/profiles/{profile['id']}", json={"port": None})
    assert response.status_code == 422
    assert client.get("/api/profiles").json()[0]["port"] == 22
    response = client.post("/api/connections", json={"profile_id": "missing"})
    assert response.status_code == 404


def test_cross_origin_and_unexpected_host_rejected(client, tmp_path):
    assert client.get("/api/files/local", headers={"Host": "attacker.example"}).status_code == 421
    assert client.get("/api/files/local", headers={"Origin": "https://attacker.example"}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/events?token=" + client.headers["X-MiniSFTP-Token"], headers={"Origin": "http://127.0.0.1:9999"}):
            pass
    assert getattr(exc.value, "code", None) == 1008


def test_websocket_connects_with_valid_token(client):
    token = client.headers["X-MiniSFTP-Token"]
    with client.websocket_connect(f"/ws/events?token={token}", headers={"host": "127.0.0.1"}) as websocket:
        # The endpoint is a subscription stream; successful handshake is sufficient here.
        assert websocket is not None


def test_websocket_rejects_invalid_token(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events?token=invalid") as websocket:
            websocket.receive()
