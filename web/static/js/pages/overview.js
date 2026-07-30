/* ============================================================================
   pages/overview.js — Overview page. High-level KPIs + today's opportunities
   + market snapshot + strategy performance + activity feed.
   ========================================================================== */
import { api, fmtPnl, fmtNum, fmtTime, $, esc } from '../core/api.js';
import { mountShell } from '../core/shell.js';
import { pnlHistoryChart } from '../core/charts.js';

const ACTIVITY_ICONS = { composite: '⬥', smart_money: '💰', oi_buildup: '📈', gap: '⚡', sonar: '📡', delivery: '📦' };

init();

async function init() {
  mountShell({ active: 'overview', title: 'Overview', icon: '⌂',
    subtitle: 'System-wide P&L, opportunities, and activity at a glance' });
  await loadAll();
}

async function loadAll() {
  const [ov, opp, snap, strat, hist, act] = await Promise.allSettled([
    api('/api/overview?days=30'),
    api('/api/opportunities?limit=12'),
    api('/api/market-snapshot'),
    api('/api/strategy-performance?days=30'),
    api('/api/paper-trades/history?days=30'),
    api('/api/activity?limit=20'),
  ]);
  renderStatRow(ov.status === 'fulfilled' ? ov.value : null);
  renderOpportunities(opp.status === 'fulfilled' ? opp.value.opportunities : []);
  renderSnapshot(snap.status === 'fulfilled' ? snap.value : null);
  renderStrategyPerf(strat.status === 'fulfilled' ? strat.value.strategies : []);
  if (hist.status === 'fulfilled') pnlHistoryChart($('pnlChart'), hist.value.daily);
  renderActivity(act.status === 'fulfilled' ? act.value.events : []);
}

function renderStatRow(o) {
  if (!o) { $('statRow').innerHTML = `<div class="empty">Could not load overview</div>`; return; }
  const pf = o.profit_factor;
  const cards = [
    ['💰', 'Net P&L (Paper)', fmtPnl(o.net_rupees), '', o.net_rupees >= 0 ? 'pos' : 'neg'],
    ['🎯', 'Win Rate', `${o.win_rate}%`, `${o.closed_trades} closed`, o.win_rate >= 50 ? 'pos' : 'neg'],
    ['📊', 'Total Trades', o.total_trades, `${o.closed_trades} closed · ${o.open_trades} open`],
    ['📈', 'Expectancy', fmtPnl(o.expectancy), 'per trade', o.expectancy >= 0 ? 'pos' : 'neg'],
    ['⚖', 'Profit Factor', pf == null ? '—' : pf.toFixed(2), pf == null ? 'no losses yet' : (pf >= 1.5 ? 'Good' : pf >= 1 ? 'Marginal' : 'Weak'), pf == null ? '' : (pf >= 1.5 ? 'pos' : pf >= 1 ? 'warn' : 'neg')],
    ['🏆', 'Best Strategy', o.best_strategy ? o.best_strategy.name : '—', o.best_strategy ? `Win Rate: ${o.best_strategy.win_rate}%` : 'no closed trades yet'],
  ];
  $('statRow').innerHTML = cards.map(([ic, label, val, sub, cls]) => `
    <div class="stat-card">
      <div class="sc-head"><span class="sc-ic">${ic}</span>${esc(label)}</div>
      <div class="sc-val ${cls || ''}">${esc(String(val))}</div>
      <div class="sc-sub">${esc(sub || '')}</div>
    </div>`).join('');
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

function renderSnapshot(d) {
  const el = $('snapshot');
  if (!d) { el.innerHTML = `<div class="empty">Could not load market snapshot</div>`; return; }
  const blank = (label) => `<span class="snap-blank">${esc(label)}</span>`;
  const vix = d.india_vix, nf = d.nifty, bn = d.banknifty, br = d.breadth;
  el.innerHTML = `<div class="snap-grid">
    <div class="snap-cell"><div class="snap-label">NIFTY 50</div>
      ${nf ? `<div class="snap-value">₹${fmtNum(nf.spot, 2)}</div><div class="snap-sub dim">PCR ${nf.pcr ?? '—'}</div>` : blank('Not tracked')}</div>
    <div class="snap-cell"><div class="snap-label">BANKNIFTY</div>
      ${bn ? `<div class="snap-value">₹${fmtNum(bn.spot, 2)}</div><div class="snap-sub dim">PCR ${bn.pcr ?? '—'}</div>` : blank('Not tracked')}</div>
    <div class="snap-cell"><div class="snap-label">India VIX</div>
      ${vix ? `<div class="snap-value">${fmtNum(vix.value, 2)}</div><div class="snap-sub" style="color:${vix.pct_change >= 0 ? 'var(--red)' : 'var(--green)'}">${vix.pct_change >= 0 ? '+' : ''}${fmtNum(vix.pct_change, 2)}%</div>` : blank('No VIX data yet')}</div>
    <div class="snap-cell"><div class="snap-label">Market Breadth</div>
      ${br ? `<div class="snap-value">${br.advancers}↑ / ${br.decliners}↓</div><div class="snap-sub dim">${br.market_pct?.toFixed(0) ?? '—'}% F&amp;O universe</div>` : blank('Not enough intraday names yet')}</div>
  </div>`;
}

function renderStrategyPerf(list) {
  const el = $('strategyPerf');
  if (!list || !list.length) { el.innerHTML = `<div class="empty">No closed trades in this window</div>`; return; }
  const rows = list.map((s) => `<tr>
    <td class="name">${esc(s.strategy)}</td>
    <td class="${s.net_rupees >= 0 ? 'pos' : 'neg'}">${fmtPnl(s.net_rupees)}</td>
    <td class="num">${s.win_rate}%</td>
    <td class="num">${s.profit_factor ?? '—'}</td>
    <td class="num">${s.trades}</td>
  </tr>`).join('');
  el.innerHTML = `<table class="data"><thead><tr><th>Strategy</th><th>Net P&amp;L</th><th>Win Rate</th><th>Profit Factor</th><th>Trades</th></tr></thead><tbody>${rows}</tbody></table>`;
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
