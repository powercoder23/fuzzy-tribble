/* ============================================================================
   pages/placeholder.js — shared "not wired up yet" page for nav items with
   no real data source yet (Signals, Reports, Logs, System Health, Backtest,
   Data Explorer). Same shell/theme as every real page so nothing 404s and
   nothing looks like a dead end — just an honest "coming later" card,
   matching the pattern settings.js already uses for its own unfinished tabs.
   ========================================================================== */
import { $ } from '../core/api.js';
import { mountShell } from '../core/shell.js';

const META = {
  signals:         { title: 'Signals',       icon: '⚡', sub: 'Live entry/exit signal feed across every strategy' },
  reports:         { title: 'Reports',       icon: '▧', sub: 'Exportable strategy performance reports' },
  logs:            { title: 'Logs',          icon: '☰', sub: 'Container and application logs' },
  'system-health': { title: 'System Health', icon: '♥', sub: 'Infrastructure and data-pipeline health' },
  backtest:        { title: 'Backtest',      icon: '⟲', sub: 'Historical strategy backtesting' },
  'data-explorer': { title: 'Data Explorer', icon: '⛃', sub: 'Browse raw collector data' },
};

const key = location.pathname.replace(/^\//, '') || 'signals';
const meta = META[key] || { title: 'Coming Soon', icon: '⚙', sub: '' };

mountShell({ active: key, title: meta.title, icon: meta.icon, subtitle: meta.sub });
$('page').innerHTML = `<div class="card">
  <div class="card-title">${meta.title}</div>
  <div class="empty">This section isn't wired up yet — coming in a later phase.</div>
</div>`;
