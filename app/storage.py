from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from app.models import ProfileCreate, ProfileOut, ProfileUpdate, TransferOut, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT,
    auth_method TEXT NOT NULL DEFAULT 'auto',
    private_key_path TEXT,
    remote_root TEXT NOT NULL DEFAULT '~',
    connect_timeout REAL NOT NULL DEFAULT 10,
    keepalive_interval REAL NOT NULL DEFAULT 15,
    ssh_alias TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    task_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    transferred_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    current_speed REAL NOT NULL DEFAULT 0,
    average_speed REAL NOT NULL DEFAULT 0,
    eta_seconds REAL,
    error_message TEXT,
    conflict_strategy TEXT NOT NULL,
    resume_metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status);
CREATE INDEX IF NOT EXISTS idx_transfers_created ON transfers(created_at DESC);
"""


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not connected")
        return self._db

    async def create_profile(self, data: ProfileCreate, source: str = "manual") -> ProfileOut:
        now = utc_now().isoformat()
        profile_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO profiles
            (id, name, host, port, username, auth_method, private_key_path, remote_root,
             connect_timeout, keepalive_interval, ssh_alias, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id, data.name, data.host, data.port, data.username, data.auth_method,
                data.private_key_path, data.remote_root, data.connect_timeout,
                data.keepalive_interval, data.ssh_alias, source, now, now,
            ),
        )
        await self.db.commit()
        return await self.get_profile(profile_id)

    async def list_profiles(self) -> list[ProfileOut]:
        async with self.db.execute("SELECT * FROM profiles ORDER BY name") as cursor:
            rows = await cursor.fetchall()
        return [self._profile_from_row(row) for row in rows]

    async def get_profile(self, profile_id: str) -> ProfileOut:
        async with self.db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(profile_id)
        return self._profile_from_row(row)

    async def update_profile(self, profile_id: str, data: ProfileUpdate) -> ProfileOut:
        current = await self.get_profile(profile_id)
        values = data.model_dump(exclude_unset=True)
        if not values:
            return current
        # Validate the combined profile before persisting partial updates.
        ProfileOut.model_validate({**current.model_dump(), **values})
        values["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        await self.db.execute(
            f"UPDATE profiles SET {assignments} WHERE id = ?", (*values.values(), profile_id)
        )
        await self.db.commit()
        return await self.get_profile(profile_id)

    async def delete_profile(self, profile_id: str) -> None:
        await self.db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await self.db.commit()

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> ProfileOut:
        return ProfileOut(
            id=row["id"], name=row["name"], host=row["host"], port=row["port"],
            username=row["username"], auth_method=row["auth_method"],
            private_key_path=row["private_key_path"], remote_root=row["remote_root"],
            connect_timeout=row["connect_timeout"], keepalive_interval=row["keepalive_interval"],
            ssh_alias=row["ssh_alias"], source=row["source"],
            created_at=parse_dt(row["created_at"]) or utc_now(),
            updated_at=parse_dt(row["updated_at"]) or utc_now(),
        )

    async def save_transfer(self, task: TransferOut) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO transfers
            (task_id, profile_id, direction, source_path, destination_path, total_bytes,
             transferred_bytes, status, created_at, started_at, finished_at, current_speed,
             average_speed, eta_seconds, error_message, conflict_strategy, resume_metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.task_id, task.profile_id, task.direction, task.source_path,
                task.destination_path, task.total_bytes, task.transferred_bytes, task.status,
                task.created_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
                task.current_speed, task.average_speed, task.eta_seconds,
                task.error_message, task.conflict_strategy, json.dumps(task.resume_metadata),
            ),
        )
        await self.db.commit()

    async def list_transfers(self, status: str | None = None) -> list[TransferOut]:
        if status:
            query = "SELECT * FROM transfers WHERE status = ? ORDER BY created_at DESC"
            args = (status,)
        else:
            query = "SELECT * FROM transfers ORDER BY created_at DESC"
            args = ()
        async with self.db.execute(query, args) as cursor:
            rows = await cursor.fetchall()
        return [self._transfer_from_row(row) for row in rows]

    async def get_transfer(self, task_id: str) -> TransferOut | None:
        async with self.db.execute("SELECT * FROM transfers WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        return self._transfer_from_row(row) if row else None

    async def clear_finished_transfers(self) -> None:
        await self.db.execute(
            "DELETE FROM transfers WHERE status IN ('completed', 'failed', 'cancelled')"
        )
        await self.db.commit()

    async def mark_startup_interrupted(self) -> None:
        await self.db.execute(
            "UPDATE transfers SET status='interrupted', error_message=? "
            "WHERE status IN ('queued', 'running', 'pausing')",
            ("Service restarted; reconnect and retry/resume the transfer.",),
        )
        await self.db.commit()

    @staticmethod
    def _transfer_from_row(row: sqlite3.Row) -> TransferOut:
        return TransferOut(
            task_id=row["task_id"], profile_id=row["profile_id"], direction=row["direction"],
            source_path=row["source_path"], destination_path=row["destination_path"],
            total_bytes=row["total_bytes"], transferred_bytes=row["transferred_bytes"],
            status=row["status"], created_at=parse_dt(row["created_at"]) or utc_now(),
            started_at=parse_dt(row["started_at"]), finished_at=parse_dt(row["finished_at"]),
            current_speed=row["current_speed"], average_speed=row["average_speed"],
            eta_seconds=row["eta_seconds"], error_message=row["error_message"],
            conflict_strategy=row["conflict_strategy"], resume_metadata=json.loads(row["resume_metadata"]),
        )
