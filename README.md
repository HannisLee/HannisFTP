# MiniSFTP Web

MiniSFTP Web is a local, single-user SFTP file manager with a standard XFTP-style dual-pane UI: local files on the left, remote files on the right, transfer actions in the middle, and a live task panel at the bottom.

It uses **SFTP over SSH only** (via AsyncSSH). It does not shell out to `scp`, `ssh`, `rm`, `mv`, or `mkdir`.

## Features

- XFTP-style local/remote dual-pane browser
- Upload and download single files, multiple files, and nested directories
- Real streaming transfers with progress, speed, and ETA
- Pause, resume, cancel, retry, and chunk-based resume from `.part` files
- Conflict strategies: Ask, Skip, Overwrite, Rename, Resume
- SSH Agent, private key, passphrase, and password authentication
- Reads existing `~/.ssh/config` host aliases
- Secure host-key verification with explicit fingerprint confirmation
- SQLite task history and restart recovery metadata
- WebSocket live progress updates with automatic reconnect
- Dark/light theme

## Install

Requires Python 3.11 or newer.

```bash
uv venv
uv pip install -e ".[dev]"
```

With pip:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
python -m app.main
# or
minisftp-web
```

Open <http://127.0.0.1:8000>.

The server binds only to `127.0.0.1`. Do not expose it through a public interface or reverse proxy without adding a separate authentication layer.

## SSH configuration

Use your existing OpenSSH configuration:

```ssh-config
Host prod
    HostName 203.0.113.10
    User alice
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

The web UI lists aliases from `~/.ssh/config` under *~/.ssh/config*. Choose one and connect.

For password or key-passphrase profiles, create a profile from **New Connection**. Credentials are used in memory for the current connection and are not written to SQLite, the app config, or logs.

### Host keys

On first connection, MiniSFTP shows the key type and SHA256 fingerprint. You must explicitly choose **Trust and connect** before it appends the key to its `known_hosts`. If a host key later changes, the connection is refused.

## Usage

1. Select a server and click **Connect**.
2. Browse the left LOCAL pane and right REMOTE pane.
3. Select one or more entries.
4. Click **Upload →** or **← Download**, or drag selected entries to the opposite pane.
5. Use the bottom Transfers panel to pause, resume, cancel, or retry tasks.

## Development and tests

```bash
pytest
```

Optional real-server integration tests can be added with environment-provided SSH credentials. Tests use temporary directories and local in-memory implementations by default, so no remote server is needed for the core test suite.

## Configuration

Environment variables can be placed in `.env`; see `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `MINISFTP_HOST` | `127.0.0.1` | Loopback bind address only |
| `MINISFTP_PORT` | `8000` | Local port |
| `MINISFTP_LOCAL_ROOT` | user home | Allowed local filesystem root |
| `MINISFTP_TRANSFER_CONCURRENCY` | `2` | Simultaneous transfers, 1–4 |
| `MINISFTP_CHUNK_SIZE` | `262144` | Transfer chunk size |

For saved profiles, **Remote root** is both the initial remote directory and the allowed remote root. Use `~` for the login user's home, or `/` if you explicitly want to allow server-root browsing. SSH config aliases default to `~`.

## Data locations

- SQLite: platform user data directory (`MiniSFTPWeb/minisftp.sqlite3`)
- Host keys: platform user data directory (`MiniSFTPWeb/known_hosts`)
- SSH config: `~/.ssh/config`

## Current limitations

- Symbolic links are detected and rejected rather than followed.
- Browser file-picker upload and inline remote file preview/editing are not included in this first version.
- Resume is reliable for transfers created by this application; source files are revalidated before resuming.
- Windows local-path support is intended but the project is primarily tested on macOS and Linux.
