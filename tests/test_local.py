from pathlib import Path


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
