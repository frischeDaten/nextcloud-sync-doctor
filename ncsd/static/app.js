'use strict';

/* ------------------------------------------------------------------ setup */
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};

// The doctor runs on Windows and macOS; the backend reports which one in /api/bootstrap.
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || '');

const state = {
  boot: null,
  issues: [],
  counts: {},
  filterKind: 'all',
  search: '',
  junk: false,
  open: new Set(),
  detail: new Map(),
  busy: new Set(),
};

const ACTION_META = {
  keep_local: { label: 'Keep local', cls: 'primary', group: 'main', hint: 'Overwrite the server copy with your local file' },
  keep_server: { label: 'Keep server', cls: 'primary', group: 'main', hint: 'Overwrite your local file with the server version' },
  keep_both: { label: 'Keep both', cls: '', group: 'main', hint: 'Keep your file under a new name and bring the server version in' },
  rename_remote: { label: 'Rename on server…', cls: '', group: 'fix', hint: 'Rename the file on the server (fixes names Windows cannot store)' },
  rename_local: { label: 'Rename local…', cls: '', group: 'fix' },
  download_as: { label: 'Download as…', cls: '', group: 'fix' },
  download_remote: { label: 'Download', cls: 'primary', group: 'main' },
  upload_local: { label: 'Upload', cls: 'primary', group: 'main' },
  delete_remote: { label: 'Delete on server', cls: 'danger', group: 'danger' },
  delete_local: { label: 'Delete local', cls: 'danger', group: 'danger' },
  open_local: { label: IS_MAC ? 'Show in Finder' : 'Show in Explorer', cls: 'ghost', group: 'misc' },
  open_remote: { label: 'Open in Nextcloud', cls: 'ghost', group: 'misc' },
  ignore: { label: 'Ignore file', cls: 'ghost', group: 'misc' },
  dismiss: { label: 'Hide', cls: 'ghost', group: 'misc' },
};

const NEEDS_NAME = new Set(['rename_remote', 'rename_local', 'download_as']);
const LOADED_ACTIONS = new Set(['keep_local', 'keep_server', 'keep_both', 'download_remote',
  'upload_local', 'delete_remote', 'delete_local', 'ignore', 'rename_remote', 'rename_local', 'download_as']);

const TEXT_EXT = new Set(['.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.xml', '.yml', '.yaml',
  '.ini', '.cfg', '.conf', '.log', '.py', '.js', '.mjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.css',
  '.scss', '.sh', '.bat', '.ps1', '.cmd', '.tex', '.rst', '.sql', '.php', '.rb', '.java', '.c', '.h',
  '.cpp', '.hpp', '.cs', '.go', '.rs', '.toml', '.env', '.srt', '.vtt', '.diff', '.patch']);
const IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.avif']);
const PDF_EXT = new Set(['.pdf']);
const AUDIO_EXT = new Set(['.mp3', '.flac', '.m4a', '.ogg', '.wav', '.opus']);
const VIDEO_EXT = new Set(['.mp4', '.mkv', '.webm', '.mov', '.avi']);

function kindOf(name) {
  const dot = name.lastIndexOf('.');
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : '';
  if (TEXT_EXT.has(ext)) return 'text';
  if (IMAGE_EXT.has(ext)) return 'image';
  if (PDF_EXT.has(ext)) return 'pdf';
  if (AUDIO_EXT.has(ext)) return 'audio';
  if (VIDEO_EXT.has(ext)) return 'video';
  return 'binary';
}

/* ------------------------------------------------------------------- utils */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  let value = Number(bytes);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}

function fmtTime(seconds) {
  if (!seconds) return '—';
  const d = new Date(seconds * 1000);
  return d.toLocaleString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function ago(seconds) {
  if (!seconds) return 'never';
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  if (diff < 90) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return `${Math.round(diff / 86400)} d ago`;
}

function toast(message, kind = '') {
  const node = el('div', `toast ${kind}`, esc(message));
  $('#toasts').appendChild(node);
  setTimeout(() => node.remove(), kind === 'err' ? 9000 : 5000);
}

async function api(path, options = {}) {
  const opts = { headers: { 'X-NCSD-Token': TOKEN }, ...options };
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------------ modal */
let modalResolve = null;

function openModal({ title, body, okLabel = 'Apply', danger = false, onOk = null, cancelLabel = 'Cancel' }) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = body;
  const okBtn = $('#modal-ok');
  const cancelBtn = $('#modal-cancel');
  okBtn.textContent = okLabel;
  okBtn.className = `btn ${danger ? 'danger' : 'primary'}`;
  cancelBtn.textContent = cancelLabel;
  cancelBtn.hidden = false;
  $('#modal').hidden = false;

  const close = () => { $('#modal').hidden = true; modalResolve = null; };
  cancelBtn.onclick = close;
  $('#modal').onclick = (event) => { if (event.target.id === 'modal') close(); };
  okBtn.onclick = async () => {
    if (!onOk) { close(); return; }
    const original = okBtn.textContent;
    okBtn.disabled = true;
    okBtn.innerHTML = '<span class="spin"></span>';
    try {
      const result = await onOk();
      if (result !== false) close();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      okBtn.disabled = false;
      okBtn.textContent = original;
    }
  };
  return { close };
}

function planBody(plan, extraHTML = '') {
  const steps = (plan.steps || []).map((s) => `<li>${esc(s)}</li>`).join('');
  const warn = plan.warning ? `<div class="warn">${esc(plan.warning)}</div>` : '';
  const danger = plan.destructive
    ? '<div class="danger-note">This changes or removes data. A backup is kept locally and server deletions go to the server trash.</div>'
    : '';
  return `<div class="summary">${esc(plan.summary)}</div>
    ${steps ? `<ul>${steps}</ul>` : ''}${warn}${danger}${extraHTML}`;
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  state.boot = await api('/api/bootstrap');
  const b = state.boot;
  state.junk = Boolean(b.settings.include_junk);
  $('#toggle-junk').checked = state.junk;
  if (b.demo) $('#demo-badge').hidden = false;

  $('#account-line').textContent =
    `${b.account.label} · ${b.folder.local} → ${b.folder.remote}`;
  $('#footer-info').innerHTML =
    `Server ${esc(b.account.server_version || '?')} · client ${esc(b.client.config_version || '?')}` +
    ` · journal ${b.folder.journal_entries.toLocaleString()} entries` +
    ` · last sync ${esc(ago(b.journal_last_sync))}` +
    (b.folder.virtual_files ? ` · virtual files (${esc(b.folder.virtual_files)})` : '') +
    (b.demo || b.client.running ? '' : ' · <b>client not running</b>');
  $('#footer-dirs').innerHTML =
    `backups: <code>${esc(b.backups_dir)}</code>`;

  renderPlatformBanner(b);
  if (b.deep.running) startDeepPolling();
  await scan({ deep: false });
}

/* -------------------------------------------------------- platform banner */
function renderPlatformBanner(b) {
  const banner = $('#platformbanner');
  const p = b.folder.platform || {};
  const lines = [];
  if (b.folder.note) lines.push(esc(b.folder.note));
  if (p.fileprovider) {
    lines.push('<b>FileProvider sync folder.</b> The macOS client manages this folder through ' +
      "the system's FileProvider, so its sync journal lives in a protected system directory and " +
      'the classic "sync folder" files (journal, .sync-exclude.lst) are not where Windows puts them.');
  }
  if (p.icloud) {
    lines.push('<b>iCloud Drive folder.</b> A Nextcloud sync folder inside iCloud Drive is managed ' +
      'by two sync services at once. Both of them rename, delete and re-create files, which produces ' +
      'conflicts that neither client can explain. Move the folder out of iCloud Drive.');
  }
  if (p.dataless && p.dataless.dataless > 0) {
    const d = p.dataless;
    lines.push(`<b>${d.dataless.toLocaleString()} of ${d.files.toLocaleString()} files are not downloaded` +
      ' (cloud placeholders). This is normal for a FileProvider folder; a preview downloads the file ' +
      'into the sync folder first.' + (d.complete ? '' : ' (count stopped early)'));
  }
  if (b.folder.journal_error) {
    lines.push(esc(b.folder.journal_error));
  }
  if (!lines.length) { banner.hidden = true; return; }
  banner.innerHTML = `<div class="platformbanner-text">${lines.map((l) => `<p>${l}</p>`).join('')}</div>
    <button class="btn small ghost" data-dismiss-banner>Hide</button>`;
  banner.hidden = false;
  banner.querySelector('[data-dismiss-banner]').onclick = () => { banner.hidden = true; };
}

/* ------------------------------------------------------------------- scan */
async function scan({ deep = false } = {}) {
  const btn = $('#btn-scan');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Scanning';
  try {
    const data = await api('/api/scan', { method: 'POST', body: { junk: state.junk, deep } });
    state.issues = data.issues;
    state.counts = data.counts;
    render();
  } catch (err) {
    toast(`Scan failed: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Rescan';
  }
}

/* ----------------------------------------------------------------- render */
function visibleIssues() {
  const needle = state.search.trim().toLowerCase();
  return state.issues.filter((issue) => {
    if (state.filterKind === 'dismissed') {
      if (!issue.dismissed) return false;
    } else if (issue.dismissed) {
      return false;
    } else if (state.filterKind === 'resolved') {
      if (!issue.resolved) return false;
    } else if (issue.resolved) {
      // already fixed - only shown through the "Resolved" chip
      return false;
    } else if (state.filterKind !== 'all' && issue.kind !== state.filterKind) {
      return false;
    }
    if (state.filterKind === 'all' && issue.severity === 'noise' && !state.junk) return false;
    if (needle && !(issue.path.toLowerCase().includes(needle) || (issue.reason || '').toLowerCase().includes(needle))) {
      return false;
    }
    return true;
  });
}

function render() {
  renderTiles();
  renderChips();
  renderIssues();
}

function renderTiles() {
  const b = state.boot;
  const counts = state.counts || {};
  const tiles = $('#tiles');
  tiles.innerHTML = '';

  const mk = (cls, value, label) => {
    const tile = el('div', `tile ${cls}`);
    tile.appendChild(el('div', 'value', String(value)));
    tile.appendChild(el('div', 'label', label));
    return tile;
  };
  tiles.appendChild(mk('blocking', counts.blocking ?? 0, 'blocking problems'));
  tiles.appendChild(mk('warning', counts.warning ?? 0, 'warnings'));
  tiles.appendChild(mk('noise', counts.noise ?? 0, 'ignored-file noise'));

  const detail = el('div', 'tile detail');
  detail.appendChild(el('div', 'label', 'sync folder'));
  detail.innerHTML += `<div class="kv">
    <span>local</span><span title="${esc(b.folder.local)}">${esc(b.folder.local)}</span>
    <span>server</span><span>${esc(b.account.url)}${esc(b.folder.remote)}</span>
    <span>last sync</span><span>${esc(ago(b.journal_last_sync))}</span>
    <span>client</span><span>${b.client.running ? 'running' : 'not running'} · ${esc(b.client.config_version)}</span>
  </div>`;
  tiles.appendChild(detail);
}

function renderChips() {
  const chips = $('#chips');
  chips.innerHTML = '';
  const counts = {};
  let open = 0;
  for (const issue of state.issues) {
    if (issue.dismissed) { counts.dismissed = (counts.dismissed || 0) + 1; continue; }
    if (issue.resolved) { counts.resolved = (counts.resolved || 0) + 1; continue; }
    counts[issue.kind] = (counts[issue.kind] || 0) + 1;
    // "Everything" counts what the list would actually show right now
    if (!(issue.severity === 'noise' && !state.junk)) open += 1;
  }
  const kinds = state.boot.kinds;
  const addChip = (id, label, count) => {
    if (!count && id !== 'all') return;
    const chip = el('button', `chip ${state.filterKind === id ? 'active' : ''}`,
      `${esc(label)}<span class="n">${count}</span>`);
    chip.onclick = () => { state.filterKind = id; render(); };
    chips.appendChild(chip);
  };
  addChip('all', 'Everything', open);
  for (const [kind, info] of Object.entries(kinds)) {
    if (kind === 'junk' && !state.junk) continue;
    addChip(kind, info.label, counts[kind] || 0);
  }
  addChip('resolved', 'Resolved', counts.resolved || 0);
  if (counts.dismissed) addChip('dismissed', 'Hidden', counts.dismissed);
}

const RESOLVED_INFO = {
  label: 'Resolved',
  blurb: 'The client reported these earlier, but nothing is left to fix: the files are gone from both sides or were renamed.',
};

function renderEmptyDetail() {
  const b = state.boot || {};
  const p = (b.folder && b.folder.platform) || {};
  const sources = [];
  sources.push('the client\'s log (errors it reported recently)');
  sources.push(b.folder && b.folder.journal_entries
    ? `the sync journal (${b.folder.journal_entries.toLocaleString()} entries)`
    : '<b>not</b> the sync journal (it is unavailable on this Mac)');
  sources.push(b.deep && b.deep.results
    ? `a deep server scan (${b.deep.results} finding(s))`
    : '<b>not</b> a deep server scan');
  const lines = [
    `Checked: ${sources.join(', ')}.`,
    'An empty list means the doctor found nothing <em>right now</em>; an issue that was fixed '
      + 'stays fixed, and log lines about it are not repeated.',
  ];
  if (!(b.deep && b.deep.results)) {
    lines.push('<b>Run <em>Deep scan</em></b> to crawl the server itself for names and paths '
      + 'Windows cannot store, files the client skipped once and never mentioned again, '
      + 'and case clashes. It needs no journal and no log.');
  }
  if (state.junk) {
    lines.push('Junk/noise is switched on, so `._*`, `.DS_Store` and Finder-trash leftovers '
      + 'would be listed above if they existed.');
  } else {
    lines.push('Switch on <em>Show junk/noise</em> to include `._*`, `.DS_Store` copies and '
      + 'leftovers in the folder\'s `.Trash` folder.');
  }
  if (p.dataless && p.dataless.dataless > 0) {
    lines.push(`${p.dataless.dataless.toLocaleString()} of ${p.dataless.files.toLocaleString()} files `
      + 'are not downloaded (cloud placeholders). They are still checked by name and by metadata.');
  }
  if (b.folder && b.folder.journal_error) lines.push(esc(b.folder.journal_error));
  lines.push('<div class="actions"><button class="btn small primary" id="btn-localcheck">'
    + 'Check the folder itself</button></div>');
  $('#empty-detail').innerHTML = lines.map((l) => `<p>${l}</p>`).join('');
  const btn = $('#btn-localcheck');
  if (btn) btn.onclick = () => runLocalCheck();
}

/* ------------------------------------------------------- local folder check */
async function runLocalCheck() {
  const btn = $('#btn-localcheck');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> checking…'; }
  const lines = ['Walking the sync folder and checking every name against the client\'s rules '
    + '(no server needed). A cloud folder costs about a second per directory, so this can take a minute.'];
  $('#empty-detail').innerHTML = lines.map((l) => `<p>${l}</p>`).join('');
  try {
    const data = await api('/api/localcheck', { method: 'POST', body: { timeout: 180 } });
    const s = data.state;
    const out = [];
    out.push(`Checked ${s.dirs.toLocaleString()} folder(s), ${s.files.toLocaleString()} file(s) in `
      + `${s.seconds.toFixed(0)}s` + (s.complete ? '.' : ' (stopped at the time limit).'));
    if (data.skipped && data.skipped.length) {
      out.push(`Skipped large folder(s): ${data.skipped.map(esc).join(', ')}.`);
    }
    if (!s.problems.length) {
      out.push('No local name problems: every file name here can be stored on Windows.');
      $('#empty-detail').innerHTML = out.map((l) => `<p>${l}</p>`).join('');
      return;
    }
    const counts = {};
    for (const p of s.problems) counts[p.label] = (counts[p.label] || 0) + 1;
    out.push(`${s.problems.length.toLocaleString()} problem(s): `
      + Object.entries(counts).map(([k, v]) => `${esc(k)} ${v}`).join(', ') + '.');
    const rows = s.problems.slice(0, 400).map((p) => {
      const actions = p.actions.filter((a) => ['rename_local', 'delete_local', 'open_local', 'ignore', 'dismiss'].includes(a));
      return `<div class="localrow"><div><b>${esc(p.path)}</b>
        <div class="dim">${esc(p.label)}${p.reason ? ' — ' + esc(p.reason) : ''}</div></div>
        <div class="actions">${actions.map((a) => `<button class="btn small ghost" data-act="${a}" data-path="${esc(p.path)}" data-key="${esc(p.kind + ':' + p.path)}">${esc((ACTION_META[a] || {}).label || a)}</button>`).join('')}</div></div>`;
    }).join('');
    if (s.problems.length > 400) out.push(`Showing the first 400 of ${s.problems.length}.`);
    $('#empty-detail').innerHTML = out.map((l) => `<p>${l}</p>`).join('') + `<div class="localrows">${rows}</div>`;
    for (const b of document.querySelectorAll('#empty-detail [data-act]')) {
      b.onclick = (event) => {
        event.stopPropagation();
        const pseudo = { path: b.dataset.path, key: b.dataset.key, title: b.dataset.path.split('/').pop() };
        if (b.dataset.act === 'dismiss') return dismissIssue(pseudo);
        if (b.dataset.act === 'open_local') return openSide(pseudo, 'open_local');
        return runAction(pseudo, b.dataset.act, {});
      };
    }
  } catch (err) {
    $('#empty-detail').innerHTML = `<p class="err">${esc(err.message)}</p>`;
    if (btn) { btn.disabled = false; btn.textContent = 'Check the folder itself'; }
  }
}

function renderIssues() {
  const list = $('#issues');
  list.innerHTML = '';
  const issues = visibleIssues();
  $('#empty').hidden = issues.length > 0;
  if (!issues.length) renderEmptyDetail();

  if (state.filterKind === 'resolved') {
    const group = el('div', 'group');
    group.innerHTML = `<div class="group-head"><h2>${esc(RESOLVED_INFO.label)}</h2>
      <span class="blurb">${esc(RESOLVED_INFO.blurb)}</span></div>`;
    const card = el('div', 'card');
    for (const issue of issues) card.appendChild(renderRow(issue));
    group.appendChild(card);
    list.appendChild(group);
    return;
  }

  const groups = new Map();
  for (const issue of issues) {
    if (!groups.has(issue.kind)) groups.set(issue.kind, []);
    groups.get(issue.kind).push(issue);
  }
  const order = Object.keys(state.boot.kinds);
  const orderedKeys = [...groups.keys()].sort((a, b) => order.indexOf(a) - order.indexOf(b));

  for (const kind of orderedKeys) {
    const group = el('div', 'group');
    const info = state.boot.kinds[kind] || {};
    const head = el('div', 'group-head');
    head.innerHTML = `<h2>${esc(info.label || kind)}</h2>
      <span class="blurb">${esc(info.blurb || '')}</span>`;
    group.appendChild(head);

    const card = el('div', 'card');
    for (const issue of groups.get(kind)) card.appendChild(renderRow(issue));
    group.appendChild(card);
    list.appendChild(group);
  }
}

function renderRow(issue) {
  const wrapper = el('div', 'issue');
  const row = el('div', `row ${state.open.has(issue.key) ? 'open' : ''}`);
  const badgeCls = issue.severity;
  const local = issue.local || {};
  const remote = issue.remote || {};
  const localLabel = !local.exists ? '<span class="absent">missing</span>'
    : local.is_dir ? 'folder' : esc(fmtSize(local.size));
  const remoteLabel = !remote.exists ? '<span class="absent">missing</span>'
    : remote.is_dir ? 'folder' : esc(fmtSize(remote.size));

  row.innerHTML = `
    <div class="caret">▶</div>
    <div class="row-main">
      <div class="row-title">
        <span class="name">${esc(issue.title)}</span>
        ${issue.resolved
          ? '<span class="badge resolved">✓ resolved</span>'
          : `<span class="badge ${badgeCls}">${esc(issue.kind_label)}</span>`}
        ${issue.dismissed ? '<span class="badge noise">hidden</span>' : ''}
        ${issue.is_dir ? '<span class="badge noise">folder</span>' : ''}
        ${issue.occurrences > 1 && !issue.resolved ? `<span class="badge noise">×${issue.occurrences}</span>` : ''}
      </div>
      <div class="row-path" title="${esc(issue.path)}">${esc(issue.path)}</div>
      ${issue.resolved && issue.resolved_note
        ? `<div class="row-reason resolved-note">${esc(issue.resolved_note)}</div>`
        : (issue.reason ? `<div class="row-reason">${esc(issue.reason)}</div>` : '')}
    </div>
    <div class="row-side">
      <div class="side-meta">
        <div>local&nbsp;<b>${localLabel}</b></div>
        <div>server&nbsp;<b>${remoteLabel}</b></div>
      </div>
    </div>`;

  row.onclick = () => toggleIssue(issue);
  wrapper.appendChild(row);

  const detail = el('div', `detail ${state.open.has(issue.key) ? 'open' : ''}`);
  detail.dataset.key = issue.key;
  if (state.open.has(issue.key)) fillDetail(detail, issue);
  wrapper.appendChild(detail);
  return wrapper;
}

function toggleIssue(issue) {
  const wrapper = [...document.querySelectorAll('.issue')].find(
    (node) => node.querySelector('.detail')?.dataset.key === issue.key);
  if (!wrapper) return;
  const row = wrapper.querySelector('.row');
  const detail = wrapper.querySelector('.detail');
  if (state.open.has(issue.key)) {
    state.open.delete(issue.key);
    row.classList.remove('open');
    detail.classList.remove('open');
  } else {
    state.open.add(issue.key);
    row.classList.add('open');
    detail.classList.add('open');
    fillDetail(detail, issue);
  }
}

/* ---------------------------------------------------------------- details */
function sideBlock(issue, side) {
  const data = side === 'local' ? issue.local : issue.remote;
  const exists = data && data.exists;
  const isDir = Boolean(data && data.is_dir) || (exists && issue.is_dir && side === 'remote' && data.is_dir);
  const kind = kindOf(issue.title);
  const metaRows = [];
  if (exists) {
    if (isDir) {
      metaRows.push(['type', 'folder']);
      if (side === 'remote' && data.size) metaRows.push(['content', esc(fmtSize(data.size))]);
    } else {
      metaRows.push(['size', esc(fmtSize(data.size))]);
    }
    metaRows.push(['modified', esc(fmtTime(data.mtime))]);
    if (side === 'remote') {
      if (data.fileid) metaRows.push(['file id', esc(data.fileid)]);
      if (data.etag) metaRows.push(['etag', `<code>${esc(data.etag.slice(0, 16))}</code>`]);
      if (data.checksums) metaRows.push(['checksum', `<code>${esc(data.checksums)}</code>`]);
      if (data.permissions) metaRows.push(['permissions', `<code>${esc(data.permissions)}</code>`]);
    }
    if (side === 'local' && data.attributes) {
      // macOS-only facts (see ncsd/detect.py: mac_local_attributes).
      const a = data.attributes;
      if (a.dataless) metaRows.push(['macOS', 'not downloaded (cloud placeholder)']);
      if (a.compressed) metaRows.push(['macOS', 'compressed on disk']);
      if (a.quarantine) metaRows.push(['macOS', 'quarantined (Gatekeeper)']);
      if (a.resource_fork) metaRows.push(['macOS', '<code>._</code> metadata sidecar present']);
    }
  }
  const meta = exists
    ? `<div class="meta">${metaRows.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join('')}</div>`
    : `<div class="absent-note">${side === 'local'
      ? (IS_MAC
        ? 'Not present locally — the file was never downloaded, or it was deleted or moved in Finder.'
        : 'Not present locally — Windows cannot create this name, or the file was never downloaded.')
      : 'Not present on the server.'}</div>`;
  const preview = exists && !isDir
    ? `<div class="preview" data-side="${side}"></div>`
    : '';
  const buttons = exists && !isDir && kind !== 'binary'
    ? `<div class="actions" style="margin-top:10px">
        <button class="btn small ghost" data-preview="${side}">Preview ${kind}</button>
      </div>`
    : '';
  return `<div class="side">
    <h3>${side === 'local' ? 'Your local version' : 'Server version'}</h3>
    ${meta}${preview}${buttons}
  </div>`;
}

function fillDetail(container, issue) {
  container.innerHTML = `
    <div class="compare">
      ${sideBlock(issue, 'local')}
      ${sideBlock(issue, 'remote')}
    </div>
    <div class="detail-extra" data-extra></div>
    <div class="actions" data-actions></div>`;

  container.querySelectorAll('[data-preview]').forEach((btn) => {
    btn.onclick = (event) => {
      event.stopPropagation();
      showPreview(container, issue, btn.dataset.preview);
    };
  });

  renderActions(container.querySelector('[data-actions]'), issue);

  if (issue.remote && issue.remote.exists && !issue.is_dir) {
    renderVersions(container.querySelector('[data-extra]'), issue);
  } else if (issue.is_dir) {
    const extra = container.querySelector('[data-extra]');
    extra.innerHTML = `<p class="dim">This is a <b>folder</b>. Version history applies to files, so
      there is nothing to compare here — but you can rename it on the server, delete it, or hide it.</p>`;
  } else {
    const extra = container.querySelector('[data-extra]');
    if (issue.resolved) {
      extra.innerHTML = `<div class="resolved-box">✓ ${esc(issue.resolved_note
        || 'Nothing is left to fix for this entry.')}</div>`;
    } else if (issue.severity === 'noise') {
      extra.innerHTML = '<p class="dim">This file is on the client\'s ignore list. Nothing is broken — the client merely reports it as a sync error.</p>';
    } else {
      extra.innerHTML = '';
    }
  }
}

function showPreview(container, issue, side) {
  const holder = container.querySelector(`.preview[data-side="${side}"]`);
  if (!holder) return;
  const kind = kindOf(issue.title);
  const params = new URLSearchParams({ path: issue.path, side, token: TOKEN });
  const raw = `/api/raw?${params.toString()}`;
  holder.innerHTML = '<span class="dim"><span class="spin"></span> loading…</span>';

  if (kind === 'image') {
    holder.innerHTML = `<img src="${raw}" alt="">`;
  } else if (kind === 'pdf') {
    holder.innerHTML = `<iframe src="${raw}" title="pdf"></iframe>`;
  } else if (kind === 'audio') {
    holder.innerHTML = `<audio controls src="${raw}" style="width:100%"></audio>`;
  } else if (kind === 'video') {
    holder.innerHTML = `<video controls src="${raw}" style="width:100%;max-height:260px"></video>`;
  } else if (kind === 'text') {
    api(`/api/diff?${new URLSearchParams({ path: issue.path, side }).toString()}`)
      .then((data) => {
        holder.innerHTML = `<pre>${esc((data.text || '').slice(0, 20000))}</pre>`;
      })
      .catch((err) => { holder.innerHTML = `<span class="dim">${esc(err.message)}</span>`; });
  } else {
    holder.innerHTML = '<span class="dim">No preview for this file type.</span>';
  }
}

function renderVersions(holder, issue) {
  holder.innerHTML = `
    <h3>Server versions</h3>
    <div class="versions" data-versions><span class="dim"><span class="spin"></span> loading…</span></div>
    <div class="actions">
      <button class="btn small ghost" data-compare>Compare contents</button>
    </div>
    <div data-diff></div>`;

  holder.querySelector('[data-compare]').onclick = async (event) => {
    event.stopPropagation();
    const box = holder.querySelector('[data-diff]');
    box.innerHTML = '<div class="diffbox"><span class="spin"></span> comparing…</div>';
    try {
      const data = await api(`/api/diff?${new URLSearchParams({ path: issue.path }).toString()}`);
      if (data.identical) {
        box.innerHTML = '<div class="diffbox">The local and server contents are identical.</div>';
        return;
      }
      if (!data.diff) {
        box.innerHTML = '<div class="diffbox">One side is missing or empty.</div>';
        return;
      }
      const html = data.diff.split('\n').map((line) => {
        const cls = line.startsWith('+++') || line.startsWith('---') ? 'hunk'
          : line.startsWith('+') ? 'add'
            : line.startsWith('-') ? 'del'
              : line.startsWith('@@') ? 'hunk' : '';
        return `<span class="${cls}">${esc(line)}</span>`;
      }).join('\n');
      box.innerHTML = `<div class="diffbox">${html}</div>`;
    } catch (err) {
      box.innerHTML = `<div class="diffbox">${esc(err.message)}</div>`;
    }
  };

  const list = holder.querySelector('[data-versions]');
  api(`/api/versions?${new URLSearchParams({ path: issue.path }).toString()}`)
    .then((data) => {
      if (!data.versions || !data.versions.length) {
        list.innerHTML = '<span class="dim">No earlier versions are stored on the server.</span>';
        return;
      }
      list.innerHTML = '';
      for (const version of data.versions) {
        const row = el('div', 'version');
        row.innerHTML = `<span class="v-when">${esc(fmtTime(version.mtime))}</span>
          <span class="v-size">${esc(fmtSize(version.size))}</span>
          <span class="spacer"></span>`;
        const asCopy = el('button', 'btn small ghost', 'Download as copy');
        asCopy.onclick = (event) => {
          event.stopPropagation();
          runAction(issue, 'restore_version', { version: version.version });
        };
        const rollback = el('button', 'btn small danger', 'Roll back server');
        rollback.onclick = (event) => {
          event.stopPropagation();
          runAction(issue, 'restore_version_in_place', { version: version.version });
        };
        row.appendChild(asCopy);
        row.appendChild(rollback);
        list.appendChild(row);
      }
    })
    .catch((err) => { list.innerHTML = `<span class="dim">${esc(err.message)}</span>`; });
}

/* ---------------------------------------------------------------- actions */
function renderActions(holder, issue) {
  holder.innerHTML = '';
  if (issue.dismissed) {
    const restore = el('button', 'btn small', 'Unhide');
    restore.onclick = async (event) => {
      event.stopPropagation();
      try {
        await api('/api/apply', { method: 'POST', body: { action: 'dismiss', keys: [issue.key], undismiss: true } });
        toast('Restored to the list.', 'ok');
        await scan();
      } catch (err) { toast(err.message, 'err'); }
    };
    holder.appendChild(restore);
    return;
  }

  const groups = { main: [], fix: [], danger: [], misc: [] };
  for (const action of issue.actions) {
    const meta = ACTION_META[action];
    if (!meta) continue;
    groups[meta.group].push({ action, meta });
  }

  for (const key of ['main', 'fix', 'danger', 'misc']) {
    if (!groups[key].length) continue;
    const box = el('div', 'action-group');
    for (const { action, meta } of groups[key]) {
      const btn = el('button', `btn small ${meta.cls}`, esc(meta.label));
      if (meta.hint) btn.title = meta.hint;
      btn.onclick = (event) => {
        event.stopPropagation();
        if (action === 'dismiss') return dismissIssue(issue);
        if (action === 'open_local' || action === 'open_remote') return openSide(issue, action);
        return runAction(issue, action, {});
      };
      box.appendChild(btn);
    }
    holder.appendChild(box);
  }
}

async function dismissIssue(issue) {
  try {
    const data = await api('/api/apply', { method: 'POST', body: { action: 'dismiss', keys: [issue.key] } });
    toast(data.message || 'Hidden.', 'ok');
    state.open.delete(issue.key);
    await scan();
  } catch (err) { toast(err.message, 'err'); }
}

async function openSide(issue, action) {
  try {
    const data = await api('/api/open', {
      method: 'POST',
      body: { path: issue.path, side: action === 'open_local' ? 'local' : 'remote' },
    });
    toast(data.message || 'Opened.', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function runAction(issue, action, params) {
  const meta = ACTION_META[action] || { label: action };
  let plan;
  try {
    plan = (await api('/api/plan', { method: 'POST', body: { action, path: issue.path, params } })).plan;
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  if (!LOADED_ACTIONS.has(action)) return;
  params = plan.params || params;

  // Explicit version actions do not need a second confirmation beyond their own plan.
  const nameField = NEEDS_NAME.has(action)
    ? `<label>New file name</label>
       <input type="text" id="modal-name" value="${esc(plan.params.new_name || '')}" spellcheck="false">
       <div class="dim" style="margin-top:6px;font-size:12px">Characters Windows cannot store are replaced automatically in the suggestion.</div>
       ${IS_MAC ? '<div class="dim" style="margin-top:4px;font-size:12px">This Mac can store the name either way, but a Windows machine sharing the folder cannot.</div>' : ''}`
    : '';

  openModal({
    title: meta.label.replace('…', ''),
    body: planBody(plan, nameField),
    okLabel: meta.label.replace('…', ''),
    danger: Boolean(plan.destructive),
    onOk: async () => {
      const body = { action, path: issue.path, params: { ...params } };
      if (NEEDS_NAME.has(action)) {
        const input = document.getElementById('modal-name');
        if (input) body.params.new_name = input.value.trim();
      }
      const result = await api('/api/apply', { method: 'POST', body });
      const outcome = result.outcome || result;
      toast(outcome.message || 'Done.', 'ok');
      state.open.delete(issue.key);
      await scan({ deep: false });
      return true;
    },
  });
}

/* -------------------------------------------------------------- deep scan */
let deepTimer = null;

async function startDeepScan() {
  $('#btn-deep').disabled = true;
  try {
    await api('/api/deepscan', { method: 'POST', body: {} });
    $('#deepbanner').hidden = false;
    startDeepPolling();
  } catch (err) {
    toast(err.message, 'err');
    $('#btn-deep').disabled = false;
  }
}

function startDeepPolling() {
  $('#deepbanner').hidden = false;
  if (deepTimer) return;
  const tick = async () => {
    try {
      const { status } = await api('/api/deepscan');
      const percent = Number(status.progress_percent ?? (status.progress || 0) * 100);
      const text = status.running
        ? `Scanning the server for names Windows cannot store — ${status.done}/${status.total} folders (${percent.toFixed(1)}%)`
        : `${status.message} · ${status.issues} finding(s)` +
          (status.errors ? ` · ${status.errors} folder(s) could not be read (transient or no permission)` : '');
      $('#deepbanner-text').textContent = text;
      $('#deep-progress').style.width = `${percent.toFixed(1)}%`;
      if (!status.running) {
        clearInterval(deepTimer);
        deepTimer = null;
        $('#btn-deep').disabled = false;
        setTimeout(() => { $('#deepbanner').hidden = true; }, 2500);
        await scan({ deep: true });
        toast(`Deep scan finished: ${status.issues} finding(s).`, 'ok');
      }
    } catch (err) {
      clearInterval(deepTimer);
      deepTimer = null;
      $('#btn-deep').disabled = false;
      $('#deepbanner').hidden = true;
      toast(err.message, 'err');
    }
  };
  deepTimer = setInterval(tick, 700);
  tick();
}

/* ------------------------------------------------------------------ history */
async function showAudit() {
  try {
    const data = await api('/api/audit');
    const rows = data.entries.length
      ? data.entries.map((entry) => `<div class="version">
          <span class="v-when">${esc(fmtTime(entry.at))}</span>
          <span class="v-size">${esc(entry.action)}</span>
          <span class="spacer"></span>
          <span class="dim" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px">${esc(entry.path)}</span>
        </div>`).join('')
      : '<p class="dim">No actions recorded yet.</p>';
    openModal({
      title: 'Action history',
      body: `<p class="dim">Every action is logged. Backups live in <code>${esc(data.data_dir)}</code>
             (${data.backups[0]} file(s), ${esc(fmtSize(data.backups[1]))}).</p>
             <div class="versions">${rows}</div>`,
      okLabel: 'Close',
      onOk: null,
    });
  } catch (err) { toast(err.message, 'err'); }
}

/* ------------------------------------------------------------------- client */
function showClient() {
  const running = state.boot?.client?.running;
  openModal({
    title: 'Desktop client',
    body: `<p>${running ? 'The client is running.' : 'The client is not running.'}
      Restarting it makes it rescan the folder immediately, which is useful after
      ignore-list changes or after applying fixes.</p>
      <div class="warn">Quitting immediately interrupts any transfer in progress. The client
      recovers safely on the next start.</div>`,
    okLabel: 'Restart client',
    onOk: async () => {
      const data = await api('/api/client', { method: 'POST', body: { op: 'restart' } });
      toast(data.message, data.ok ? 'ok' : 'err');
      state.boot.client.running = data.running;
      return true;
    },
  });
}

/* ------------------------------------------------------------------- wire */
$('#btn-scan').onclick = () => scan({ deep: false });
$('#btn-deep').onclick = startDeepScan;
$('#btn-audit').onclick = showAudit;
$('#btn-client').onclick = showClient;
$('#toggle-junk').onchange = async (event) => {
  state.junk = event.target.checked;
  await api('/api/settings', { method: 'POST', body: { include_junk: state.junk } });
  await scan({ deep: false });
};
$('#search').oninput = (event) => {
  state.search = event.target.value;
  renderIssues();
};
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('#modal').hidden) $('#modal').hidden = true;
});

boot().catch((err) => {
  document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif">
    <h2>Could not talk to the Sync Doctor backend</h2><pre>${esc(err.message)}</pre>
    <p>Check the console window for the correct URL (it contains a security token).</p></div>`;
});
