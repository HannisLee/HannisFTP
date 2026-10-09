"""Run OpenSSH SFTP on the source host; only control/progress crosses the PC."""
from __future__ import annotations

import asyncio
import shlex
import hashlib
import posixpath

import asyncssh

from app.services.path_safety import remote_kind


class DirectWriterUncertain(Exception):
    """Do not start another writer when the remote process couldn't be stopped."""


def batch_path(path: str) -> str:
    if any(char in path for char in "\r\n\x00"):
        raise ValueError("This filename requires local relay")
    # OpenSSH's makeargv() escapes quoted glob characters itself. Escaping
    # them again would look for a literal backslash before e.g. '['.
    escaped = ''.join('\\' + char if char in '\\"' else char for char in path)
    return '"' + escaped + '"'


class DirectTransfer:
    def __init__(self, source, destination):
        self.source = source
        self.destination = destination
        self.key_path = None
        self.known_hosts_path = None
        self.host_alias = hashlib.sha256(destination.profile_id.encode()).hexdigest()[:24]

    async def prepare(self):
        sessions = sorted({self.source.id: self.source, self.destination.id: self.destination}.values(), key=lambda s: s.id)
        async with sessions[0].lock:
            if len(sessions) == 1:
                await self._prepare_pair()
            else:
                async with sessions[1].lock:
                    await self._prepare_pair()

    async def _prepare_pair(self):
        source_key, source_known, source_public = await self._identity(self.source)
        destination_key, destination_known, destination_public = await self._identity(self.destination)
        await self._authorize(self.destination, source_public)
        await self._authorize(self.source, destination_public)
        await self._trust(self.source, self.destination, source_known)
        await self._trust(self.destination, self.source, destination_known)
        self.key_path, self.known_hosts_path = source_key, source_known

    @staticmethod
    async def _ssh_dir(session):
        sftp = await session.service.sftp()
        home = await sftp.realpath('.')
        folder = posixpath.join(home, '.ssh')
        if not await sftp.exists(folder):
            try:
                await sftp.mkdir(folder, asyncssh.SFTPAttrs(permissions=0o700))
            except asyncssh.SFTPFileAlreadyExists:
                pass
        attrs = await sftp.lstat(folder)
        if remote_kind(attrs)[1] or not remote_kind(attrs)[0]:
            raise ValueError('The SSH directory must be a real directory')
        return sftp, folder

    @staticmethod
    async def _regular(sftp, path):
        try:
            attrs = await sftp.lstat(path)
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
            return False
        if remote_kind(attrs)[1] or remote_kind(attrs)[0]:
            raise ValueError('SSH configuration files must be regular files')
        return True

    @classmethod
    async def _identity(cls, session):
        sftp, folder = await cls._ssh_dir(session)
        key_path = posixpath.join(folder, 'hannisftp_ed25519')
        public_path = key_path + '.pub'
        if not await cls._regular(sftp, key_path):
            if await cls._regular(sftp, public_path):
                raise ValueError('An existing dedicated public key requires manual repair')
            key = asyncssh.generate_private_key('ssh-ed25519', comment='HannisFTP remote transfers')
            async with sftp.open(key_path, asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_EXCL,
                                 attrs=asyncssh.SFTPAttrs(permissions=0o600), encoding=None) as handle:
                await handle.write(key.export_private_key('openssh'))
            public = key.export_public_key('openssh')
            async with sftp.open(public_path, asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_EXCL,
                                 attrs=asyncssh.SFTPAttrs(permissions=0o644), encoding=None) as handle:
                await handle.write(public)
        else:
            if not await cls._regular(sftp, public_path):
                raise ValueError('Dedicated SSH public key is missing; existing private key was preserved')
            async with sftp.open(public_path, 'rb') as handle:
                public = await handle.read(16384)
        public = asyncssh.import_public_key(public).export_public_key('openssh').decode().strip()
        return key_path, posixpath.join(folder, 'hannisftp_known_hosts'), public

    @classmethod
    async def _authorize(cls, session, public):
        sftp, folder = await cls._ssh_dir(session)
        path = posixpath.join(folder, 'authorized_keys')
        existing = ''
        if await cls._regular(sftp, path):
            async with sftp.open(path, 'r') as handle:
                existing = await handle.read()
        if public.split()[1] not in [word for line in existing.splitlines() for word in line.split()[:3]]:
            async with sftp.open(path, asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_APPEND,
                                 attrs=asyncssh.SFTPAttrs(permissions=0o600), encoding='utf-8') as handle:
                await handle.write(('\n' if existing and not existing.endswith('\n') else '') + 'restrict ' + public + '\n')
        await sftp.chmod(path, 0o600)

    @classmethod
    async def _trust(cls, source, destination, path):
        sftp = await source.service.sftp()
        alias = hashlib.sha256(destination.profile_id.encode()).hexdigest()[:24]
        key = destination.connection.get_server_host_key().export_public_key('openssh').decode().strip()
        # HostKeyAlias is already the complete lookup name, including on
        # non-default ports. It must not get OpenSSH's usual [host]:port form.
        marker = alias
        lines = []
        if await cls._regular(sftp, path):
            async with sftp.open(path, 'r') as handle:
                lines = (await handle.read()).splitlines()
        lines = [line for line in lines if not line.startswith(marker + ' ')]
        lines.append(marker + ' ' + key)
        async with sftp.open(path, 'w', attrs=asyncssh.SFTPAttrs(permissions=0o600)) as handle:
            await handle.write('\n'.join(lines) + '\n')
        await sftp.chmod(path, 0o600)

    def command(self) -> str:
        conn = self.destination.connection
        host = conn.get_extra_info("host") or self.destination.host
        port = conn.get_extra_info("port", 22)
        username = self.destination.username
        if not username or any(c in host + username for c in "\r\n\x00") or host.startswith('-'):
            raise ValueError("Destination cannot be used for direct SFTP")
        args = [
            "exec", "sftp", "-b", "-", "-P", str(port),
            "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=5",
            "-oConnectionAttempts=1", "-oServerAliveInterval=5", "-oServerAliveCountMax=2",
            "-oForwardAgent=no", "-oClearAllForwardings=yes", "-oProxyCommand=none", "-oProxyJump=none",
            "-oUser=" + username,
        ]
        if self.key_path:
            args.extend(['-i', self.key_path, '-oIdentitiesOnly=yes', '-oUserKnownHostsFile=' + self.known_hosts_path,
                         '-oGlobalKnownHostsFile=/dev/null', '-oHostKeyAlias=' + self.host_alias])
        target = '[' + host + ']' if ':' in host and not host.startswith('[') else host
        return shlex.join(args + ['--', target])

    async def probe(self) -> dict:
        try:
            await self.prepare()
        except Exception:
            return {"route": "relay", "detail": "双方免密配置未成功，将自动由本机中转", "keys_configured": False}
        process = None
        try:
            process = await self.source.connection.create_process(self.command(), encoding='utf-8')
            process.stdin.write('pwd\nquit\n')
            process.stdin.write_eof()
            await asyncio.wait_for(process.communicate(), 8)
            if process.exit_status == 0:
                return {"route": "direct", "detail": "服务器直连可用"}
            return {"route": "relay", "detail": "双方免密已配置，但直连不可用，将自动由本机中转", "keys_configured": True}
        except Exception:
            return {"route": "relay", "detail": "双方免密已配置，直连检测超时或失败，将自动由本机中转", "keys_configured": True}
        finally:
            if process:
                if process.exit_status is None:
                    try:
                        await self._stop(process)
                    except Exception:
                        pass
                process.close()
                await process.wait_closed()

    async def copy(self, task, source_path, part, offset, size, report, destination_io):
        # Path validation still runs through the authenticated SFTP services.
        await self.source.service.checked_path(source_path)
        await self.destination.service.checked_path(part, allow_missing=True)
        batch = f"{'reput' if offset else 'put'} {batch_path(source_path)} {batch_path(part)}\nquit\n"
        process = await self.source.connection.create_process(self.command(), encoding="utf-8")
        waiter = None
        try:
            process.stdin.write(batch)
            process.stdin.write_eof()
            # communicate drains stdout/stderr while we poll the destination.
            waiter = asyncio.create_task(process.communicate())
            while not waiter.done():
                if task.control.cancel.is_set() or task.control.pause.is_set():
                    await self._stop(process)
                    return
                if await destination_io.exists(part):
                    await report(min((await destination_io.stat(part)).size, size))
                await asyncio.wait({waiter}, timeout=.2)
            _, stderr = await waiter
            if process.exit_status != 0:
                raise OSError('Direct SFTP stopped: ' + stderr.strip()[:400])
            await report(size)
        finally:
            if waiter and not waiter.done():
                await self._stop(process)
                waiter.cancel()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)
            process.close()
            await process.wait_closed()

    @staticmethod
    async def _stop(process):
        # Terminate the remote writer before resuming its part through the PC.
        if process.exit_status is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait_closed(), 5)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait_closed(), 5)
            except asyncio.TimeoutError as exc:
                raise DirectWriterUncertain('Remote writer did not stop; retry after checking the server') from exc
