from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINISFTP_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    app_name: str = "MiniSFTP Web"
    local_root: str | None = None
    transfer_concurrency: int = 2
    chunk_size: int = 262144
    ssh_config_path: str = "~/.ssh/config"
    theme: Literal["dark", "light"] = "dark"

    @property
    def resolved_local_root(self) -> Path:
        root = Path(self.local_root or "~").expanduser()
        return root.resolve()

    @property
    def data_dir(self) -> Path:
        from platformdirs import user_data_dir

        path = Path(user_data_dir("MiniSFTPWeb", "Hannis"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "minisftp.sqlite3"

    @property
    def known_hosts_path(self) -> Path:
        return self.data_dir / "known_hosts"

    def validate_runtime(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("MiniSFTP Web must only bind to a loopback address")
        if not 1 <= self.transfer_concurrency <= 4:
            raise ValueError("transfer_concurrency must be between 1 and 4")
        if self.chunk_size < 64 * 1024 or self.chunk_size > 4 * 1024 * 1024:
            raise ValueError("chunk_size must be between 64 KiB and 4 MiB")
        root = self.resolved_local_root
        if not root.exists() or not root.is_dir():
            raise RuntimeError(f"local root does not exist: {root}")
