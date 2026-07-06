/* ============================================================================
   pages/settings.js — Settings page (Fuzzy Tribble).
   Wires the mockup to real endpoints: /api/settings/* + /api/system/metrics
   + /api/scanners. Data policy: real where cheap; placeholders clearly muted.
   ========================================================================== */
import { api, postJSON, $, esc, toast, fmtInt } from '../core/api.js';
import { mountShell } from '../core/shell.js';
import { sparkline } from '../core/charts.js';

let OVERVIEW = null;   // /api/settings/overview
let METRICS = null;    // /api/system/metrics
let SCANNERS = [];     // /api/scanners
let activeTab = 'Scanners';

const TABS = [
  ['Trading', '◫'], ['Scanners', '◎'], ['Alerts', '⚑'], ['Startup Profiles', '⚡'],
  ['Containers', '▦'], ['Dashboard', '▤'], ['Data Sources', '⛃'], ['Logs', '☰'],
  ['Backup', '⟲'], ['Advanced', '⚙'],
];

const PRESETS = [
  { key: 'fast',     ic: '⚡', name: 'Fast Startup',    desc: 'Essential scanners only',  match: (s) => /iv-collector|discount|dashboard|break/.test(s) },
  { key: 'intraday', ic: '▤', name: 'Intraday Trading', desc: 'All intraday scanners',    match: (s) => !/momentum|directional/.test(s) },
  { key: 'research', ic: '◎', name: 'Research Mode',    desc: 'All scanners + analysis',  match: () => true },
  { key: 'dev',      ic: '⛃', name: 'Development',       desc: 'Dashboard + API only',     match: (s) => /dashboard/.test(s) },
];
let selectedPreset = 'intraday';

init();

async function init() {
  mountShell({ active: 'settings', title: 'Settings', icon: '⚙',
    subtitle: 'Configure your trading system, alerts, containers and preferences' });
  renderTabs();
  await loadAll();
}

async function loadAll() {
  const [ov, mx, sc] = await Promise.allSettled([
    api('/api/settings/overview'),
    api('/api/system/metrics'),
    api('/api/scanners'),
  ]);
  OVERVIEW = ov.status === 'fulfilled' ? ov.value : emptyOverview();
  METRICS  = mx.status === 'fulfilled' ? mx.value : null;
  SCANNERS = sc.status === 'fulfilled' ? (sc.value.scanners || []) : [];
  renderStatRow();
  renderMetricRow();
  renderTab(activeTab);
}

function emptyOverview() {
  return { services: [], profiles: {}, status: {}, alert_matrix: [], alert_types: [], channels: ['none','telegram','discord','both'], kill_switch: false, startup_profile: {} };
}

/* ── Stat cards ──────────────────────────────────────────────────────────── */
function renderStatRow() {
  const m = METRICS || {};
  const c = m.containers || {};
  const running = c.running ?? Object.values(OVERVIEW.status || {}).filter((s) => String(s).includes('running')).length;
  const total = c.total ?? (OVERVIEW.services || []).length;
  const healthy = total > 0 && running === total;
  const db = m.database || {};

  const cards = [
    statCard('✓', 'System Health', healthy ? 'Healthy' : (total ? 'Degraded' : '—'), healthy ? 'All systems operational' : `${running}/${total} running`, healthy ? 'pos' : 'warn'),
    statCard('▦', 'Containers', total ? `${running} / ${total}` : '—', 'Running'),
    statCard('⚑', 'Alerts Today', ph(m.alerts_today), 'Total Alerts', '', m.alerts_today == null),
    statCard('⚡', 'Signals Today', fmtInt(m.signals_today), 'Total Signals'),
    statCard('✈', 'Telegram', m.telegram?.status || 'Unknown', m.telegram?.detail || 'status unavailable', m.telegram?.ok ? 'pos' : '', !m.telegram),
    statCard('⛁', 'Database', db.ok ? 'Healthy' : (db.ok === false ? 'Error' : '—'), db.resp_ms != null ? `Response ${db.resp_ms} ms` : 'read-only', db.ok ? 'pos' : ''),
    statCard('⟲', 'Last Backup', m.last_backup?.when || '—', m.last_backup?.detail || 'not tracked yet', '', !m.last_backup),
  ];
  $('statRow').innerHTML = cards.join('');
}
function statCard(ic, label, val, sub, cls = '', placeholder = false) {
  return `<div class="stat-card ${placeholder ? 'placeholder' : ''}">
    <div class="sc-head"><span class="sc-ic">${ic}</span>${esc(label)}${placeholder ? '<span class="sc-ph" style="margin-left:auto">static</span>' : ''}</div>
    <div class="sc-val ${cls}">${esc(val)}</div>
    <div class="sc-sub">${esc(sub)}</div>
  </div>`;
}
const ph = (v) => (v == null ? '—' : fmtInt(v));

/* ── Tabs ────────────────────────────────────────────────────────────────── */
function renderTabs() {
  $('tabs').innerHTML = TABS.map(([name, ic]) =>
    `<div class="tab ${name === activeTab ? 'active' : ''}" data-tab="${name}"><span>${ic}</span>${name}</div>`).join('');
  $('tabs').querySelectorAll('.tab').forEach((t) =>
    t.addEventListener('click', () => { activeTab = t.dataset.tab; renderTabs(); renderTab(activeTab); }));
}

function renderTab(name) {
  const host = $('tabContent');
  switch (name) {
    case 'Scanners':          host.innerHTML = scannersTab(); wireScanners(); wirePresets(); wireQuickActions(); break;
    case 'Alerts':            host.innerHTML = alertsTab(); wireAlerts(); break;
    case 'Startup Profiles':  host.innerHTML = startupTab(); wireStartup(); break;
    case 'Containers':        host.innerHTML = containersTab(); wireContainers(); break;
    default:                  host.innerHTML = placeholderTab(name);
  }
}

/* ── Scanners tab: table + right rail ────────────────────────────────────── */
function scannersTab() {
  return `<div class="grid-main">
    <div class="card">
      <div class="card-head">
        <div><div class="card-title">Scanner Management</div><div class="card-sub">Manage all scanners, their priority and status</div></div>
        <button class="btn add" id="addScanner">+ Add Custom Scanner</button>
      </div>
      <div class="table-wrap">
        <table class="data stack">
          <thead><tr><th>Scanner</th><th>Priority</th><th>Status</th><th>Last Signal</th><th>Signals Today</th><th>Actions</th></tr></thead>
          <tbody>${SCANNERS.map(scannerRow).join('') || `<tr><td colspan="6"><div class="empty">No scanners found (compose not mounted?)</div></td></tr>`}</tbody>
        </table>
      </div>
      <div class="card-sub" style="margin-top:12px">Showing ${SCANNERS.length} of ${(OVERVIEW.services||[]).length} services</div>
    </div>
    <div class="rail">
      ${profilesCard()}
      ${quickActionsCard()}
    </div>
  </div>`;
}

function scannerRow(s) {
  const st = normStatus(s.status);
  const stars = starRating(s.priority);
  const ph = s.signals_today == null;
  return `<tr>
    <td data-label="Scanner" class="name"><div class="cell-strong">${esc(s.label || s.name)}</div><div class="card-sub">${esc(s.sub || s.name)}</div></td>
    <td data-label="Priority">${stars}</td>
    <td data-label="Status"><span class="tag ${st}">${st.toUpperCase()}</span></td>
    <td data-label="Last Signal" class="num">${esc(s.last_signal || '—')}</td>
    <td data-label="Signals Today" class="num${ph ? ' dim' : ''}">${s.signals_today == null ? '—' : s.signals_today}</td>
    <td data-label="Actions">
      <div class="btn-group">
        <button class="btn sm" data-act="stop" data-svc="${esc(s.name)}">❚❚ Pause</button>
        <button class="btn sm" data-act="restart" data-svc="${esc(s.name)}">⟳ Restart</button>
        <button class="btn sm" data-act="cfg" data-svc="${esc(s.name)}">⚙ Settings</button>
      </div>
    </td>
  </tr>`;
}

function wireScanners() {
  $('addScanner')?.addEventListener('click', () => toast('Add Custom Scanner — coming soon', 'ok'));
  $('tabContent').querySelectorAll('button[data-act]').forEach((b) => {
    b.addEventListener('click', () => {
      const svc = b.dataset.svc, act = b.dataset.act;
      if (act === 'cfg') return toast(`Settings for ${svc} — coming soon`);
      containerAction(svc, act);
    });
  });
}

/* ── Right rail cards ────────────────────────────────────────────────────── */
function profilesCard() {
  const services = OVERVIEW.services || [];
  const rows = PRESETS.map((p) => {
    const count = services.length ? services.filter(p.match).length : '—';
    return `<div class="profile ${p.key === selectedPreset ? 'active' : ''}" data-preset="${p.key}">
      <span class="p-ic">${p.ic}</span>
      <div class="p-body"><div class="p-name">${p.name}</div><div class="p-desc">${p.desc}</div></div>
      <span class="p-count">${count} services</span>
      <span class="p-go" data-apply="${p.key}" title="Apply">▶</span>
    </div>`;
  }).join('');
  return `<div class="card">
    <div class="card-head"><div><div class="card-title">Startup Profiles</div><div class="card-sub">Quick start with pre-defined profiles</div></div></div>
    ${rows}
    <button class="btn" style="width:100%;justify-content:center;margin-top:12px" id="manageProfiles">⚙ Manage Profiles</button>
  </div>`;
}
function wirePresets() {
  $('tabContent').querySelectorAll('[data-preset]').forEach((el) =>
    el.addEventListener('click', () => { selectedPreset = el.dataset.preset; renderTab('Scanners'); wireScanners(); wirePresets(); wireQuickActions(); }));
  $('tabContent').querySelectorAll('[data-apply]').forEach((el) =>
    el.addEventListener('click', (e) => { e.stopPropagation(); applyPreset(el.dataset.apply); }));
  $('manageProfiles')?.addEventListener('click', () => { activeTab = 'Startup Profiles'; renderTabs(); renderTab(activeTab); });
}
async function applyPreset(key) {
  const preset = PRESETS.find((p) => p.key === key);
  const services = OVERVIEW.services || [];
  if (!services.length) return toast('No services available (compose not mounted)', 'err');
  if (!confirm(`Apply "${preset.name}" profile? This sets autostart for ${services.filter(preset.match).length} services and starts them.`)) return;
  toast(`Applying ${preset.name}…`);
  try {
    for (const svc of services) {
      await postJSON('/api/settings/startup-profile', { container_name: svc, autostart: preset.match(svc) });
    }
    const r = await postJSON('/api/settings/startup-profile/apply', {});
    toast(r.ok ? `${preset.name} applied` : `Apply failed: ${(r.stderr || '').slice(0, 120)}`, r.ok ? 'ok' : 'err');
    loadAll();
  } catch (e) { toast(String(e.message || e).slice(0, 140), 'err'); }
}

function quickActionsCard() {
  return `<div class="card">
    <div class="card-head"><div class="card-title">Quick Actions</div></div>
    <div class="qa-grid">
      ${qa('restartAll', '⟳', 'Restart All', 'Restart all containers')}
      ${qa('stopAll', '◼', 'Stop All', 'Stop all containers')}
      ${qa('refresh', '↻', 'Refresh Status', 'Refresh system status')}
      ${qa('clearCache', '🗑', 'Clear Cache', 'Clear application cache')}
      ${qa('viewLogs', '☰', 'View Logs', 'View system logs')}
      ${qa('emergency', '⛔', 'Emergency Stop', 'Kill all processes', true)}
    </div>
  </div>`;
}
function qa(id, ic, t, s, danger = false) {
  return `<button class="qa ${danger ? 'danger' : ''}" id="qa-${id}"><span class="qa-ic">${ic}</span><span><span class="qa-t">${t}</span><br><span class="qa-s">${s}</span></span></button>`;
}
function wireQuickActions() {
  const all = () => OVERVIEW.services || [];
  $('qa-refresh')?.addEventListener('click', () => { toast('Refreshing…'); loadAll(); });
  $('qa-viewLogs')?.addEventListener('click', () => location.href = '/logs');
  $('qa-clearCache')?.addEventListener('click', () => toast('Clear Cache — coming soon'));
  $('qa-restartAll')?.addEventListener('click', () => bulkAction(all(), 'restart', 'Restart all containers?'));
  $('qa-stopAll')?.addEventListener('click', () => bulkAction(all(), 'stop', 'Stop all containers?'));
  $('qa-emergency')?.addEventListener('click', async () => {
    if (!confirm('EMERGENCY STOP — stop every container and arm the alert kill-switch?')) return;
    await postJSON('/api/settings/kill-switch', { on: true }).catch(() => {});
    bulkAction(all(), 'stop', null);
  });
}

/* ── Alerts tab ──────────────────────────────────────────────────────────── */
function alertsTab() {
  const o = OVERVIEW;
  const head = `<tr><th>Container</th>${o.alert_types.map((t) => `<th>${esc(t)}</th>`).join('')}</tr>`;
  const body = o.services.map((c) => {
    const cells = o.alert_types.map((at) => {
      const f = (o.alert_matrix || []).find((x) => x.container_name === c && x.alert_type === at) || { enabled: 1, channel: 'both' };
      const opts = o.channels.map((ch) => `<option value="${ch}" ${ch === f.channel ? 'selected' : ''}>${ch}</option>`).join('');
      return `<td data-label="${esc(at)}">
        <input type="checkbox" ${f.enabled ? 'checked' : ''} data-cb="${esc(c)}|${esc(at)}">
        <select class="inp" data-sel="${esc(c)}|${esc(at)}" style="margin-top:4px">${opts}</select>
      </td>`;
    }).join('');
    return `<tr><td class="name" data-label="Container">${esc(c)}</td>${cells}</tr>`;
  }).join('');

  return `<div class="card">
    <div class="card-head"><div><div class="card-title">Kill Switch</div><div class="card-sub">Master suppression for every alert</div></div>
      <label class="switch"><input type="checkbox" id="killSwitch" ${o.kill_switch ? 'checked' : ''}><span class="track"></span></label>
    </div>
    <div class="card-sub" id="killLabel">${o.kill_switch ? 'All alerts SUPPRESSED' : 'All alerts flowing normally'}</div>
  </div>
  <div class="card">
    <div class="card-head"><div class="card-title">Alert Flag Matrix</div>
      <select class="inp" id="masterAlert"><option value="">Set all channels…</option>${o.channels.map((c) => `<option value="${c}">${c}</option>`).join('')}</select>
    </div>
    <div class="table-wrap"><table class="data"><thead>${head}</thead><tbody>${body || `<tr><td class="empty">No containers</td></tr>`}</tbody></table></div>
  </div>`;
}
function wireAlerts() {
  $('killSwitch')?.addEventListener('change', async (e) => {
    await postJSON('/api/settings/kill-switch', { on: e.target.checked });
    toast(e.target.checked ? 'Kill switch ARMED' : 'Kill switch OFF');
    loadAll();
  });
  $('masterAlert')?.addEventListener('change', async (e) => {
    const ch = e.target.value; if (!ch) return;
    if (!confirm(`Override ALL alert flags to "${ch}"?`)) { e.target.value = ''; return; }
    await postJSON('/api/settings/alert-flag/bulk', { channel: ch });
    toast(`All alerts set to ${ch}`); loadAll();
  });
  const upd = (key) => {
    const [c, at] = key.split('|');
    const enabled = $('tabContent').querySelector(`[data-cb="${CSS.escape(c)}|${CSS.escape(at)}"]`).checked;
    const channel = $('tabContent').querySelector(`[data-sel="${CSS.escape(c)}|${CSS.escape(at)}"]`).value;
    postJSON('/api/settings/alert-flag', { container_name: c, alert_type: at, enabled, channel })
      .then(() => toast(`${c} / ${at} updated`)).catch((e) => toast(String(e.message).slice(0, 120), 'err'));
  };
  $('tabContent').querySelectorAll('[data-cb]').forEach((el) => el.addEventListener('change', () => upd(el.dataset.cb)));
  $('tabContent').querySelectorAll('[data-sel]').forEach((el) => el.addEventListener('change', () => upd(el.dataset.sel)));
}

/* ── Startup Profiles tab (per-container autostart) ──────────────────────── */
function startupTab() {
  const o = OVERVIEW;
  const rows = o.services.map((s) => `
    <div class="profile" style="cursor:default">
      <div class="p-body"><div class="p-name">${esc(s)}</div><div class="p-desc">${(o.profiles[s] || []).join(', ') || 'default'}</div></div>
      <label class="switch"><input type="checkbox" data-auto="${esc(s)}" ${(o.startup_profile[s] ?? true) ? 'checked' : ''}><span class="track"></span></label>
    </div>`).join('');
  return `<div class="card">
    <div class="card-head"><div><div class="card-title">Startup Profile</div><div class="card-sub">Which containers auto-start with "Apply"</div></div>
      <button class="btn primary" id="applyStartup">Apply Startup Profile</button></div>
    ${rows || `<div class="empty">No services</div>`}
  </div>`;
}
function wireStartup() {
  $('tabContent').querySelectorAll('[data-auto]').forEach((el) =>
    el.addEventListener('change', () => postJSON('/api/settings/startup-profile', { container_name: el.dataset.auto, autostart: el.checked })
      .then(() => toast(`${el.dataset.auto} autostart ${el.checked ? 'on' : 'off'}`))));
  $('applyStartup')?.addEventListener('click', async () => {
    toast('Applying startup profile…');
    const r = await postJSON('/api/settings/startup-profile/apply', {});
    toast(r.ok ? 'Startup profile applied' : `Failed: ${(r.stderr || '').slice(0, 120)}`, r.ok ? 'ok' : 'err');
    loadAll();
  });
}

/* ── Containers tab ──────────────────────────────────────────────────────── */
function containersTab() {
  const o = OVERVIEW;
  const rows = o.services.map((s) => {
    const st = normStatus(o.status[s]);
    return `<div class="profile" style="cursor:default">
      <div class="p-body"><div class="p-name"><span class="status-dot ${st}"></span>${esc(s)}</div><div class="p-desc">${esc(o.status[s] || 'unknown')}</div></div>
      <div class="btn-group">
        <button class="btn sm" data-cact="start" data-svc="${esc(s)}">start</button>
        <button class="btn sm" data-cact="stop" data-svc="${esc(s)}">stop</button>
        <button class="btn sm" data-cact="restart" data-svc="${esc(s)}">restart</button>
        <button class="btn sm danger" data-cact="rebuild" data-svc="${esc(s)}">rebuild</button>
      </div>
    </div>`;
  }).join('');
  return `<div class="card">
    <div class="card-head"><div class="card-title">Containers</div><button class="btn" id="refreshC">↻ Refresh</button></div>
    ${rows || `<div class="empty">No services (compose not mounted)</div>`}
  </div>`;
}
function wireContainers() {
  $('refreshC')?.addEventListener('click', () => loadAll());
  $('tabContent').querySelectorAll('[data-cact]').forEach((b) =>
    b.addEventListener('click', () => containerAction(b.dataset.svc, b.dataset.cact)));
}

/* ── Placeholder tabs (not yet wired) ────────────────────────────────────── */
function placeholderTab(name) {
  return `<div class="card"><div class="card-title">${esc(name)}</div>
    <div class="empty">This section isn't wired up yet — coming in a later phase.</div></div>`;
}

/* ── Bottom metric row ───────────────────────────────────────────────────── */
function renderMetricRow() {
  const m = METRICS || {};
  const c = m.containers || {};
  const spark = () => Array.from({ length: 16 }, () => 30 + Math.random() * 40); // decorative only
  const metrics = [
    metricCard('Total Containers', c.total != null ? `${c.total}` : '—', c.total != null ? `${c.running}/${c.total} running` : 'compose not mounted', c.total != null ? Math.round((c.running / Math.max(c.total,1)) * 100) : null, 'var(--green)', c.total == null),
    metricCard('CPU Usage', pct(m.cpu_pct), m.cpu_pct == null ? 'not tracked' : 'host', m.cpu_pct, 'var(--blue)', m.cpu_pct == null, spark()),
    metricCard('Memory Usage', pct(m.mem_pct), m.mem?.detail || 'not tracked', m.mem_pct, 'var(--purple)', m.mem_pct == null, spark()),
    metricCard('Disk Usage', pct(m.disk_pct), m.disk?.detail || 'not tracked', m.disk_pct, 'var(--amber)', m.disk_pct == null, spark()),
    metricCard('Network I/O', m.net?.value || '—', m.net?.detail || 'not tracked', null, 'var(--green)', !m.net, spark()),
  ];
  $('metricRow').innerHTML = metrics.join('');
  // draw sparklines after DOM insert
  document.querySelectorAll('#metricRow canvas').forEach((cv) => {
    const data = JSON.parse(cv.dataset.spark || '[]');
    if (data.length) sparkline(cv, data, cv.dataset.color);
  });
}
const pct = (v) => (v == null ? '—' : `${v}%`);
function metricCard(label, val, sub, barPct, color, placeholder, spark) {
  const bar = barPct != null ? `<div class="m-bar"><i style="width:${barPct}%;background:${color}"></i></div>` : '';
  const canvas = spark && placeholder ? `<canvas data-spark="${esc(JSON.stringify(spark))}" data-color="${color}"></canvas>` : '';
  return `<div class="metric ${placeholder ? 'placeholder' : ''}">
    <div class="m-lbl">${esc(label)}${placeholder ? '<span class="m-badge">static</span>' : ''}</div>
    <div class="m-val">${esc(val)}</div>
    <div class="m-sub">${esc(sub)}</div>
    ${bar}${canvas}
  </div>`;
}

/* ── Shared container actions ────────────────────────────────────────────── */
async function containerAction(svc, action) {
  toast(`${action} ${svc}…`);
  try {
    const r = await postJSON('/api/settings/container-action', { services: [svc], action });
    const res = (r.results || [])[0] || {};
    toast(res.ok ? `${svc}: ${action} OK` : `${svc}: ${action} failed — ${(res.stderr || '').slice(0, 100)}`, res.ok ? 'ok' : 'err');
    loadAll();
  } catch (e) { toast(String(e.message || e).slice(0, 140), 'err'); }
}
async function bulkAction(services, action, confirmMsg) {
  if (!services.length) return toast('No services', 'err');
  if (confirmMsg && !confirm(confirmMsg)) return;
  toast(`${action} ${services.length} containers…`);
  try {
    await postJSON('/api/settings/container-action', { services, action });
    toast(`${action} all: done`); loadAll();
  } catch (e) { toast(String(e.message || e).slice(0, 140), 'err'); }
}

/* ── utils ───────────────────────────────────────────────────────────────── */
function normStatus(s) {
  s = String(s || '').toLowerCase();
  if (s.includes('running')) return 'running';
  if (s.includes('exit') || s.includes('stop')) return 'stopped';
  return 'unknown';
}
function starRating(p) {
  const n = Math.max(0, Math.min(5, Math.round(p ?? 3)));
  return `<span class="stars">${'★'.repeat(n)}<span class="off">${'★'.repeat(5 - n)}</span></span>`;
}
