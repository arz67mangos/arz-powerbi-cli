# Power BI — cli-anything SOP

## What the "software" is

Power BI is closed source, so there is no codebase to analyse. The harness wraps
its two documented, scriptable surfaces:

| Surface | Native format / protocol | Role in harness |
|---|---|---|
| **PBIP project** (Power BI Desktop "Save as project") | `X.pbip` JSON, `X.Report/` (`definition.pbir`, `report.json` or PBIR `definition/`), `X.SemanticModel/` (`definition.pbism`, `definition/*.tmdl`) | Data layer — the CLI edits TMDL text directly |
| **Power BI REST API** `https://api.powerbi.com/v1.0/myorg` | JSON over HTTPS, Entra ID (MSAL) bearer tokens | Real backend — workspaces, datasets, refresh, DAX (`executeQueries`) |
| **Power BI Desktop** (`PBIDesktop.exe` / `PBIDesktopStore.exe`) | Opens `.pbip` | Real renderer for the report; `desktop open` |

`model.bim` (TMSL JSON) models are rejected with instructions to enable TMDL.

## GUI action → CLI mapping

| Desktop / Service action | CLI |
|---|---|
| File > Save as > Power BI project | `project new DIR -n NAME` |
| Model view: New measure / edit formula / Properties pane | `measure add/update/remove` |
| Modeling > New table (DAX) | `table add NAME --dax ... -c col:type` |
| Model view: drag a relationship | `relationship add Sales.Key Product.Key [--both]` |
| Open project | `project open`, `desktop open` |
| Service: workspace picker | `workspace list/use` |
| Service: dataset Refresh now / Refresh history | `dataset refresh [--wait]`, `dataset refresh-history` |
| DAX query view | `dax query "EVALUATE ..." [--csv out.csv]` |
| Get Data › Text/CSV | `import csv FILE [-t NAME] [--type COL=TYPE]` |
| Get Data › Excel workbook (sheet / table) | `import excel FILE [--sheet S \| --item TBL]` |
| Power Query › Advanced Editor | `query show`, `query set` |
| Power Query › Applied Steps (add/delete) | `query steps`, `query add-step` (`{prev}`), `query remove-step` |
| Transform › Data type | `clean set-type T COL=TYPE...` |
| Rename / Remove columns | `clean rename T OLD=NEW`, `clean remove-columns T COL...` |
| Filter rows, Replace values | `clean filter T "<M condition>"`, `clean replace T COL OLD NEW [--whole]` |
| Format › Trim / Clean / UPPER / lower / Capitalize | `clean text T trim\|clean\|upper\|lower\|proper COL...` |
| Remove duplicates, Fill down | `clean dedupe T [COL...]`, `clean fill-down T COL...` |
| Add Column › Custom column | `clean add-column T NAME "<M expr>" --type TYPE` |
| Home › Merge queries | `query merge LEFT RIGHT --on K [--right-on K] [--kind left\|inner\|…] [-e COL]` |
| Home › Append queries (as new) | `query append TARGET OTHER... [--as-new NAME]` |
| View › Themes › Customize / Save current theme | `theme create OUT.json [--preset P --palette ... --font ...]` |
| View › Themes › Browse for themes (import) | `theme apply FILE` or `theme apply --preset modern` |
| View › Themes › Default (remove custom) | `theme remove`, `theme show` |
| New page / Delete page / Page size | `page add NAME [-W 1280 -H 720]`, `page remove`, `page list` |
| Insert a visual, set fields, move/resize, title | `visual add PAGE TYPE -f FIELD [-c CAT] --x --y -W -H -t TITLE` |
| Lay out a dashboard page | `page build NAME --title T --slicer F --card [M] --chart TYPE:CAT:VALS[:Title] --table F,F` |
| Insert › Slicer (dropdown / between) | `visual add PAGE slicer -f FIELD [--mode Dropdown\|Between\|Basic]`, `page build --slicer F` |
| Copilot-style "suggest a page" / Quick Insights | `page suggest TABLE` |
| Visualizations pane: pick a chart type, drag fields into wells | `visual add PAGE TYPE -r ROLE=FIELD ...`, `visual types` |
| Format pane (legend, labels, axes, title, background, border...) | `visual format PAGE ID object.property=value ...` |
| Format › Columns/Data colors (all, per series, per point) | `visual color PAGE ID #RRGGBB [--series F \| --point VALUE]` |
| More options › Sort axis / Sort by | `visual sort PAGE ID FIELD [--asc]` |
| Drag / resize / bring forward | `visual move PAGE ID --x --y -W -H --z` |
| Visualizations › … › Import a visual from a file | `visual custom import FILE.pbiviz` |
| Visualizations › … › Get more visuals (AppSource) | `visual custom add-public GUID` |
| Remove a custom visual | `visual custom list`, `visual custom remove REF [--force]` |
| Build a full dashboard page from a table | `page template NAME -t TABLE [-k overview\|trend\|comparison] [--create-measures] [--monthly]` |
| Build a themed multi-page report | `page generate spec.json [--replace]` |

## Power Query layer

- Import tables get an `m` partition (`mode: import`, `source =` with M at depth 4)
  and `annotation PBI_ResultType = Table`, the same layout Desktop writes.
- Step names follow the Query Editor (`Changed Type`, `Changed Type1`, `Removed Duplicates`, …).
- Every `clean`/`merge`/`append` step also updates the TMDL column list to match the query
  output (type, name, added/removed columns). Raw `query add-step`/`query set` do **not**
  touch the column list, so keep it in sync yourself.
- Types: `text, int, number, decimal, date, datetime, bool` → TMDL `string, int64, double,
  decimal, dateTime, boolean` / M `type text, Int64.Type, type number, Currency.Type, …`.
- CSV/Excel inference reads ≤200 sample rows (Python `csv` / openpyxl); the real
  load happens when Power BI refreshes.
- Verified: generated queries refreshed in Power BI Desktop with correct results
  (trim/proper, dedupe, blank→Unknown, decimal type, rename, custom column, append, merge).
- Known limits: comments *between* steps are dropped when a query is rewritten. Parameters
  (`expressions.tmdl`), folder/SQL/web sources, unpivot and group-by aren't wrapped
  (use `query add-step`).

## Report layer (themes, pages, visuals)

- Writes the **PBIR** format Desktop saves: `definition/report.json`, `pages/pages.json`,
  `pages/<id>/page.json`, `pages/<id>/visuals/<id>/visual.json`. New projects start in PBIR.
  A legacy single `report.json` is refused: open it in Desktop and save once to upgrade.
- A custom theme is stored the way Desktop's *Import theme* stores it: the JSON in
  `StaticResources/RegisteredResources/<Name><17 digits>.json`, registered in
  `report.json` (`resourcePackages` + `themeCollection.customTheme`). Applying a theme
  replaces the previous custom theme.
- `build_theme` presets (modern, dark, corporate, warm, accessible) set the palette,
  text classes and `visualStyles["*"]["*"]`: card background, rounded border
  (`radius`), soft drop shadow, padding and left-aligned titles, plus a tinted page.
  Textboxes get no card. Palettes: indigo, ocean, sunset, forest, mono, colorblind (Okabe-Ito).
- Fields: `[Measure]`, `Table[Measure]`, `Table.Column`, `sum|avg|min|max|count|distinctcount(Table.Column)`.
  A numeric column in a value role is summed automatically. Roles: charts use Category + Y,
  card/table/slicer use Values.
- `page build` grid: 24px margin, 16px gap, a 52px title band, a 104px KPI card row, then charts
  two per row and a full-width table filling the rest. It refuses if rows would be < 120px.
- `project validate` also flags visuals that point at missing measures or columns, and a
  registered theme whose file is missing.
- Verified in Power BI Desktop: the modern preset rendered rounded shadowed cards on a
  grey page; KPI cards, column and donut charts and the table showed correct data after refresh.

## Report templates (core/suggest.py)

- **Column roles**: names ending `ID`/`Key`/`Code`/`SKU` → key (counted, never charted);
  `dateTime` → date; `Year`/`Month`/`Quarter`… → time bucket; numeric → value (Sum, or Avg when
  the name says price/rate/%/score); boolean → flag; other text → category.
- **Cardinality** is sampled (≤1000 rows) from the raw CSV/Excel file behind an import query,
  so it reflects the source before cleaning. DAX tables and missing files give `distinct: null`.
- **Chart rules**: date → line trend + range slicer; ≤6 values → donut; more (≤50) → ranked
  bars; time buckets → columns. The second comparison picks a different chart type when it can.
  Slicers: the date plus up to two categories with ≤30 values, preferring geo names and flags.
  Categories from dimension tables the fact table points to (many → one) are included.
- **KPI cards**: the table's measures (up to 4); otherwise Sum/Avg of numeric columns plus a
  distinct count of the ID column. `--create-measures` writes them as real measures
  (`Total X`, `Average X`, `<Table> Count`) with `#,0`, `#,0.00` or `0.0%` formats.
- **Templates**: overview = trend + 2 comparisons + detail table; trend = full-width trend plus
  lines for the next KPIs; comparison = a clustered column of 2 KPIs by the top category, a
  second comparison, and the table. Every template gets the title, slicer band and KPI row.
- `--monthly` adds `<Date> Month = Date.StartOfMonth([Date])` as a Power Query step (once) and
  plots the trend by month (columns on overview, lines on trend pages). Daily lines are noisy.
- **Spec file** (`page generate`): `{"theme": "modern" | "file.json" | {build_theme args},
  "create_measures": bool, "monthly": bool, "replace": bool, "pages": [{"name", "table",
  "template", "title", "kpis", "slicers", "charts", "detail"}]}`. Charts are `TYPE:CAT:VALS[:Title]`
  strings or dicts; a list inside `charts` is one row. Without `pages`, every table that has
  measures/numbers and isn't a pure dimension gets an overview. Unknown keys are rejected.
- Verified in Power BI Desktop: a 3-page spec (240 orders CSV + products xlsx) rendered the
  ocean theme, date range slicer, dropdowns, 4 KPI cards (1,211 / 28.69 / 3.9% / 240), monthly
  trend, region donut, product bars, clustered comparison and detail tables after refresh.

## Visual types, formatting and custom visuals (core/visuals.py)

- **25 built-in types** with their data roles (`visual types`): card, multicard, column, bar,
  stackedcolumn, stackedbar, column100, bar100, line, area, stackedarea, combo (Y = columns,
  Y2 = lines), stackedcombo, ribbon, pie, donut, funnel, waterfall (Category, Breakdown, Y),
  treemap (Group, Details, Values), scatter (Category, Series, X, Y, Size), gauge (Y, MinValue,
  MaxValue, TargetValue), kpi (Indicator, TrendLine, Goal), matrix (`pivotTable`: Rows, Columns,
  Values), table, slicer. Measure roles auto-sum numeric columns; grouping roles bind raw columns.
- `-f`/`-c` still fill each type's default value/category role; `-r ROLE=FIELD` binds any role
  (case-insensitive). Missing required roles and unknown roles are refused.
- **Formatting** writes literal expressions: `true/false`, integers (`12L`), decimals (`1.5D`),
  `#RRGGBB` (as a solid colour), anything else as text (quote it to force text). Container
  cards (title, subTitle, background, border, dropShadow, padding, visualHeader, ...) go to
  `visualContainerObjects`; the rest to `visual.objects`. Settings merge into the card's
  unselected entry; `--reset CARD` drops it first.
- **Colours**: `dataPoint.fill` with no selector (whole visual), `{"metadata": queryRef}` for a
  series, or a `Comparison` scope on the category column for one value.
- **Sort**: `query.sortDefinition` with one field and direction.
- **Custom visuals**: a `.pbiviz` is a zip of `package.json` + `resources/<guid>.pbiviz.json`
  (the capabilities list the data roles). `import` copies both files to
  `<Report>/CustomVisuals/<guid>/` and registers a `CustomVisual` resource package in
  `definition/report.json`. AppSource visuals are listed in `publicCustomVisuals` (Desktop
  downloads them), so their roles are unknown and any role name is accepted. `project validate`
  flags unknown roles, missing required roles (warning) and custom visual types that aren't
  registered. A real package made with Microsoft's `powerbi-visuals-tools` 7.2.1 is the test
  fixture (`tests/fixtures/probeViz.pbiviz`).
- **Checked in Desktop (2026-11-18)**: Desktop's own import of the fixture wrote the same
  `CustomVisuals/<guid>/package.json` + `resources/<guid>.pbiviz.json` files, and one resource
  package item `{name/path: "<guid>.pbiviz.json", type: "CustomVisualMetadata"}`, which the CLI
  now writes too. A CLI-built page with combo, scatter, matrix, gauge, KPI, waterfall, treemap,
  ribbon, stacked column and funnel rendered with data. Legend, data labels, title size and
  ascending sort applied. `visual color` with no flag and with `--point` write the same selectors
  Desktop does. `--series` writes a `metadata` selector (legend, multi-series charts); for a lone
  measure in the main value role with no legend field, Desktop's `metadata` entry only reached the
  legend, so the CLI also writes the unselected entry that colours the bars (not re-rendered).

## TMDL facts the parser relies on

- Tab indentation. Object header at depth N, properties at N+1, multi-line
  expressions at N+2 or inside ```` ``` ```` fences.
- `/// text` lines directly above an object are its description.
- Names needing quotes use `'single quotes'`, `''` escapes a quote.
- `model.tmdl` may hold `ref table X` ordering lines (written by TOM); kept in sync.
- Measure names are unique model-wide and may not clash with a column in their table.

Verified: TMDL written by this CLI loads in Microsoft TOM (Power BI modeling MCP
`ConnectFolder`) and TOM re-serialises it byte-identically
(`tests/fixtures/TomExport.SemanticModel`).

## State model

- **Project**: TMDL files read into memory; mutations snapshot for undo/redo;
  one-shot commands auto-save on exit unless `--dry-run`; REPL saves on `project save`.
- **Context** (`~/.cli-anything-powerbi/session.json`, override with
  `CLI_ANYTHING_POWERBI_HOME`): remembered project, default workspace and dataset.
- **Auth** (`token_cache.json` in the same dir): MSAL cache. Precedence:
  `POWERBI_ACCESS_TOKEN` → service principal (`POWERBI_TENANT_ID` /
  `POWERBI_CLIENT_ID` / `POWERBI_CLIENT_SECRET`) → cached user from `auth login`.

## Not covered (by design, add when needed)

- Conditional formatting (rules/gradients), bookmarks, drillthrough, mobile layout, maps
  (need Bing), AI visuals (decomposition tree, key influencers, Q&A).
- Embedding custom fonts: themes can only name fonts installed where the report is viewed
  (`theme validate` warns on fonts outside the Power BI safe list).
- Local DAX against a running Desktop instance (needs ADOMD/XMLA).
- Deploying a PBIP to the service (Fabric Items API / `DeployToFabric`).
- Previews: Power BI has no headless renderer, so no `preview` group.
