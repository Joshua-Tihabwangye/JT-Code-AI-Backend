# Dashboard builder: audit and implementation plan

Status: **approved with the user's decisions (§10). Phases 1–2 done 2026-10-14; Phase 3 next.** This plan turns the Data Visualization
module into a Power BI-class dashboard builder, following SKILL.md (master-fixer) phases.

## 1. Audit summary (what exists today)

### Frontend (`jt-code-frontend`)
- **Stack.** React 19, React Router 7, TanStack Query 5, Tailwind 4, zustand 5 and zod 3 (both
  already installed), ECharts 6.1 and echarts-gl 2.1 (lazy-loaded), react-i18next with 20
  locales, puppeteer-core e2e, vitest.
- **Missing.** No grid or drag library, no html-to-image.
- **Module.** `src/features/visualize/`.
  - `VisualizeLayout` is a header plus tabs plus an Ask AI drawer.
  - `context.tsx` holds the catalog, datasets, active dataset, Studio and Predict drafts, and
    assistant state.
  - Seven pages: Overview, Data (1,220 lines), Gallery, Studio (883), Predict, Reports, Report
    detail.
- **Charts.** The server builds every ECharts `option` (85 renderers in Python). `EChart.tsx`
  only displays it: no events, no instance access, no client theming.
- **Data layer.** `VisualizeRepository` in `src/lib/data`. The mock is a Proxy that rejects
  every call, so new methods need no mock code.
- **i18n.** Strings use `t('visualize.key', 'English default')`. `en.json` holds only a few
  visualize keys, and other locales fall back to the defaults.
- **Nothing dashboard-shaped exists.** There are no slicers, filters, cross-filtering, pages,
  bookmarks or JSON themes. Studio's panel grid is the closest thing, and it is not saved.

### Backend (`jt-code-backend`)
- **Models.** `apps/visualize` has `SavedChart` (config plus an ECharts snapshot, `is_shared`) and
  `Report` (ordered chart and text items, light/dark).
- **Access.** Visibility is owner, `is_shared`, or org admin. Only the owner or an admin can
  change an object.
- **Every computation runs in an isolated sandbox process** (`run_engine`, `python -I`, rlimits).
  - Each call costs about 0.6–0.9 s for process start and pandas import, then a full CSV parse
    and a full profile.
  - Nothing is cached between calls.
  - **No endpoint aggregates with filters.** `viz_render` has no filters, and `apply_transform`
    is reachable only through Organize or asynchronous analysis runs.
- **Datasets.** Inline (≤ 5 MB) or Supabase Storage. Limits: 100k rows, 25 MB, 200 columns.
  `DatasetGrant` gives per-user view or analyze access.
- **Roles.** Org-level admin, editor and viewer. There is no per-object editor sharing and no
  share tokens or public links anywhere.
- **Caching and metering.** Redis default cache. Predict is metered as `analysis_runs` and the
  assistant as `chat_messages`; everything else is free but throttled (visualize: 900/hour).
- **AI.** `apps.ai_gateway.service.generate_completion` has no native JSON mode. The local echo
  provider never returns JSON, so a deterministic fallback is required.
- **Versioning pattern to reuse.** `apps/documents`: a `version` field plus `DocumentVersion`
  snapshots, taken under `select_for_update`.
- **Installed.** pandas 2.3, numpy, scipy, statsmodels, scikit-learn, openpyxl, weasyprint, and
  pydantic (indirectly).
- **Not installed.** DuckDB, pyarrow, Prophet, python-pptx and Python Playwright.
- **CI must stay green:**
  - ruff, format and mypy;
  - `spectacular --fail-on-warn`;
  - `makemigrations --check`;
  - `inventory_matrix --strict` (every new third-party import must be registered);
  - pytest;
  - a ZAP scan of the OpenAPI spec;
  - `npm run check`.

### Verified facts relevant to the brief
- **S7 blank chart.** The server renders a bar chart with x = channel and no value correctly
  ("Count by channel", three bars). The blank panel is therefore a frontend problem, to be traced
  in Phase 1.
- **S17 thin bars.** The bar width is capped at 56 px and axis labels use `hideOverlap`, so
  labels can disappear. Chart defaults (brief §7.3) fix this.

## 2. Architecture decisions

| # | Decision | Why |
|---|---|---|
| A1 | **Python builds every visual** (user decision). The sandboxed engine turns query results into ECharts options, KPI-card values, table/matrix rows and slicer values, using the theme plus the visual's format, for all ~85 catalog charts and the new card, KPI, gauge, table, matrix and slicer visuals. The browser only displays what Python returns and reports clicks for cross-filtering. | Python does all analysis and visualization, as the user requires. Formatting and cross-highlight round trips are kept fast by A2/A12 (one engine call per page, aggregates cached, aggregation pushed into Postgres). |
| A2 | **One engine call per page.** `POST /visualize/query/batch` runs all of a page's queries in one sandbox process. Results are cached in Redis, keyed by dataset checksum, `updated_at` and the normalized query. | Process start (~0.7 s) is paid once per page instead of per visual. Cached slicer states return without spawning a process. This meets "15 visuals < 2 s" on the sample. |
| A3 | **The sandbox stays and is hardened** (§2.1): no in-process pandas on user data. | Python is the engine, so the boundary around it must be strong. |
| A4 | **Dashboard = one versioned JSON spec** (pages → visuals → wells and format), validated server-side (DRF + explicit limits) and client-side (zod). | Mirrors Power BI's report definition. Atomic autosave, simple version history, and AI output validated against the same schema. |
| A5 | **Custom canvas, no grid library.** A 24-column snap grid with free mode, smart guides, multi-select and grouping, built on pointer events. | react-grid-layout has no free mode, smart guides, marquee or grouping, and its React 19 support is uncertain. No new dependency. |
| A6 | **Editor state in a zustand store** with an undo/redo history (50 steps), autosave debounced 800 ms, and version snapshots on demand and every N minutes. | zustand is already a dependency. Reuses the documents version pattern. |
| A7 | **Themes are JSON** in Power BI spirit. 13 built-ins (light and dark each) are defined in Python and served from the catalog. Custom themes are stored per org. Applied through CSS variables on the canvas plus a shared ECharts option builder. | One source for tiles, KPI cards and charts; per-visual Format overrides win. |
| A8 | **Safe expression language** for calculated measures and what-if parameters: a whitelisted AST (`+ - * /`, numbers, `[field]`, SUM/AVG/COUNT/DISTINCTCOUNT/MIN/MAX/MEDIAN, DIVIDE, IF, comparisons), evaluated in the sandbox. | Never `eval` or `DataFrame.eval` on user text. |
| A9 | **Sharing.** Org `is_shared` keeps working. New `DashboardMember(user, role=viewer\|editor)` adds per-dashboard roles. Personal views are per user and never modify the author's spec. Public links and embeds use a hashed, revocable, opt-in token with a read-only endpoint (Phase 6). | Fits the existing org/role model and closes the "no share tokens" gap safely. |
| A12 | **Supabase Postgres is the query store for large data** (user decision: keep Supabase). On import, replace, edit or organize, each dataset is copied (`COPY`, streamed) into a typed table `viz_data.ds_<id>`. Columns are renamed `c0…cN`; the mapping lives in the dataset profile. Indexes go on date and low-cardinality columns. Filters, group-bys and aggregates for dashboards compile to parameterized SQL that runs as a read-only role (`jt_viz_reader`, `SET LOCAL ROLE`, `statement_timeout`). Python receives only the small aggregated result and builds the visual. Files stay in Supabase Storage as the source of truth; the table is a rebuildable copy. | The database does the heavy scans (millions of rows stay in Postgres, never shipped to Python or the browser). Fewer bytes per request means lower latency. Cached aggregates in Redis repeat for free. Raising the per-dataset row limit becomes possible (Phase 5: chunked streaming import). |
| A13 | **Reports are removed** (user decision). The `Report` model, its endpoints, the report editor and the existing report rows are deleted. Saved charts stay as "My visuals". The Reports tab becomes the export hub for dashboards (HTML, PDF, PNG, Excel, JSON). | As requested. |
| A10 | **Exports.** HTML, Excel and JSON are kept. PNG per page is composed in the browser from each chart's `getDataURL` plus the tile layout on a canvas. PDF is those PNGs packed server-side with weasyprint (already installed). | No headless browser needed on the server. |
| A11 | **AI emits the spec only.** The rule-based layer (recommendations, insights, auto-dashboard) runs first and is free. The LLM is called only for free-text prompts; its JSON is validated, previewed as a diff, then applied or discarded. Metered as `chat_messages`, with a deterministic fallback for the echo provider. | Respects credits and the "never HTML from AI" rule. |

### 2.1 Securing Python (the engine stays Python)
Defence in depth, from the outside in:
1. **Process isolation (exists).** A separate `python -I` process with an empty environment (no
   secrets), a temporary HOME and working directory, rlimits (memory, CPU, open files, file size,
   no core dumps) and a wall-clock timeout.
2. **New: audit-hook guard**, installed by the engine before it touches user data (PEP 578 hooks
   cannot be removed). It blocks:
   - sockets and DNS;
   - `subprocess`, `os.system`, `exec*`, `fork` and `spawn`;
   - `ctypes` and `cffi`;
   - writes outside the temporary directory;
   - reads outside the Python installation and the temporary directory;
   - `sys.setrecursionlimit` abuse.
   Child processes are blocked by vetoing `fork`, `spawn` and `exec`. `RLIMIT_NPROC` is not used:
   on Linux it also counts threads, which numerical libraries need.
3. **New: user Python visuals.**
   - Scripts are checked by an AST validator before running:
     - only allow-listed modules (pandas, numpy, scipy, statsmodels, sklearn, math, statistics,
       datetime);
     - no dunder attribute access;
     - no `exec`, `eval`, `compile`, `open`, `getattr`, `globals` or `__import__`.
   - They run inside the same guarded process, receiving the visual's already-filtered data frame
     as `dataset`.
   - They may only return a figure spec (ECharts option dict), a table or a number, which is
     validated and size-capped.
   - They are never given database access or the network.
4. **Production.** The engine runs in its own container with `network_mode: none`, a read-only
   root filesystem, a non-root user, `cap_drop: [ALL]`, `no-new-privileges` and the default
   seccomp profile. It is reached by the API over a Unix socket. (Local development keeps the
   subprocess.)
5. **SQL path.** Identifiers come only from the stored `c0…cN` mapping (`psycopg.sql.Identifier`)
   and values are always bound parameters. The read-only role can only `SELECT` from `viz_data`.
   A statement timeout and row caps apply.

## 3. Data model (backend, migration `visualize/0003_dashboards`)

```
Dashboard(id uuid, organization, owner, name, description, spec JSON, spec_version int,
          thumbnail text (data URL, ≤ 200 KB), is_shared, version int, created_at, updated_at)
DatasetTable(dataset 1:1, table_name, columns JSON [{name, column, kind}], row_count,
             source_checksum, status: building|ready|failed, built_at)   ← A12 query store
DashboardVersion(dashboard, version, spec, name, created_by, created_at)   unique(dashboard, version)
DashboardMember(dashboard, user, role: viewer|editor)                       unique(dashboard, user)
PersonalView(dashboard, user, name, state JSON, is_default, updated_at)
Theme(organization, owner, name, spec JSON, is_shared, created_at, updated_at)
DashboardShareLink(dashboard, token_hash, created_by, expires_at, revoked_at)  ← Phase 6
```

`spec` follows brief §4. Main limits:
- 20 pages, 60 visuals per page, 8 datasets;
- 200 KB of format JSON per visual;
- 1.5 MB per spec.

Spec migration helpers upgrade `spec_version` on read.

**Existing data stays untouched:**
- **Saved charts.** `SavedChart` *is* the "My visuals" library. Inserting one into a dashboard
  converts it to a `Visual` (core family → wells; other types → server-rendered visual).
- **Reports.** The `Report` model and all existing report rows are deleted (decision D3). Exports
  move to dashboards.

## 4. API (all under `/api/v1/visualize/`, authenticated, org-scoped)

| Endpoint | Purpose |
|---|---|
| `POST query/` and `POST query/batch/` | `{datasetId, groupBy[{field, grain?}], measures[{field\|expr, agg, showValueAs}], filters[], crossFilters[], highlight?, sort, topN{n, others}, visualCalcs[], parameters{}}` → `{rows, totals, columns, meta{truncated, cached}}`. Batch takes up to 60 queries for one dataset group. Own throttle scope `visualize_query` (default 3,600/hour), because slicer use generates many calls. |
| `dashboards/` CRUD | Plus `duplicate`, `versions/` (list, create snapshot, restore), `members/`, `personal-view/` (GET/PUT/DELETE), `export/` (pdf/png/xlsx/html/json). |
| `themes/` | Built-in and org themes, import/export JSON. |
| `dashboards/auto/` | Auto-dashboard from a dataset (rule-based, free); also used for templates via a field-mapping wizard. |
| `explain/`, `anomalies/` | Free pandas analyses for right-click actions. Forecast reuses the Predict code. |
| `ai/page/`, `ai/suggest/`, `ai/summary/`, `ai/ask/` | Phase 6, metered. |

Every endpoint uses the existing permission helpers:
- **Read:** owner, member, shared-in-org, or admin.
- **Write:** owner, editor member, or admin, and the user needs org write access.
- **Personal views:** anyone who can read the dashboard.

Filters are validated against the dataset's column profile, and query sizes are capped (10k rows,
tables paginated server-side).

## 5. Frontend structure

```
src/features/visualize/dashboards/
  spec.ts            zod schema + types + defaults + spec-version upgrade
  store.ts           zustand editor store: spec, selection, history (undo/redo), dirty, autosave
  query.ts           buildQuery(visual, filters, params), useBatchQueries (debounce 150 ms, abort)
  filters.ts         precedence: report ∧ page ∧ visual ∧ slicers ∧ cross-filters; sync groups
  themes.ts          theme → CSS variables + ECharts base option; contrast / colour-blind checks
  render/            option builders per family (bar, line, combo, donut, treemap, funnel, scatter, map,
                     gauge, waterfall), chart defaults §7.3, number format (K/M/B), Top N + Others
  VisualRenderer.tsx single adapter: core family → client option; others → server render (+filters)
  Tile.tsx KpiCard.tsx MultiCard.tsx Gauge.tsx DataTable.tsx Matrix.tsx Slicer*.tsx TextBox.tsx Shape.tsx Button.tsx
  Canvas.tsx         grid + free mode, drag/resize/snap/guides/marquee/group, zoom, mobile layout
  panes/             BuildPane, FormatPane (searchable sections), FiltersPane, SelectionPane, Personalize
  DashboardsHome.tsx DashboardEditor.tsx DashboardViewer.tsx PresentMode.tsx
```

- `EChart.tsx` gains `onEvents`, an instance ref and `getDataURL`.
- Routes:
  - `/app/visualize` becomes the Dashboards home; the old Overview content moves into it.
  - New `/dashboards/:id`, `/dashboards/:id/view` and `/explore` routes.
  - `/studio` redirects to `/explore`.
- The module bar becomes one row: `Dataset ▾ | Dashboards · Data · Explore · Predict · Gallery ·
  Reports | Search · Ask AI · Share`.
- The dashboard editor has its own slim header and auto-collapses the app sidebar
  (`setSidebarCollapsed`).
- All new strings use `t()` with English defaults, and the keys are added to `en.json`.

## 6. Phases and deliverables

Each phase ends with:
- backend: ruff, format, mypy, schema, migrations check, inventory check and full pytest;
- frontend: `npm run check`;
- integration and e2e tests against the live stack;
- light and dark screenshots on the Retail sales sample, checked against brief §21;
- no commit: all work stays uncommitted (D4).

1. **Foundations**
   - Models and migration 0003; remove `Report` (model, rows, endpoints, editor UI).
   - Audit-hook guard for the engine (§2.1).
   - Postgres query store (A12): `DatasetTable`, `viz_data` schema, read-only role, `COPY`
     build, SQL compiler for filters, group-bys (date grains) and aggregates.
   - Spec validation, dashboards CRUD, versions and personal-view endpoints.
   - `viz_query` engine module (Python builds the visuals from aggregates) covering:
     - filters (eq/in/range/relative date/contains/Top N);
     - date grains;
     - aggregations (sum/avg/count/distinct/min/max/median/std/first/last);
     - show-value-as (% of total/row/column, running total, difference and % difference from
       previous, rank);
     - Top N + Others;
     - highlight;
     - visual calculations.
   - Batch endpoint with cache, query throttle scope, built-in themes.
   - Frontend: spec types, theme system, `Tile`, `KpiCard`, `VisualRenderer` (displays the
     Python output) with the chart defaults, an `EChart` events API, and the Studio blank-chart
     fix with a test.
2. **Dashboard editor**
   - Dashboards home with thumbnails, canvas (grid/free, drag, resize, snap, guides, align,
     distribute, group, selection pane, zoom), pages and page tabs.
   - Insert menu, Build pane (fields list, wells, aggregations, auto-suggest visual type), Format
     pane, on-object mini toolbar.
   - Autosave, versions, undo/redo, My visuals library.
3. **Interactivity**
   - Cross-filter/highlight and edit interactions.
   - Slicers: dropdown, list, tiles/buttons, between, date range, relative date, input,
     hierarchy; sync groups; apply/clear all.
   - Filters pane at three levels with lock/hide.
   - Drill-down on hierarchies, drill-through pages with Back.
   - Report-page tooltips; visual header (focus, show as table, export data, spotlight).
4. **Customization**
   - Viewer personalization and personal views, field parameters, bookmarks and views, buttons
     and navigators.
   - What-if parameters, quick measures, measures library, conditional formatting, analytics
     lines.
   - Small multiples, theme customizer with import/export, mobile layout, format painter.
   - User Python visuals (AST-validated, run under the guard, §2.1).
5. **Data and module polish**
   - New module navigation.
   - Data tab: compact toolbar, quality bars, Applied steps panel, transform catalogue (including
     merge/append, pivot/unpivot, conditional column, bins), relationships, date table.
   - Explore (replaces Studio), gallery SVG thumbnails, and "Recommended for this dataset".
   - Short insight cards, Predict wizard and pinnable results.
   - Reports: PDF, PNG and per-visual Excel.
6. **AI and presentation**
   - Templates (9) and auto-dashboard with a field-mapping wizard.
   - AI: create page, suggest visuals, Q&A box, summary visual, explain change.
   - Present mode with auto-cycle; share links and embeds.
   - Performance and accessibility pass; final e2e flow from brief §18.

## 7. Edge cases to guard

- **Dataset changes.** A dataset replaced or edited after a dashboard was built can lose columns.
  Visuals show "Field missing — remap" instead of failing, and the cache key includes the
  dataset checksum.
- **Deleted datasets.** The dashboard opens and its affected tiles show an error state.
- **Access.** Users without access to a dashboard's dataset get 404 on queries, even if the
  dashboard is shared with them; the share dialog warns about this.
- **Concurrent edits.** Two editors on one dashboard are detected with `version`
  (If-Match-style check on PATCH, 409 with "Reload / Overwrite").
- **Huge cardinality.** Group-bys are capped at 10k rows, Top N + Others is the default above 12
  categories, and slicers search server-side.
- **Mixed types.** Numeric strings, mixed date formats and time zones are typed by the engine's
  existing `typed()`. Relative dates use the dashboard's time zone (from the browser).
- **Personal views.** A personal view referencing a visual the author has deleted is pruned when
  merged.
- **Bad AI output.** Invalid AI specs are rejected with the reason shown, and nothing is
  auto-applied.
- **Throttling.** Slicer storms are debounced, stale requests aborted, and cached results reused.
- **Large exports.** Export of very large dashboards is capped by pages and visuals; thumbnails
  are capped at 200 KB.
- **i18n and RTL.** Arabic and Urdu use logical CSS properties in the new components.

## 8. Testing strategy

- **Backend engine (in-process):**
  - query builder: filters, grains, aggregations, show-value-as, Top N + Others, highlight,
    visual calculations;
  - expression evaluator, including rejection of unsafe input;
  - auto-dashboard layout fits 1280×720;
  - theme catalogue.
- **Backend API:**
  - dashboards CRUD, version restore, members and roles;
  - personal views isolated per user;
  - query and batch permissions and caching;
  - spec validation errors;
  - report→dashboard migration;
  - share-link token security (Phase 6).
- **Frontend unit (vitest):**
  - spec zod round-trip, filter precedence, cross-filter state, slicer sync groups;
  - theme application;
  - field parameters, personal-view merge, undo/redo;
  - number formatting, chart defaults (bar width, Top N, label rules), canvas snapping and
    alignment maths.
- **Integration (live stack):** a repository-level flow covering dashboards, query batch,
  personal views and exports.
- **E2E (puppeteer):**
  1. create a dashboard → auto-dashboard;
  2. click a bar → the KPI changes;
  3. change a slicer → clear all;
  4. personalize as a viewer → reset;
  5. save → reload persists;
  6. export PDF;
  7. light and dark screenshots;
  8. 1366×768 has no scroll;
  9. timing of a 15-visual page.

## 9. Scope notes and deviations from the brief

- **"< 4 s on 1M rows"** is outside today's limits (100k rows / 25 MB per dataset). It needs a
  columnar engine (DuckDB plus Parquet caching) and higher limits. Proposed as a separate,
  opt-in step (decision D2).
- **Python visual** would run user-written code on the server. Even in the sandbox this is a
  significant security risk, so it is not built unless explicitly approved (decision D1).
- **Prophet** is not installed. Forecasting keeps the existing statsmodels models (Holt-Winters,
  ARIMA-style, linear and seasonal); "Prophet-like" maps to Holt-Winters with seasonality.
- **PowerPoint export** needs a new dependency (python-pptx). PNG per page plus PDF are delivered
  instead unless approved.
- **Scheduled email snapshots and comments** are listed as "later phase" in the brief and are
  not in phases 1–6.
- **Brief §18 names Playwright.** This repo's e2e runs on puppeteer-core, which stays; the
  scenarios are the same.

## 10. Decisions (answered 2026-10-13)

- **D1. Python.** Python stays the engine for all visualization and analysis, and user Python
  visuals are allowed. Security comes from the hardening in §2.1.
- **D2. Database.** Supabase stays. Large data and latency are handled by pushing aggregation into
  Supabase Postgres (A12); no DuckDB.
- **D3. Reports.** The existing reports are removed (A13).
- **D4. Commits.** Nothing is committed; all work stays uncommitted.

## 11. Progress log

### Phase 1 — done 2026-10-14
- **Engine guard** (`viz_guard.py`) is active for every engine run; its tests are in
  `tests/test_visualize_guard.py`.
- **Postgres query store:**
  - `store.py`, the `viz_data` schema and the `jt_viz_reader` role (migration 0003);
  - `COPY` build with indexes, compiled SQL, and a pandas fallback (`viz_query.py`) with the same
    semantics (`tests/test_visualize_store.py` checks they agree).
- **Planner and visuals:**
  - `visual_plan.py` turns visuals into queries;
  - `viz_visuals.py` builds every visual in Python: card, multi-card, KPI (delta, sparkline,
    target), gauge, bar/column (stacked, %), line, area, combo (dual axis), donut/pie, treemap,
    funnel, waterfall, scatter, map, table, matrix and slicers;
  - it applies the chart defaults from brief §7.3 and draws cross-highlight.
- **Themes and API:**
  - `themes.py` holds the 13 built-in themes (light and dark);
  - `dashboard_spec.py` validates specs;
  - `dashboards.py` handles batching and the Redis cache;
  - `dashboard_views.py` provides CRUD, 409 version conflicts, snapshots and restore, members,
    personal views, themes, `auto/` and `visuals/`;
  - `autodash.py` builds the starter layout.
- **Reports removed** (model, rows, endpoints, editor UI). The Reports tab became "My visuals".
- **Frontend:**
  - Dashboards home and viewer (`src/features/visualize/dashboards/`): theme variables, KPI cards,
    tables, matrix, slicers, click-to-filter or highlight, Esc to clear, "Clear all slicers";
  - the module bar is hidden inside a dashboard;
  - `EChart` gained click events.
- **Studio blank chart:** not reproducible with the current code. It is covered by an e2e step and
  an engine test ("Count by channel" has three bars).
- **Measured:** first view of the auto-dashboard (16 visuals, with the table build) about 1–2 s;
  later views 0.7–0.8 s; unchanged pages are answered from cache with no engine run.

### Phase 2 — done 2026-10-14
- **Editor** (`src/features/visualize/dashboards/editor/`), at `/app/visualize/dashboards/:id/edit`:
  - **Canvas:** 24-column snap grid plus quarter-unit free mode, drag and multi-drag (groups move
    together), 8 resize handles, smart guides to other visuals' edges and centres, area
    selection, gridlines and zoom.
  - **Arrange menu:** align ×6, distribute, match width/height, bring to front / send to back,
    duplicate, delete.
  - **Insert menu:** every visual type plus "From My visuals" (saved charts become live gallery
    charts).
  - **Build pane:** field list with click or drag into wells, aggregation, date grain, "show value
    as", a "Better as …" type suggestion, and gallery-chart slots and options.
  - **Format pane:** searchable, with reset per section: title, X/Y/W/H, alt text, tile, colours,
    labels, legend position, sort and Top N, stacking, smooth lines, slices, KPI, gauge, table,
    slicer and map. With nothing selected it shows the page's canvas size and background.
  - **Selection pane:** layers with rename, hide, lock, order and group/ungroup.
  - **On the canvas:** a mini toolbar (type switch, labels, legend, sort, format, duplicate,
    delete) and inline title rename on double-click.
  - **Pages:** add, rename, reorder, duplicate, hide and delete.
  - **Editing safety:** undo/redo (50 steps, gestures merged), autosave after 800 ms with a 409
    conflict banner (load theirs / keep mine), versions (save and restore), keyboard shortcuts.
  - **Thumbnails:** painted in the browser from the live canvas.
- **Backend:**
  - the `chart` visual type: any gallery chart, rendered by its Python renderer on filtered rows
    that are evenly sampled (`mod(_row, k)`) in Postgres or pandas with identical results;
  - per-visual colours and legend position applied in Python;
  - quarter-unit layouts.
- **Fixed during testing:** duplicate React keys for alignment guides left lines on screen (now
  de-duplicated); drag tracking uses capture-phase pointer listeners with one update per frame.
