from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import getpass

import asyncssh
import pytest

from app.core.config import Settings
from app.models import ProfileOut, TransferCreate, utc_now
from app.services.connection_manager import ConnectionManager
from app.services.direct_transfer import DirectTransfer, DirectWriterUncertain, batch_path
from app.services.local_file_service import LocalFileService
from app.services.progress_manager import ProgressManager
from app.services.transfer_manager import TransferManager
from app.storage import Storage


@pytest.fixture
async def remote_pair(tmp_path):
    roots = [tmp_path / 'source', tmp_path / 'destination']
    for root in roots:
        root.mkdir()
    listeners = []
    settings = Settings(local_root=str(tmp_path), data_path=str(tmp_path / 'data'), chunk_size=65536)
    connections = ConnectionManager(settings)
    sessions = []
    for index, root in enumerate(roots):
        class Server(asyncssh.SSHServer):
            def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
                return True

            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == 'test-user' and password == 'test-password'

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key, auth_root=root):
                path = auth_root / '.ssh' / 'authorized_keys'
                return path.exists() and key.export_public_key().decode().split()[1] in path.read_text()

        # Execute the actual OpenSSH SFTP binary as the source-side client.
        # Only test harness paths are translated out of its SFTP chroot.
        async def execute(process, exec_root=root):
            binary = shutil.which('sftp')
            if not binary:
                process.exit(127)
                return
            args = shlex.split(process.command)
            if args[:2] != ['exec', 'sftp']:
                process.exit(127)
                return
            args = args[2:]
            for i, arg in enumerate(args):
                if i and args[i - 1] == '-i':
                    args[i] = str(exec_root / arg.lstrip('/'))
                    if os.name == 'nt':
                        acl = await asyncio.create_subprocess_exec('icacls', args[i], '/inheritance:r',
                                                                  '/grant:r', getpass.getuser() + ':R',
                                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await acl.communicate()
                if arg.startswith('-oUserKnownHostsFile='):
                    args[i] = '-oUserKnownHostsFile=' + str(exec_root / arg.split('=', 1)[1].lstrip('/'))
            batch = await process.stdin.read()
            lines = []
            for line in batch.splitlines():
                parts = shlex.split(line)
                if parts and parts[0] in {'put', 'reput'}:
                    source_path = parts[1]
                    for char in '*?[]':
                        source_path = source_path.replace('\\' + char, char)
                    native_source = (exec_root / source_path.lstrip('/')).as_posix()
                    line = f'{parts[0]} {batch_path(native_source)} {batch_path(parts[2])}'
                lines.append(line)
            child = await asyncio.create_subprocess_exec(binary, *args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await child.communicate(('\n'.join(lines) + '\n').encode())
            process.stdout.write(stdout.decode(errors='replace'))
            process.stderr.write(stderr.decode(errors='replace'))
            process.exit(child.returncode)

        host_key = asyncssh.generate_private_key('ssh-ed25519')
        listener = await asyncssh.create_server(
            Server, '127.0.0.1', 0, server_host_keys=[host_key], process_factory=execute,
            sftp_factory=lambda channel, chroot=root: asyncssh.SFTPServer(channel, chroot=str(chroot)),
        )
        listeners.append(listener)
        profile = ProfileOut(id=f'pair-{index}', name=f'Server {index}', host='127.0.0.1',
                             port=listener.get_port(), username='test-user', auth_method='password',
                             source='manual', created_at=utc_now(), updated_at=utc_now())
        sessions.append(await connections.connect_profile(profile, password='test-password', confirm_host_key=True,
                                                          host_key_fingerprint=host_key.get_fingerprint()))
    storage = Storage(settings.database_path)
    await storage.connect()
    manager = TransferManager(settings, storage, connections, LocalFileService(settings), ProgressManager())
    await manager.start()
    try:
        yield manager, sessions, roots
    finally:
        await manager.shutdown()
        await connections.close_all()
        await storage.close()
        for listener in listeners:
            listener.close()
            await listener.wait_closed()


async def complete(model):
    async def wait():
        while model.status in {'queued', 'running', 'pausing'}:
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), 20)
    assert model.status == 'completed', model.error_message


async def create_remote(manager, sessions, source='/folder', strategy='overwrite'):
    return await manager.create(TransferCreate(direction='remote', connection_id=sessions[0].id,
                                               destination_connection_id=sessions[1].id, source_path=source,
                                               destination_path='/', conflict_strategy=strategy))


async def test_remote_relay_nested_binary_empty_and_persistence(remote_pair, monkeypatch):
    manager, sessions, roots = remote_pair
    folder = roots[0] / 'folder'
    (folder / 'nested' / 'empty').mkdir(parents=True)
    payload = os.urandom(300007)
    (folder / 'nested' / '中文.bin').write_bytes(payload)
    (folder / 'zero').touch()
    async def unavailable(self):
        return {'route': 'relay', 'detail': 'Network unavailable'}
    monkeypatch.setattr(DirectTransfer, 'probe', unavailable)
    task = await create_remote(manager, sessions)
    await complete(task)
    assert (roots[1] / 'folder/nested/中文.bin').read_bytes() == payload
    assert (roots[1] / 'folder/nested/empty').is_dir()
    assert (roots[1] / 'folder/zero').stat().st_size == 0
    saved = await manager.storage.get_transfer(task.task_id)
    assert saved.direction == 'remote'
    assert saved.resume_metadata['route'] == 'relay'
    assert saved.resume_metadata['endpoints']['destination_profile_id'] == sessions[1].profile_id


async def test_real_native_direct_sftp_and_idempotent_key_exchange(remote_pair):
    if not shutil.which('sftp'):
        pytest.skip('OpenSSH SFTP executable is unavailable')
    manager, sessions, roots = remote_pair
    (roots[0] / '.ssh').mkdir()
    existing_key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()
    (roots[0] / '.ssh/authorized_keys').write_text(existing_key)
    direct, route = await manager.route(sessions[0].id, sessions[1].id)
    if route['route'] != 'direct':
        diagnostic = await sessions[0].connection.run(direct.command(), input='pwd\nquit\n')
        pytest.fail(f'{direct.command()} {(roots[0] / ".ssh/hannisftp_known_hosts").read_text()} {diagnostic.stdout} {diagnostic.stderr}')
    private_before = [(root / '.ssh/hannisftp_ed25519').read_bytes() for root in roots]
    await direct.prepare()
    assert [(root / '.ssh/hannisftp_ed25519').read_bytes() for root in roots] == private_before
    assert (roots[0] / '.ssh/authorized_keys').read_text().startswith(existing_key)
    assert (roots[0] / '.ssh/authorized_keys').read_text().count('restrict ') == 1
    (roots[0] / 'folder/empty').mkdir(parents=True)
    payload = os.urandom(500021)
    (roots[0] / 'folder/中文 空格.bin').write_bytes(payload)
    (roots[0] / 'folder/zero').touch()
    task = await create_remote(manager, sessions)
    await complete(task)
    assert task.resume_metadata['route'] == 'direct', task.resume_metadata
    assert (roots[1] / 'folder/中文 空格.bin').read_bytes() == payload
    assert (roots[1] / 'folder/empty').is_dir()
    assert (roots[1] / 'folder/zero').stat().st_size == 0


async def test_failed_direct_prefix_resumes_via_relay_preserving_target(remote_pair, monkeypatch):
    manager, sessions, roots = remote_pair
    payload = os.urandom(200017)
    (roots[0] / 'file.bin').write_bytes(payload)
    (roots[1] / 'file.bin').write_bytes(b'original')
    async def available(self):
        return {'route': 'direct', 'detail': 'Available'}
    async def fail(self, task, source, part, offset, size, report, io):
        assert (roots[1] / 'file.bin').read_bytes() == b'original'
        handle = await io.open_write(part, resume=False)
        await handle.write(payload[:77777])
        await handle.close()
        raise OSError('Disconnected')
    monkeypatch.setattr(DirectTransfer, 'probe', available)
    monkeypatch.setattr(DirectTransfer, 'copy', fail)
    task = await create_remote(manager, sessions, '/file.bin')
    await complete(task)
    assert task.resume_metadata['route'] == 'relay'
    assert (roots[1] / 'file.bin').read_bytes() == payload
    assert task.transferred_bytes == len(payload)
    assert not list(roots[1].glob('*.part'))


async def test_remote_self_copy_rejected(remote_pair):
    manager, sessions, roots = remote_pair
    (roots[0] / 'folder').mkdir()
    with pytest.raises(ValueError, match='own directory'):
        await create_remote(manager, [sessions[0], sessions[0]])


async def test_connect_through_existing_server_and_disconnect_dependencies(remote_pair):
    manager, sessions, roots = remote_pair
    previous = sessions[1]
    port = previous.connection.get_extra_info('port')
    await manager.connections.disconnect(previous.id)
    profile = ProfileOut(id=previous.profile_id, name=previous.profile_name, host='127.0.0.1', port=port,
                         username='test-user', auth_method='password', source='manual',
                         created_at=utc_now(), updated_at=utc_now())
    tunneled = await manager.connections.connect_profile(profile, password='test-password', via_connection_id=sessions[0].id)
    assert tunneled.out().via_connection_id == sessions[0].id
    (roots[1] / 'through-hop').mkdir()
    assert (await tunneled.service.list_dir('/')).entries[0].name == 'through-hop'
    await manager.connections.disconnect(sessions[0].id)
    assert not manager.connections.list()


async def test_remote_pause_native_resume_and_original_preserved(remote_pair, monkeypatch):
    if not shutil.which('sftp'):
        pytest.skip('OpenSSH SFTP executable is unavailable')
    manager, sessions, roots = remote_pair
    _, route = await manager.route(sessions[0].id, sessions[1].id)
    assert route['route'] == 'direct'
    payload = os.urandom(400017)
    (roots[0] / 'file.bin').write_bytes(payload)
    (roots[1] / 'file.bin').write_bytes(b'original')
    original_copy = DirectTransfer.copy
    async def pause_prefix(self, task, source, part, offset, size, report, io):
        handle = await io.open_write(part, resume=False)
        await handle.write(payload[:77777])
        await handle.close()
        task.control.pause.set()
    monkeypatch.setattr(DirectTransfer, 'copy', pause_prefix)
    task = await create_remote(manager, sessions, '/file.bin')
    for _ in range(300):
        if task.status == 'paused':
            break
        await asyncio.sleep(.01)
    assert task.status == 'paused', task.error_message
    assert (roots[1] / 'file.bin').read_bytes() == b'original'
    assert task.resume_metadata['offset'] == 77777
    assert (await manager.storage.get_transfer(task.task_id)).resume_metadata['endpoints']['destination_profile_id'] == sessions[1].profile_id
    monkeypatch.setattr(DirectTransfer, 'copy', original_copy)
    await manager.resume(task.task_id)
    await complete(task)
    assert task.resume_metadata['route'] == 'direct'
    assert (roots[1] / 'file.bin').read_bytes() == payload


async def test_remote_restart_cancel_cleans_destination_part(remote_pair, monkeypatch):
    manager, sessions, roots = remote_pair
    payload = os.urandom(100017)
    (roots[0] / 'file.bin').write_bytes(payload)
    (roots[1] / 'file.bin').write_bytes(b'original')
    async def available(self):
        return {'route': 'direct', 'detail': 'Available'}
    async def pause_prefix(self, task, source, part, offset, size, report, io):
        handle = await io.open_write(part, resume=False)
        await handle.write(payload[:77777])
        await handle.close()
        task.control.pause.set()
    monkeypatch.setattr(DirectTransfer, 'probe', available)
    monkeypatch.setattr(DirectTransfer, 'copy', pause_prefix)
    task = await create_remote(manager, sessions, '/file.bin')
    for _ in range(300):
        if task.status == 'paused':
            break
        await asyncio.sleep(.01)
    assert task.status == 'paused'
    await manager.shutdown()
    restored = TransferManager(manager.settings, manager.storage, manager.connections, manager.local, manager.progress)
    await restored.start()
    try:
        await restored.cancel(task.task_id)
        assert restored.get(task.task_id).model.status == 'cancelled'
        assert (roots[1] / 'file.bin').read_bytes() == b'original'
        assert not list(roots[1].glob('*.part'))
    finally:
        await restored.shutdown()


async def test_remote_key_setup_rejects_symlink(remote_pair):
    if os.name == 'nt':
        pytest.skip('Windows symlink creation requires additional privileges')
    manager, sessions, roots = remote_pair
    (roots[0] / 'elsewhere').mkdir()
    (roots[0] / '.ssh').symlink_to(roots[0] / 'elsewhere', target_is_directory=True)
    _, route = await manager.route(sessions[0].id, sessions[1].id)
    assert route['route'] == 'relay'
    assert not list((roots[0] / 'elsewhere').iterdir())


async def test_uncertain_direct_writer_fails_without_relay_or_commit(remote_pair, monkeypatch):
    manager, sessions, roots = remote_pair
    (roots[0] / 'file.bin').write_bytes(b'new data')
    (roots[1] / 'file.bin').write_bytes(b'original')
    async def available(self):
        return {'route': 'direct', 'detail': 'Available'}
    async def uncertain(self, task, source, part, offset, size, report, io):
        handle = await io.open_write(part, resume=False)
        await handle.write(b'new')
        await handle.close()
        raise DirectWriterUncertain('Writer may still be active')
    monkeypatch.setattr(DirectTransfer, 'probe', available)
    monkeypatch.setattr(DirectTransfer, 'copy', uncertain)
    task = await create_remote(manager, sessions, '/file.bin')
    for _ in range(300):
        if task.status == 'failed':
            break
        await asyncio.sleep(.01)
    assert task.status == 'failed'
    assert task.resume_metadata['route'] == 'direct'
    assert (roots[1] / 'file.bin').read_bytes() == b'original'
    assert list(roots[1].glob('*.part'))
