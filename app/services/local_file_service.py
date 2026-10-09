from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from app.models import FileEntry, FileListResponse
from app.core.config import Settings


class PathOutsideRootError(ValueError):
    pass


class LocalFileService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.resolved_local_root

    def validate(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (ValueError, RuntimeError) as exc:
            raise PathOutsideRootError(f"Path is outside the allowed local root: {self.root}") from exc
        return resolved

    async def list_dir(self, path: str, show_hidden: bool = False, sort: str = "name") -> FileListResponse:
        target = self.validate(path)
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Not a directory: {target}")
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, self._scan, target)
        if not show_hidden:
            rows = [row for row in rows if not row.name.startswith(".")]
        key, direction = self._parse_sort(sort)
        rows.sort(key=key, reverse=direction == "desc")
        files = sum(not entry.is_dir for entry in rows)
        dirs = sum(entry.is_dir for entry in rows)
        parent = str(target.parent) if target != target.parent else None
        return FileListResponse(path=str(target), parent=parent, entries=rows, total_files=files, total_directories=dirs)

    def _scan(self, target: Path) -> list[FileEntry]:
        entries: list[FileEntry] = []
        for item in target.iterdir():
            try:
                info = item.lstat()
            except OSError:
                continue
            is_link = stat.S_ISLNK(info.st_mode)
            try:
                final_info = item.stat()
                is_dir = final_info.st_dir if hasattr(final_info, "st_dir") else stat.S_ISDIR(final_info.st_mode)
                size = final_info.st_size if not is_dir else 0
                modified = final_info.st_mtime
            except OSError:
                is_dir, size, modified = False, 0, None
            entries.append(
                FileEntry(
                    name=item.name,
                    path=str(item),
                    is_dir=is_dir,
                    is_symlink=is_link,
                    size=size,
                    modified_at=modified,
                    permissions=self._mode(info.st_mode),
                )
            )
        return entries

    async def mkdir(self, parent: str, name: str) -> str:
        target = self.validate(Path(parent) / name)
        target.mkdir(parents=False, exist_ok=False)
        return str(target)

    async def rename(self, path: str, new_name: str) -> str:
        source = self.validate(path)
        target = self.validate(source.parent / new_name)
        if target.exists():
            raise FileExistsError(target)
        os.rename(source, target)
        return str(target)

    async def delete(self, path: str, recursive: bool = False) -> None:
        target = self.validate(path)
        if target == self.root:
            raise PermissionError("Refusing to delete the allowed local root")
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                raise IsADirectoryError("Directory is not empty")
            await asyncio.to_thread(self._rmtree, target)
        else:
            target.unlink()

    def _rmtree(self, path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                self._rmtree(child)
            else:
                child.unlink()
        path.rmdir()

    async def stat(self, path: str) -> tuple[int, float, bool]:
        target = self.validate(path)
        info = target.stat()
        return info.st_size, info.st_mtime, target.is_dir()

    async def exists(self, path: str) -> bool:
        return self.validate(path).exists()

    async def unlink(self, path: str) -> None:
        self.validate(path).unlink()

    async def mkdir_if_missing(self, path: str) -> None:
        self.validate(path).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_sort(sort: str) -> tuple[tuple[bool, str, int, float], str]:
        field, _, direction = sort.partition(":")
        direction = direction or "asc"
        def key(entry: FileEntry):
            return (not entry.is_dir, entry.name.casefold(), entry.size, entry.modified_at or 0)
        if field == "size":
            return lambda e: (not e.is_dir, e.size, e.name.casefold()), direction
        if field == "time":
            return lambda e: (not e.is_dir, e.modified_at or 0, e.name.casefold()), direction
        return key, direction

    @staticmethod
    def _mode(mode: int) -> str:
        perms = ""
        for shift in (6, 3, 0):
            bits = (mode >> shift) & 7
            perms += ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
        return ("d" if stat.S_ISDIR(mode) else "-") + perms
