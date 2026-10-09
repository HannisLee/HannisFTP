const state = {
  token: '',
  local: { path: '', parent: null, entries: [], sort: 'name:asc', selected: new Set(), showHidden: false },
  remote: { path: '', parent: null, entries: [], sort: 'name:asc', selected: new Set(), showHidden: false, active: false },
  connection: null,
  activeTab: 'all',
  transfers: new Map(),
  profiles: new Map(),
};

const $ = (id) => document.getElementById(id);
const els = {};
['serverSelect','connectBtn','disconnectBtn','connectionStatus','newProfileBtn','refreshHostsBtn','themeBtn',
 'localBody','remoteBody','localPath','remotePath','localStats','remoteStats','localBreadcrumb','remoteBreadcrumb',
 'transferList','toggleTransfers','clearFinished','toast','modalBackdrop','modal','uploadBtn','downloadBtn','conflictStrategy',
].forEach(id => els[id] = $(id));

function toast(message, error = false) {
  els.toast.textContent = message;
  els.toast.className = `toast show${error ? ' error' : ''}`;
  setTimeout(() => els.toast.className = 'toast', 4500);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const method = (options.method || 'GET').toUpperCase();
  if (method !== 'GET') headers['X-MiniSFTP-Token'] = state.token;
  if (options.body && typeof options.body !== 'string') {
    options.body = JSON.stringify(options.body);
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(`/api${path}`, { ...options, method, headers });
  const type = response.headers.get('content-type') || '';
  const data = type.includes('json') ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(typeof data === 'object' ? (data.detail || JSON.stringify(data)) : data);
    if (typeof data === 'object' && data) {
      error.code = data.code;
      error.host_key = data.host_key;
      error.path = data.path;
    }
    throw error;
  }
  return data;
}

function fmtSize(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B','KB','MB','GB','TB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
function fmtSpeed(v) { return v ? `${fmtSize(v)}/s` : '—'; }
function fmtTime(v) { return v == null ? '—' : v < 60 ? `${Math.ceil(v)}s` : `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`; }
function fmtDate(v) { return v ? new Date(v * 1000).toLocaleString() : ''; }
function basename(path) { return path.replace(/\/+$/, '').split('/').pop() || '/'; }
function dirname(path, pane) { return pane === 'local' ? path.split('/').slice(0, -1).join('/') || '/' : path.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/'; }

async function loadConfig() {
  const config = await api('/config');
  state.token = config.token;
  document.documentElement.dataset.theme = localStorage.getItem('minisftp-theme') || config.theme || 'dark';
  state.local.path = config.local_root;
  await Promise.all([loadFiles('local'), loadServers(), loadTransfers()]);
}

async function loadServers() {
  const [aliases, profiles, connections] = await Promise.all([api('/ssh-hosts'), api('/profiles'), api('/connections')]);
  els.serverSelect.innerHTML = '';
  const group = (label) => { const optgroup = document.createElement('optgroup'); optgroup.label = label; els.serverSelect.appendChild(optgroup); return optgroup; };
  const add = (value, text, target) => { const option = new Option(text, value); target.appendChild(option); };
  const aliasGroup = group('~/.ssh/config');
  aliases.forEach(item => add(`alias:${item.alias}`, `${item.alias}${item.user ? ` (${item.user}@${item.host || item.alias})` : ''}`, aliasGroup));
  const profileGroup = group('Saved profiles');
  profiles.forEach(item => add(`profile:${item.id}`, item.name, profileGroup));
  profiles.forEach(item => state.profiles.set(item.id, item));
  state.connection = connections[0] || null;
  updateConnectionState();
  if (connections[0]) {
    const option = [...els.serverSelect.options].find(item => item.value === `profile:${connections[0].profile_id}`);
    if (option) option.selected = true;
    state.remote.active = true;
    state.remote.path = connections[0].remote_root;
    await loadFiles('remote');
  }
}

function updateConnectionState() {
  const connected = !!state.connection;
  els.connectionStatus.textContent = connected ? 'Connected' : 'Offline';
  els.connectionStatus.className = `status ${connected ? 'online' : 'offline'}`;
  els.remotePath.disabled = !connected;
  els.uploadBtn.disabled = !connected;
  els.downloadBtn.disabled = !connected;
  els.uploadBtn.classList.toggle('disabled', !connected);
  els.downloadBtn.classList.toggle('disabled', !connected);
}

async function connect() {
  const value = els.serverSelect.value;
  if (!value) return toast('Select a server first', true);
  const [type, id] = value.split(':');
  let payload = type === 'alias' ? { ssh_alias: id } : { profile_id: id };
  const profile = type === 'profile' ? state.profiles.get(id) : null;
  if (profile && (profile.auth_method === 'password' || profile.private_key_path)) {
    const credentials = await credentialsModal(profile);
    if (!credentials) return;
    payload = { ...payload, ...credentials };
  }
  try {
    const result = await api('/connections', { method: 'POST', body: payload });
    state.connection = result;
    state.remote.active = true;
    state.remote.path = result.remote_root;
    updateConnectionState();
    await loadFiles('remote');
    toast(`Connected to ${result.profile_name}`);
  } catch (error) {
    if (error.code === 'host_key_confirmation_required') {
      confirmHostKey(payload, error.host_key, error.message);
      return;
    }
    toast(error.message, true);
  }
}

function confirmHostKey(payload, hostKey, message) {
  showModal(`<h3>Verify SSH Host Key</h3><p>${escapeHtml(message || '')}</p>
    <div class="host-key">${escapeHtml(hostKey.host)}:${hostKey.port}<br>${escapeHtml(hostKey.key_type || '')}<br>${escapeHtml(hostKey.fingerprint || '')}</div>
    <div class="modal-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-confirm>Trust and connect</button></div>`);
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
  els.modal.querySelector('[data-confirm]').onclick = async () => {
    closeModal();
    try {
      const result = await api('/connections', { method: 'POST', body: { ...payload, confirm_host_key: true } });
      state.connection = result; state.remote.active = true; state.remote.path = result.remote_root;
      updateConnectionState(); await loadFiles('remote'); toast('Host key saved and connected');
    } catch (error) { toast(error.message, true); }
  };
}

async function disconnect() {
  if (!state.connection) return;
  try { await api(`/connections/${state.connection.id}`, { method: 'DELETE' }); } catch (error) { toast(error.message, true); }
  state.connection = null; state.remote.active = false; state.remote.entries = []; state.remote.selected.clear();
  els.remoteBody.innerHTML = ''; els.remotePath.value = ''; updateConnectionState(); toast('Disconnected');
}

async function loadFiles(pane) {
  const info = state[pane];
  const query = new URLSearchParams({ path: info.path, show_hidden: info.showHidden, sort: info.sort });
  if (pane === 'remote') query.set('connection_id', state.connection.id);
  try {
    const result = await api(`/files/${pane}?${query}`);
    info.path = result.path; info.parent = result.parent; info.entries = result.entries; info.selected.clear();
    $(`${pane}Path`).value = result.path; $(`${pane}Stats`).textContent = `${result.total_files} files, ${result.total_directories} folders`;
    renderBreadcrumbs(pane); renderFiles(pane);
  } catch (error) { toast(error.message, true); }
}

function renderBreadcrumbs(pane) {
  const info = state[pane], element = $(`${pane}Breadcrumb`);
  element.innerHTML = '';
  const parts = info.path.split(/\/+/).filter(Boolean);
  const root = info.path.startsWith('/') ? '/' : '';
  const make = (path, label, last) => {
    const button = document.createElement('button'); button.textContent = label;
    button.onclick = () => { info.path = path; loadFiles(pane); };
    element.appendChild(button);
    if (!last) element.insertAdjacentText('beforeend', ' ›');
  };
  make(root || parts[0], pane === 'local' ? (root || parts[0]) : '/', parts.length <= 1 && (root || true));
  let current = root;
  parts.forEach((part, index) => {
    current = current === '/' ? `/${part}` : `${current}/${part}`;
    make(current, part, index === parts.length - 1);
  });
}

function renderFiles(pane) {
  const info = state[pane], body = $(`${pane}Body`);
  body.innerHTML = '';
  info.entries.forEach(entry => {
    const row = document.createElement('tr');
    row.dataset.path = entry.path; row.dataset.dir = String(entry.is_dir); row.dataset.name = entry.name;
    row.draggable = true;
    row.innerHTML = `<td class="${entry.is_dir ? 'folder' : ''}${entry.is_symlink ? ' link' : ''}">${escapeHtml(entry.name)}</td>
      <td>${entry.is_dir ? '<Folder>' : fmtSize(entry.size)}</td><td>${fmtDate(entry.modified_at)}</td><td>${entry.is_dir ? 'Directory' : entry.is_symlink ? 'Symlink' : 'File'}</td>`;
    row.onclick = (event) => {
      if (!event.metaKey && !event.ctrlKey && !event.shiftKey) info.selected.clear();
      info.selected.has(entry.path) ? info.selected.delete(entry.path) : info.selected.add(entry.path);
      renderFiles(pane);
    };
    row.ondblclick = async () => {
      if (entry.is_dir) { info.path = entry.path; await loadFiles(pane); }
      else toast('Select the entry and use Upload/Download');
    };
    row.ondragstart = (event) => {
      event.dataTransfer.setData('application/x-minisftp', JSON.stringify({ pane, paths: [...info.selected.size ? info.selected : new Set([entry.path])] }));
      event.dataTransfer.effectAllowed = 'copy';
    };
    body.appendChild(row);
  });
  [...body.children].forEach(row => row.classList.toggle('selected', info.selected.has(row.dataset.path)));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

async function openDirectory(pane) { const info = state[pane]; if (info.parent) { info.path = info.parent; await loadFiles(pane); } }
function bindPane(pane) {
  $(`${pane}Up`).onclick = () => openDirectory(pane);
  $(`${pane}Refresh`).onclick = () => loadFiles(pane);
  $(`${pane}Go`).onclick = () => { state[pane].path = $(`${pane}Path`).value.trim(); loadFiles(pane); };
  $(`${pane}Hidden`).onclick = () => { state[pane].showHidden = !state[pane].showHidden; $(`${pane}Hidden`).classList.toggle('active'); loadFiles(pane); };
  $(`${pane}Path`).onkeydown = (event) => { if (event.key === 'Enter') $(`${pane}Go`).click(); };
  $(`${pane}Mkdir`).onclick = async () => {
    const name = prompt('New folder name'); if (!name) return;
    const parent = state[pane].path;
    const body = pane === 'local' ? { path: `${parent}/${name}` } : { path: `${parent}/${name}` };
    try { await api(`/files/${pane}/mkdir${pane === 'remote' ? `?connection_id=${state.connection.id}` : ''}`, { method: 'POST', body }); await loadFiles(pane); }
    catch (error) { toast(error.message, true); }
  };
  document.querySelectorAll(`#${pane}Pane th`).forEach(th => th.onclick = () => {
    const [field, direction] = state[pane].sort.split(':');
    state[pane].sort = `${th.dataset.sort}:${field === th.dataset.sort && direction === 'asc' ? 'desc' : 'asc'}`;
    loadFiles(pane);
  });
  const drop = $(`${pane}Drop`);
  drop.ondragover = (event) => { if (event.dataTransfer.types.includes('application/x-minisftp')) { event.preventDefault(); drop.closest('.pane').classList.add('drop-target'); } };
  drop.ondragleave = () => drop.closest('.pane').classList.remove('drop-target');
  drop.ondrop = async (event) => {
    drop.closest('.pane').classList.remove('drop-target');
    const raw = event.dataTransfer.getData('application/x-minisftp'); if (!raw) return;
    event.preventDefault();
    const payload = JSON.parse(raw);
    if (payload.pane === pane) return;
    const direction = payload.pane === 'local' ? 'upload' : 'download';
    for (const path of payload.paths) await createTransfer(direction, path, state[pane].path);
  };
}

async function createTransfer(direction, sourcePath, destinationPath) {
  if (!state.connection) return toast('Connect to a remote server first', true);
  const conflictStrategy = els.conflictStrategy.value;
  try {
    const result = await api('/transfers', { method: 'POST', body: { direction, source_path: sourcePath, destination_path: destinationPath, connection_id: state.connection.id, conflict_strategy: conflictStrategy } });
    state.transfers.set(result.task_id, result); renderTransfers();
    els.transferList.classList.add('open');
    return result;
  } catch (error) {
    toast(error.message, true);
    if (error.message.startsWith('Target already exists:')) await loadTransfers();
  }
}

async function selectedTransfers(direction) {
  const sourcePane = direction === 'upload' ? 'local' : 'remote';
  const targetPane = direction === 'upload' ? 'remote' : 'local';
  const selected = state[sourcePane].selected;
  if (!selected.size) return toast(`Select items to ${direction}`, true);
  if (direction === 'upload' && !state.connection) return toast('Connect first', true);
  for (const path of [...selected]) await createTransfer(direction, path, state[targetPane].path);
}

async function loadTransfers() {
  const list = await api('/transfers');
  list.forEach(task => state.transfers.set(task.task_id, task));
  renderTransfers();
}

function transferMatches(task) {
  return state.activeTab === 'all' ||
    (state.activeTab === 'active' && ['running','paused','queued','pausing'].includes(task.status)) ||
    state.activeTab === task.status;
}

function renderTransfers() {
  els.transferList.innerHTML = '';
  [...state.transfers.values()].filter(transferMatches).sort((a,b) => b.created_at.localeCompare(a.created_at)).forEach(task => {
    const card = document.createElement('article'); card.className = 'transfer-card';
    const percent = task.total_bytes ? Math.min(100, task.transferred_bytes / task.total_bytes * 100) : 0;
    const name = basename(task.source_path);
    card.innerHTML = `<div class="transfer-main"><div class="transfer-title" title="${escapeHtml(task.source_path)} → ${escapeHtml(task.destination_path)}">${task.direction === 'upload' ? '↑' : '↓'} ${escapeHtml(name)}</div>
      <div class="transfer-controls"><button data-action="pause" title="Pause">❚❚</button><button data-action="resume" title="Resume">▶</button><button data-action="cancel" title="Cancel">✕</button><button data-action="retry" title="Retry">⟳</button></div></div>
      <div class="progress"><div class="progress-bar" style="width:${percent}%"></div></div>
      <div class="progress-info"><span>${fmtSize(task.transferred_bytes)} / ${fmtSize(task.total_bytes)} (${percent.toFixed(1)}%)</span><span>${fmtSize(task.current_speed)}/s</span><span>ETA ${fmtTime(task.eta_seconds)}</span></div>
      <div class="progress-info"><span>${escapeHtml(task.current_file || '')}</span><span class="status-badge ${task.status}">${task.status}</span></div>
      ${task.error_message ? `<div class="error-text">${escapeHtml(task.error_message)}</div>` : ''}`;
    card.querySelectorAll('button').forEach(button => button.onclick = () => transferAction(task.task_id, button.dataset.action));
    els.transferList.appendChild(card);
  });
}

async function transferAction(taskId, action) {
  try { const task = await api(`/transfers/${taskId}/${action}`, { method: 'POST' }); state.transfers.set(taskId, task); renderTransfers(); }
  catch (error) { toast(error.message, true); }
}

function connectWebsocket() {
  const ws = new WebSocket(`ws://${location.host}/ws/events?token=${encodeURIComponent(state.token)}`);
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data); if (!message.task_id) return;
    state.transfers.set(message.task_id, message.payload); renderTransfers();
  };
  ws.onclose = async () => {
    toast('Live updates disconnected; reconnecting…', true);
    try {
      // The local session token changes when the service restarts.
      state.token = (await api('/config')).token;
    } catch (_) {}
    setTimeout(connectWebsocket, 1000);
  };
}

function showModal(html) { els.modal.innerHTML = html; els.modalBackdrop.classList.remove('hidden'); }
function closeModal() { els.modalBackdrop.classList.add('hidden'); els.modal.innerHTML = ''; }

function credentialsModal(profile) {
  return new Promise(resolve => {
    const needsPassword = profile.auth_method === 'password';
    showModal(`<h3>SSH Credentials</h3>
      <p>${escapeHtml(profile.name)} (${escapeHtml(profile.username || 'current user')}@${escapeHtml(profile.host || profile.ssh_alias || '')})</p>
      <form id="credentialsForm" class="form-grid">
        ${needsPassword ? '<label class="full">Password<input name="password" type="password" autocomplete="current-password"></label>' : ''}
        ${profile.private_key_path ? `<label class="full">Key passphrase<input name="private_key_passphrase" type="password" autocomplete="current-password"></label>` : ''}
        <div class="modal-actions"><button type="button" class="btn" data-cancel>Cancel</button><button class="btn">Connect</button></div>
      </form>
      <p class="form-note">Credentials remain in memory for this connection and are not saved or logged.</p>`);
    const close = (value) => { closeModal(); resolve(value); };
    els.modal.querySelector('[data-cancel]').onclick = () => close(null);
    els.modal.querySelector('form').onsubmit = (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.target).entries());
      close({
        ...(data.password ? { password: data.password } : {}),
        ...(data.private_key_passphrase ? { private_key_passphrase: data.private_key_passphrase } : {}),
      });
    };
  });
}

function profileModal() {
  showModal(`<h3>New Connection</h3><form id="profileForm" class="form-grid">
    <label class="full">Name<input name="name" required></label>
    <label>Host<input name="host" required></label><label>Port<input name="port" type="number" value="22"></label>
    <label>Username<input name="username"></label><label>Auth<select name="auth_method"><option>auto</option><option>agent</option><option>key</option><option>password</option></select></label>
    <label class="full">Private key<input name="private_key_path" placeholder="~/.ssh/id_ed25519"></label>
    <label class="full">Remote root<input name="remote_root" value="~"></label>
    <div class="modal-actions"><button type="button" class="btn" data-cancel>Cancel</button><button class="btn">Save</button></div></form>`);
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
  els.modal.querySelector('form').onsubmit = async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries()); data.port = Number(data.port);
    try { const profile = await api('/profiles', { method: 'POST', body: data }); closeModal(); await loadServers();
      const option = [...els.serverSelect.options].find(item => item.value === `profile:${profile.id}`); if (option) option.selected = true; toast('Connection profile saved'); }
    catch (error) { toast(error.message, true); }
  };
}

bindPane('local'); bindPane('remote');
els.connectBtn.onclick = connect;
els.disconnectBtn.onclick = disconnect;
els.refreshHostsBtn.onclick = loadServers;
els.newProfileBtn.onclick = profileModal;
els.uploadBtn.onclick = () => selectedTransfers('upload');
els.downloadBtn.onclick = () => selectedTransfers('download');
els.toggleTransfers.onclick = () => { els.transferList.classList.toggle('open'); els.toggleTransfers.textContent = els.transferList.classList.contains('open') ? 'Transfers ▼' : 'Transfers ▲'; };
els.clearFinished.onclick = async () => { await api('/transfers/clear-finished', { method: 'POST' }); state.transfers.clear(); await loadTransfers(); };
els.themeBtn.onclick = () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; localStorage.setItem('minisftp-theme', theme); };
document.querySelectorAll('.tab').forEach(tab => tab.onclick = () => { document.querySelectorAll('.tab').forEach(item => item.classList.remove('active')); tab.classList.add('active'); state.activeTab = tab.dataset.tab; renderTransfers(); });
els.modalBackdrop.onclick = (event) => { if (event.target === els.modalBackdrop) closeModal(); };
loadConfig().then(connectWebsocket).catch(error => toast(error.message, true));
