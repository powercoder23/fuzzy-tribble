# Frontend Redesign & Restructure Plan

Full re-skin of the trading dashboard to the new design system (the "Fuzzy
Tribble" mockup), plus an architecture refactor: extract shared CSS, split the
monolithic `dashboard.html` into per-page files, and structure the frontend JS.
Data policy: **real where cheap, clearly-labeled static placeholders elsewhere.**

---

## 1. Current state

- **Two monolith HTML files** served as raw text by FastAPI: `dashboard.html`
  (~1.8k lines, an SPA with hash-tabbed sections) and `settings.html` (a plain,
  functional settings page).
- **All CSS and JS are inline** in each file, duplicated across the two.
- **No static mount, no build step, no templating** — `dashboard_app.py` just
  reads the files and returns them (`GET /` and `GET /settings`).
- **Backend control already exists and works**: `settings_routes.py` +
  `docker_control.py` expose kill-switch, container start/stop/restart/rebuild,
  startup-profile apply, and the alert-flag matrix. `settings_store.py` persists
  settings. Read APIs cover IV, analytics, paper-trades, overview, strategy
  performance, market snapshot, health, cockpit, convex journal.
- **No backend yet** for: host CPU/Mem/Disk/Network, per-scanner
  "signals today" + priority, Telegram/backup status.

Operational caveat: the project folder is a **lazily-synced mount** — `bash`
sees a stale/partial copy of large files; use the Read/Edit tools for the true
file state. Don't trust whole-file `bash` reads during migration.

---

## 2. Target architecture

No build step, no framework — vanilla ES modules + a shared stylesheet, served
statically. One thin HTML shell per page; the sidebar/topbar are rendered once
by a shared `shell.js`.

```
web/
  static/
    css/
      tokens.css        # design tokens (colors, spacing, radius, fonts)
      base.css          # reset, typography, scrollbars
      components.css    # cards, stat-cards, tables, tabs, tags, buttons, sparklines
      shell.css         # sidebar, topbar, page layout grid
    js/
      core/
        api.js          # fetch wrapper + ALL formatters (fmtPnl, fmtExpiry,
                        #   contractName, tradePnl, fmtTime, fmtNum, ivrClass…)
        shell.js        # renders sidebar + topbar, sets active nav,
                        #   market-status pill, notification bell
        charts.js       # Chart.js factories (pnl bar+line, sparkline, iv, skew)
      pages/
        overview.js  positions.js  scanners.js  market.js  alerts.js
        reports.js   data-explorer.js  system-health.js  logs.js
        backtest.js  settings.js  dashboard.js  signals.js
  pages/                # thin per-route HTML (shared <link>/<script src>)
    overview.html  positions.html  scanners.html  … settings.html
```

Backend (`dashboard_app.py`):

- `app.mount("/static", StaticFiles(directory="web/static"), name="static")`
- One loop registers `GET /{page}` → `FileResponse("web/pages/{page}.html")`;
  `GET /` redirects to `/overview`.
- Keep the old `dashboard.html` route alive **during** migration so nothing
  breaks; retire it in the last phase.

---

## 3. Design system (from the mockup)

- **Surfaces:** near-black navy background, slightly lighter panels, hairline
  borders, 8–10px radius, soft shadow. Reuse/extend the existing tokens in
  `dashboard.html` (`--bg #080810`, `--surface`, `--surface2`, `--border`,
  `--green/--red/--amber/--blue/--purple`) so the palette stays consistent.
- **Accent:** teal→cyan brand gradient (logo) + indigo active-nav pill.
- **Shell:** ~230px left sidebar (logo, icon nav, active pill, Live-Mode
  dropdown + user footer); per-page topbar (title + subtitle left; Market-Status
  pill + notification bell right).
- **Components:** stat-card row (icon, label, big value, sub), tab strip,
  data tables with status tags, right-rail cards (Startup Profiles, Quick
  Actions), bottom metric cards with mini sparklines.

### Responsive / mobile (vanilla, no framework)

Mobile is designed in from the start, not bolted on. One shared set of
breakpoints in `tokens.css`, mobile-first CSS in every component.

- **Breakpoints:** `≤600px` phone, `601–1024px` tablet, `>1024px` desktop.
- **Shell:** desktop shows the fixed 230px sidebar; **≤1024px the sidebar
  collapses to an off-canvas drawer** opened by a hamburger in the topbar
  (reuse the existing `.sidebar.open` toggle already in `dashboard.html`), with
  a scrim behind it. On phones, add a **bottom tab bar** for the 4–5 most-used
  destinations so the primary nav stays thumb-reachable. Topbar condenses:
  title stays, subtitle hides, Market-Status pill + bell shrink to icons.
- **Layout grids:** the two-column (main + right-rail) and multi-card stat rows
  use `display:grid` with `auto-fit`/`minmax` so cards reflow to 2-up then 1-up;
  the right rail (Startup Profiles, Quick Actions) drops below the main panel on
  tablet and stacks on phone.
- **Tables → cards:** wide tables (scanners, positions, alert matrix) get a
  horizontal-scroll wrapper on tablet and **restack as stacked key/value cards
  on phones** (each row becomes a card with labeled fields) so nothing needs
  pinch-zoom. The new Positions table (contract, times, exit, live P&L) uses
  this card mode on phone.
- **Touch:** ≥44px tap targets, larger inputs/toggles, no hover-only actions
  (Pause/Restart/Settings become always-visible buttons on touch).
- **Charts:** Chart.js `responsive:true` + `maintainAspectRatio:false` inside
  fixed-height wrappers; sparklines simplify (fewer ticks) below tablet.
- **Testing:** verify at 375px (phone), 768px (tablet), 1440px (desktop).

---

## 4. Navigation → page → data map

| Sidebar item   | Route            | Reuses today            | Data source                              | Work        |
|----------------|------------------|-------------------------|------------------------------------------|-------------|
| Overview       | `/overview`      | `overview` tab          | `/api/overview`, `/api/cockpit`          | re-skin     |
| Dashboard      | `/dashboard`     | `convex` tab            | `/api/cockpit`, `/api/convex/journal`    | re-skin     |
| Signals        | `/signals`       | opportunities/cockpit   | `/api/opportunities`, `/api/cockpit`     | re-skin     |
| Positions      | `/positions`     | `trades` tab            | `/api/paper-trades(+/history)`           | re-skin*    |
| Scanner Hub    | `/scanners`      | (settings scanners)     | docker status + `/api/scanners` (new)    | new         |
| Market Overview| `/market`        | `market` tab            | `/api/market-snapshot`                    | re-skin     |
| Alerts         | `/alerts`        | `alerts` tab            | `/api/activity` + alert-flag matrix      | re-skin     |
| Reports        | `/reports`       | strategy perf/history   | `/api/strategy-performance`, history      | re-skin     |
| Settings       | `/settings`      | `settings.html`         | `/api/settings/*`                         | **rebuild** |
| Logs           | `/logs`          | —                       | new (container logs)                      | new         |
| System Health  | `/system-health` | health                  | `/api/health` + `/api/system/metrics`    | new-ish     |
| Backtest       | `/backtest`      | —                       | backtest harness (open item)              | stub        |
| Data Explorer  | `/data-explorer` | `iv`+`analytics`+`universe` | `/api/iv/*`, `/api/analytics/*`, `/api/symbols` | re-skin (merge 3) |

\* Positions already gets the new trade-detail table (full contract name,
entry/exit time, exit price, live P&L, total P&L bar) — built this session in
`dashboard.html`; it migrates into `positions.js`.

---

## 5. Backend additions (real where cheap)

- **`GET /api/system/metrics`** — one call for the stat-card + bottom-metric
  rows. **Real:** containers running/total (`docker_control.get_running_status`),
  DB health + response ms (`/api/health`), signals today (`paper_trades` /
  cockpit emitted). **Static/placeholder (labeled):** CPU, Memory, Disk,
  Network I/O, Telegram status, Last Backup — return `null` + a `"placeholder":
  true` flag so the UI renders a muted "—". (psutil/`docker stats` can make
  these real in a later pass if wanted.)
- **`GET /api/scanners`** — per-scanner row: `status` (real, from docker),
  `last_signal` / `signals_today` / `priority` (static for now; wire to a
  per-scanner counter table later). Actions reuse the existing
  `/api/settings/container-action`.

Everything else reuses endpoints that already exist.

---

## 6. Phased delivery

**Phase 0 — Scaffolding + design system (no behavior change).**
Create `web/` tree; extract tokens/base/components/shell into shared CSS
**mobile-first, with the shared breakpoints baked in**; add StaticFiles mount;
write `core/api.js`, `core/shell.js` (incl. drawer toggle + bottom tab bar),
`core/charts.js`; build **Overview** end-to-end as the responsive reference
page (validated at 375 / 768 / 1440px). Old dashboard stays live.

**Phase 1 — Settings page to the mockup (the screen you showed).**
Rebuild `/settings` on the new shell: top stat cards, tab strip, Scanner-
Management table, Startup-Profiles rail, Quick Actions, Containers, Alert
matrix, bottom system-metrics row. Wire to existing `settings_routes` +
new `/api/system/metrics` and `/api/scanners`.

**Phase 2 — Split remaining existing tabs into pages.**
Positions (new trade table), Market, Alerts, Reports, Data Explorer (merges
iv+analytics+universe). Move each tab's JS into `pages/*.js`, re-skin.

**Phase 3 — New pages.**
Scanner Hub, System Health, Dashboard, Signals, Logs, Backtest (stub).

**Phase 4 — Cut over.**
Point `/` at `/overview`, retire monolith `dashboard.html`, add redirects,
delete dead inline CSS/JS.

**Phase 5 — QA.**
Per-page JS-error check, nav active states, side-by-side vs mockup, verify no
endpoint regressions, and a **full responsive pass** at 375 / 768 / 1440px:
drawer + bottom-nav behavior, card reflow, table→card restacking, tap targets.

---

## 7. Decisions & risks

- **No build step** keeps the current simple deploy; plain `<script type=
  "module">` imports. If you'd rather bundle later, the structure already suits
  it.
- **Migrate in parallel** — never break the running dashboard; each page ships
  behind its own route before the old one is removed.
- **Lazy mount sync** — verify file state with the Read tool during the split,
  not bulk `bash` reads.
- **Placeholders are explicit** — every not-yet-real tile is visibly muted, so
  the dashboard never shows fake numbers as if live.

---

## 8. Already done this session

Positions/trades table upgraded in `dashboard.html`: full contract name
(`28 JUL BANKNIFTY CE 5800`), entry/exit time, exit price, **live P&L for open
positions**, and a **Current Total P&L** bar above the grid. This carries into
`positions.js` in Phase 2.
