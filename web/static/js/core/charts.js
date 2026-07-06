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
