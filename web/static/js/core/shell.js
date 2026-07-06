/* ============================================================================
   core/shell.js — renders sidebar + topbar + mobile drawer/bottom-nav.
   Call mountShell({ active, title, subtitle, icon }) once per page.
   ========================================================================== */
import { $, el } from './api.js';

const BRAND = { name: 'Fuzzy Tribble', sub: 'Trading System', logo: 'FT' };
const USER  = { name: 'Dhiraj', role: 'Administrator', initials: 'DH' };

// key, label, icon, href
const NAV = [
  ['overview',     'Overview',        '⌂', '/overview'],
  ['dashboard',    'Dashboard',       '▦', '/dashboard'],
  ['signals',      'Signals',         '⚡', '/signals'],
  ['positions',    'Positions',       '▤', '/positions'],
  ['scanners',     'Scanner Hub',     '◎', '/scanners'],
  ['market',       'Market Overview', '◉', '/market'],
  ['alerts',       'Alerts',          '⚑', '/alerts'],
  ['reports',      'Reports',         '▧', '/reports'],
  ['settings',     'Settings',        '⚙', '/settings'],
  ['logs',         'Logs',            '☰', '/logs'],
  ['system-health','System Health',   '♥', '/system-health'],
  ['backtest',     'Backtest',        '⟲', '/backtest'],
  ['data-explorer','Data Explorer',   '⛃', '/data-explorer'],
];
const BOTTOM = ['overview', 'positions', 'scanners', 'alerts', 'settings'];

export function mountShell({ active, title, subtitle, icon = '⚙' }) {
  renderSidebar(active);
  renderTopbar({ title, subtitle, icon });
  renderBottomNav(active);
  wireDrawer();
  startMarketClock();
}

function renderSidebar(active) {
  const nav = NAV.map(([key, label, ic, href]) => `
    <a class="nav-item ${key === active ? 'active' : ''}" href="${href}">
      <span class="ic">${ic}</span>${label}
    </a>`).join('');

  $('sidebar').innerHTML = `
    <div class="brand">
      <div class="brand-logo">${BRAND.logo}</div>
      <div>
        <div class="brand-name">${BRAND.name}</div>
        <div class="brand-sub">${BRAND.sub}</div>
      </div>
    </div>
    <nav class="nav">${nav}</nav>
    <div class="sidebar-foot">
      <div class="mode-toggle"><span class="mode-dot"></span> Live Mode <span style="margin-left:auto;color:var(--dim)">▾</span></div>
      <div class="user">
        <div class="user-av">${USER.initials}</div>
        <div><div class="user-name">${USER.name}</div><div class="user-role">${USER.role}</div></div>
      </div>
    </div>`;
}

function renderTopbar({ title, subtitle, icon }) {
  $('topbar').innerHTML = `
    <div class="topbar-title">
      <button class="hamburger" id="hamburger" aria-label="Menu">☰</button>
      <span class="t-ic">${icon}</span>
      <div>
        <h1>${title}</h1>
        ${subtitle ? `<div class="sub">${subtitle}</div>` : ''}
      </div>
    </div>
    <div class="topbar-right">
      <div class="mkt-status">
        <span class="lbl">MARKET STATUS</span>
        <span class="val" id="mktVal">—</span>
      </div>
      <div class="bell">🔔<span class="badge" id="bellBadge">0</span></div>
    </div>`;
}

function renderBottomNav(active) {
  const bn = $('bottomNav');
  if (!bn) return;
  bn.innerHTML = BOTTOM.map((key) => {
    const item = NAV.find((n) => n[0] === key);
    return `<a class="${key === active ? 'active' : ''}" href="${item[3]}"><span class="ic">${item[2]}</span>${item[1].split(' ')[0]}</a>`;
  }).join('');
}

function wireDrawer() {
  const sb = $('sidebar');
  const scrim = $('scrim');
  const ham = $('hamburger');
  const open = () => { sb.classList.add('open'); scrim && scrim.classList.add('show'); };
  const close = () => { sb.classList.remove('open'); scrim && scrim.classList.remove('show'); };
  ham && ham.addEventListener('click', () => sb.classList.contains('open') ? close() : open());
  scrim && scrim.addEventListener('click', close);
  document.addEventListener('keydown', (e) => e.key === 'Escape' && close());
}

/* Market status computed client-side in IST (NSE 09:15–15:30, Mon–Fri). */
function startMarketClock() {
  const tick = () => {
    const now = new Date();
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const mins = ist.getHours() * 60 + ist.getMinutes();
    const weekday = ist.getDay() >= 1 && ist.getDay() <= 5;
    const open = weekday && mins >= 555 && mins <= 930;
    const hhmm = ist.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    const v = $('mktVal');
    if (v) { v.className = `val ${open ? 'open' : 'closed'}`; v.textContent = `${open ? 'OPEN' : 'CLOSED'} ${hhmm} IST`; }
  };
  tick();
  setInterval(tick, 30000);
}
