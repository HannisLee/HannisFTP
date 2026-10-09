from pathlib import Path
import os
import pytest


async def test_local_list_sorts_directories_first(settings, temp_root):
    from app.services.local_file_service import LocalFileService
    service = LocalFileService(settings)
    (temp_root / "zdir").mkdir()
    (temp_root / "a.txt").write_text("hello")
    result = await service.list_dir(str(temp_root), sort="name:asc")
    assert [entry.name for entry in result.entries] == ["source", "target", "zdir", "a.txt"]
    assert result.entries[2].is_dir
    assert result.total_files == 1


async def test_path_escape_rejected(settings):
    from app.services.local_file_service import LocalFileService
    service = LocalFileService(settings)
    try:
        service.validate("../../outside")
    except Exception as exc:
        assert "outside the allowed local root" in str(exc)
    else:
        raise AssertionError("escape was not rejected")


async def test_local_operations(settings, temp_root):
    from app.services.local_file_service import LocalFileService
    service = LocalFileService(settings)
    path = await service.mkdir(str(temp_root), "新建 目录")
    assert Path(path).exists()
    renamed = await service.rename(path, "renamed")
    assert Path(renamed).exists()
    await service.delete(renamed)
    assert not Path(renamed).exists()


async def test_local_root_cannot_be_renamed_or_deleted(settings, temp_root):
    from app.services.local_file_service import LocalFileService
    service = LocalFileService(settings)
    with pytest.raises(PermissionError):
        await service.rename(str(temp_root), "other")
    with pytest.raises(PermissionError):
        await service.delete(str(temp_root), recursive=True)
    assert (await service.list_dir(str(temp_root))).parent is None


async def test_local_links_are_rejected_before_resolution(settings, temp_root):
    from app.services.local_file_service import LocalFileService
    from app.services.transfer_worker import LocalTransferIO
    service = LocalFileService(settings)
    target = temp_root / "source" / "real.txt"
    target.write_text("original")
    link = temp_root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks requires OS privileges")
    for operation in [service.delete(str(link)), service.rename(str(link), "renamed"), LocalTransferIO(temp_root).open_read(str(link))]:
        with pytest.raises(ValueError):
            await operation
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name != "nt", reason="Windows filename rules")
@pytest.mark.parametrize("name", ["..\\escape", "C:\\escape", "CON", "file:stream", "trailing."])
async def test_windows_local_names_are_validated(settings, temp_root, name):
    from app.services.local_file_service import LocalFileService
    with pytest.raises(ValueError):
        await LocalFileService(settings).mkdir(str(temp_root), name)
