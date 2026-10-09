from __future__ import annotations

import asyncio
import os
import stat

import asyncssh
import pytest

from app.core.config import Settings
from app.models import ProfileOut, TransferCreate, utc_now
from app.services.connection_manager import ConnectionManager, HostKeyConfirmationRequired, HostKeyMismatch
from app.services.local_file_service import LocalFileService
from app.services.progress_manager import ProgressManager
from app.services.transfer_manager import TransferManager
from app.storage import Storage


class TestSSHServer(asyncssh.SSHServer):
    __test__ = False

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "test-user" and password == "test-password"


@pytest.fixture
async def sftp_server(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    key = asyncssh.generate_private_key("ssh-ed25519")
    listener = await asyncssh.create_server(
        TestSSHServer, "127.0.0.1", 0, server_host_keys=[key],
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(remote)),
    )
    profile = ProfileOut(
        id="test-server", name="Test server", host="127.0.0.1", port=listener.get_port(),
        username="test-user", auth_method="password", remote_root="~", source="manual",
        created_at=utc_now(), updated_at=utc_now(),
    )
    try:
        yield remote, key, profile
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.fixture
async def connected(tmp_path, sftp_server):
    remote, key, profile = sftp_server
    local = tmp_path / "local"
    local.mkdir()
    settings = Settings(local_root=str(local), data_path=str(tmp_path / "data"), chunk_size=65536)
    manager = ConnectionManager(settings)
    with pytest.raises(HostKeyConfirmationRequired):
        await manager.connect_profile(profile, password="test-password")
    session = await manager.connect_profile(
        profile, password="test-password", confirm_host_key=True,
        host_key_fingerprint=key.get_fingerprint(),
    )
    try:
        yield settings, manager, session, local, remote
    finally:
        await manager.close_all()


@pytest.fixture
async def transfer_manager(connected):
    settings, connections, session, local, remote = connected
    storage = Storage(settings.database_path)
    await storage.connect()
    transfers = TransferManager(settings, storage, connections, LocalFileService(settings), ProgressManager())
    await transfers.start()
    try:
        yield transfers, session, local, remote
    finally:
        await transfers.shutdown()
        await storage.close()


async def finished(transfers, model):
    async def wait():
        while model.status in {"queued", "running", "pausing"}:
            await asyncio.sleep(0.01)
        return model
    result = await asyncio.wait_for(wait(), 15)
    assert result.status == "completed", result.error_message
    return result


async def test_connection_root_reuse_and_listing(connected):
    settings, manager, session, local, remote = connected
    assert session.remote_root == "/"
    assert session.out().username == "test-user"
    assert session.out().connected_at == session.out().connected_at
    assert len(manager.list()) == 1
    (remote / "folder").mkdir()
    (remote / "file.bin").write_bytes(b"\x00\xff")
    listing = await session.service.list_dir("~")
    assert listing.parent is None
    assert [entry.name for entry in listing.entries] == ["folder", "file.bin"]
    assert listing.entries[0].is_dir
    # Repeated listing must reuse the actual AsyncSSH SFTPClient API.
    assert (await session.service.list_dir("/")).total_files == 1


async def test_single_binary_roundtrip_and_atomic_overwrite(transfer_manager):
    transfers, session, local, remote = transfer_manager
    source = local / "binary.bin"
    payload = os.urandom(300001)
    source.write_bytes(payload)
    (remote / source.name).write_bytes(b"old")
    upload = await transfers.create(TransferCreate(
        direction="upload", source_path=str(source), destination_path="/",
        connection_id=session.id, conflict_strategy="overwrite",
    ))
    await finished(transfers, upload)
    assert (remote / source.name).read_bytes() == payload
    destination = local / "download"
    destination.mkdir()
    download = await transfers.create(TransferCreate(
        direction="download", source_path="/binary.bin", destination_path=str(destination),
        connection_id=session.id, conflict_strategy="ask",
    ))
    await finished(transfers, download)
    assert (destination / source.name).read_bytes() == payload
    assert not list(remote.glob("*.part"))


async def test_nested_unicode_and_empty_directory_roundtrip(transfer_manager):
    transfers, session, local, remote = transfer_manager
    source = local / "项目 A"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "nested" / "文件 名.bin").write_bytes(b"\x00\xff\xfe")
    (source / "zero.txt").write_bytes(b"")
    upload = await transfers.create(TransferCreate(
        direction="upload", source_path=str(source), destination_path="/",
        connection_id=session.id, conflict_strategy="ask",
    ))
    await finished(transfers, upload)
    destination = local / "download"
    destination.mkdir()
    download = await transfers.create(TransferCreate(
        direction="download", source_path="/项目 A", destination_path=str(destination),
        connection_id=session.id, conflict_strategy="ask",
    ))
    await finished(transfers, download)
    assert (destination / source.name / "empty").is_dir()
    assert (destination / source.name / "nested" / "文件 名.bin").read_bytes() == b"\x00\xff\xfe"
    empty = local / "empty-root"
    empty.mkdir()
    task = await transfers.create(TransferCreate(
        direction="upload", source_path=str(empty), destination_path="/",
        connection_id=session.id, conflict_strategy="ask",
    ))
    await finished(transfers, task)
    assert (remote / empty.name).is_dir()


async def test_remote_root_boundaries_and_names(connected):
    settings, manager, session, local, remote = connected
    (remote / "restricted").mkdir()
    await session.service.set_allowed_root("/restricted")
    for path in ["/", "/restricted/..", "/elsewhere"]:
        with pytest.raises(ValueError):
            await session.service.list_dir(path)
    for name in ["..", ".", "../escape", "/escape", ""]:
        with pytest.raises(ValueError):
            await session.service.mkdir("/restricted", name)
    with pytest.raises(ValueError):
        await session.service.delete("/restricted", recursive=True)
    with pytest.raises(ValueError):
        await session.service.rename("/restricted", "other")


async def test_confirmed_fingerprint_change_is_rejected(tmp_path, sftp_server):
    remote, key, profile = sftp_server
    manager = ConnectionManager(Settings(data_path=str(tmp_path / "data")))
    with pytest.raises(HostKeyMismatch):
        await manager.connect_profile(
            profile, password="test-password", confirm_host_key=True,
            host_key_fingerprint=asyncssh.generate_private_key("ssh-ed25519").get_fingerprint(),
        )
    assert not manager.known_hosts_path.exists()


async def test_ssh_config_nondefault_port_and_reuse(tmp_path, sftp_server):
    remote, key, profile = sftp_server
    config = tmp_path / "config"
    config.write_text(f"Host local-alias\n HostName 127.0.0.1\n Port {profile.port}\n User test-user\n", encoding="utf-8")
    manager = ConnectionManager(Settings(data_path=str(tmp_path / "data"), ssh_config_path=str(config)))
    manager._append_known_host("127.0.0.1", profile.port, key)
    try:
        session = await manager.connect_ssh_alias("local-alias", password="test-password")
        assert session.remote_root == "/"
        assert await manager.connect_ssh_alias("local-alias", password="test-password") is session
        assert len(manager.list()) == 1
    finally:
        await manager.close_all()


async def test_remote_symlink_is_rejected_before_following(connected, monkeypatch):
    settings, manager, session, local, remote = connected
    sftp = await session.service.sftp()
    original = sftp.lstat

    async def lstat(path):
        if path == "/link":
            return asyncssh.SFTPAttrs(permissions=stat.S_IFLNK | 0o777)
        return await original(path)

    monkeypatch.setattr(sftp, "lstat", lstat)
    with pytest.raises(ValueError, match="Symbolic links"):
        await session.service.realpath("/link/child")


async def test_concurrent_connect_creates_one_session(tmp_path, sftp_server):
    remote, key, profile = sftp_server
    manager = ConnectionManager(Settings(data_path=str(tmp_path / "concurrent-data")))
    manager._append_known_host(profile.host, profile.port, key)
    try:
        sessions = await asyncio.gather(*[manager.connect_profile(profile, password="test-password") for _ in range(3)])
        assert len(manager.sessions) == 1
        assert all(session is sessions[0] for session in sessions)
    finally:
        await manager.close_all()


async def test_cancel_after_restart_cleans_part(transfer_manager, monkeypatch):
    from app.services.transfer_worker import TransferWorker
    transfers, session, local, remote = transfer_manager
    source = local / "restart.bin"
    source.write_bytes(os.urandom(400001))
    original_write = TransferWorker._write_chunk

    async def pause_after_write(worker, handle, offset, data):
        await original_write(worker, handle, offset, data)
        await transfers.pause(model.task_id)

    with monkeypatch.context() as patch:
        patch.setattr(TransferWorker, "_write_chunk", pause_after_write)
        model = await transfers.create(TransferCreate(
            direction="upload", source_path=str(source), destination_path="/",
            connection_id=session.id, conflict_strategy="ask",
        ))
        async def wait():
            while model.status != "paused":
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 10)
        await transfers._wait_worker(model.task_id)
    assert list(remote.glob("*.part"))
    restored = TransferManager(transfers.settings, transfers.storage, transfers.connections, transfers.local, transfers.progress)
    await restored._load_persisted_tasks()
    assert restored.tasks[model.task_id].current_part is None
    await restored.cancel(model.task_id)
    assert not list(remote.glob("*.part"))


async def test_pause_queued_task_can_resume_without_waiting_for_slot(transfer_manager):
    transfers, session, local, remote = transfer_manager
    source = local / "queued.bin"
    source.write_bytes(b"queued transfer")
    await transfers.semaphore.acquire()
    await transfers.semaphore.acquire()
    try:
        model = await transfers.create(TransferCreate(
            direction="upload", source_path=str(source), destination_path="/",
            connection_id=session.id, conflict_strategy="ask",
        ))
        async def wait_for_worker():
            while model.task_id not in transfers.workers:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_worker(), 3)
        await transfers.pause(model.task_id)
        assert model.status == "paused"
        await asyncio.wait_for(transfers.resume(model.task_id), 1)
        assert model.status == "queued"
    finally:
        transfers.semaphore.release()
        transfers.semaphore.release()
    await finished(transfers, model)
    assert (remote / source.name).read_bytes() == b"queued transfer"
