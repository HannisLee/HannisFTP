from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from dataclasses import asdict
from typing import Literal

import asyncssh

from app.core.config import Settings
from app.models import EventMessage, TransferCreate, TransferOut, utc_now
from app.services.connection_manager import ConnectionManager
from app.services.local_file_service import LocalFileService
from app.services.progress_manager import ProgressManager
from app.services.transfer_worker import (
    AsyncSFTPTransferIO,
    LocalTransferIO,
    MutableTask,
    TransferEntry,
    TransferWorker,
)


class TransferConflictError(Exception):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Target already exists: {path}")


class TransferManager:
    def __init__(
        self,
        settings: Settings,
        storage,
        connections: ConnectionManager,
        local: LocalFileService,
        progress: ProgressManager,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.connections = connections
        self.local = local
        self.progress = progress
        self.tasks: dict[str, MutableTask] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.semaphore = asyncio.BoundedSemaphore(settings.transfer_concurrency)
        self.workers: dict[str, asyncio.Task[TransferOut]] = {}
        self.running = True
        self._scheduler: asyncio.Task[None] | None = None
        self._counter_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self._load_persisted_tasks()
        if self._scheduler is None:
            self._scheduler = asyncio.create_task(self._scheduler_loop())

    async def _load_persisted_tasks(self) -> None:
        for model in await self.storage.list_transfers():
            if model.status == "completed" or not model.resume_metadata.get("entries"):
                continue
            try:
                entries = [TransferEntry(**entry) for entry in model.resume_metadata["entries"]]
            except TypeError:
                continue
            self.tasks[model.task_id] = MutableTask.from_model(model, entries)

    async def shutdown(self) -> None:
        self.running = False
        if self._scheduler:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
        for task in list(self.workers.values()):
            mutable = self.tasks.get(task.get_name())
            if mutable:
                mutable.control.cancel.set()
            task.cancel()
        for task in list(self.workers.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in list(self.tasks.values()):
            if task.model.status in {"queued", "running", "pausing"}:
                task.model.status = "interrupted"
                await self.storage.save_transfer(task.model)

    async def _scheduler_loop(self) -> None:
        while self.running:
            try:
                task_id = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            mutable = self.tasks.get(task_id)
            if not mutable or mutable.model.status not in {"queued", "paused"}:
                continue
            worker = asyncio.create_task(self._run_with_semaphore(mutable), name=task_id)
            self.workers[task_id] = worker
            worker.add_done_callback(self._worker_done)

    def _worker_done(self, future: asyncio.Task[TransferOut]) -> None:
        self.workers.pop(future.get_name(), None)
        if not future.cancelled() and future.exception() and isinstance(future.exception(), asyncio.CancelledError):
            mutable = self.tasks.get(future.get_name())
            if mutable and mutable.model.status in {"running", "pausing"}:
                mutable.model.status = "interrupted"

    async def _run_with_semaphore(self, mutable: MutableTask) -> TransferOut:
        async with self.semaphore:
            if not self.running or mutable.control.cancel.is_set():
                return mutable.model
            session = self.connections.find_by_profile(mutable.model.profile_id)
            if not session:
                mutable.model.status = "interrupted"
                mutable.model.error_message = "SSH connection is not active; reconnect and retry/resume."
                await self.storage.save_transfer(mutable.model)
                return mutable.model
            if mutable.model.direction == "upload":
                source_io = LocalTransferIO(self.local.root)
                destination_io = AsyncSFTPTransferIO(await session.connection.start_sftp_client())
            else:
                source_io = AsyncSFTPTransferIO(await session.connection.start_sftp_client())
                destination_io = LocalTransferIO(self.local.root)
            try:
                worker = TransferWorker(self.settings, self.progress, source_io, destination_io, self._save)
                return await worker.run(mutable)
            finally:
                pass

    async def _save(self, model: TransferOut) -> None:
        await self.storage.save_transfer(model)

    async def create(self, data: TransferCreate) -> TransferOut:
        if data.direction == "upload":
            session = self.connections.get(data.connection_id)
            source = self.local.validate(data.source_path)
            source_display = str(source)
            destination_root = await session.service.realpath(data.destination_path)
            final_root = posixjoin(destination_root, Path(source).name)
            io_source = LocalTransferIO(self.local.root)
            io_destination = AsyncSFTPTransferIO(await session.connection.start_sftp_client())
        else:
            session = self.connections.get(data.connection_id)
            source = await session.service.realpath(data.source_path)
            destination_root = str(self.local.validate(data.destination_path))
            final_root = str(Path(destination_root) / posixbasename(source))
            io_source = AsyncSFTPTransferIO(await session.connection.start_sftp_client())
            io_destination = LocalTransferIO(self.local.root)

        if data.conflict_strategy == "ask" and await io_destination.exists(final_root):
            raise TransferConflictError(final_root)
        entries = await self._scan(data.direction, source, io_source)
        if not entries:
            raise FileNotFoundError("Nothing to transfer")
        total = sum(entry.size for entry in entries)
        task_id = uuid.uuid4().hex
        model = TransferOut(
            task_id=task_id,
            profile_id=session.profile_id,
            direction=data.direction,
            source_path=source_display if data.direction == "upload" else source,
            destination_path=final_root,
            total_bytes=total,
            transferred_bytes=0,
            status="queued",
            created_at=utc_now(),
            conflict_strategy=data.conflict_strategy,
        )
        model.resume_metadata = {"entries": [asdict(entry) for entry in entries]}
        mutable = MutableTask.from_model(model, entries)
        self.tasks[task_id] = mutable
        await self.storage.save_transfer(model)
        await self.queue.put(task_id)
        await self.progress.emit(EventMessage(event_type="transfer_created", task_id=task_id, payload=model.model_dump(mode="json")), force=True)
        return model

    async def _scan(self, direction: Literal["upload", "download"], root: str, io) -> list[TransferEntry]:
        async def visit(path: str, relative: str, depth: int = 0) -> list[TransferEntry]:
            if depth > 64:
                raise ValueError("Directory nesting is too deep")
            try:
                info = await io.stat(path)
            except Exception as exc:
                raise ValueError(f"Cannot access source path: {path}") from exc
            if not info.is_dir:
                return [TransferEntry(relative, False, info.size, info.mtime)]
            if relative:
                result = [TransferEntry(relative, True, 0, info.mtime)]
            else:
                result = []
            names = await io.listdir(path)
            for name in sorted(names):
                child = str(Path(path) / name) if direction == "upload" else posixjoin(path, name)
                result.extend(await visit(child, posixjoin(relative, name) if relative else name, depth + 1))
            return result
        return await visit(root, "")

    async def pause(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        mutable.control.pause.set()
        return mutable.model

    async def resume(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        if mutable.model.status not in {"paused", "interrupted", "failed"}:
            return mutable.model
        mutable.control = type(mutable.control)()
        mutable.model.status = "queued"
        await self.storage.save_transfer(mutable.model)
        await self.queue.put(task_id)
        return mutable.model

    async def cancel(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        mutable.control.cancel.set()
        if mutable.model.status == "queued":
            mutable.model.status = "cancelled"
            from app.models import utc_now
            mutable.model.finished_at = utc_now()
            await self.storage.save_transfer(mutable.model)
        elif mutable.model.status in {"paused", "failed", "interrupted"}:
            await self._cleanup_task_part(mutable)
            mutable.model.status = "cancelled"
            from app.models import utc_now
            mutable.model.finished_at = utc_now()
            await self.storage.save_transfer(mutable.model)
        return mutable.model

    async def retry(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        if mutable.model.status not in {"failed", "cancelled", "interrupted"}:
            return mutable.model
        mutable.control = type(mutable.control)()
        mutable.model.status = "queued"
        mutable.model.error_message = None
        mutable.model.finished_at = None
        await self.storage.save_transfer(mutable.model)
        await self.queue.put(task_id)
        return mutable.model

    async def clear_finished(self) -> None:
        await self.storage.clear_finished_transfers()
        for task_id, mutable in list(self.tasks.items()):
            if mutable.model.status in {"completed", "failed", "cancelled"}:
                self.tasks.pop(task_id, None)
                self.progress.clear_task(task_id)

    async def _cleanup_task_part(self, mutable: MutableTask) -> None:
        if not mutable.current_part:
            return
        session = self.connections.find_by_profile(mutable.model.profile_id)
        if mutable.model.direction == "upload":
            if not session:
                return
            destination_io = AsyncSFTPTransferIO(await session.connection.start_sftp_client())
        else:
            destination_io = LocalTransferIO(self.local.root)
        try:
            if await destination_io.exists(mutable.current_part):
                await destination_io.remove(mutable.current_part)
        except Exception:
            # Do not turn cancellation into a failure if a server already removed the part.
            pass
        mutable.current_part = None

    def get(self, task_id: str) -> MutableTask:
        mutable = self.tasks.get(task_id)
        if not mutable:
            raise KeyError(task_id)
        return mutable

    async def list(self, status: str | None = None) -> list[TransferOut]:
        return await self.storage.list_transfers(status)


def posixjoin(*parts: str) -> str:
    import posixpath
    return posixpath.join(*parts)


def posixbasename(path: str) -> str:
    import posixpath
    return posixpath.basename(path.rstrip("/")) or posixpath.basename(path)
