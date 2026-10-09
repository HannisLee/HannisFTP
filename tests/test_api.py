from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, lifespan


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINISFTP_LOCAL_ROOT", str(tmp_path))
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
