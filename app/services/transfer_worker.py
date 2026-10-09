from __future__ import annotations

import asyncio
import os
import stat
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiofiles
import asyncssh

from app.core.config import Settings
from app.models import EventMessage, TransferOut
from app.services.progress_manager import ProgressManager
from app.services.path_safety import local_path, remote_kind


class TransferControl:
    def __init__(self) -> None:
        self.pause = asyncio.Event()
        self.cancel = asyncio.Event()


@dataclass
class TransferEntry:
    relative_path: str
    is_dir: bool
    size: int
    mtime: float
    is_symlink: bool = False


@dataclass
class MutableTask:
    model: TransferOut
    entries: list[TransferEntry]
    control: TransferControl = field(default_factory=TransferControl)
    current_part: str | None = None

    @classmethod
    def from_model(cls, model: TransferOut, entries: list[TransferEntry]) -> "MutableTask":
        return cls(model=model, entries=entries)


class StatInfo(Protocol):
    size: int
    mtime: float
    is_dir: bool
    is_symlink: bool


class TransferIO(ABC):
    @abstractmethod
    async def stat(self, path: str) -> StatInfo: ...
    @abstractmethod
    async def exists(self, path: str) -> bool: ...
    @abstractmethod
    async def mkdir(self, path: str) -> None: ...
    @abstractmethod
    async def listdir(self, path: str) -> list[str]: ...
    @abstractmethod
    async def lstat(self, path: str) -> StatInfo: ...
    @abstractmethod
    async def open_read(self, path: str) -> Any: ...
    @abstractmethod
    async def open_write(self, path: str, *, resume: bool) -> Any: ...
    @abstractmethod
    async def remove(self, path: str) -> None: ...
    @abstractmethod
    async def rename(self, source: str, target: str) -> None: ...
    @abstractmethod
    async def unique_path(self, path: str) -> str: ...

    async def replace(self, source: str, target: str) -> None:
        raise NotImplementedError("Atomic replacement is not supported")


class AsyncSFTPTransferIO(TransferIO):
    def __init__(self, sftp: asyncssh.SFTPClient, service=None) -> None:
        self.sftp = sftp
        self.service = service

    async def _validate(self, path: str, *, missing: bool = False) -> str:
        if self.service:
            return await self.service.checked_path(path, allow_missing=missing)
        return path

    async def stat(self, path: str) -> StatInfo:
        attrs = await self.sftp.stat(await self._validate(path))
        return type("Stat", (), {
            "size": int(getattr(attrs, "size", 0) or 0),
            "mtime": float(getattr(attrs, "mtime", 0) or 0),
            "is_dir": remote_kind(attrs)[0],
            "is_symlink": remote_kind(attrs)[1],
        })()

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
            return False

    async def listdir(self, path: str) -> list[str]:
        entries = await self.sftp.readdir(await self._validate(path))
        return [item.filename for item in entries if item.filename not in {".", ".."}]

    async def lstat(self, path: str) -> StatInfo:
        attrs = await self.sftp.lstat(await self._validate(path))
        return type("Stat", (), {
            "size": int(getattr(attrs, "size", 0) or 0),
            "mtime": float(getattr(attrs, "mtime", 0) or 0),
            "is_dir": remote_kind(attrs)[0],
            "is_symlink": remote_kind(attrs)[1],
        })()

    async def mkdir(self, path: str) -> None:
        try:
            await self.sftp.mkdir(await self._validate(path, missing=True))
        except (asyncssh.SFTPFailure, asyncssh.SFTPFileAlreadyExists):
            # Some servers return SSH_FX_FAILURE for an existing directory.
            if not await self.exists(path) or not (await self.stat(path)).is_dir:
                raise

    async def open_read(self, path: str) -> Any:
        return await self.sftp.open(await self._validate(path), "rb")

    async def open_write(self, path: str, *, resume: bool) -> Any:
        flags = asyncssh.FXF_WRITE | asyncssh.FXF_CREAT
        if not resume:
            flags |= asyncssh.FXF_TRUNC
        return await self.sftp.open(await self._validate(path, missing=True), flags, encoding=None)

    async def remove(self, path: str) -> None:
        info = await self.stat(path)
        if info.is_dir:
            await self.sftp.rmdir(path)
        else:
            await self.sftp.remove(path)

    async def rename(self, source: str, target: str) -> None:
        await self.sftp.rename(await self._validate(source), await self._validate(target, missing=True))

    async def replace(self, source: str, target: str) -> None:
        await self.sftp.posix_rename(await self._validate(source), await self._validate(target, missing=True))

    async def unique_path(self, path: str) -> str:
        base, ext = os.path.splitext(path)
        counter = 1
        while await self.exists(f"{base} ({counter}){ext}"):
            counter += 1
        return f"{base} ({counter}){ext}"


class LocalTransferIO(TransferIO):
    def __init__(self, root: Path | tuple[Path, ...]) -> None:
        self.root = root

    def _validate(self, path: str) -> Path:
        return local_path(self.root, path)

    async def stat(self, path: str) -> StatInfo:
        target = self._validate(path)
        info = target.stat()
        return type("Stat", (), {
            "size": info.st_size,
            "mtime": info.st_mtime,
            "is_dir": stat.S_ISDIR(info.st_mode),
            "is_symlink": False,
        })()

    async def exists(self, path: str) -> bool:
        return self._validate(path).exists()

    async def mkdir(self, path: str) -> None:
        self._validate(path).mkdir(parents=True, exist_ok=True)

    async def listdir(self, path: str) -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, os.listdir, self._validate(path))

    async def lstat(self, path: str) -> StatInfo:
        info = self._validate(path).lstat()
        return type("Stat", (), {
            "size": info.st_size,
            "mtime": info.st_mtime,
            "is_dir": stat.S_ISDIR(info.st_mode),
            "is_symlink": stat.S_ISLNK(info.st_mode),
        })()

    async def open_read(self, path: str) -> Any:
        return await aiofiles.open(self._validate(path), "rb")

    async def open_write(self, path: str, *, resume: bool) -> Any:
        target = self._validate(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return await aiofiles.open(target, "rb+" if resume else "wb+")

    async def remove(self, path: str) -> None:
        target = self._validate(path)
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    async def rename(self, source: str, target: str) -> None:
        source_path, target_path = self._validate(source), self._validate(target)
        if target_path.exists():
            raise FileExistsError(target_path)
        os.rename(source_path, target_path)

    async def replace(self, source: str, target: str) -> None:
        os.replace(self._validate(source), self._validate(target))

    async def unique_path(self, path: str) -> str:
        original = self._validate(path)
        candidate = original
        counter = 1
        while candidate.exists():
            candidate = original.with_name(f"{original.stem} ({counter}){original.suffix}")
            counter += 1
        return str(candidate)


class TransferPaused(Exception):
    pass


class TransferCancelled(Exception):
    pass


class TransferWorker:
    def __init__(
        self,
        settings: Settings,
        progress: ProgressManager,
        source_io: TransferIO,
        destination_io: TransferIO,
        persist,
        direct_copy=None,
    ) -> None:
        self.settings = settings
        self.progress = progress
        self.source_io = source_io
        self.destination_io = destination_io
        self.persist = persist
        self.direct_copy = direct_copy

    async def run(self, task: MutableTask) -> TransferOut:
        model = task.model
        model.status = "running"
        if model.started_at is None:
            from app.models import utc_now
            model.started_at = utc_now()
        model.error_message = None
        model.current_speed = 0
        model.average_speed = 0
        await self.persist(model)
        await self.progress.emit(EventMessage(event_type="transfer_progress", task_id=model.task_id, payload=model.model_dump(mode="json")), force=True)
        try:
            await self._run_entries(task)
        except TransferPaused:
            model.status = "paused"
            model.current_speed = 0
            model.eta_seconds = None
            await self.persist(model)
            await self.progress.emit(EventMessage(event_type="transfer_paused", task_id=model.task_id, payload=model.model_dump(mode="json")), force=True)
            return model
        except TransferCancelled as exc:
            model.status = "cancelled"
            model.current_speed = 0
            model.eta_seconds = None
            model.error_message = str(exc) or "Cancelled"
            from app.models import utc_now
            model.finished_at = utc_now()
            await self._cleanup_part(task)
            await self.persist(model)
            await self.progress.emit(EventMessage(event_type="transfer_cancelled", task_id=model.task_id, payload=model.model_dump(mode="json")), force=True)
            return model
        except Exception as exc:
            model.status = "failed" if model.status != "interrupted" else "interrupted"
            model.error_message = str(exc) or exc.__class__.__name__
            model.current_speed = 0
            model.eta_seconds = None
            await self.persist(model)
            await self.progress.emit(EventMessage(event_type="transfer_failed", task_id=model.task_id, payload=model.model_dump(mode="json")), force=True)
            return model
        from app.models import utc_now
        model.status = "completed"
        model.transferred_bytes = model.total_bytes
        model.finished_at = utc_now()
        model.current_speed = 0
        model.eta_seconds = 0
        model.current_file = None
        model.resume_metadata = {key: value for key, value in model.resume_metadata.items()
                                 if key in {"endpoints", "route", "route_detail"}}
        await self.persist(model)
        await self.progress.emit(EventMessage(event_type="transfer_completed", task_id=model.task_id, payload=model.model_dump(mode="json")), force=True)
        return model

    async def _run_entries(self, task: MutableTask) -> None:
        model = task.model
        completed_before = int(model.resume_metadata.get("completed_entries", 0))
        if not 0 <= completed_before <= len(task.entries):
            raise ValueError("Resume metadata is inconsistent with the file manifest")
        model.transferred_bytes = sum(entry.size for entry in task.entries[:completed_before])
        for index, entry in enumerate(task.entries):
            if index < completed_before:
                continue
            if task.control.cancel.is_set():
                raise TransferCancelled()
            if task.control.pause.is_set():
                raise TransferPaused()
            target = self._destination_for(model, entry)
            if entry.is_symlink:
                raise ValueError(f"Symbolic links are not transferred: {entry.relative_path}")
            if entry.is_dir:
                await self.destination_io.mkdir(target)
                model.resume_metadata["completed_entries"] = index + 1
                continue
            model.current_file = entry.relative_path
            transferred = await self._transfer_file(task, entry, target, index)
            if transferred:
                model.resume_metadata["completed_entries"] = index + 1
                model.resume_metadata.pop("offset", None)
                model.resume_metadata.pop("part_size", None)
                await self.persist(model)
        model.current_file = None

    async def _transfer_file(self, task: MutableTask, entry: TransferEntry, target: str, index: int) -> bool:
        model = task.model
        source_path = self._source_for(model, entry)
        source_stat = await self.source_io.stat(source_path)
        if source_stat.size != entry.size or source_stat.mtime != entry.mtime:
            raise ValueError(
                f"Source changed since the transfer was created: {entry.relative_path} "
                f"(expected {entry.size} bytes, got {source_stat.size})"
            )
        if await self.destination_io.exists(target) and model.conflict_strategy in {"skip", "ask"}:
            if model.conflict_strategy == "ask":
                raise ValueError(f"Target already exists: {target}")

            model.transferred_bytes += entry.size
            return True

        part = self._part_path(model, target, index)
        task.current_part = part
        resume_requested = model.conflict_strategy == "resume" or model.resume_metadata.get("resuming") or "offset" in model.resume_metadata
        part_exists = await self.destination_io.exists(part)
        offset = 0
        if resume_requested and part_exists:
            part_stat = await self.destination_io.stat(part)
            if part_stat.size > source_stat.size:
                offset = 0
            else:
                offset = part_stat.size
        elif part_exists:
            offset = 0

        if target != source_path and await self.destination_io.exists(target) and model.conflict_strategy == "rename":
            target = await self.destination_io.unique_path(target)

        read_handle = None
        write_handle = None
        transferred_at_start = model.transferred_bytes
        started = time.monotonic()
        bytes_this_file = offset
        initial_offset = offset
        ema = model.current_speed
        try:
            if self.direct_copy:
                async def report(copied):
                    nonlocal ema
                    model.transferred_bytes = transferred_at_start + copied
                    elapsed = max(time.monotonic() - started, 1e-6)
                    speed = max(copied - initial_offset, 0) / elapsed
                    ema = speed if not ema else .25 * speed + .75 * ema
                    model.current_speed = ema
                    model.average_speed = speed
                    model.eta_seconds = max(model.total_bytes - model.transferred_bytes, 0) / ema if ema else None
                    await self.progress.emit(EventMessage(event_type="transfer_progress", task_id=model.task_id,
                                                         payload=model.model_dump(mode="json")))
                await self.direct_copy(task, source_path, part, offset, source_stat.size, report)
                # A failed direct attempt may have written a prefix. The relay
                # continues the same task part, never the existing target file.
                offset = (await self.destination_io.stat(part)).size if await self.destination_io.exists(part) else 0
                if offset > source_stat.size:
                    offset = 0
                bytes_this_file = offset
                model.transferred_bytes = transferred_at_start + offset
                if task.control.cancel.is_set():
                    raise TransferCancelled()
                if task.control.pause.is_set():
                    model.resume_metadata.update(completed_entries=index, offset=offset)
                    raise TransferPaused()
            read_handle = await self.source_io.open_read(source_path)
            write_handle = await self.destination_io.open_write(part, resume=offset > 0)
            while offset < source_stat.size:
                if task.control.cancel.is_set():
                    raise TransferCancelled()
                if task.control.pause.is_set():
                    model.resume_metadata.update({
                        "completed_entries": index,
                        "offset": offset,
                        "part_size": source_stat.size,
                        "source_size": source_stat.size,
                        "source_mtime": source_stat.mtime,
                    })
                    model.status = "pausing"
                    raise TransferPaused()
                chunk_size = min(self.settings.chunk_size, source_stat.size - offset)
                chunk = await self._read_chunk(read_handle, offset, chunk_size)
                if len(chunk) != chunk_size:
                    raise IOError(f"Short read at offset {offset}: expected {chunk_size}, got {len(chunk)}")
                await self._write_chunk(write_handle, offset, chunk)
                # Offset-based SFTP handles may expose write(offset=...); aiofiles uses seek().
                offset += len(chunk)
                bytes_this_file += len(chunk)
                model.transferred_bytes = transferred_at_start + bytes_this_file
                elapsed = max(time.monotonic() - started, 1e-6)
                instant = (bytes_this_file - initial_offset) / elapsed
                ema = instant if ema == 0 else 0.25 * instant + 0.75 * ema
                model.current_speed = ema
                model.average_speed = instant
                remaining = max(model.total_bytes - model.transferred_bytes, 0)
                model.eta_seconds = remaining / ema if ema > 0 else None
                await self.progress.emit(EventMessage(
                    event_type="transfer_progress", task_id=model.task_id,
                    payload=model.model_dump(mode="json"),
                ))
            flush = getattr(write_handle, "flush", None)
            if flush:
                result = flush()
                if asyncio.iscoroutine(result):
                    await result
            # Close buffered local handles before checking the on-disk size.
            await write_handle.close()
            write_handle = None
            final_stat = await self.destination_io.stat(part)
            if final_stat.size != source_stat.size:
                raise IOError("Final size verification failed")
            final_source = await self.source_io.stat(source_path)
            if final_source.size != source_stat.size or final_source.mtime != source_stat.mtime:
                raise ValueError("Source changed during the transfer")
        finally:
            for handle in (read_handle, write_handle):
                close = getattr(handle, "close", None)
                if close:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result

        await self._commit_part(task, part, target, source_stat.size)
        task.current_part = None
        return True


    @staticmethod
    def _is_aiofiles_handle(handle: Any) -> bool:
        # aiofiles wrappers only expose (*args, **kwargs) in their generated methods.
        return type(handle).__module__.startswith("aiofiles.")

    @staticmethod
    def _supports_keyword(func: Any, name: str) -> bool:
        try:
            code = getattr(func, "__func__", func).__code__
            if code.co_varnames[:code.co_argcount] == ("self", "args", "kwargs", "cb"):
                return False
            return name in code.co_varnames[:code.co_argcount]
        except (AttributeError, TypeError):
            return False

    async def _read_chunk(self, handle: Any, offset: int, size: int) -> bytes:
        if hasattr(handle, "read") and self._supports_keyword(handle.read, "offset"):
            result = handle.read(offset=offset, size=size)
        else:
            await handle.seek(offset)
            result = handle.read(size)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def _write_chunk(self, handle: Any, offset: int, data: bytes) -> None:
        if hasattr(handle, "write") and self._supports_keyword(handle.write, "offset"):
            result = handle.write(data, offset=offset)
        else:
            await handle.seek(offset)
            result = handle.write(data)
        if asyncio.iscoroutine(result):
            await result

    async def _commit_part(self, task: MutableTask, part: str, target: str, expected_size: int) -> None:
        model = task.model
        part_stat = await self.destination_io.stat(part)
        if part_stat.size != expected_size:
            raise IOError("Part-file size verification failed")
        if await self.destination_io.exists(target):
            if model.conflict_strategy == "skip":
                await self.destination_io.remove(part)
                return
            if model.conflict_strategy == "overwrite":
                await self.destination_io.replace(part, target)
                return
            if model.conflict_strategy == "resume":
                await self.destination_io.replace(part, target)
                return
            if model.conflict_strategy == "ask":
                raise FileExistsError(f"Target already exists: {target}")
            target = await self.destination_io.unique_path(target)
        await self.destination_io.rename(part, target)

    async def _cleanup_part(self, task: MutableTask) -> None:
        if not task.current_part:
            return
        try:
            if await self.destination_io.exists(task.current_part):
                await self.destination_io.remove(task.current_part)
        except Exception:
            pass
        task.current_part = None

    @staticmethod
    def _destination_for(model: TransferOut, entry: TransferEntry) -> str:
        import posixpath
        if not entry.relative_path:
            return model.destination_path
        if model.direction in {"upload", "remote"}:
            return posixpath.join(model.destination_path, entry.relative_path)
        return str(Path(model.destination_path) / entry.relative_path)

    @staticmethod
    def _source_for(model: TransferOut, entry: TransferEntry) -> str:
        if not entry.relative_path:
            return model.source_path
        if model.direction == "upload":
            return str(Path(model.source_path) / entry.relative_path)
        import posixpath
        return posixpath.join(model.source_path, entry.relative_path)

    @staticmethod
    def _part_path(model: TransferOut, target: str, index: int) -> str:
        if model.direction in {"upload", "remote"}:
            import posixpath
            directory = posixpath.dirname(target)
            return posixpath.join(directory, f".{model.task_id}.{index}.part")
        target_path = Path(target)
        return str(target_path.parent / f".{model.task_id}.{index}.part")
