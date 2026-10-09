from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from dataclasses import asdict
from typing import Literal


from app.core.config import Settings
from app.models import EventMessage, TransferCreate, TransferOut, utc_now
from app.services.connection_manager import ConnectionManager
from app.services.local_file_service import LocalFileService
from app.services.progress_manager import ProgressManager
from app.services.path_safety import validate_name
from app.services.direct_transfer import DirectTransfer, DirectWriterUncertain
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
        self.routes: dict[tuple[str, str], tuple[DirectTransfer, dict, float]] = {}

    async def route(self, source_id: str, destination_id: str, force: bool = False):
        import time
        source = self.connections.get(source_id)
        destination = self.connections.get(destination_id)
        key = (source_id, destination_id)
        cached = self.routes.get(key)
        if cached and not force and time.monotonic() - cached[2] < 30:
            return cached[0], cached[1]
        direct = DirectTransfer(source, destination)
        try:
            result = await asyncio.wait_for(direct.probe(), 20)
        except asyncio.TimeoutError:
            result = {"route": "relay", "detail": "直连检测超时，自动由本机中转"}
        self.routes[key] = (direct, result, time.monotonic())
        return direct, result

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
            if not mutable or mutable.model.status != "queued" or task_id in self.workers:
                continue
            worker = asyncio.create_task(self._run_with_semaphore(mutable), name=task_id)
            self.workers[task_id] = worker
            worker.add_done_callback(self._worker_done)

    def _worker_done(self, future: asyncio.Task[TransferOut]) -> None:
        self.workers.pop(future.get_name(), None)
        if not future.cancelled():
            # Retrieve exceptions even during shutdown so no background error
            # goes unobserved. Normal connection failures are persisted above.
            future.exception()

    async def _run_with_semaphore(self, mutable: MutableTask) -> TransferOut:
        async with self.semaphore:
            if not self.running or mutable.control.cancel.is_set() or mutable.model.status != "queued":
                return mutable.model
            session = self.connections.find_by_profile(mutable.model.profile_id)
            if not session:
                mutable.model.status = "interrupted"
                mutable.model.error_message = "SSH connection is not active; reconnect and retry/resume."
                await self.storage.save_transfer(mutable.model)
                await self.progress.emit(EventMessage(event_type="transfer_failed", task_id=mutable.model.task_id, payload=mutable.model.model_dump(mode="json")), force=True)
                return mutable.model
            sftp = None
            destination_sftp = None
            try:
                sftp = await session.connection.start_sftp_client()
                remote_io = AsyncSFTPTransferIO(sftp, session.service)
                local_io = LocalTransferIO(self.local.allowed_roots)
                if mutable.model.direction == "upload":
                    source_io, destination_io = local_io, remote_io
                elif mutable.model.direction == "download":
                    source_io, destination_io = remote_io, local_io
                else:
                    destination = self.connections.find_by_profile(mutable.model.resume_metadata["endpoints"]["destination_profile_id"])
                    if not destination:
                        raise ValueError("Destination SSH connection is not active; reconnect and resume")
                    destination_sftp = await destination.connection.start_sftp_client()
                    source_io = remote_io
                    destination_io = AsyncSFTPTransferIO(destination_sftp, destination.service)
                direct_copy = None
                if mutable.model.direction == "remote":
                    direct, route = await self.route(session.id, destination.id)
                    mutable.model.resume_metadata.update(route=route["route"], route_detail=route["detail"])
                    async def copy(task, source_path, part, offset, size, report):
                        if task.model.resume_metadata["route"] != "direct":
                            return
                        try:
                            await direct.copy(task, source_path, part, offset, size, report, destination_io)
                        except DirectWriterUncertain:
                            raise
                        except Exception as exc:
                            task.model.resume_metadata.update(route="relay", route_detail="直传中断，已切换为本机中转：" + str(exc)[:400])
                            await self._save(task.model)
                            await self.progress.emit(EventMessage(event_type="transfer_progress", task_id=task.model.task_id,
                                                                 payload=task.model.model_dump(mode="json")), force=True)
                    direct_copy = copy
                if mutable.control.cancel.is_set() or mutable.model.status != "queued":
                    return mutable.model
                worker = TransferWorker(self.settings, self.progress, source_io, destination_io, self._save, direct_copy)
                return await worker.run(mutable)
            except Exception as exc:
                mutable.model.status = "failed"
                mutable.model.error_message = str(exc) or type(exc).__name__
                await self._save(mutable.model)
                await self.progress.emit(EventMessage(event_type="transfer_failed", task_id=mutable.model.task_id, payload=mutable.model.model_dump(mode="json")), force=True)
                return mutable.model
            finally:
                if sftp:
                    sftp.exit()
                    await sftp.wait_closed()
                if destination_sftp:
                    destination_sftp.exit()
                    await destination_sftp.wait_closed()

    async def _save(self, model: TransferOut) -> None:
        await self.storage.save_transfer(model)

    async def create(self, data: TransferCreate) -> TransferOut:
        if data.direction == "upload":
            session = self.connections.get(data.connection_id)
            source = self.local.validate(data.source_path)
            source_display = str(source)
            destination_root = await session.service.realpath(data.destination_path)
            final_root = posixjoin(destination_root, Path(source).name)
            io_source = LocalTransferIO(self.local.allowed_roots)
            io_destination = AsyncSFTPTransferIO(await session.service.sftp(), session.service)
        elif data.direction == "download":
            session = self.connections.get(data.connection_id)
            source = await session.service.realpath(data.source_path)
            destination_root = str(self.local.validate(data.destination_path))
            name = posixbasename(source)
            validate_name(name, windows=os.name == "nt")
            final_root = str(self.local.validate(Path(destination_root) / name))
            io_source = AsyncSFTPTransferIO(await session.service.sftp(), session.service)
            io_destination = LocalTransferIO(self.local.allowed_roots)
        else:
            session = self.connections.get(data.connection_id)
            destination = self.connections.get(data.destination_connection_id)
            source = await session.service.realpath(data.source_path)
            destination_root = await destination.service.realpath(data.destination_path)
            name = posixbasename(source)
            validate_name(name, windows=False)
            final_root = posixjoin(destination_root, name)
            if session.profile_id == destination.profile_id and (final_root == source or final_root.startswith(source.rstrip('/') + '/')):
                raise ValueError("Cannot copy a remote path onto itself or into its own directory")
            io_source = AsyncSFTPTransferIO(await session.service.sftp(), session.service)
            io_destination = AsyncSFTPTransferIO(await destination.service.sftp(), destination.service)

        if data.conflict_strategy == "ask" and await io_destination.exists(final_root):
            raise TransferConflictError(final_root)
        if data.conflict_strategy == "rename" and await io_destination.exists(final_root):
            final_root = await io_destination.unique_path(final_root)
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
        if data.direction == "remote":
            model.resume_metadata.update(endpoints={
                "destination_profile_id": destination.profile_id,
                "source_name": session.profile_name, "destination_name": destination.profile_name,
            }, route="checking", route_detail="正在检测服务器直连")
        mutable = MutableTask.from_model(model, entries)
        self.tasks[task_id] = mutable
        await self.storage.save_transfer(model)
        await self.queue.put(task_id)
        await self.progress.emit(EventMessage(event_type="transfer_created", task_id=task_id, payload=model.model_dump(mode="json")), force=True)
        return model

    async def _scan(self, direction: Literal["upload", "download", "remote"], root: str, io) -> list[TransferEntry]:
        async def visit(path: str, relative: str, depth: int = 0) -> list[TransferEntry]:
            if depth > 64:
                raise ValueError("Directory nesting is too deep")
            try:
                info = await io.lstat(path)
            except Exception as exc:
                raise ValueError(f"Cannot access source path: {path}") from exc
            if getattr(info, "is_symlink", False):
                raise ValueError(f"Symbolic links are not transferred: {path}")
            if not info.is_dir:
                return [TransferEntry(relative, False, info.size, info.mtime)]
            result = [TransferEntry(relative, True, 0, info.mtime)]
            names = await io.listdir(path)
            for name in sorted(names):
                validate_name(name, windows=os.name == "nt" and direction == "download")
                child = str(Path(path) / name) if direction == "upload" else posixjoin(path, name)
                result.extend(await visit(child, posixjoin(relative, name) if relative else name, depth + 1))
            return result
        return await visit(root, "")

    async def pause(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        if mutable.model.status not in {"queued", "running", "pausing"}:
            return mutable.model
        mutable.control.pause.set()
        if mutable.model.status == "queued":
            mutable.model.status = "paused"
            await self._save(mutable.model)
            await self.progress.emit(EventMessage(event_type="transfer_paused", task_id=task_id, payload=mutable.model.model_dump(mode="json")), force=True)
        return mutable.model

    async def resume(self, task_id: str) -> TransferOut:
        mutable = self.get(task_id)
        if mutable.model.status not in {"paused", "interrupted", "failed"}:
            return mutable.model
        await self._wait_worker(task_id)
        mutable.control = type(mutable.control)()
        mutable.model.status = "queued"
        mutable.model.resume_metadata["resuming"] = True
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
        await self._wait_worker(task_id)
        mutable.control = type(mutable.control)()
        mutable.model.status = "queued"
        mutable.model.error_message = None
        mutable.model.finished_at = None
        mutable.model.transferred_bytes = 0
        mutable.model.resume_metadata = {**{key: value for key, value in mutable.model.resume_metadata.items()
                                           if key in {"endpoints", "route", "route_detail"}},
                                         "entries": [asdict(entry) for entry in mutable.entries]}
        await self.storage.save_transfer(mutable.model)
        await self.queue.put(task_id)
        return mutable.model

    async def _wait_worker(self, task_id: str) -> None:
        worker = self.workers.get(task_id)
        if worker:
            mutable = self.tasks[task_id]
            if mutable.model.status == "paused" and mutable.model.started_at is None:
                # A queued job may already have a worker waiting for the
                # semaphore. Resuming must not wait for other large transfers.
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            else:
                await worker

    async def clear_finished(self) -> None:
        await self.storage.clear_finished_transfers()
        for task_id, mutable in list(self.tasks.items()):
            if mutable.model.status in {"completed", "failed", "cancelled"}:
                self.tasks.pop(task_id, None)
                self.progress.clear_task(task_id)

    async def _cleanup_task_part(self, mutable: MutableTask) -> None:
        if not mutable.current_part:
            index = int(mutable.model.resume_metadata.get("completed_entries", 0))
            if not 0 <= index < len(mutable.entries) or mutable.entries[index].is_dir:
                return
            target = TransferWorker._destination_for(mutable.model, mutable.entries[index])
            mutable.current_part = TransferWorker._part_path(mutable.model, target, index)
        session = self.connections.find_by_profile(mutable.model.profile_id)
        if mutable.model.direction in {"upload", "remote"}:
            if mutable.model.direction == "remote":
                session = self.connections.find_by_profile(mutable.model.resume_metadata["endpoints"]["destination_profile_id"])
            if not session:
                return
            destination_io = AsyncSFTPTransferIO(await session.service.sftp(), session.service)
        else:
            destination_io = LocalTransferIO(self.local.allowed_roots)
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
