from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    is_symlink: bool
    size: int = 0
    modified_at: float | None = None
    permissions: str | None = None


class FileListResponse(BaseModel):
    path: str
    parent: str | None = None
    entries: list[FileEntry]
    total_files: int
    total_directories: int


class PathRequest(BaseModel):
    path: str = Field(min_length=1)


class RenameRequest(BaseModel):
    path: str
    new_name: str = Field(min_length=1, pattern=r"^[^/\x00]+$")


class ProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None
    auth_method: Literal["auto", "agent", "key", "password"] = "auto"
    private_key_path: str | None = None
    remote_root: str = "~"
    connect_timeout: float = Field(default=10, ge=1, le=60)
    keepalive_interval: float = Field(default=15, ge=0, le=120)
    ssh_alias: str | None = None


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    auth_method: Literal["auto", "agent", "key", "password"] | None = None
    private_key_path: str | None = None
    remote_root: str | None = None
    connect_timeout: float | None = Field(default=None, ge=1, le=60)
    keepalive_interval: float | None = Field(default=None, ge=0, le=120)
    ssh_alias: str | None = None


class ProfileOut(ProfileBase):
    id: str
    source: Literal["manual", "ssh_config"]
    created_at: datetime
    updated_at: datetime


class SSHHost(BaseModel):
    alias: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    source: str = "~/.ssh/config"


class ConnectionCreate(BaseModel):
    profile_id: str | None = None
    ssh_alias: str | None = None
    password: str | None = Field(default=None, exclude=True)
    private_key_passphrase: str | None = Field(default=None, exclude=True)
    confirm_host_key: bool = False


class ConnectionOut(BaseModel):
    id: str
    profile_id: str
    profile_name: str
    host: str
    username: str | None
    connected_at: datetime
    remote_root: str


class HostKeyInfo(BaseModel):
    host: str
    port: int
    key_type: str | None = None
    fingerprint: str | None = None
    reason: str = "unknown_host_key"


class ConnectionStatus(BaseModel):
    connected: bool
    connection: ConnectionOut | None = None


class TransferCreate(BaseModel):
    direction: Literal["upload", "download"]
    source_path: str = Field(min_length=1)
    destination_path: str = Field(min_length=1)
    connection_id: str
    conflict_strategy: Literal["ask", "skip", "overwrite", "rename", "resume"] = "ask"


class TransferOut(BaseModel):
    task_id: str
    profile_id: str
    direction: Literal["upload", "download"]
    source_path: str
    destination_path: str
    total_bytes: int
    transferred_bytes: int
    status: Literal[
        "queued", "running", "pausing", "paused", "completed", "failed", "cancelled", "interrupted"
    ]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_speed: float = 0
    average_speed: float = 0
    eta_seconds: float | None = None
    error_message: str | None = None
    conflict_strategy: str
    resume_metadata: dict[str, Any] = Field(default_factory=dict)
    current_file: str | None = None


class EventMessage(BaseModel):
    event_type: str
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
