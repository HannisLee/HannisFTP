# MiniSFTP Web

MiniSFTP Web is a local, single-user SFTP file manager with independent tabs on both sides. Each tab browses the local computer or an SSH server. Transfer actions and directional connection status appear in the middle, with live tasks below.

It uses **SFTP over SSH**. Browsing, file management, and local relay use AsyncSSH. Direct server-to-server copying runs the source server's native OpenSSH `sftp` client over an SSH command channel. SSH-config `ProxyCommand` helpers such as `cloudflared` are launched locally by AsyncSSH when configured.

## Features

- Independent local/remote tabs in both panes
- Drag tabs between panes or reorder them within a pane, preserving their browsing directory
- Restore a local tab with **添加本机**, choosing the left or right pane in the toolbar
- Manage each remote connection separately, including reopening a closed tab
- Remote-to-remote direct transfer with automatic streaming relay through this computer
- Automatic bidirectional dedicated SSH key setup and pinned destination host keys
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

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app.main
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

Choose the target pane in the toolbar and click **添加远程** to select an alias from `~/.ssh/config` or a saved profile. Move a server tab to the opposite pane by dragging its label or using its arrow button. Closing a tab retains its SSH session; **管理连接** reopens or disconnects individual servers. Drag files into the opposite pane, or select files and use the directional transfer buttons.

For password or key-passphrase profiles, create a profile from **新建配置**. Credentials are used in memory for the current connection and are not written to SQLite, the app config, or logs.

### ISLAB-CF / Cloudflare Access

The existing alias can be selected directly in the UI:

```ssh-config
Host ISLAB-CF
    HostName islab.lihan.online
    User lihan
    ProxyCommand cloudflared access ssh --hostname %h
```

Install `cloudflared` and ensure it is on the PATH inherited by the app. Cloudflare Access must have a valid login session, and normal SSH key/agent/password authentication is still required. If your Access session has expired, authenticate with `ssh ISLAB-CF` first, then reconnect in the app. The configured command is trusted local configuration and is executed as the current user.

The app expands the SSH config path and lets AsyncSSH resolve HostName, User, Port, ProxyCommand and ProxyJump. Alias connections inherit their configured port. Discovery supports multiple aliases, tab/equals separators, Include files, and wildcard defaults; Match-dependent display metadata is best effort, while connections use AsyncSSH's configuration parser.

On 2026-10-09, ISLAB-CF was verified from Windows through both the web UI and the actual transfer manager: directory browsing, binary upload/download, SHA-256 roundtrip verification, nested Unicode paths, empty directories/files, rename, atomic overwrite, pause/resume, and cancellation cleanup. Tests use a unique remote audit directory and remove it afterward.

Remote-to-remote checks also verified ISLAB-CF → ISLAB-40 as a direct transfer and ISLAB-40 → ISLAB-CF as an automatic local relay, with 2,000,013-byte SHA-256 roundtrip validation, quoted/bracketed Unicode filenames, empty files/directories, and cleanup. When the computer's private-IP route became unavailable, the audit connected ISLAB-40 through ISLAB-CF; the application API and connection picker support that same jump path.

### Host keys

On first connection, MiniSFTP shows the key type and SHA256 fingerprint. You must explicitly choose **Trust and connect** before it appends the key to its `known_hosts`. Confirmation is bound to the fingerprint displayed in the dialog. Changed or revoked keys are refused.

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

The default suite includes an ephemeral authenticated SSH/SFTP server and temporary local data directories. It does not write to your app history or require a real server.

To run the opt-in real-server roundtrip on Windows (uses existing SSH configuration and trusted host keys):

```powershell
$env:MINISFTP_TEST_SSH_ALIAS = "ISLAB-CF"
.\.venv\Scripts\python.exe -m pytest -q -s tests/test_live_sftp.py
Remove-Item Env:MINISFTP_TEST_SSH_ALIAS
```

This creates only `.minisftp-audit-<random id>` beneath the remote home, uploads generated test data, verifies downloads, and removes the audit directory in a finally block. A failed cleanup is reported by the test.

## Configuration

Environment variables can be placed in `.env`; see `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `MINISFTP_HOST` | `127.0.0.1` | Loopback bind address only |
| `MINISFTP_PORT` | `8000` | Local port |
| `MINISFTP_LOCAL_ROOT` | unset | Optional boundary for local filesystem access; starts at user home when unset |
| `MINISFTP_TRANSFER_CONCURRENCY` | `2` | Simultaneous transfers, 1–4 |
| `MINISFTP_CHUNK_SIZE` | `262144` | Transfer chunk size |
| `MINISFTP_SSH_CONFIG_PATH` | `~/.ssh/config` | OpenSSH configuration file |
| `MINISFTP_DATA_PATH` | platform user data directory | Optional isolated SQLite/host-key data directory |

For saved profiles, **Remote root** is both the initial remote directory and the allowed remote root. Use `~` for the login user's home, or `/` if you explicitly want to allow server-root browsing. SSH config aliases default to `~`.

The LOCAL pane opens at the user home by default, but can navigate upward and browse other local folders. On Windows, returning above a drive root opens **此电脑**, listing the machine's current drive letters (including mapped drives); double-click a drive to enter it. The list refreshes from Windows rather than assuming particular letters. On macOS/Linux, navigation ends at `/`, with no drive-letter view. Setting `MINISFTP_LOCAL_ROOT` explicitly restricts local browsing and transfers to that directory.

## Data locations

- SQLite: platform user data directory (`MiniSFTPWeb/minisftp.sqlite3`)
- Host keys: platform user data directory (`MiniSFTPWeb/known_hosts`)
- SSH config: `~/.ssh/config`

## Current limitations

### Remote-to-remote transfers

Choose a remote tab on each side. The center checks **both directions separately**, since the source server's network access to the destination can differ in each direction. Select entries and use the arrow buttons, or drag entries into the other pane. Switching tabs does not change an existing task's saved source/destination servers.

When adding a remote tab, **连接路径** can use this computer's SSH configuration or an already-connected server as a jump. This supports an internal server reachable through ISLAB-CF even when the computer cannot reach its private IP directly. The destination still authenticates independently and verifies its host key; no agent forwarding is enabled. Disconnecting a jump also closes its dependent connections; the UI blocks this while a dependent transfer is active.

Following the application's enabled key-exchange behavior, the first pair check creates a dedicated `~/.ssh/hannisftp_ed25519` key pair on each server, appends the other server's public key to `authorized_keys` with OpenSSH's `restrict` option, and records the already-verified destination host key in `~/.ssh/hannisftp_known_hosts`. Existing dedicated keys and existing authorized-key lines are preserved. No local SSH Agent forwarding is used. Dedicated private keys are generated transiently in application memory, written only to their owning server, and never returned to the browser, stored in SQLite, or logged. These credentials remain on the servers after disconnecting so subsequent sessions can reuse them.

Direct copying requires a POSIX command shell, native OpenSSH `sftp`, writable SSH configuration for both login accounts, public-key authentication accepted by the destination, and source-to-destination connectivity to its resolved hostname/port. Local `ProxyCommand`/`ProxyJump` routes are not assumed to exist on the source host. For example, a CF tunnel may permit this computer to connect while another server cannot reach the CF hostname directly; that direction uses local relay. Windows servers, restricted shells, chroot path differences, or denied key setup can also fall back to relay. Files are streamed through this computer without staging complete copies on its disk.

Direct and relayed routes use the same destination-side task part and atomic final commit. A failed direct copy continues its part through relay after the direct writer has exited. Pause/cancel stop that writer before handling its part; if stopping cannot be confirmed, the task fails instead of starting a second writer. Task cards retain the chosen route and both server names, including after restart.

### Other limitations

- Symbolic links and Windows junctions are detected and rejected rather than followed. Application path checks are not a server-side sandbox and cannot prevent a concurrent external process from swapping filesystem entries between checking and opening them.
- Browser file-picker upload and inline remote file preview/editing are not included in this first version.
- Resume uses this app's task-specific `.part` files and preserves the original conflict strategy. Source size and modification time are checked before and after copying; changes which preserve both metadata fields cannot be detected by these checks.
- Remote overwrite requires the server's atomic `posix-rename` support. If replacement fails or the extension is unavailable, the original destination is preserved and the task fails with its partial file retained.
- Windows local paths and Cloudflare ProxyCommand connections are covered by the regression suite and real-server checks.
