/* ============================================================================
   pages/backtest.js — Backtest page. Pick a strategy + duration, run a
   simulated backtest (Black-Scholes premium reconstruction from historical
   spot + IV — see the card-sub disclaimer in backtest.html), watch progress,
   explore P&L / equity curve / trade log, reopen past runs.
   ========================================================================== */
import { api, postJSON, fmtPnl, fmtNum, fmtTime, esc, toast, $ } from '../core/api.js';
import { mountShell } from '../core/shell.js';
import { equityCurveChart } from '../core/charts.js';

let pollTimer = null;
let currentRunId = null;

init();

async function init() {
  mountShell({ active: 'backtest', title: 'Backtest', icon: '⟲',
    subtitle: 'Run a strategy against history and see how it would have traded' });
  await loadStrategies();
  await loadRuns();
  $('btnRun').addEventListener('click', startRun);
}

async function loadStrategies() {
  try {
    const data = await api('/api/backtest/strategies');
    $('fStrategy').innerHTML = (data.strategies || [])
      .map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join('');
  } catch (e) {
    $('fStrategy').innerHTML = '<option value="">(failed to load strategies)</option>';
    toast(`Could not load strategies: ${e.message}`, 'err');
  }
}

function dateRangeFromDuration(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - Number(days));
  const iso = (d) => d.toISOString().slice(0, 10);
  return { start_date: iso(start), end_date: iso(end) };
}

async function startRun() {
  const strategy = $('fStrategy').value;
  if (!strategy) { toast('Pick a strategy first', 'err'); return; }
  const { start_date, end_date } = dateRangeFromDuration($('fDuration').value);
  const symbolsRaw = $('fSymbols').value.trim();
  const symbols = symbolsRaw
    ? symbolsRaw.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
    : null;

  $('btnRun').disabled = true;
  $('btnRun').textContent = 'Starting…';
  try {
    const res = await postJSON('/api/backtest/run', { strategy, symbols, start_date, end_date });
    currentRunId = res.run_id;
    showProgress(0, 'queued');
    pollRun(currentRunId);
    toast(`Backtest #${res.run_id} started`, 'ok');
  } catch (e) {
    toast(`Could not start backtest: ${e.message}`, 'err');
  } finally {
    $('btnRun').disabled = false;
    $('btnRun').textContent = 'Run Backtest';
  }
}

function showProgress(pct, status) {
  $('progressWrap').style.display = 'flex';
  const fill = $('progressFill');
  fill.style.width = `${pct}%`;
  fill.className = `bt-progress-fill ${status === 'error' ? 'error' : status === 'done' ? 'done' : ''}`;
  $('progressLabel').textContent =
    status === 'error' ? 'Failed — see toast' :
    status === 'done'  ? 'Done' :
    `${status === 'queued' ? 'Queued' : 'Running'}… ${pct}%`;
}

function pollRun(runId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const run = await api(`/api/backtest/runs/${runId}`);
      showProgress(run.progress_pct || 0, run.status);
      if (run.status === 'done') {
        clearInterval(pollTimer);
        await renderResult(run);
        await loadRuns();
      } else if (run.status === 'error') {
        clearInterval(pollTimer);
        toast(`Backtest #${runId} failed: ${(run.error || '').slice(0, 160)}`, 'err');
        await loadRuns();
      }
    } catch (e) {
      clearInterval(pollTimer);
      toast(`Lost track of backtest #${runId}: ${e.message}`, 'err');
    }
  }, 3000);
}

async function loadRuns() {
  try {
    const data = await api('/api/backtest/runs?limit=25');
    renderRuns(data.runs || []);
  } catch (e) {
    $('runsList').innerHTML = `<div class="empty">Could not load past runs: ${esc(e.message)}</div>`;
  }
}

function statusTag(status) {
  const cls = status === 'done' ? 'win' : status === 'error' ? 'loss' : 'open';
  return `<span class="tag ${cls}">${status.toUpperCase()}</span>`;
}

function renderRuns(runs) {
  if (!runs.length) {
    $('runsList').innerHTML = '<div class="empty">No backtests run yet — configure one above and hit Run.</div>';
    return;
  }
  $('runsList').innerHTML = runs.map((r) => {
    const s = r.summary;
    const pnl = s ? fmtPnl(s.net_rupees) : '—';
    return `<div class="bt-run-row ${r.id === currentRunId ? 'active' : ''}" data-run="${r.id}">
      <div class="bt-run-meta">
        <div class="bt-run-title">#${r.id} · ${esc(r.strategy)} ${statusTag(r.status)}</div>
        <div class="bt-run-sub">${esc(r.start_date)} → ${esc(r.end_date)} · ${esc((r.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      </div>
      <div class="bt-run-sub" style="text-align:right">
        ${s ? `<span class="${s.net_rupees >= 0 ? 'pos' : 'neg'}">${pnl}</span> · ${s.trades} trades` : (r.status === 'error' ? esc((r.error || '').slice(0, 60)) : '—')}
      </div>
    </div>`;
  }).join('');
  $('runsList').querySelectorAll('.bt-run-row').forEach((row) => {
    row.addEventListener('click', () => openRun(Number(row.dataset.run)));
  });
}

async function openRun(runId) {
  currentRunId = runId;
  try {
    const run = await api(`/api/backtest/runs/${runId}`);
    if (run.status === 'running' || run.status === 'queued') {
      showProgress(run.progress_pct || 0, run.status);
      pollRun(runId);
      toast(`Backtest #${runId} is still ${run.status}`, 'ok');
      return;
    }
    if (run.status === 'error') {
      toast(`Backtest #${runId} failed: ${(run.error || '').slice(0, 160)}`, 'err');
      return;
    }
    await renderResult(run);
  } catch (e) {
    toast(`Could not open run #${runId}: ${e.message}`, 'err');
  }
}

async function renderResult(run) {
  $('progressWrap').style.display = 'none';
  renderStatRow(run.summary);
  renderEquity(run.summary, run);
  try {
    const data = await api(`/api/backtest/runs/${run.id}/trades`);
    renderTrades(data.trades || [], run.summary);
  } catch (e) {
    $('tradesCard').style.display = 'block';
    $('tradesCard').innerHTML = `<div class="empty">Could not load trades: ${esc(e.message)}</div>`;
  }
}

function signalCoverageNote(s) {
  if (!s || !s.signals_found) return '';
  const skipped = s.signals_found - (s.signals_priced || 0);
  if (skipped <= 0) return '';
  const reasons = Object.entries(s.skip_reasons || {})
    .map(([reason, n]) => `${n}× ${reason.replace(/_/g, ' ')}`).join(', ');
  return `<div class="empty" style="text-align:left;padding:var(--s3) 0 0">
    ${s.signals_found} signal(s) found, ${s.signals_priced || 0} priced into trades.
    ${skipped} skipped — could not be priced (no historical option contract or IV close enough to the entry date): ${esc(reasons)}.
  </div>`;
}

function renderStatRow(s) {
  s = s || {};
  $('statRow').style.display = 'grid';
  const cards = [
    ['📊', 'Trades', s.trades ?? '—'],
    ['🎯', 'Win Rate', s.win_rate != null ? `${s.win_rate}%` : '—', s.win_rate >= 50 ? 'pos' : s.win_rate != null ? 'neg' : ''],
    ['💰', 'Net P&L', fmtPnl(s.net_rupees), s.net_rupees >= 0 ? 'pos' : 'neg'],
    ['📈', 'Expectancy', fmtPnl(s.expectancy), s.expectancy >= 0 ? 'pos' : 'neg'],
    ['⚖️', 'Profit Factor', s.profit_factor != null ? fmtNum(s.profit_factor) : '—'],
    ['📉', 'Max Drawdown', fmtPnl(s.max_drawdown), 'neg'],
  ];
  $('statRow').innerHTML = cards.map(([ic, label, val, cls]) => `
    <div class="stat-card">
      <div class="sc-head"><span class="sc-ic">${ic}</span>${label}</div>
      <div class="sc-val ${cls || ''}">${val}</div>
    </div>`).join('');
}

function renderEquity(s, run) {
  $('equityCard').style.display = 'block';
  $('equitySub').textContent = `${run.strategy} · ${run.start_date} → ${run.end_date} · ${(run.symbols || []).length} symbol(s)`;
  equityCurveChart($('equityChart'), (s && s.equity_curve) || []);
}

function renderTrades(trades, summary) {
  $('tradesCard').style.display = 'block';
  if (!trades.length) {
    $('tradesCard').innerHTML = `<div class="empty">No trades were generated for this run.</div>${signalCoverageNote(summary)}`;
    return;
  }
  const rows = trades.map((t) => `
    <tr>
      <td data-label="Symbol" class="name">${esc(t.symbol)}</td>
      <td data-label="Side"><span class="tag ${t.side === 'CE' ? 'buy' : 'sell'}">${esc(t.side)}</span></td>
      <td data-label="Strike">${t.strike ?? ''}</td>
      <td data-label="Trigger">${esc(t.trigger || '')}</td>
      <td data-label="Entry">${fmtTime(t.entry_ts)} @ ₹${fmtNum(t.entry_premium)}</td>
      <td data-label="Exit">${fmtTime(t.exit_ts)} @ ₹${fmtNum(t.exit_premium)}</td>
      <td data-label="Reason">${esc(t.exit_reason || '')}</td>
      <td data-label="P&amp;L"><span class="${t.pnl_rupees >= 0 ? 'pos' : 'neg'}">${fmtPnl(t.pnl_rupees)} (${fmtNum(t.pnl_pct, 1)}%)</span></td>
    </tr>`).join('');
  $('tradesCard').innerHTML = `<div class="card-head"><div><div class="card-title">Trade Log</div><div class="card-sub">${trades.length} simulated trade(s)</div></div></div>
    <div class="table-wrap"><table class="data stack">
      <thead><tr><th>Symbol</th><th>Side</th><th>Strike</th><th>Trigger</th><th>Entry</th><th>Exit</th><th>Reason</th><th>P&amp;L</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>${signalCoverageNote(summary)}`;
}
