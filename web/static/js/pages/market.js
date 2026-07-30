/* ============================================================================
   pages/market.js — Market page: India VIX / NIFTY / BANKNIFTY snapshot +
   sector trend breadth table.
   ========================================================================== */
import { api, fmtNum, $, esc } from '../core/api.js';
import { mountShell } from '../core/shell.js';

init();

async function init() {
  mountShell({ active: 'market', title: 'Market Overview', icon: '◉',
    subtitle: 'Index snapshot, VIX, and sector breadth' });
  await loadAll();
}

async function loadAll() {
  const [snap, sector] = await Promise.allSettled([
    api('/api/market-snapshot'),
    api('/api/sector-trend'),
  ]);
  renderSnapshot(snap.status === 'fulfilled' ? snap.value : null);
  renderSectorTrend(sector.status === 'fulfilled' ? sector.value : null);
}

function renderSnapshot(d) {
  const el = $('snapshot');
  if (!d) { el.innerHTML = `<div class="empty">Could not load market snapshot</div>`; return; }
  const blank = (label) => `<span class="snap-blank">${esc(label)}</span>`;
  const vix = d.india_vix, nf = d.nifty, bn = d.banknifty, br = d.breadth;
  el.innerHTML = `<div class="snap-grid">
    <div class="snap-cell"><div class="snap-label">NIFTY 50</div>
      ${nf ? `<div class="snap-value">₹${fmtNum(nf.spot, 2)}</div><div class="snap-sub dim">PCR ${nf.pcr ?? '—'}</div>` : blank('Not tracked in this scan universe')}</div>
    <div class="snap-cell"><div class="snap-label">BANKNIFTY</div>
      ${bn ? `<div class="snap-value">₹${fmtNum(bn.spot, 2)}</div><div class="snap-sub dim">PCR ${bn.pcr ?? '—'}</div>` : blank('Not tracked in this scan universe')}</div>
    <div class="snap-cell"><div class="snap-label">India VIX</div>
      ${vix ? `<div class="snap-value">${fmtNum(vix.value, 2)}</div><div class="snap-sub" style="color:${vix.pct_change >= 0 ? 'var(--red)' : 'var(--green)'}">${vix.pct_change >= 0 ? '+' : ''}${fmtNum(vix.pct_change, 2)}%</div>` : blank('No VIX data yet')}</div>
    <div class="snap-cell"><div class="snap-label">Market Breadth</div>
      ${br ? `<div class="snap-value">${br.advancers}↑ / ${br.decliners}↓</div><div class="snap-sub dim">${br.market_pct?.toFixed(0) ?? '—'}% F&amp;O universe</div>` : blank('Not enough intraday names yet')}</div>
    <div class="snap-cell"><div class="snap-label">FII (Cash)</div>${blank('Not collected in this system')}</div>
    <div class="snap-cell"><div class="snap-label">DII (Cash)</div>${blank('Not collected in this system')}</div>
  </div>`;
}

function renderSectorTrend(d) {
  const el = $('sectorTrend');
  if (!d || d.market_pct === null || d.market_pct === undefined) {
    el.innerHTML = `<div class="empty">No intraday breadth yet — appears once the IV collector has two spot snapshots (≈09:30).</div>`;
    return;
  }
  const m = d.market_pct;
  const mCls = m >= 55 ? 'pos' : m <= 45 ? 'neg' : '';
  const header = `<div class="total-bar">
    <span class="tb-label">Market breadth${d.day ? ' · ' + d.day : ''}</span>
    <span class="tb-value ${mCls}">${m.toFixed(0)}% adv <span class="dim">(${d.adv}↑ / ${d.dec}↓)</span></span>
  </div>`;
  if (!d.sectors || !d.sectors.length) { el.innerHTML = header + `<div class="empty">No sector map / not enough names per sector yet.</div>`; return; }
  const rows = d.sectors.map((s) => {
    const avg = s.avg == null ? 0 : s.avg;
    const dot = avg > 0.15 ? '🟢' : avg < -0.15 ? '🔴' : '⚪';
    const pCls = avg >= 0 ? 'pos' : 'neg';
    return `<tr>
      <td class="name">${dot} ${esc(s.sector)}</td>
      <td class="${pCls}">${avg >= 0 ? '+' : ''}${avg.toFixed(2)}%</td>
      <td class="num">${s.pct == null ? '—' : s.pct.toFixed(0) + '%'}</td>
      <td class="num">${s.adv}↑ / ${s.dec}↓</td>
      <td class="num">${s.n}</td>
    </tr>`;
  }).join('');
  el.innerHTML = header + `<table class="data stack"><thead><tr><th>Sector</th><th>Avg move</th><th>Breadth</th><th>Adv/Dec</th><th>Names</th></tr></thead><tbody>${rows}</tbody></table>`;
}
