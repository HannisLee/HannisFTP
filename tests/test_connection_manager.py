from __future__ import annotations

import asyncssh
import pytest

from app.core.config import Settings
from app.services.connection_manager import (
    ConnectionManager,
    HostKeyMismatch,
    HostKeyVerificationClient,
)


@pytest.fixture
def manager(tmp_path):
    settings = Settings(local_root=str(tmp_path), host="127.0.0.1", port=8000)
    manager = ConnectionManager(settings)
    manager.known_hosts_path = tmp_path / "known_hosts"
    return manager


def test_host_key_verification_requires_confirmation(manager):
    client = HostKeyVerificationClient(manager, confirm=False)
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    assert client.validate_host_public_key("example.com", "192.0.2.10", 22, key) is False
    assert client.pending is not None
    assert client.pending.host == "example.com"
    assert client.pending.fingerprint.startswith("SHA256:")


def test_host_key_confirmation_appends_known_host(manager):
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    client = HostKeyVerificationClient(manager, confirm=True, fingerprint=key.get_fingerprint())
    assert client.validate_host_public_key("example.com", "192.0.2.10", 22, key) is True
    assert manager.known_hosts_path.exists()
    trusted = asyncssh.read_known_hosts([str(manager.known_hosts_path)]).match(
        "example.com", "192.0.2.10", None
    )[0]
    assert key in trusted


def test_host_key_change_is_rejected(manager):
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    manager._append_known_host("example.com", 22, key)
    changed = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    client = HostKeyVerificationClient(manager, confirm=True)
    assert client.validate_host_public_key("example.com", "192.0.2.10", 22, changed) is False
    assert isinstance(client.error, HostKeyMismatch)


async def test_ssh_alias_config_uses_asyncssh_config_argument(monkeypatch, manager):
    from app.models import ProfileOut, utc_now

    captured = {}

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    profile = ProfileOut(
        id="ssh-config:alias", name="alias", host="alias", port=22, username=None,
        auth_method="auto", private_key_path=None, remote_root="~", connect_timeout=10,
        keepalive_interval=15, ssh_alias="alias", source="ssh_config",
        created_at=utc_now(), updated_at=utc_now(),
    )
    client = HostKeyVerificationClient(manager, False)
    await manager._connect(profile, None, None, client)
    from pathlib import Path
    assert captured["config"] == str(Path(manager.settings.ssh_config_path).expanduser())
    assert "port" not in captured
    assert "config_path" not in captured


def test_revoked_host_key_cannot_be_confirmed(manager):
    key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    manager.known_hosts_path.write_text("@revoked example.com " + key.export_public_key().decode(), encoding="ascii")
    client = HostKeyVerificationClient(manager, True, fingerprint=key.get_fingerprint())
    assert client.validate_host_public_key("example.com", "192.0.2.10", 22, key) is False
    assert isinstance(client.error, HostKeyMismatch)
