from __future__ import annotations

import posixpath
import stat
from dataclasses import dataclass
from typing import Any

import asyncssh

from app.models import FileEntry, FileListResponse


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

    async def sftp(self) -> asyncssh.SFTPClient:
        if self._sftp is None or self._sftp.is_closed():
            self._sftp = await self.connection.start_sftp_client()
        return self._sftp

    async def close(self) -> None:
        if self._sftp and not self._sftp.is_closed():
            self._sftp.close()

    @staticmethod
    def normalize(path: str) -> str:
        if "\x00" in path:
            raise RemotePathError("Remote path contains a NUL byte")
        if not path.startswith("/"):
            path = posixpath.join("~", path)
        return posixpath.normpath(path)

    async def set_allowed_root(self, path: str) -> str:
        sftp = await self.sftp()
        self.allowed_root = None
        root = await sftp.realpath(self.normalize(path))
        if not root.startswith("/"):
            raise RemotePathError("SFTP did not return an absolute path")
        self.allowed_root = root
        return root

    async def realpath(self, path: str) -> str:
        sftp = await self.sftp()
        result: str = await sftp.realpath(self.normalize(path))
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
            is_dir = bool(getattr(attrs, "is_dir", lambda: stat.S_ISDIR(attrs.permissions or 0))())
            is_symlink = bool(getattr(attrs, "is_symlink", lambda: False)())
            entries.append(
                FileEntry(
                    name=item.filename,
                    path=full,
                    is_dir=is_dir,
                    is_symlink=is_symlink,
                    size=int(getattr(attrs, "size", 0) or 0) if not is_dir else 0,
                    modified_at=float(getattr(attrs, "mtime", 0) or 0) or None,
                    permissions=str(getattr(attrs, "permissions_str", "")) or None,
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
            parent=posixpath.dirname(real) if real != "/" else None,
            entries=entries,
            total_files=sum(not e.is_dir for e in entries),
            total_directories=sum(e.is_dir for e in entries),
        )

    async def stat(self, path: str) -> RemoteStat:
        sftp = await self.sftp()
        attrs = await sftp.stat(self.normalize(path))
        return RemoteStat(int(getattr(attrs, "size", 0) or 0), float(getattr(attrs, "mtime", 0) or 0), bool(attrs.is_dir()))

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except asyncssh.SFTPNoSuchPath:
            return False
        except asyncssh.SFTPError:
            return False

    async def mkdir(self, parent: str, name: str) -> str:
        sftp = await self.sftp()
        target = posixpath.join(await self.realpath(parent), name)
        await sftp.mkdir(target)
        return target

    async def rename(self, path: str, new_name: str) -> str:
        if "/" in new_name or "\x00" in new_name:
            raise RemotePathError("Invalid remote file name")
        sftp = await self.sftp()
        source = await self.realpath(path)
        target = posixpath.join(posixpath.dirname(source), new_name)
        await sftp.rename(source, target)
        return target

    async def delete(self, path: str, recursive: bool = False) -> None:
        sftp = await self.sftp()
        target = await self.realpath(path)
        info = await sftp.stat(target)
        if not info.is_dir():
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
