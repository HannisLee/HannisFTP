from __future__ import annotations

import posixpath
import stat
from dataclasses import dataclass
from typing import Any

import asyncssh

from app.models import FileEntry, FileListResponse
from app.services.path_safety import remote_kind, validate_name


@dataclass
class RemoteStat:
    size: int
    mtime: float
    is_dir: bool


class RemotePathError(ValueError):
    pass


class SFTPService:
    def __init__(self, connection: asyncssh.SSHClientConnection, allowed_root: str | None = None) -> None:
        self.connection = connection
        self._sftp: asyncssh.SFTPClient | None = None
        self.allowed_root: str | None = allowed_root
        self.home: str | None = None

    async def sftp(self) -> asyncssh.SFTPClient:
        if self._sftp is None:
            self._sftp = await self.connection.start_sftp_client()
        return self._sftp

    async def close(self) -> None:
        if self._sftp:
            self._sftp.exit()
            await self._sftp.wait_closed()
            self._sftp = None

    @staticmethod
    def normalize(path: str) -> str:
        if "\x00" in path:
            raise RemotePathError("Remote path contains a NUL byte")
        return posixpath.normpath(path)

    async def checked_path(self, path: str, *, allow_missing: bool = False) -> str:
        sftp = await self.sftp()
        if self.home is None:
            self.home = await sftp.realpath(".")
        if path == "~":
            path = self.home
        elif path.startswith("~/"):
            path = posixpath.join(self.home, path[2:])
        elif path.startswith("~"):
            raise RemotePathError("Only the login user's home is supported")
        elif not path.startswith("/"):
            path = posixpath.join(self.allowed_root or self.home, path)
        target = self.normalize(path)
        self._validate_allowed(target)
        current = "/"
        for part in path.split("/"):
            if not part or part == ".":
                continue
            current = posixpath.normpath(posixpath.join(current, part))
            try:
                attrs = await sftp.lstat(current)
            except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
                if allow_missing:
                    continue
                raise
            if remote_kind(attrs)[1]:
                raise RemotePathError("Symbolic links are not allowed")
        return target

    async def set_allowed_root(self, path: str) -> str:
        sftp = await self.sftp()
        self.allowed_root = None
        root = await sftp.realpath(await self.checked_path(path))
        if not root.startswith("/"):
            raise RemotePathError("SFTP did not return an absolute path")
        self.allowed_root = root
        if not remote_kind(await sftp.stat(root))[0]:
            raise RemotePathError("Remote root is not a directory")
        return root

    async def realpath(self, path: str) -> str:
        sftp = await self.sftp()
        result: str = await sftp.realpath(await self.checked_path(path))
        if not result.startswith("/"):
            raise RemotePathError("SFTP did not return an absolute path")
        self._validate_allowed(result)
        return result

    def _validate_allowed(self, path: str) -> None:
        if not self.allowed_root:
            return
        try:
            common = posixpath.commonpath([path, self.allowed_root])
        except ValueError as exc:
            raise RemotePathError(f"Remote path is outside the allowed root: {self.allowed_root}") from exc
        if common != self.allowed_root:
            raise RemotePathError(f"Remote path is outside the allowed root: {self.allowed_root}")

    async def list_dir(self, path: str, show_hidden: bool = False, sort: str = "name") -> FileListResponse:
        real = await self.realpath(path)
        sftp = await self.sftp()
        names = await sftp.readdir(real)
        entries: list[FileEntry] = []
        for item in names:
            if item.filename in {".", ".."}:
                continue
            if not show_hidden and item.filename.startswith("."):
                continue
            full = posixpath.join(real, item.filename)
            attrs: Any = item.attrs
            is_dir, is_symlink = remote_kind(attrs)
            entries.append(
                FileEntry(
                    name=item.filename,
                    path=full,
                    is_dir=is_dir,
                    is_symlink=is_symlink,
                    size=int(getattr(attrs, "size", 0) or 0) if not is_dir else 0,
                    modified_at=float(getattr(attrs, "mtime", 0) or 0) or None,
                    permissions=stat.filemode(attrs.permissions) if attrs.permissions is not None else None,
                )
            )
        field, _, direction = sort.partition(":")
        direction = direction or "asc"
        if field == "size":
            entries.sort(key=lambda e: (not e.is_dir, e.size, e.name.casefold()), reverse=direction == "desc")
        elif field == "time":
            entries.sort(key=lambda e: (not e.is_dir, e.modified_at or 0, e.name.casefold()), reverse=direction == "desc")
        else:
            entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()), reverse=direction == "desc")
        return FileListResponse(
            path=real,
            parent=posixpath.dirname(real) if real != self.allowed_root and real != "/" else None,
            entries=entries,
            total_files=sum(not e.is_dir for e in entries),
            total_directories=sum(e.is_dir for e in entries),
        )

    async def stat(self, path: str) -> RemoteStat:
        sftp = await self.sftp()
        attrs = await sftp.stat(await self.checked_path(path))
        return RemoteStat(int(attrs.size or 0), float(attrs.mtime or 0), remote_kind(attrs)[0])

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
            return False

    async def mkdir(self, parent: str, name: str) -> str:
        validate_name(name)
        sftp = await self.sftp()
        target = posixpath.join(await self.realpath(parent), name)
        await sftp.mkdir(target)
        return target

    async def rename(self, path: str, new_name: str) -> str:
        validate_name(new_name)
        sftp = await self.sftp()
        source = await self.realpath(path)
        if source == self.allowed_root:
            raise RemotePathError("Refusing to rename the allowed remote root")
        target = posixpath.join(posixpath.dirname(source), new_name)
        await self.checked_path(target, allow_missing=True)
        if await self.exists(target):
            raise FileExistsError(target)
        await sftp.rename(source, target)
        return target

    async def delete(self, path: str, recursive: bool = False) -> None:
        sftp = await self.sftp()
        target = await self.realpath(path)
        if target == self.allowed_root:
            raise RemotePathError("Refusing to delete the allowed remote root")
        info = await sftp.stat(target)
        if not remote_kind(info)[0]:
            await sftp.remove(target)
            return
        if not recursive:
            entries = await sftp.readdir(target)
            if any(item.filename not in {".", ".."} for item in entries):
                raise IsADirectoryError("Remote directory is not empty")
            await sftp.rmdir(target)
            return
        for item in await sftp.readdir(target):
            if item.filename in {".", ".."}:
                continue
            await self.delete(posixpath.join(target, item.filename), recursive=True)
        await sftp.rmdir(target)

    async def close_if_open(self) -> None:
        await self.close()
