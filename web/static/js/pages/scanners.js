/* ============================================================================
   pages/scanners.js — Scanner Hub: per-scanner health/status grid + the
   fused composite conviction signal feed underneath. Container start/stop
   controls live in Settings > Scanners — this page is read-only status +
   the live trading signal each scanner ultimately feeds.
   ========================================================================== */
import { api, fmtTime, $, esc, toast } from '../core/api.js';
import { mountShell } from '../core/shell.js';

init();

async function init() {
  mountShell({ active: 'scanners', title: 'Scanner Hub', icon: '◎',
    subtitle: 'Health of every scanner and the live fused signal feed' });
  $('refresh').addEventListener('click', loadAll);
  await loadAll();
}

async function loadAll() {
  const [sc, opp] = await Promise.allSettled([
    api('/api/scanners'),
    api('/api/opportunities?limit=25'),
  ]);
  renderScanners(sc.status === 'fulfilled' ? sc.value.scanners : []);
  renderOpportunities(opp.status === 'fulfilled' ? opp.value.opportunities : []);
  if (sc.status !== 'fulfilled') toast('Could not load scanner status', 'err');
}

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

function renderScanners(scanners) {
  const el = $('scannerGrid');
  if (!scanners || !scanners.length) { el.innerHTML = `<div class="empty">No scanners found (compose not mounted?)</div>`; return; }
  el.innerHTML = scanners.map((s) => {
    const st = normStatus(s.status);
    return `<div class="stat-card">
      <div class="sc-head"><span class="status-dot ${st}"></span>${esc(s.label || s.name)}</div>
      <div class="sc-val" style="font-size:14px">${starRating(s.priority)}</div>
      <div class="sc-sub">${esc(s.sub || s.name)} · <span class="tag ${st}">${st.toUpperCase()}</span></div>
      <div class="sc-sub" style="margin-top:4px">Last signal: ${s.last_signal ? fmtTime(s.last_signal) : '—'} · ${s.signals_today == null ? '—' : s.signals_today} today</div>
    </div>`;
  }).join('');
}

function renderOpportunities(list) {
  const el = $('opportunities');
  if (!list || !list.length) { el.innerHTML = `<div class="empty">No Composite Conviction data yet — populated by the composite scanner (runs 20:15 / 22:45).</div>`; return; }
  const rows = list.map((o) => {
    const dirTag = o.direction === 'CE' ? 'BUY CALL' : o.direction === 'PE' ? 'BUY PUT' : (o.direction || '—');
    const dirCls = o.direction === 'CE' ? 'ce' : o.direction === 'PE' ? 'pe' : 'unknown';
    const badges = [];
    if (o.grade === 'STRONG' || (o.score || 0) >= 70) badges.push('<span class="tag win">COMPOSITE</span>');
    if (o.smart_money_bias) badges.push('<span class="tag running">SMART MONEY</span>');
    if (o.block_deal) badges.push('<span class="tag ce">BLOCK DEAL</span>');
    if (o.delivery_bias) badges.push('<span class="tag unknown">DELIVERY</span>');
    return `<tr>
      <td class="name">${esc(o.symbol)}</td>
      <td class="num">${o.score ?? '—'}</td>
      <td><span class="tag ${dirCls}">${dirTag}</span></td>
      <td class="num">${o.iv_rank ?? '—'} <span class="dim">/ ${o.iv_percentile ?? '—'}</span></td>
      <td class="num">${o.pcr ?? '—'}</td>
      <td>${badges.join(' ') || '<span class="tag unknown">—</span>'}</td>
      <td class="dim num">${fmtTime(o.composite_as_of)}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="data stack">
    <thead><tr><th>Symbol</th><th>Score</th><th>Setup</th><th>IVR / IVP</th><th>PCR</th><th>Signals</th><th>As of</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}
