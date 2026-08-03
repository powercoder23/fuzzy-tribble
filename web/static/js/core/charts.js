/* ============================================================================
   core/charts.js — small Chart.js factories. Chart.js is loaded globally
   via CDN <script> in the page; this module reads window.Chart.
   ========================================================================== */

const store = new WeakMap(); // canvas -> chart instance (destroy before redraw)

/* Minimal sparkline for the bottom metric cards. */
export function sparkline(canvas, data, color = '#3b82f6') {
  if (!canvas || !window.Chart) return;
  const prev = store.get(canvas);
  if (prev) prev.destroy();
  const chart = new window.Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: data.map((_, i) => i),
      datasets: [{
        data,
        borderColor: color,
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        backgroundColor: (ctx) => {
          const { ctx: c, chartArea } = ctx.chart;
          if (!chartArea) return 'transparent';
          const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          g.addColorStop(0, color + '55');
          g.addColorStop(1, color + '00');
          return g;
        },
        tension: 0.35,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
      animation: false,
    },
  });
  store.set(canvas, chart);
  return chart;
}

/* Daily P&L bar + cumulative line, used by Overview and Positions pages.
   `daily` is the /api/paper-trades/history `.daily` array: [{date, net_rupees, cumulative}]. */
export function pnlHistoryChart(canvas, daily, onBarClick) {
  if (!canvas || !window.Chart) return null;
  const prev = store.get(canvas);
  if (prev) prev.destroy();
  if (!daily || !daily.length) return null;
  const chart = new window.Chart(canvas.getContext('2d'), {
    data: {
      labels: daily.map((d) => d.date.slice(5)),
      datasets: [
        { type: 'bar', label: 'Daily P&L', data: daily.map((d) => d.net_rupees),
          backgroundColor: daily.map((d) => (d.net_rupees || 0) >= 0 ? 'rgba(16,185,129,0.5)' : 'rgba(239,68,68,0.5)'),
          borderRadius: 3, yAxisID: 'y' },
        { type: 'line', label: 'Cumulative', data: daily.map((d) => d.cumulative),
          borderColor: '#f59e0b', borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3, yAxisID: 'y' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
      onClick: (e, els) => { if (els.length && onBarClick) onBarClick(daily[els[0].index]); },
      plugins: {
        legend: { labels: { color: '#8b98b0', font: { family: 'JetBrains Mono', size: 10 }, boxWidth: 16 } },
        tooltip: { backgroundColor: '#111725', borderColor: '#232c40', borderWidth: 1, titleColor: '#8b98b0', bodyColor: '#e6edf7', bodyFont: { family: 'JetBrains Mono', size: 11 }, padding: 8 },
      },
      scales: {
        x: { ticks: { color: '#5b6678', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 10 }, grid: { color: '#1a2233' } },
        y: { ticks: { color: '#8b98b0', font: { family: 'JetBrains Mono', size: 9 }, callback: (v) => '₹' + v.toLocaleString('en-IN') }, grid: { color: '#161d2e' } },
      },
    },
  });
  store.set(canvas, chart);
  return chart;
}

/* Backtest equity curve — cumulative P&L per trade, in trade sequence.
   `curve` is [{ts, equity}, ...] as returned by /api/backtest/runs/{id}. */
export function equityCurveChart(canvas, curve) {
  if (!canvas || !window.Chart) return null;
  const prev = store.get(canvas);
  if (prev) prev.destroy();
  if (!curve || !curve.length) return null;
  const positive = curve[curve.length - 1].equity >= 0;
  const color = positive ? '#10b981' : '#ef4444';
  const chart = new window.Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: curve.map((_, i) => i + 1),
      datasets: [{
        label: 'Equity',
        data: curve.map((c) => c.equity),
        borderColor: color, borderWidth: 1.5, pointRadius: 0, tension: 0.15, fill: true,
        backgroundColor: (ctx) => {
          const { ctx: c, chartArea } = ctx.chart;
          if (!chartArea) return 'transparent';
          const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          g.addColorStop(0, color + '33');
          g.addColorStop(1, color + '00');
          return g;
        },
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#111725', borderColor: '#232c40', borderWidth: 1,
          titleColor: '#8b98b0', bodyColor: '#e6edf7', bodyFont: { family: 'JetBrains Mono', size: 11 }, padding: 8,
          callbacks: { title: (items) => `Trade #${items[0].label}`, label: (item) => `₹${item.raw.toLocaleString('en-IN')}` },
        },
      },
      scales: {
        x: { title: { display: true, text: 'Trade #', color: '#5b6678', font: { size: 9 } },
             ticks: { color: '#5b6678', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 12 }, grid: { color: '#1a2233' } },
        y: { ticks: { color: '#8b98b0', font: { family: 'JetBrains Mono', size: 9 }, callback: (v) => '₹' + v.toLocaleString('en-IN') }, grid: { color: '#161d2e' } },
      },
    },
  });
  store.set(canvas, chart);
  return chart;
}
