from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import config
from app.core.config import Settings
from app.services import local_file_service
from app.services.local_file_service import DRIVES_PATH, LocalFileService
from app.services.transfer_worker import LocalTransferIO


@pytest.mark.skipif(os.name != "nt", reason="Windows navigation")
async def test_home_can_go_up_and_drive_root_opens_actual_drives(monkeypatch):
    drives = [Path("C:/"), Path("D:/")]
    monkeypatch.setattr(config, "windows_drives", lambda: tuple(drives))
    service = LocalFileService(Settings(_env_file=None, local_root=None))
    monkeypatch.setattr(service, "_scan", lambda path: [])
    home = await service.list_dir(str(service.root))
    assert home.parent == str(service.root.parent)
    drive = await service.list_dir("C:/")
    assert drive.parent == DRIVES_PATH
    listing = await service.list_dir(drive.parent)
    assert listing.parent is None
    assert [entry.name for entry in listing.entries] == ["C:", "D:"]
    assert listing.total_directories == 2
    # Drive enumeration is refreshed, so mounted/removed drives are reflected.
    drives[:] = [Path("C:/"), Path("E:/"), Path("W:/")]
    assert [entry.name for entry in (await service.list_dir(DRIVES_PATH)).entries] == ["C:", "E:", "W:"]
    with pytest.raises(ValueError):
        service.validate(DRIVES_PATH)
    with pytest.raises(PermissionError):
        await service.delete("C:/", recursive=True)


def test_non_windows_default_uses_filesystem_root_without_drive_view(monkeypatch):
    # Replace each module's OS reference without changing pathlib's real OS.
    monkeypatch.setattr(config, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(local_file_service, "os", SimpleNamespace(name="posix"))
    def unexpected_windows_api():
        raise AssertionError("Windows drive API must not run on macOS/Linux")
    monkeypatch.setattr(config, "windows_drives", unexpected_windows_api)
    settings = Settings(_env_file=None, local_root=None)
    service = LocalFileService(settings)
    assert settings.allowed_local_roots == (Path("/"),)
    assert service.drives_path is None


async def test_explicit_root_keeps_configured_boundary(tmp_path):
    service = LocalFileService(Settings(_env_file=None, local_root=str(tmp_path)))
    assert service.drives_path is None
    assert (await service.list_dir(str(tmp_path))).parent is None
    with pytest.raises(ValueError):
        await service.list_dir(DRIVES_PATH)


async def test_transfer_io_uses_all_allowed_roots(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = first / "file.bin"
    source.write_bytes(b"cross root")
    io = LocalTransferIO((first, second))
    reader = await io.open_read(str(source))
    try:
        payload = await reader.read()
    finally:
        await reader.close()
    writer = await io.open_write(str(second / source.name), resume=False)
    try:
        await writer.write(payload)
    finally:
        await writer.close()
    assert (second / source.name).read_bytes() == payload
    with pytest.raises(ValueError):
        await io.open_write(str(tmp_path / "outside.bin"), resume=False)
