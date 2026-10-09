from __future__ import annotations

import os
import pytest

from app.models import TransferOut, utc_now
from app.services.progress_manager import ProgressManager
from app.services.transfer_worker import LocalTransferIO, MutableTask, TransferEntry, TransferWorker


async def noop_persist(model):
    pass


async def make_task(model: TransferOut, entries) -> MutableTask:
    return MutableTask.from_model(model, entries)


async def test_transfer_single_file_preserves_data_and_completes(settings, temp_root):
    source = temp_root / "source" / "hello.txt"
    source.write_bytes(b"hello world")
    destination = temp_root / "target" / "hello.txt"
    entries = [TransferEntry("hello.txt", False, source.stat().st_size, source.stat().st_mtime)]
    model = TransferOut(
        task_id="task1", profile_id="profile", direction="upload", source_path=str(source.parent),
        destination_path=str(destination.parent), total_bytes=source.stat().st_size, status="queued", transferred_bytes=0,
        created_at=utc_now(), conflict_strategy="overwrite",
    )
    worker = TransferWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)
    result = await worker.run(await make_task(model, entries))
    assert result.status == "completed"
    assert destination.read_bytes() == b"hello world"


async def test_nested_and_unicode_transfer(settings, temp_root):
    source_root = temp_root / "source" / "项目 A"
    (source_root / "nested").mkdir(parents=True)
    (source_root / "nested" / "文件 名.txt").write_text("中文 content")
    (source_root / "empty").mkdir()
    (source_root / "empty file").write_text("")
    entries = [
        TransferEntry("项目 A", True, 0, source_root.stat().st_mtime),
        TransferEntry("项目 A/nested", True, 0, (source_root / "nested").stat().st_mtime),
        TransferEntry("项目 A/nested/文件 名.txt", False, (source_root / "nested" / "文件 名.txt").stat().st_size, (source_root / "nested" / "文件 名.txt").stat().st_mtime),
        TransferEntry("项目 A/empty", True, 0, (source_root / "empty").stat().st_mtime),
        TransferEntry("项目 A/empty file", False, 0, (source_root / "empty file").stat().st_mtime),
    ]
    target = temp_root / "target" / "项目 A"
    model = TransferOut(
        task_id="task2", profile_id="p", direction="upload", source_path=str(source_root.parent),
        destination_path=str(temp_root / "target"), total_bytes=(source_root / "nested" / "文件 名.txt").stat().st_size, status="queued", transferred_bytes=0, created_at=utc_now(),
        conflict_strategy="overwrite",
    )
    worker = TransferWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)
    result = await worker.run(await make_task(model, entries))
    assert result.status == "completed", result.error_message
    assert (target / "nested" / "文件 名.txt").read_text() == "中文 content"
    assert (target / "empty").is_dir()
    assert (target / "empty file").read_text() == ""


async def test_pause_resume_and_size_verification(settings, temp_root):
    source = temp_root / "source" / "large.bin"
    payload = os.urandom(180000)
    source.write_bytes(payload)
    target = temp_root / "target" / "large.bin"
    entry = TransferEntry("large.bin", False, len(payload), source.stat().st_mtime)
    model = TransferOut(
        task_id="task3", profile_id="p", direction="upload", source_path=str(source.parent),
        destination_path=str(target.parent), total_bytes=len(payload), status="queued", transferred_bytes=0, created_at=utc_now(),
        conflict_strategy="overwrite",
    )
    mutable = await make_task(model, [entry])
    worker = TransferWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)

    class PausingIO(LocalTransferIO):
        pass

    # Pause after the first chunk has actually been written.
    class PausingWorker(TransferWorker):
        first_write = True

        async def _write_chunk(self, handle, offset, data):
            await super()._write_chunk(handle, offset, data)
            if self.first_write:
                self.first_write = False
                mutable.control.pause.set()

    worker = PausingWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)
    await worker.run(mutable)
    assert mutable.model.status == "paused"
    assert mutable.model.resume_metadata["offset"] > 0
    assert mutable.model.resume_metadata["offset"] < len(payload)

    mutable.control.pause.clear()
    result = await worker.run(mutable)
    assert result.status == "completed", result.error_message
    assert target.read_bytes() == payload
    assert target.stat().st_size == len(payload)


async def test_conflict_strategy_ask_and_rename(settings, temp_root):
    source = temp_root / "source" / "conflict.txt"
    source.write_text("new data")
    target = temp_root / "target" / "conflict.txt"
    target.write_text("old data")
    entry = TransferEntry("conflict.txt", False, source.stat().st_size, source.stat().st_mtime)

    def model(strategy):
        return TransferOut(
            task_id=f"conflict-{strategy}", profile_id="p", direction="upload",
            source_path=str(source.parent), destination_path=str(target.parent),
            total_bytes=source.stat().st_size, transferred_bytes=0, status="queued",
            created_at=utc_now(), conflict_strategy=strategy,
        )

    worker = TransferWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)
    asked = await worker.run(MutableTask.from_model(model("ask"), [entry]))
    assert asked.status == "failed"
    assert "Target already exists" in asked.error_message

    renamed = await worker.run(MutableTask.from_model(model("rename"), [entry]))
    assert renamed.status == "completed"
    assert target.read_text() == "old data"
    assert (temp_root / "target" / "conflict (1).txt").read_text() == "new data"


async def test_failed_atomic_replace_keeps_original(settings, temp_root):
    source = temp_root / "source" / "file.bin"
    source.write_bytes(b"new")
    target = temp_root / "target" / source.name
    target.write_bytes(b"original")

    class FailedReplace(LocalTransferIO):
        async def replace(self, source, target):
            raise ConnectionError("Connection lost during commit")

    model = TransferOut(
        task_id="safe-overwrite", profile_id="p", direction="upload",
        source_path=str(source.parent), destination_path=str(target.parent),
        total_bytes=3, transferred_bytes=0, status="queued", created_at=utc_now(), conflict_strategy="overwrite",
    )
    worker = TransferWorker(settings, ProgressManager(), LocalTransferIO(temp_root), FailedReplace(temp_root), noop_persist)
    result = await worker.run(MutableTask.from_model(model, [TransferEntry(source.name, False, 3, source.stat().st_mtime)]))
    assert result.status == "failed"
    assert target.read_bytes() == b"original"
    assert (target.parent / ".safe-overwrite.0.part").read_bytes() == b"new"


async def test_unique_local_name_is_stable(temp_root):
    io = LocalTransferIO(temp_root)
    path = temp_root / "file.txt"
    path.write_bytes(b"existing")
    (temp_root / "file (1).txt").write_bytes(b"existing")
    assert await io.unique_path(str(path)) == str(temp_root / "file (2).txt")


async def test_transfer_io_rejects_path_escape(temp_root):
    io = LocalTransferIO(temp_root)
    with pytest.raises(ValueError):
        await io.open_write(str(temp_root / ".." / "escape.bin"), resume=False)


async def test_source_change_during_transfer_keeps_original(settings, temp_root):
    source = temp_root / "source" / "changing.bin"
    source.write_bytes(os.urandom(200001))
    initial = source.stat()
    target = temp_root / "target" / source.name
    target.write_bytes(b"original")

    class ChangingWorker(TransferWorker):
        async def _write_chunk(self, handle, offset, data):
            await super()._write_chunk(handle, offset, data)
            os.utime(source, (initial.st_atime, initial.st_mtime + 1))

    model = TransferOut(
        task_id="changing-source", profile_id="p", direction="upload",
        source_path=str(source.parent), destination_path=str(target.parent),
        total_bytes=initial.st_size, transferred_bytes=0, status="queued", created_at=utc_now(), conflict_strategy="overwrite",
    )
    worker = ChangingWorker(settings, ProgressManager(), LocalTransferIO(temp_root), LocalTransferIO(temp_root), noop_persist)
    result = await worker.run(MutableTask.from_model(model, [TransferEntry(source.name, False, initial.st_size, initial.st_mtime)]))
    assert result.status == "failed"
    assert "Source changed during" in result.error_message
    assert target.read_bytes() == b"original"
