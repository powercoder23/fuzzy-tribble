/* ============================================================================
   core/api.js — fetch wrapper + shared formatters (ES module).
   Imported by every page. Single home for number/date/trade formatting.
   ========================================================================== */

export async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text().catch(() => res.statusText)}`.slice(0, 200));
  return res.json();
}

export function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/* ── Number / money ──────────────────────────────────────────────────────── */
export function fmtPnl(v) {
  if (v === null || v === undefined) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}₹${Math.round(v).toLocaleString('en-IN')}`;
}
export function fmtNum(v, nd = 2) {
  return (v === null || v === undefined) ? '—' : Number(v).toFixed(nd);
}
export function fmtInt(v) {
  return (v === null || v === undefined) ? '—' : Number(v).toLocaleString('en-IN');
}

/* ── Trade / contract ────────────────────────────────────────────────────── */
const MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
export function fmtExpiry(e) {
  if (!e) return '';
  const m = String(e).match(/(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[3]} ${MONTHS[+m[2] - 1]}`;
  const d = new Date(e);
  if (!isNaN(d)) return `${String(d.getDate()).padStart(2, '0')} ${MONTHS[d.getMonth()]}`;
  return String(e);
}
export function ceOrPe(side) {
  const s = (side || '').toUpperCase();
  if (s.startsWith('C')) return 'CE';
  if (s.startsWith('P')) return 'PE';
  return s;
}
export function contractName(t) {
  return `${fmtExpiry(t.expiry)} ${t.symbol} ${ceOrPe(t.side)} ${t.strike ?? ''}`
    .replace(/\s+/g, ' ').trim();
}
export function fmtTime(ts) {
  if (!ts) return '—';
  const m = String(ts).match(/(\d{2}):(\d{2})/);
  return m ? `${m[1]}:${m[2]}` : String(ts);
}
/* Live (unrealized) P&L for open trades; realized for closed. */
export function tradePnl(t) {
  if (t.status === 'open') {
    const lot = t.lot_size || 1;
    return {
      rupees: ((t.last_price || 0) - (t.entry || 0)) * lot,
      pct: t.entry ? ((t.last_price || 0) - t.entry) / t.entry * 100 : 0,
      live: true,
    };
  }
  return { rupees: t.realized_rupees, pct: t.realized_pct, live: false };
}

/* ── DOM + toast helpers ─────────────────────────────────────────────────── */
export const $ = (id) => document.getElementById(id);
export function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}
export function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

let _toastTimer;
export function toast(msg, kind = 'ok') {
  let t = $('toast');
  if (!t) { t = el('div', 'toast'); t.id = 'toast'; document.body.appendChild(t); }
  t.className = `toast ${kind}`;
  t.textContent = msg;
  requestAnimationFrame(() => t.classList.add('show'));
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2800);
}
