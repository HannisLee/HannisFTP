from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from app.core.config import Settings
from app.models import ConnectionOut, HostKeyInfo, ProfileOut, utc_now
from app.services.sftp_service import SFTPService


class HostKeyConfirmationRequired(Exception):
    def __init__(self, info: HostKeyInfo, host_key: Any) -> None:
        self.info = info
        self.host_key = host_key
        super().__init__("SSH host key is not known")


class HostKeyMismatch(Exception):
    pass


@dataclass
class HostKeyVerificationClient(asyncssh.SSHClient):
    def __init__(self, manager: "ConnectionManager", confirm: bool) -> None:
        self.manager = manager
        self.confirm = confirm
        self.pending: HostKeyInfo | None = None
        self.error: HostKeyMismatch | None = None

    @staticmethod
    def _fingerprint(key: Any) -> str:
        fingerprint = key.get_fingerprint()
        return fingerprint if fingerprint.startswith("SHA256:") else f"SHA256:{fingerprint}"

    def validate_host_public_key(self, host: str, addr: str, port: int, key: Any) -> bool:
        try:
            known_files = [
                str(self.manager.known_hosts_path),
                str(Path("~/.ssh/known_hosts").expanduser()),
            ]
            known_files = [path for path in known_files if Path(path).exists()]
            if known_files:
                known = asyncssh.read_known_hosts(known_files)
                port_arg = port if port != 22 else None
                trusted_keys = known.match(host, addr, port_arg)[0]
                if trusted_keys:
                    if key in trusted_keys:
                        return True
                    self.error = HostKeyMismatch(
                        "SSH host key does not match the trusted key in known_hosts; connection refused"
                    )
                    return False
            if self.confirm:
                self.manager._append_known_host(host, port, key)
                return True
            self.pending = HostKeyInfo(
                host=host,
                port=port,
                key_type=getattr(key, "algorithm", None) or "unknown",
                fingerprint=self._fingerprint(key),
            )
            return False
        except HostKeyMismatch:
            raise
        except Exception as exc:
            self.error = HostKeyMismatch(f"Host key verification failed: {exc}")
            return False


class ConnectionSession:
    id: str
    profile_id: str
    profile_name: str
    host: str
    username: str | None
    remote_root: str
    created_at: float
    connection: asyncssh.SSHClientConnection
    service: SFTPService
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def out(self) -> ConnectionOut:
        from app.models import utc_now
        return ConnectionOut(
            id=self.id,
            profile_id=self.profile_id,
            profile_name=self.profile_name,
            host=self.host,
            username=self.username,
            connected_at=utc_now(),
            remote_root=self.remote_root,
        )


class ConnectionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[str, ConnectionSession] = {}
        self.known_hosts_path = settings.known_hosts_path

    async def connect_profile(
        self,
        profile: ProfileOut,
        *,
        password: str | None = None,
        passphrase: str | None = None,
        confirm_host_key: bool = False,
    ) -> ConnectionSession:
        existing = self.find_by_profile(profile.id)
        if existing:
            return existing
        client = HostKeyVerificationClient(self, confirm_host_key)
        try:
            conn = await self._connect(profile, password, passphrase, client)
        except asyncssh.HostKeyNotVerifiable as exc:
            if client.error:
                raise client.error from exc
            if client.pending:
                raise HostKeyConfirmationRequired(client.pending, None) from exc
            raise HostKeyMismatch("SSH host key verification failed") from exc
        service = SFTPService(conn)
        remote_root = await service.set_allowed_root(profile.remote_root)
        session = ConnectionSession(
            id=uuid.uuid4().hex,
            profile_id=profile.id,
            profile_name=profile.name,
            host=profile.host or profile.ssh_alias or "remote",
            username=profile.username,
            remote_root=remote_root,
            created_at=time.time(),
            connection=conn,
            service=service,
        )
        self.sessions[session.id] = session
        return session

    async def connect_ssh_alias(
        self, alias: str, *, password: str | None = None, passphrase: str | None = None,
        confirm_host_key: bool = False,
    ) -> ConnectionSession:
        profile = ProfileOut(
            id=f"ssh-config:{alias}", name=alias, host=alias, port=22, username=None,
            auth_method="auto", private_key_path=None, remote_root="~", connect_timeout=10,
            keepalive_interval=15, ssh_alias=alias, source="ssh_config",
            created_at=utc_now(), updated_at=utc_now(),
        )
        client = HostKeyVerificationClient(self, confirm_host_key)
        try:
            conn = await self._connect(profile, password, passphrase, client)
        except asyncssh.HostKeyNotVerifiable as exc:
            if client.error:
                raise client.error from exc
            if client.pending:
                raise HostKeyConfirmationRequired(client.pending, None) from exc
            raise HostKeyMismatch("SSH host key verification failed") from exc
        service = SFTPService(conn)
        remote_root = await service.set_allowed_root(profile.remote_root)
        session = ConnectionSession(
            id=uuid.uuid4().hex, profile_id=profile.id, profile_name=alias, host=alias,
            username=None, remote_root="~", created_at=time.time(), connection=conn, service=service,
        )
        self.sessions[session.id] = session
        return session

    async def _connect(
        self, profile: ProfileOut, password: str | None, passphrase: str | None,
        client: asyncssh.SSHClient,
    ) -> asyncssh.SSHClientConnection:
        kwargs: dict[str, Any] = {
            "host": profile.host or profile.ssh_alias,
            "port": profile.port,
            "known_hosts": self._known_hosts_argument(),
            "client_factory": lambda: client,
            "connect_timeout": profile.connect_timeout,
            "keepalive_interval": profile.keepalive_interval,
        }
        if profile.source == "ssh_config":
            kwargs["config"] = self.settings.ssh_config_path
        if profile.username:
            kwargs["username"] = profile.username
        if profile.auth_method == "agent":
            kwargs["agent_path"] = "*"
        elif profile.auth_method == "key" and profile.private_key_path:
            kwargs["client_keys"] = [profile.private_key_path]
            if passphrase:
                kwargs["passphrase"] = passphrase
        elif profile.auth_method == "password":
            if not password:
                raise ValueError("Password authentication requires a password")
            kwargs["password"] = password
            kwargs["preferred_auth"] = "password"
        elif profile.auth_method == "auto" and password:
            kwargs["password"] = password
        return await asyncssh.connect(**kwargs)

    def _known_hosts_argument(self):
        files = [str(self.known_hosts_path), str(Path("~/.ssh/known_hosts").expanduser())]
        existing = [path for path in files if Path(path).exists()]
        return existing or b""

    def _append_known_host(self, host: str, port: int, host_key: Any) -> None:
        self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        # Always record the bracketed non-default-port form. For port 22, keep the
        # conventional unbracketed hostname form used by OpenSSH.
        marker = host if port == 22 else f"[{host}]:{port}"
        public = host_key.export_public_key("openssh").decode("ascii").strip()
        with self.known_hosts_path.open("a", encoding="ascii") as handle:
            handle.write(f"{marker} {public}\n")
        try:
            self.known_hosts_path.chmod(0o600)
        except OSError:
            pass

    def get(self, connection_id: str) -> ConnectionSession:
        session = self.sessions.get(connection_id)
        if not session or session.connection.is_closed():
            raise KeyError("Connection is not active")
        return session

    def find_by_profile(self, profile_id: str) -> ConnectionSession | None:
        for session in self.sessions.values():
            if session.profile_id == profile_id and not session.connection.is_closed():
                return session
        return None

    def list(self) -> list[ConnectionOut]:
        return [session.out() for session in self.sessions.values() if not session.connection.is_closed()]

    async def disconnect(self, connection_id: str) -> None:
        session = self.sessions.pop(connection_id, None)
        if not session:
            return
        await session.service.close()
        session.connection.close()

    async def close_all(self) -> None:
        for session in list(self.sessions.values()):
            await self.disconnect(session.id)
