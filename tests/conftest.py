from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "source").mkdir()
    (tmp_path / "target").mkdir()
    return tmp_path


@pytest.fixture
def settings(temp_root: Path) -> Settings:
    return Settings(
        local_root=str(temp_root),
        transfer_concurrency=2,
        chunk_size=64 * 1024,
        host="127.0.0.1",
        port=8000,
    )
