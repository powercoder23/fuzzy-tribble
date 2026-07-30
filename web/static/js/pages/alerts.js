/* ============================================================================
   pages/alerts.js — Alerts page: ranked opportunity feed + chronological
   scanner activity feed.
   ========================================================================== */
import { api, fmtTime, $, esc } from '../core/api.js';
import { mountShell } from '../core/shell.js';

const ACTIVITY_ICONS = { composite: '⬥', smart_money: '💰', oi_buildup: '📈', gap: '⚡', sonar: '📡', delivery: '📦' };

init();

async function init() {
  mountShell({ active: 'alerts', title: 'Alerts', icon: '⚑',
    subtitle: 'Ranked opportunities and the live scanner activity feed' });
  await loadAll();
}

async function loadAll() {
  const [opp, act] = await Promise.allSettled([
    api('/api/opportunities?limit=25'),
    api('/api/activity?limit=40'),
  ]);
  renderOpportunities(opp.status === 'fulfilled' ? opp.value.opportunities : []);
  renderActivity(act.status === 'fulfilled' ? act.value.events : []);
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

function renderActivity(events) {
  const el = $('activity');
  if (!events || !events.length) { el.innerHTML = `<div class="empty">No scanner activity recorded yet</div>`; return; }
  el.innerHTML = `<div class="activity-list">${events.map((e) => `
    <div class="activity-row">
      <div class="activity-ic">${ACTIVITY_ICONS[e.type] || '•'}</div>
      <div class="activity-body">
        <div class="activity-label">${esc(e.label)} <span class="dim">· ${esc(e.symbol)}</span></div>
        <div class="activity-detail">${esc(e.detail)}</div>
      </div>
      <div class="activity-time">${fmtTime(e.ts)}</div>
    </div>`).join('')}</div>`;
}
