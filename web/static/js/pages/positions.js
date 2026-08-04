/* ============================================================================
   pages/positions.js — Paper Trades / Positions page.
   Migrated from the legacy dashboard.html trades tab: combo-spread cards
   (combined P&L, margin required/saved, SL<->target progress bar) sourced
   live from /api/hedge-config so the UI can never drift from what
   paper_trader.apply_combo_tick actually triggers on. See
   [[spread-exit-and-margin-design]] memory for the full design rationale.
   ========================================================================== */
import { api, fmtPnl, fmtExpiry, ceOrPe, contractName, fmtTime, tradePnl, $, esc, toast } from '../core/api.js';
import { mountShell } from '../core/shell.js';
import { pnlHistoryChart } from '../core/charts.js';

let hedgeCfg = { spread_sl_pct: 0.40, spread_target_capture_pct: 0.55, naked_span_pct_of_notional: 0.15 };
let pnlChart = null;

init();

async function init() {
  mountShell({ active: 'positions', title: 'Positions', icon: '▤',
    subtitle: 'Open and closed paper trades across every strategy' });
  try { hedgeCfg = await api('/api/hedge-config'); } catch (e) {}
  await loadAll();
}

async function loadAll() {
  try {
    // /api/paper-trades with NO date/days param defaults to TODAY server-side
    // (see dashboard_app.py's paper_trades()) — this is a Positions page, it
    // must show today's live book by default, same as dashboard_old's
    // Overview "Recent Paper Trades" widget (loadOvTrades). The previous
    // `?days=365` call pulled the entire trailing-year book (hundreds of
    // trades, including ones from days ago still stuck open) and rendered it
    // as if it were "today" — the trades table and the Overall P&L bar were
    // both effectively meaningless as a "current positions" view.
    const [today, hist] = await Promise.all([
      api('/api/paper-trades'),
      api('/api/paper-trades/history?days=30'),
    ]);
    renderStatRow(hist.summary);
    renderTrades(today.trades || [], "Today's");
    renderPnlChart(hist.daily);
  } catch (e) {
    $('tradesCard').innerHTML = `<div class="empty">Could not load trades: ${esc(e.message)}</div>`;
  }
}

function renderStatRow(s) {
  s = s || {};
  const cards = [
    ['📊', 'Total Trades', s.total_trades ?? '—', 'last 30 days'],
    ['🎯', 'Win Rate', s.win_rate != null ? `${s.win_rate}%` : '—', '', s.win_rate >= 50 ? 'pos' : s.win_rate != null ? 'neg' : ''],
    ['💰', 'Net P&L', fmtPnl(s.net_rupees), '', s.net_rupees >= 0 ? 'pos' : 'neg'],
    ['📈', 'Expectancy', fmtPnl(s.expectancy), 'per trade', s.expectancy >= 0 ? 'pos' : 'neg'],
  ];
  $('statRow').innerHTML = cards.map(([ic, label, val, sub, cls]) => `
    <div class="stat-card">
      <div class="sc-head"><span class="sc-ic">${ic}</span>${label}</div>
      <div class="sc-val ${cls || ''}">${val}</div>
      <div class="sc-sub">${sub || ''}</div>
    </div>`).join('');
}

function renderPnlChart(daily) {
  pnlChart = pnlHistoryChart($('pnlChart'), daily, (row) => showDayTrades(row.date));
}

async function showDayTrades(date) {
  try {
    const data = await api(`/api/paper-trades?date=${encodeURIComponent(date)}`);
    toast(`${date}: ${data.count || 0} trade(s) — see table below`, 'ok');
    renderTrades(data.trades || [], date);
  } catch (e) { toast(String(e.message || e).slice(0, 140), 'err'); }
}

/* ── Trade formatting ────────────────────────────────────────────────────── */
function legSideTag(t) {
  const short = t.direction === 'short';
  return `<span class="tag ${short ? 'sell' : 'buy'}">${short ? 'SELL' : 'BUY'}</span>`;
}
function fmtStrike(v) {
  if (v === null || v === undefined || v === '') return '';
  return String(Number(v));
}

function renderTrades(trades, label = "Today's") {
  const openT = (trades || []).filter((t) => t.status === 'open');
  const closedT = (trades || []).filter((t) => t.status !== 'open');
  let overall = 0, haveAny = false;
  (trades || []).forEach((t) => {
    const { rupees } = tradePnl(t);
    if (rupees !== null && rupees !== undefined) { overall += rupees; haveAny = true; }
  });
  const overallBar = `<div class="total-bar">
    <span class="tb-label">${esc(label)} P&amp;L <span class="dim">(${(trades || []).length} position${(trades || []).length === 1 ? '' : 's'})</span></span>
    <span class="tb-value ${overall >= 0 ? 'pos' : 'neg'}">${haveAny ? fmtPnl(overall) : '—'}</span>
  </div>`;
  const block = (label, arr) => arr.length
    ? `<div class="split-label">${label} (${arr.length})</div>${tradesTable(arr)}`
    : `<div class="split-label">${label} (0)</div><div class="empty">No trades in this view</div>`;
  $('tradesCard').innerHTML = overallBar + block('Open positions', openT) + block('Closed positions', closedT);
}

function tradesTable(trades) {
  const multiDay = new Set(trades.map((t) => t.date).filter(Boolean)).size > 1;
  const colCount = (multiDay ? 1 : 0) + 11;

  const legRow = (t) => {
    const { rupees, pct, live } = tradePnl(t);
    const status = t.status === 'open' ? 'open' : ((t.realized_rupees || 0) >= 0 ? 'win' : 'loss');
    const pnlCell = (rupees !== null && rupees !== undefined)
      ? `<span class="${rupees >= 0 ? 'pos' : 'neg'}">${fmtPnl(rupees)} (${pct > 0 ? '+' : ''}${(pct || 0).toFixed(1)}%)${live ? '<span class="pnl-live"> ●</span>' : ''}</span>`
      : '—';
    const exitPx = (t.status !== 'open' && t.last_price != null) ? `₹${t.last_price.toFixed(2)}` : '—';
    const tgt = (t.t1 != null && +t.t1 > 0) ? `₹${(+t.t1).toFixed(2)}` : '—';
    const cname = contractName(t);
    const strategyLabel = t.combo_id ? (t.strategy || '—').replace(/\s*\[hedge\]\s*$/i, '') : (t.strategy || '—');
    return `<tr class="${t.combo_id ? 'combo-leg' : ''}">
      ${multiDay ? `<td data-label="Date">${(t.date || '').slice(5)}</td>` : ''}
      <td data-label="Side">${legSideTag(t)}</td>
      <td data-label="Contract" class="name">${esc(cname)}</td>
      <td data-label="Strategy">${esc(strategyLabel)}</td>
      <td data-label="Entry">₹${(t.entry || 0).toFixed(2)}</td>
      <td data-label="LTP">₹${(t.last_price || 0).toFixed(2)}</td>
      <td data-label="Target">${tgt}</td>
      <td data-label="Exit">${exitPx}</td>
      <td data-label="In">${fmtTime(t.opened_at)}</td>
      <td data-label="Out">${fmtTime(t.closed_at)}</td>
      <td data-label="P&amp;L">${pnlCell}</td>
      <td data-label="Status"><span class="tag ${status}">${status.toUpperCase()}</span></td>
    </tr>`;
  };

  const comboHeaderRow = (legs) => {
    const long = legs.find((t) => t.direction !== 'short') || legs[0];
    const short = legs.find((t) => t.direction === 'short');
    const lot = long.lot_size || 1;
    let combined = 0, haveCombined = false;
    legs.forEach((t) => {
      const { rupees } = tradePnl(t);
      if (rupees !== null && rupees !== undefined) { combined += rupees; haveCombined = true; }
    });
    const pnlChip = `<span class="combo-chip ${combined >= 0 ? 'pos' : 'neg'}">Combined P&amp;L ${haveCombined ? fmtPnl(combined) : '—'}</span>`;

    let statChips = '', progressBar = '';
    if (long && short) {
      const spot = long.spot || short.spot || null;
      const nakedMargin = spot ? (spot * lot * hedgeCfg.naked_span_pct_of_notional + (short.entry || 0) * lot) : null;
      const entryDebit = (long.entry || 0) - (short.entry || 0);
      const spreadMargin = Math.max(entryDebit, 0) * lot;
      const benefit = nakedMargin != null ? Math.max(nakedMargin - spreadMargin, 0) : null;

      const strikeWidth = Math.abs((long.strike || 0) - (short.strike || 0));
      const maxProfitPotential = Math.max(strikeWidth - entryDebit, 0);
      const slLevel = entryDebit * (1 - hedgeCfg.spread_sl_pct);
      const targetLevel = entryDebit + maxProfitPotential * hedgeCfg.spread_target_capture_pct;
      const maxProfitRupees = maxProfitPotential * lot;

      statChips =
        `<span class="combo-chip">Net debit (cost) ₹${Math.round(entryDebit * lot).toLocaleString('en-IN')}</span>` +
        `<span class="combo-chip">Max profit ₹${Math.round(maxProfitRupees).toLocaleString('en-IN')}</span>` +
        `<span class="combo-chip">Margin required ₹${Math.round(spreadMargin).toLocaleString('en-IN')}</span>` +
        (benefit != null
          ? `<span class="combo-chip highlight" title="Naked short would need ~₹${Math.round(nakedMargin).toLocaleString('en-IN')} margin (rough SPAN estimate, ~${(hedgeCfg.naked_span_pct_of_notional * 100).toFixed(0)}% of notional) — hedged spread only needs the net debit">Margin saved ₹${Math.round(benefit).toLocaleString('en-IN')}</span>`
          : `<span class="combo-chip na">Margin saved —</span>`);

      if (long.status === 'open' && short.status === 'open') {
        const nowValue = (long.last_price || 0) - (short.last_price || 0);
        const span = (targetLevel - slLevel) || 1;
        const pctOf = (v) => Math.min(100, Math.max(0, (v - slLevel) / span * 100));
        progressBar = `
          <div class="combo-progress">
            <div class="combo-progress-track">
              <div class="combo-progress-entry-mark" style="left:${pctOf(entryDebit)}%"></div>
              <div class="combo-progress-now ${nowValue >= entryDebit ? 'pos' : 'neg'}" style="left:${pctOf(nowValue)}%"></div>
            </div>
            <div class="combo-progress-labels">
              <span>SL ₹${Math.round(slLevel * lot).toLocaleString('en-IN')}</span>
              <span>Entry ₹${Math.round(entryDebit * lot).toLocaleString('en-IN')}</span>
              <span>Now ₹${Math.round(nowValue * lot).toLocaleString('en-IN')}</span>
              <span>Target ₹${Math.round(targetLevel * lot).toLocaleString('en-IN')}</span>
            </div>
          </div>`;
      }
    }

    const strikes = short ? `${fmtStrike(long.strike)}/${fmtStrike(short.strike)}` : fmtStrike(long.strike);
    const label = `${long.symbol || ''} ${fmtExpiry(long.expiry)} ${ceOrPe(long.side)} ${strikes} spread`;
    return `<tr class="combo-header"><td colspan="${colCount}">
      <div class="combo-card">
        <div class="combo-card-top"><span class="combo-label">🔗 ${esc(label)}</span>${pnlChip}</div>
        <div class="combo-stats">${statChips}</div>
        ${progressBar}
      </div>
    </td></tr>`;
  };

  const rawGroups = new Map();
  trades.forEach((t) => { if (t.combo_id) { if (!rawGroups.has(t.combo_id)) rawGroups.set(t.combo_id, []); rawGroups.get(t.combo_id).push(t); } });
  const comboOrder = [], comboMap = new Map(), singles = [];
  trades.forEach((t) => {
    const group = t.combo_id ? rawGroups.get(t.combo_id) : null;
    const isRealPair = group && group.length === 2 && group.some((g) => g.direction === 'short') && group.some((g) => g.direction !== 'short');
    if (isRealPair) {
      if (!comboMap.has(t.combo_id)) { comboMap.set(t.combo_id, []); comboOrder.push(t.combo_id); }
      comboMap.get(t.combo_id).push(t);
    } else singles.push(t);
  });

  const rows = [];
  singles.forEach((t) => rows.push(legRow(t)));
  comboOrder.forEach((id) => {
    const legs = comboMap.get(id);
    rows.push(comboHeaderRow(legs));
    legs.forEach((t) => rows.push(legRow(t)));
  });

  return `<div class="table-wrap"><table class="data stack">
    <thead><tr>${multiDay ? '<th>Date</th>' : ''}<th>Side</th><th>Contract</th><th>Strategy</th><th>Entry</th><th>LTP</th><th>Target</th><th>Exit</th><th>In</th><th>Out</th><th>P&amp;L</th><th>Status</th></tr></thead>
    <tbody>${rows.join('')}</tbody>
  </table></div>`;
}
