const state = {
  token: '',
  panes: { local: { tabs: [], active: null }, remote: { tabs: [], active: null } },
  connections: new Map(), localDefaults: {}, focusedPane: 'remote', routePair: '', routeResults: {},
  activeTab: 'all',
  transfers: new Map(),
  profiles: new Map(),
};
for (const pane of ['local', 'remote']) Object.defineProperty(state, pane, { get: () => state.panes[pane].tabs.find(tab => tab.id === state.panes[pane].active) });

const $ = (id) => document.getElementById(id);
const els = {};
['serverSelect','connectBtn','connectionStatus','newProfileBtn','refreshHostsBtn','themeBtn',
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
function basename(path) { return path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '/'; }
function dirname(path, pane) { return pane === 'local' ? path.split('/').slice(0, -1).join('/') || '/' : path.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/'; }

async function loadConfig() {
  const config = await api('/config');
  state.token = config.token;
  document.documentElement.dataset.theme = localStorage.getItem('minisftp-theme') || config.theme || 'dark';
  state.localDefaults = { path: config.local_root, root: config.local_navigation_root || config.local_root, drivesPath: config.local_drives_path || null };
  addTab('local', 'local');
  addTab('remote', 'empty');
  await Promise.all([loadServers(), loadTransfers()]);
}

function addTab(pane, kind, connection = null) {
  const tab = { id: crypto.randomUUID(), kind, connection, path: '', root: '', parent: null, entries: [], sort: 'name:asc', selected: new Set(), showHidden: false };
  if (kind === 'local') Object.assign(tab, state.localDefaults);
  if (connection) Object.assign(tab, { path: connection.remote_root, root: connection.remote_root });
  const group = state.panes[pane];
  group.tabs = group.tabs.filter(item => item.kind !== 'empty');
  group.tabs.push(tab); group.active = tab.id; state.focusedPane = pane;
  activateTab(pane, tab.id);
  return tab;
}
function tabName(tab) { return tab.kind === 'local' ? '本机' : tab.connection?.profile_name || '未选择'; }
function activateTab(pane, id) {
  state.panes[pane].active = id; state.focusedPane = pane;
  renderTabs(pane); renderFiles(pane); renderBreadcrumbs(pane); updateConnectionState();
  const info = state[pane];
  $(`${pane}Path`).value = info.path === info.drivesPath ? '此电脑' : info.path;
  $(`${pane}Hidden`).classList.toggle('active', info.showHidden);
  $(`${pane}Stats`).textContent = '';
  loadFiles(pane);
}
function renderTabs(pane) {
  const group = state.panes[pane], container = $(`${pane}Tabs`); container.innerHTML = '';
  group.tabs.forEach(tab => {
    const item = document.createElement('div'); item.className = `pane-tab${tab.id === group.active ? ' active' : ''}`;
    item.dataset.tabId = tab.id;
    item.dataset.movable = String(tab.kind !== 'empty');
    item.innerHTML = `<button class="tab-label" role="tab" aria-selected="${tab.id === group.active}" title="${escapeHtml(tabName(tab))} · 可拖动到另一侧">${tab.kind === 'local' ? '▣' : '◉'} ${escapeHtml(tabName(tab))}</button>${tab.kind !== 'empty' ? `<button class="tab-move" aria-label="将 ${escapeHtml(tabName(tab))} 移到${pane === 'local' ? '右' : '左'}侧" title="移到另一侧">${pane === 'local' ? '⇥' : '⇤'}</button>` : ''}<button class="tab-close" aria-label="关闭 ${escapeHtml(tabName(tab))} 标签">×</button>`;
    item.onclick = event => {
      if (!item.dataset.dragged && !event.target.closest('.tab-close, .tab-move')) activateTab(pane, tab.id);
    };
    const moveButton = item.querySelector('.tab-move');
    if (moveButton) moveButton.onclick = () => moveTab(pane, pane === 'local' ? 'remote' : 'local', tab.id);
    if (tab.kind !== 'empty') bindTabDragging(item, pane, tab.id);
    item.querySelector('.tab-close').onclick = () => {
      group.tabs = group.tabs.filter(value => value.id !== tab.id);
      if (!group.tabs.length) return addTab(pane, 'empty');
      activateTab(pane, group.active === tab.id ? group.tabs.at(-1).id : group.active);
    };
    container.appendChild(item);
  });
}

function tabDropPosition(element, x) {
  const target = element?.closest('.pane-tab');
  if (!target) return null;
  const rect = target.getBoundingClientRect();
  return x > rect.left + rect.width / 2 ? target.nextElementSibling?.dataset.tabId || null : target.dataset.tabId;
}

function bindTabDragging(item, pane, tabId) {
  let start = null;
  const clear = () => {
    item.classList.remove('dragging');
    document.querySelectorAll('.tab-drop-target').forEach(element => element.classList.remove('tab-drop-target'));
    start = null;
  };
  item.onpointerdown = event => {
    if (event.button !== 0 || event.target.closest('.tab-close, .tab-move')) return;
    delete item.dataset.dragged;
    start = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    item.setPointerCapture(event.pointerId);
  };
  item.onpointermove = event => {
    if (!start || event.pointerId !== start.pointerId) return;
    if (!item.dataset.dragged && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 6) return;
    item.dataset.dragged = 'true'; item.classList.add('dragging');
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.pane');
    document.querySelectorAll('.pane').forEach(element => element.classList.toggle('tab-drop-target', element === target));
  };
  item.onpointerup = event => {
    if (!start || event.pointerId !== start.pointerId) return;
    const moved = Boolean(item.dataset.dragged);
    const target = document.elementFromPoint(event.clientX, event.clientY);
    const destination = target?.closest('.pane');
    item.releasePointerCapture(event.pointerId); clear();
    if (moved && destination) moveTab(pane, destination.id === 'localPane' ? 'local' : 'remote', tabId, tabDropPosition(target, event.clientX));
    setTimeout(() => delete item.dataset.dragged, 0);
  };
  item.onpointercancel = clear;
  item.onlostpointercapture = clear;
}

function moveTab(from, to, tabId, beforeId = null) {
  const source = state.panes[from], destination = state.panes[to];
  if (!source || !destination || tabId === beforeId) return;
  const tab = source.tabs.find(item => item.id === tabId);
  if (!tab || tab.kind === 'empty') return;
  source.tabs = source.tabs.filter(item => item.id !== tabId);
  destination.tabs = destination.tabs.filter(item => item.kind !== 'empty');
  const index = destination.tabs.findIndex(item => item.id === beforeId);
  destination.tabs.splice(index < 0 ? destination.tabs.length : index, 0, tab);
  if (from !== to) {
    if (!source.tabs.length) addTab(from, 'empty');
    else activateTab(from, source.active === tabId ? source.tabs.at(-1).id : source.active);
  }
  activateTab(to, tabId);
  toast(`${tabName(tab)} 已移到${to === 'local' ? '左' : '右'}侧`);
}

function remoteTabModal(pane) {
  state.focusedPane = pane; updateConnectionState();
  const jumps = [...state.connections.values()].map(connection => `<option value="${escapeHtml(connection.id)}">通过 ${escapeHtml(connection.profile_name)}</option>`).join('');
  showModal(`<h3>添加远程到${pane === 'local' ? '左侧' : '右侧'}</h3><div class="form-grid"><label class="full">服务器<select id="tabServerSelect" aria-label="选择 SSH 连接">${els.serverSelect.innerHTML}</select></label><label class="full">连接路径<select id="tabJumpSelect" aria-label="连接跳转路径"><option value="">使用本机 SSH 配置</option>${jumps}</select></label></div><div class="modal-actions"><button class="btn" data-cancel>取消</button><button class="btn" data-connect>连接并添加标签</button></div>`);
  $('tabServerSelect').value = els.serverSelect.value;
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
  els.modal.querySelector('[data-connect]').onclick = () => { const value = $('tabServerSelect').value, via = $('tabJumpSelect').value; closeModal(); connect(pane, value, via); };
}

function manageConnectionsModal() {
  const items = [...state.connections.values()].map(connection => {
    const via = state.connections.get(connection.via_connection_id);
    return `<article class="connection-item"><div><strong>${escapeHtml(connection.profile_name)}</strong><small>${escapeHtml(connection.username || '')}@${escapeHtml(connection.host)}${via ? ' · 通过 ' + escapeHtml(via.profile_name) : ''}</small></div><div class="connection-buttons"><button class="btn small" data-open="local" data-id="${escapeHtml(connection.id)}">在左侧打开</button><button class="btn small" data-open="remote" data-id="${escapeHtml(connection.id)}">在右侧打开</button><button class="btn small" data-disconnect="${escapeHtml(connection.id)}">断开</button></div></article>`;
  }).join('');
  showModal(`<h3>管理远程连接 · ${state.connections.size} 个</h3><div class="connection-list">${items || '<p>暂无远程连接，请使用顶部的“添加远程”。</p>'}</div><p class="form-note">关闭标签会保留连接；断开跳转服务器会同时关闭依赖它的连接。</p><div class="modal-actions"><button class="btn" data-cancel>关闭</button></div>`);
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
  els.modal.querySelectorAll('[data-open]').forEach(button => button.onclick = () => {
    const connection = state.connections.get(button.dataset.id); if (!connection) return;
    const pane = button.dataset.open, existing = state.panes[pane].tabs.find(tab => tab.connection?.id === connection.id);
    closeModal(); if (existing) activateTab(pane, existing.id); else addTab(pane, 'remote', connection);
  });
  els.modal.querySelectorAll('[data-disconnect]').forEach(button => button.onclick = async () => {
    const connection = state.connections.get(button.dataset.disconnect);
    if (connection && await disconnect(connection)) manageConnectionsModal();
  });
}
function addTabModal(pane) {
  state.focusedPane = pane;
  showModal(`<h3>添加${pane === 'local' ? '左侧' : '右侧'}标签</h3><p>每个标签独立保存浏览目录，可将两侧都设为远程服务器。</p><div class="modal-actions"><button class="btn" data-local>本机文件</button><button class="btn" data-remote>从 SSH / 已保存连接添加</button><button class="btn" data-cancel>取消</button></div>`);
  els.modal.querySelector('[data-local]').onclick = () => { closeModal(); addTab(pane, 'local'); };
  els.modal.querySelector('[data-remote]').onclick = () => remoteTabModal(pane);
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
}

async function loadServers() {
  const [aliases, profiles, connections] = await Promise.all([api('/ssh-hosts'), api('/profiles'), api('/connections')]);
  els.serverSelect.innerHTML = '';
  const group = (label) => { const optgroup = document.createElement('optgroup'); optgroup.label = label; els.serverSelect.appendChild(optgroup); return optgroup; };
  const add = (value, text, target) => { const option = new Option(text, value); target.appendChild(option); };
  const aliasGroup = group('从 SSH 配置添加');
  aliases.forEach(item => add(`alias:${item.alias}`, `${item.alias}${item.user ? ` (${item.user}@${item.host || item.alias})` : ''}`, aliasGroup));
  const profileGroup = group('已保存的连接');
  profiles.forEach(item => add(`profile:${item.id}`, item.name, profileGroup));
  profiles.forEach(item => state.profiles.set(item.id, item));
  const initial = state.connections.size === 0 && state.remote.kind === 'empty';
  state.connections = new Map(connections.map(item => [item.id, item]));
  for (const pane of ['local', 'remote']) state.panes[pane].tabs.forEach(tab => {
    if (tab.kind !== 'remote') return;
    const current = connections.find(connection => connection.profile_id === tab.connection.profile_id);
    if (current) tab.connection = current;
    else { tab.entries = []; tab.selected.clear(); tab.parent = null; }
  });
  for (const pane of ['local', 'remote']) renderFiles(pane);
  updateConnectionState();
  if (initial) connections.forEach(connection => addTab('remote', 'remote', connection));
}

function updateConnectionState() {
  els.connectionStatus.textContent = `${state.connections.size} 个远程连接`;
  els.connectionStatus.className = `status ${state.connections.size ? 'online' : 'offline'}`;
  $('targetPaneSelect').value = state.focusedPane;
  for (const [pane, id, label] of [['local','leftEndpoint','左侧'], ['remote','rightEndpoint','右侧']]) {
    const info = state[pane]; if (!info) continue;
    const offline = info.kind === 'remote' && !state.connections.has(info.connection?.id);
    $(id).textContent = `${label} · ${tabName(info)}${offline ? '（未连接）' : ''}`;
    $(id).classList.toggle('offline', offline);
    $(id).title = info.connection?.via_connection_id ? `${info.path} · 通过 ${state.connections.get(info.connection.via_connection_id)?.profile_name || '跳转服务器'}` : info.path;
  }
  for (const pane of ['local', 'remote']) {
    const info = state[pane]; if (!info) continue;
    const available = info.kind === 'local' || state.connections.has(info.connection?.id);
    $(`${pane}Pane`).classList.toggle('focused', state.focusedPane === pane);
    $(`${pane}Title`).textContent = info.kind === 'empty' ? '添加本机或远程标签' : `${info.kind === 'local' ? 'LOCAL' : 'REMOTE'} · ${tabName(info)}${info.kind === 'remote' && !available ? '（未连接）' : ''}`;
    $(`${pane}Path`).disabled = !available;
    for (const id of ['Up', 'Refresh', 'Go', 'Hidden']) $(`${pane}${id}`).disabled = !available;
    $(`${pane}Up`).disabled = !available || !info.parent;
  }
  const usable = tab => tab && !tab.loading && tab.kind !== 'empty' && (tab.kind === 'local' || state.connections.has(tab.connection?.id)) && tab.path && tab.path !== tab.drivesPath;
  const enabled = usable(state.local) && usable(state.remote) && !(state.local.kind === 'local' && state.remote.kind === 'local');
  els.uploadBtn.disabled = !enabled; els.downloadBtn.disabled = !enabled;
  updateRoute();
}

function updateRoute(force = false) {
  const left = state.local, right = state.remote;
  if (!left || !right) return;
  const remotePair = left?.connection && right?.connection && state.connections.has(left.connection.id) && state.connections.has(right.connection.id);
  $('routeRefresh').disabled = !remotePair;
  const pair = remotePair ? `${left.connection.id}:${right.connection.id}` : '';
  if (!pair) {
    state.routePair = ''; state.routeResults = {};
    $('routeForward').className = ''; $('routeReverse').className = '';
    $('routeForward').title = ''; $('routeReverse').title = '';
    $('routeForward').textContent = left.kind === 'local' && right.kind === 'local' ? '请选择至少一个远程标签' : '本机 ↔ 远程\n通过本机 SSH / SFTP 连接';
    if ([left, right].some(tab => tab.kind === 'remote' && !state.connections.has(tab.connection?.id))) $('routeForward').textContent = '服务器连接已断开\n请重新连接后传输';
    if (left.kind === 'empty' || right.kind === 'empty') $('routeForward').textContent = '选择两侧标签开始传输';
    $('routeReverse').textContent = ''; return;
  }
  if (state.routePair === pair && !force) return;
  state.routePair = pair; state.routeResults = {};
  [['local', 'remote', 'routeForward', '→'], ['remote', 'local', 'routeReverse', '←']].forEach(async ([from, to, id, arrow]) => {
    const source = state[from].connection, destination = state[to].connection;
    const prefix = from === 'local' ? `${source.profile_name} ${arrow} ${destination.profile_name}` : `${destination.profile_name} ${arrow} ${source.profile_name}`;
    $(id).className = ''; $(id).textContent = `${prefix}\n配置免密并检测直连…`;
    try {
      const result = await api('/connection-route', { method: 'POST', body: { source_connection_id: source.id, destination_connection_id: destination.id, force } });
      if (state.routePair !== pair) return;
      state.routeResults[`${source.id}:${destination.id}`] = result;
      $(id).className = result.route; $(id).textContent = `${prefix}\n${result.route === 'direct' ? '● 服务器直传' : '● 本机中转'}`; $(id).title = result.detail;
    } catch (error) { if (state.routePair === pair) { $(id).className = 'relay'; $(id).textContent = `${prefix}\n检测失败，将在传输时重试`; $(id).title = error.message; } }
  });
}

async function connect(pane = state.focusedPane, value = els.serverSelect.value, via = '') {
  if (!value) return toast('Select a server first', true);
  const split = value.indexOf(':'); const type = value.slice(0, split), id = value.slice(split + 1);
  let payload = type === 'alias' ? { ssh_alias: id } : { profile_id: id };
  if (via) payload.via_connection_id = via;
  const profile = type === 'profile' ? state.profiles.get(id) : null;
  if (profile && (profile.auth_method === 'password' || profile.private_key_path)) {
    const credentials = await credentialsModal(profile);
    if (!credentials) return;
    payload = { ...payload, ...credentials };
  }
  try {
    const result = await api('/connections', { method: 'POST', body: payload });
    attachConnection(result); addTab(pane, 'remote', result);
    updateConnectionState();
    toast(`Connected to ${result.profile_name}`);
  } catch (error) {
    if (error.code === 'host_key_confirmation_required') {
      confirmHostKey(payload, error.host_key, error.message, pane);
      return;
    }
    toast(error.message, true);
  }
}

function confirmHostKey(payload, hostKey, message, pane) {
  showModal(`<h3>Verify SSH Host Key</h3><p>${escapeHtml(message || '')}</p>
    <div class="host-key">${escapeHtml(hostKey.host)}:${hostKey.port}<br>${escapeHtml(hostKey.key_type || '')}<br>${escapeHtml(hostKey.fingerprint || '')}</div>
    <div class="modal-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-confirm>Trust and connect</button></div>`);
  els.modal.querySelector('[data-cancel]').onclick = closeModal;
  els.modal.querySelector('[data-confirm]').onclick = async () => {
    closeModal();
    try {
      const result = await api('/connections', { method: 'POST', body: { ...payload, confirm_host_key: true, host_key_fingerprint: hostKey.fingerprint } });
      attachConnection(result); addTab(pane, 'remote', result);
      updateConnectionState(); toast('Host key saved and connected');
    } catch (error) { toast(error.message, true); }
  };
}

function attachConnection(connection) {
  state.connections.set(connection.id, connection);
  for (const pane of ['local', 'remote']) state.panes[pane].tabs.forEach(tab => {
    if (tab.connection?.profile_id === connection.profile_id) tab.connection = connection;
  });
}

async function disconnect(connection = state[state.focusedPane]?.connection) {
  if (!connection) return;
  const affected = new Set([connection.id]);
  for (let changed = true; changed;) {
    changed = false;
    for (const candidate of state.connections.values()) if (affected.has(candidate.via_connection_id) && !affected.has(candidate.id)) { affected.add(candidate.id); changed = true; }
  }
  const profiles = new Set([...state.connections.values()].filter(item => affected.has(item.id)).map(item => item.profile_id));
  if ([...state.transfers.values()].some(task => ['queued','running','pausing'].includes(task.status) && (profiles.has(task.profile_id) || profiles.has(task.resume_metadata?.endpoints?.destination_profile_id)))) return toast('此连接或依赖它的跳转连接还有传输任务，请先暂停或取消', true);
  try { await api(`/connections/${connection.id}`, { method: 'DELETE' }); } catch (error) { return toast(error.message, true); }
  affected.forEach(id => state.connections.delete(id));
  for (const pane of ['local', 'remote']) {
    state.panes[pane].tabs.forEach(tab => { if (affected.has(tab.connection?.id)) { tab.entries = []; tab.selected.clear(); tab.parent = null; } });
    renderFiles(pane);
  }
  updateConnectionState(); toast('连接已断开，标签目录已保留，可重新连接');
  return true;
}

async function loadFiles(pane, path = state[pane].path) {
  const info = state[pane];
  if (info.kind === 'empty' || (info.kind === 'remote' && !state.connections.has(info.connection.id))) return;
  const query = new URLSearchParams({ path, show_hidden: info.showHidden, sort: info.sort });
  const requestId = info.requestId = (info.requestId || 0) + 1;
  info.loading = true; updateConnectionState();
  if (info.kind === 'remote') query.set('connection_id', info.connection.id);
  try {
    const result = await api(`/files/${info.kind}?${query}`);
    if (info.requestId !== requestId) return;
    info.path = result.path; info.parent = result.parent; info.entries = result.entries; info.selected.clear();
    if (state[pane] !== info) return;
    $(`${pane}Up`).disabled = !result.parent;
    const drivesView = info.kind === 'local' && info.drivesPath && result.path === info.drivesPath;
    $(`${pane}Path`).value = drivesView ? '此电脑' : result.path;
    $(`${pane}Stats`).textContent = drivesView ? `${result.total_directories} 个磁盘` : `${result.total_files} files, ${result.total_directories} folders`;
    renderBreadcrumbs(pane); renderFiles(pane);
    updateConnectionState();
  } catch (error) { toast(error.message, true); }
  finally { if (info.requestId === requestId) { info.loading = false; updateConnectionState(); } }
}

function renderBreadcrumbs(pane) {
  const info = state[pane], element = $(`${pane}Breadcrumb`);
  element.innerHTML = '';
  const make = (path, label, last) => {
    const button = document.createElement('button'); button.textContent = label;
    button.onclick = () => loadFiles(pane, path);
    element.appendChild(button);
    if (!last) element.insertAdjacentText('beforeend', ' ›');
  };
  if (info.kind === 'empty') return;
  const path = info.kind === 'local' ? info.path.replace(/\\/g, '/') : info.path;
  if (info.kind === 'local' && info.drivesPath) {
    const drivesView = info.path === info.drivesPath;
    make(info.drivesPath, '此电脑', drivesView);
    if (drivesView) return;
  }
  let root = (info.kind === 'local' ? info.root.replace(/\\/g, '/') : info.root).replace(/\/+$/, '') || '/';
  if (info.kind === 'local' && info.drivesPath) root = path.match(/^[A-Za-z]:\//)?.[0] || root;
  const parts = path.slice(root.length).split(/\/+/).filter(Boolean);
  make(root, root, parts.length === 0);
  let current = root;
  parts.forEach((part, index) => {
    current = current.endsWith('/') ? `${current}${part}` : `${current}/${part}`;
    make(current, part, index === parts.length - 1);
  });
}

function renderFiles(pane) {
  const info = state[pane], body = $(`${pane}Body`);
  const drivesView = info.kind === 'local' && info.drivesPath && info.path === info.drivesPath;
  body.innerHTML = '';
  if (info.kind === 'empty') {
    body.innerHTML = '<tr class="empty-row"><td colspan="4">拖入另一侧的标签，或使用顶部按钮添加本机 / 远程。</td></tr>';
    return;
  }
  info.entries.forEach(entry => {
    const row = document.createElement('tr');
    row.dataset.path = entry.path; row.dataset.dir = String(entry.is_dir); row.dataset.name = entry.name;
    row.draggable = !drivesView;
    row.innerHTML = `<td class="${entry.is_dir ? 'folder' : ''}${entry.is_symlink ? ' link' : ''}">${escapeHtml(entry.name)}</td>
      <td>${drivesView ? '—' : entry.is_dir ? '&lt;Folder&gt;' : fmtSize(entry.size)}</td><td>${fmtDate(entry.modified_at)}</td><td>${drivesView ? '磁盘' : entry.is_dir ? 'Directory' : entry.is_symlink ? 'Symlink' : 'File'}</td>`;
    row.onclick = (event) => {
      if (!event.metaKey && !event.ctrlKey && !event.shiftKey) info.selected.clear();
      info.selected.has(entry.path) ? info.selected.delete(entry.path) : info.selected.add(entry.path);
      renderFiles(pane);
    };
    row.ondblclick = async () => {
      if (entry.is_dir) await loadFiles(pane, entry.path);
      else toast('Select the entry and use Upload/Download');
    };
    row.ondragstart = (event) => {
      const paths = info.selected.has(entry.path) ? [...info.selected] : [entry.path];
      event.dataTransfer.setData('application/x-minisftp', JSON.stringify({ pane, tabId: info.id, paths }));
      event.dataTransfer.effectAllowed = 'copy';
    };
    body.appendChild(row);
  });
  [...body.children].forEach(row => row.classList.toggle('selected', info.selected.has(row.dataset.path)));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

async function openDirectory(pane) { const info = state[pane]; if (info.parent) await loadFiles(pane, info.parent); }
function bindPane(pane) {
  $(`${pane}AddTab`).onclick = () => addTabModal(pane);
  $(`${pane}Pane`).onpointerdown = () => { state.focusedPane = pane; updateConnectionState(); };
  $(`${pane}Up`).onclick = () => openDirectory(pane);
  $(`${pane}Refresh`).onclick = () => loadFiles(pane);
  $(`${pane}Go`).onclick = () => {
    let path = $(`${pane}Path`).value.trim();
    if (!path) return toast('请输入文件夹路径', true);
    if (state[pane].kind === 'local' && state[pane].drivesPath) {
      if (path === '此电脑') path = state[pane].drivesPath;
      else if (/^[A-Za-z]:$/.test(path)) path += '/';
    }
    return loadFiles(pane, path);
  };
  $(`${pane}Hidden`).onclick = () => { state[pane].showHidden = !state[pane].showHidden; $(`${pane}Hidden`).classList.toggle('active'); loadFiles(pane); };
  $(`${pane}Path`).onkeydown = (event) => { if (event.key === 'Enter') $(`${pane}Go`).click(); };
  document.querySelectorAll(`#${pane}Pane th[data-sort]`).forEach(th => th.onclick = () => {
    const [field, direction] = state[pane].sort.split(':');
    state[pane].sort = `${th.dataset.sort}:${field === th.dataset.sort && direction === 'asc' ? 'desc' : 'asc'}`;
    loadFiles(pane);
  });
  const drop = $(`${pane}Pane`);
  drop.ondragover = event => {
    if (event.dataTransfer.types.includes('application/x-minisftp')) {
      event.preventDefault(); event.dataTransfer.dropEffect = 'copy';
      drop.classList.add('drop-target');
    }
  };
  drop.ondragleave = event => { if (!drop.contains(event.relatedTarget)) drop.classList.remove('tab-drop-target', 'drop-target'); };
  drop.ondrop = async event => {
    drop.classList.remove('tab-drop-target', 'drop-target');
    const raw = event.dataTransfer.getData('application/x-minisftp'); if (!raw) return;
    event.preventDefault();
    let payload;
    try { payload = JSON.parse(raw); } catch (_) { return; }
    if (payload.pane === pane) return;
    if (state[payload.pane]?.id !== payload.tabId) return toast('源标签已经切换，请重新拖拽', true);
    const source = { ...state[payload.pane] }, target = { ...state[pane] };
    for (const path of payload.paths) await createTransfer(source, target, path);
  };
}

async function createTransfer(source, destination, sourcePath) {
  if (source.kind === 'empty' || destination.kind === 'empty' || source.kind === 'local' && destination.kind === 'local') return toast('请选择远程连接', true);
  if ([source, destination].some(tab => tab.path === tab.drivesPath)) return toast('请先进入具体磁盘或文件夹', true);
  if ([source, destination].some(tab => tab.kind === 'remote' && !state.connections.has(tab.connection.id))) return toast('请先连接服务器', true);
  const direction = source.kind === 'local' ? 'upload' : destination.kind === 'local' ? 'download' : 'remote';
  const payload = { direction, source_path: sourcePath, destination_path: destination.path, connection_id: (source.kind === 'remote' ? source : destination).connection.id, conflict_strategy: els.conflictStrategy.value };
  if (direction === 'remote') payload.destination_connection_id = destination.connection.id;
  try {
    const result = await api('/transfers', { method: 'POST', body: payload });
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
  const source = { ...state[sourcePane] }, target = { ...state[targetPane] };
  for (const path of [...selected]) await createTransfer(source, target, path);
}

async function loadTransfers() {
  const list = await api('/transfers');
  state.transfers.clear();
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
  const active = [...state.transfers.values()].filter(task => ['queued','running','pausing'].includes(task.status));
  $('taskStatus').textContent = active.length ? `${active.length} 项传输 · ${fmtSpeed(active.reduce((sum, task) => sum + (task.current_speed || 0), 0))}` : '无活动传输';
  [...state.transfers.values()].filter(transferMatches).sort((a,b) => b.created_at.localeCompare(a.created_at)).forEach(task => {
    const card = document.createElement('article'); card.className = 'transfer-card';
    const percent = task.status === 'completed' ? 100 : task.total_bytes ? Math.min(100, task.transferred_bytes / task.total_bytes * 100) : 0;
    const name = basename(task.source_path);
    const meta = task.resume_metadata || {};
    const routeText = task.direction === 'remote' ? `${meta.endpoints?.source_name || '源服务器'} → ${meta.endpoints?.destination_name || '目标服务器'} · ${meta.route === 'direct' ? '服务器直传' : meta.route === 'relay' ? '本机中转' : '检测连接中'}` : task.direction === 'upload' ? '本机 → 远程' : '远程 → 本机';
    card.innerHTML = `<div class="transfer-main"><div class="transfer-title" title="${escapeHtml(task.source_path)} → ${escapeHtml(task.destination_path)}">${task.direction === 'remote' ? '⇄' : task.direction === 'upload' ? '↑' : '↓'} ${escapeHtml(name)}</div>
      <div class="transfer-controls"><button data-action="pause" title="Pause">❚❚</button><button data-action="resume" title="Resume">▶</button><button data-action="cancel" title="Cancel">✕</button><button data-action="retry" title="Retry">⟳</button></div></div>
      <div class="progress"><div class="progress-bar" style="width:${percent}%"></div></div>
      <div class="progress-info"><span>${fmtSize(task.transferred_bytes)} / ${fmtSize(task.total_bytes)} (${percent.toFixed(1)}%)</span><span>${fmtSize(task.current_speed)}/s</span><span>ETA ${fmtTime(task.eta_seconds)}</span></div>
      <div class="progress-info"><span>${escapeHtml(task.current_file || '')}</span><span class="status-badge ${task.status}">${task.status}</span></div>
      <div class="progress-info" title="${escapeHtml(meta.route_detail || '')}">${escapeHtml(routeText)}</div>
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
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/events?token=${encodeURIComponent(state.token)}`);
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data); if (!message.task_id) return;
    state.transfers.set(message.task_id, message.payload); renderTransfers();
    const task = message.payload;
    if (task.direction === 'remote') {
      const destinationProfile = task.resume_metadata?.endpoints?.destination_profile_id;
      for (const [from, to, id] of [['local','remote','routeForward'], ['remote','local','routeReverse']]) {
        if (state[from]?.connection?.profile_id === task.profile_id && state[to]?.connection?.profile_id === destinationProfile) {
          const route = task.resume_metadata.route; $(id).className = route;
          $(id).textContent = `${tabName(state[from])} → ${tabName(state[to])}\n${route === 'direct' ? '● 服务器直传' : route === 'relay' ? '● 本机中转' : '检测直连…'}`;
        }
      }
    }
    if (message.event_type === 'transfer_completed') {
      const destinationProfile = task.direction === 'remote' ? task.resume_metadata?.endpoints?.destination_profile_id : task.profile_id;
      for (const pane of ['local', 'remote']) {
        const tab = state[pane];
        if (task.direction === 'download' ? tab.kind === 'local' : tab.connection?.profile_id === destinationProfile) loadFiles(pane);
      }
    }
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
document.querySelector('.workspace').ondragover = event => {
  if (event.dataTransfer.types.includes('application/x-minisftp')) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }
};
els.connectBtn.onclick = () => remoteTabModal($('targetPaneSelect').value);
$('addLocalBtn').onclick = () => addTab($('targetPaneSelect').value, 'local');
$('targetPaneSelect').onchange = event => { state.focusedPane = event.target.value; updateConnectionState(); };
$('manageConnectionsBtn').onclick = manageConnectionsModal;
els.refreshHostsBtn.onclick = loadServers;
els.newProfileBtn.onclick = profileModal;
els.uploadBtn.onclick = () => selectedTransfers('upload');
els.downloadBtn.onclick = () => selectedTransfers('download');
$('routeRefresh').onclick = () => updateRoute(true);
els.toggleTransfers.onclick = () => { els.transferList.classList.toggle('open'); els.toggleTransfers.textContent = els.transferList.classList.contains('open') ? 'Transfers ▼' : 'Transfers ▲'; };
els.clearFinished.onclick = async () => { await api('/transfers/clear-finished', { method: 'POST' }); state.transfers.clear(); await loadTransfers(); };
els.themeBtn.onclick = () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; localStorage.setItem('minisftp-theme', theme); };
document.querySelectorAll('.tab').forEach(tab => tab.onclick = () => { document.querySelectorAll('.tab').forEach(item => item.classList.remove('active')); tab.classList.add('active'); state.activeTab = tab.dataset.tab; renderTransfers(); });
els.modalBackdrop.onclick = (event) => { if (event.target === els.modalBackdrop) closeModal(); };
loadConfig().then(connectWebsocket).catch(error => toast(error.message, true));
