"""Opt-in real-server roundtrip; touches only a uniquely named test directory."""
from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import uuid

import pytest

from app.core.config import Settings
from app.models import TransferCreate
from app.services.connection_manager import ConnectionManager
from app.services.local_file_service import LocalFileService
from app.services.progress_manager import ProgressManager
from app.services.transfer_manager import TransferManager
from app.services.transfer_worker import TransferWorker
from app.storage import Storage


@pytest.mark.skipif(not os.environ.get("MINISFTP_TEST_SSH_ALIAS"), reason="Set MINISFTP_TEST_SSH_ALIAS to opt in")
async def test_live_sftp_roundtrip(tmp_path, monkeypatch):
    alias = os.environ["MINISFTP_TEST_SSH_ALIAS"]
    local = tmp_path / "local"
    local.mkdir()
    settings = Settings(local_root=str(local), data_path=str(tmp_path / "data"), chunk_size=65536)
    connections = ConnectionManager(settings)
    storage = Storage(settings.database_path)
    transfers = TransferManager(settings, storage, connections, LocalFileService(settings), ProgressManager())
    remote_dir = None
    session = None
    await storage.connect()
    await transfers.start()

    async def settle(model):
        async def wait():
            while model.status in {"queued", "running", "pausing"}:
                await asyncio.sleep(0.02)
            return model
        return await asyncio.wait_for(wait(), 60)

    async def create(direction, source, destination, strategy="ask"):
        return await transfers.create(TransferCreate(
            direction=direction, source_path=str(source), destination_path=str(destination),
            connection_id=session.id, conflict_strategy=strategy,
        ))

    try:
        session = await asyncio.wait_for(connections.connect_ssh_alias(alias), 30)
        print(f"Connected {alias}; remote root {session.remote_root}")
        listing = await session.service.list_dir(session.remote_root)
        assert listing.path == session.remote_root
        remote_dir = await session.service.mkdir(session.remote_root, f".minisftp-audit-{uuid.uuid4().hex}")
        source = local / "项目 A"
        (source / "nested").mkdir(parents=True)
        (source / "empty").mkdir()
        payload = os.urandom(400001)
        (source / "nested" / "文件 名.bin").write_bytes(payload)
        (source / "zero.txt").write_bytes(b"")
        upload = await create("upload", source, remote_dir)
        assert (await settle(upload)).status == "completed", upload.error_message
        print("Nested binary/unicode/empty-directory upload passed")
        remote_source = posixpath.join(remote_dir, source.name)
        renamed = await session.service.rename(remote_source, "renamed 项目")
        download_root = local / "download"
        download_root.mkdir()
        download = await create("download", renamed, download_root)
        assert (await settle(download)).status == "completed", download.error_message
        downloaded = download_root / "renamed 项目"
        assert (downloaded / "empty").is_dir()
        assert (downloaded / "zero.txt").read_bytes() == b""
        assert hashlib.sha256((downloaded / "nested" / "文件 名.bin").read_bytes()).digest() == hashlib.sha256(payload).digest()
        print("Download SHA256 and remote rename passed")

        single = local / "single.bin"
        single.write_bytes(os.urandom(200001))
        single_upload = await create("upload", single, remote_dir)
        assert (await settle(single_upload)).status == "completed", single_upload.error_message
        single.write_bytes(os.urandom(400001))
        original_write = TransferWorker._write_chunk
        paused_once = False

        async def pause_after_write(worker, handle, offset, data):
            nonlocal paused_once
            await original_write(worker, handle, offset, data)
            if not paused_once:
                paused_once = True
                await transfers.pause(overwrite.task_id)

        with monkeypatch.context() as patch:
            patch.setattr(TransferWorker, "_write_chunk", pause_after_write)
            overwrite = await create("upload", single, remote_dir, "overwrite")
            assert (await settle(overwrite)).status == "paused", overwrite.error_message
        assert 0 < overwrite.transferred_bytes < overwrite.total_bytes
        await transfers.resume(overwrite.task_id)
        assert (await settle(overwrite)).status == "completed", overwrite.error_message
        assert overwrite.transferred_bytes == overwrite.total_bytes
        verify = await create("download", posixpath.join(remote_dir, single.name), download_root)
        assert (await settle(verify)).status == "completed", verify.error_message
        assert (download_root / single.name).read_bytes() == single.read_bytes()
        print("Single-file atomic overwrite, pause/resume and byte verification passed")

        cancel_source = local / "cancel.bin"
        cancel_source.write_bytes(os.urandom(400001))
        cancelled_once = False

        async def cancel_after_write(worker, handle, offset, data):
            nonlocal cancelled_once
            await original_write(worker, handle, offset, data)
            if not cancelled_once:
                cancelled_once = True
                await transfers.cancel(cancel_task.task_id)

        with monkeypatch.context() as patch:
            patch.setattr(TransferWorker, "_write_chunk", cancel_after_write)
            cancel_task = await create("upload", cancel_source, remote_dir)
            assert (await settle(cancel_task)).status == "cancelled", cancel_task.error_message
        assert not await session.service.exists(posixpath.join(remote_dir, cancel_source.name))
        remaining = await session.service.list_dir(remote_dir, show_hidden=True)
        assert not any(entry.name.endswith(".part") for entry in remaining.entries)
        print("Cancellation removes partial files; all real-server checks passed")
    finally:
        await transfers.shutdown()
        if session and remote_dir:
            assert posixpath.dirname(remote_dir) == session.remote_root
            assert posixpath.basename(remote_dir).startswith(".minisftp-audit-")
            await session.service.delete(remote_dir, recursive=True)
            print("Remote audit directory removed")
        await connections.close_all()
        await storage.close()
