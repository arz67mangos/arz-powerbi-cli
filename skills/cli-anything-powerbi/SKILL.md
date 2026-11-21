---
name: "cli-anything-powerbi"
description: "Edit Power BI PBIP/TMDL projects (tables, DAX measures, relationships), import and clean data with Power Query M (CSV/Excel, types, merge, append), style reports with JSON themes and card layouts, generate dashboard pages (KPI cards, slicers, trend and comparison charts) from column types, add advanced and custom visuals (.pbiviz/AppSource) with formatting, and drive the Power BI REST API (workspaces, datasets, refresh, DAX queries), with --json output for agents."
---

# cli-anything-powerbi

Command-line harness for Power BI: edit **PBIP / TMDL** projects (tables, DAX measures, relationships), import and clean data with **Power Query (M)** (CSV/Excel, types, merges, appends), and drive the **Power BI REST API** (workspaces, datasets, refresh, DAX queries). Every command supports `--json` for agents.

## Installation

```bash
cd powerbi/agent-harness && pip install -e .
```

**Prerequisites:**
- Python 3.10+ (installs click, prompt-toolkit, requests, msal, openpyxl)
- Power BI Desktop (to open/refresh projects): `winget install Microsoft.PowerBI`
- Projects saved as PBIP with TMDL (Desktop: Options > Preview features > TMDL). `model.bim` is rejected.
- For service commands: `auth login` (device code), or `POWERBI_ACCESS_TOKEN`, or service principal env vars `POWERBI_TENANT_ID` / `POWERBI_CLIENT_ID` / `POWERBI_CLIENT_SECRET`.

## Usage

```bash
cli-anything-powerbi --help
cli-anything-powerbi                       # REPL (undo/redo/save/status/quit)
cli-anything-powerbi --json -p Demo.pbip project info
```

Global options: `--json`, `-p/--project PATH` (.pbip, its folder, or *.SemanticModel; env `POWERBI_PROJECT`),
`-w/--workspace ID|NAME|me` (env `POWERBI_WORKSPACE`), `--dry-run` (don't write project files).

## Command Groups

### Cli

Power BI from the command line: edit PBIP/TMDL projects and drive the
    Power BI service (workspaces, datasets, refresh, DAX queries).

    Run without a subcommand to start the interactive REPL.

| Command | Description |
|---------|-------------|
| `repl` | Interactive session (edits stay in memory until 'project save'). |

### Project

Create, open and inspect PBIP projects.

| Command | Description |
|---------|-------------|
| `new` | Create a minimal PBIP (report + TMDL semantic model). |
| `open` | Open a project and remember it for later one-shot commands. |
| `close` | Forget the remembered project. |
| `info` | Summary counts for the open project. |
| `validate` | Structural checks: names, references, DAX bracket balance. |
| `pages` | List report pages. |
| `save` | Write pending changes (REPL; one-shot commands auto-save). |

### Table

Tables in the semantic model.

| Command | Description |
|---------|-------------|
| `list` | List tables with column/measure counts. |
| `show` | Columns, measures and partitions of one table. |
| `add` | Add a calculated table defined by DAX. |
| `remove` | Remove a table (blocked while a relationship uses it). |

### Measure

DAX measures.

| Command | Description |
|---------|-------------|
| `list` | List measures, optionally for one table. |
| `show` | Show one measure with all properties. |
| `add` | Add a measure: measure add Sales "Total Sales" "SUM(Sales[Amount])". |
| `update` | Change a measure's expression, format, folder, description or name. |
| `remove` | Remove a measure. |

### Relationship

Relationships between tables.

| Command | Description |
|---------|-------------|
| `list` | List relationships. |
| `add` | Add FROM (many side, e.g. Sales.ProductKey) -> TO (one side, e.g. Product.ProductKey). |
| `remove` | Remove a relationship by id. |

### Import

Get Data: create Power Query tables from CSV / Excel files.

| Command | Description |
|---------|-------------|
| `csv` | Import a CSV: Csv.Document -> Promote Headers -> Changed Type (types inferred). |
| `excel` | Import an Excel sheet or table via Excel.Workbook (types inferred from cells). |

### Query

View and edit a table's Power Query (M) steps; merge and append queries.

| Command | Description |
|---------|-------------|
| `show` | Print the full M query (Advanced Editor view). |
| `steps` | List applied steps. |
| `set` | Replace the whole M query (M, '-' for stdin, or --file). Column list is not changed. |
| `add-step` | Append a raw M step; {prev} = previous step, e.g. 'Table.Sort({prev}, {{"Date", Order.Ascending}})'. |
| `remove-step` | Remove a step; later steps are rewired to the one before it. |
| `merge` | Merge Queries: join RIGHT into LEFT and expand its columns. |
| `append` | Append Queries: stack OTHERS under TARGET (Table.Combine). |

### Clean

Data cleaning steps (each adds a Power Query step and updates column metadata).

| Command | Description |
|---------|-------------|
| `set-type` | Change column types, e.g. Amount=decimal Date=date (text|int|number|decimal|date|datetime|bool). |
| `rename` | Rename columns: clean rename Sales "cust id=Customer ID". |
| `remove-columns` | Remove columns from the query and the model. |
| `filter` | Keep rows matching an M condition: clean filter Sales '[Amount] > 0 and [Region] <> null'. |
| `replace` | Replace values: substring for text (unless --whole); blank '' and 'null' match whole cells. |
| `text` | Text transforms: clean text Sales trim Product Region. |
| `dedupe` | Remove duplicate rows (optionally judged on COLUMNS only). |
| `fill-down` | Fill nulls with the value above. |
| `add-column` | Add a custom column: clean add-column Sales Net '[Amount] * 0.9' --type number. |

### Theme

Report themes: palettes, fonts and the card-style look.

| Command | Description |
|---------|-------------|
| `presets` | List built-in theme presets. |
| `palettes` | List named colour palettes. |
| `create` | Write a theme JSON file (importable via Design > Themes > Import theme). |
| `apply` | Apply a theme JSON FILE, or generate one from --preset/--palette/... and apply it. |
| `show` | Show the report's current custom theme. |
| `remove` | Remove the custom theme (back to the default theme). |
| `validate` | Check a theme JSON file. |

### Page

Report pages and consistent grid layouts.

| Command | Description |
|---------|-------------|
| `list` | List pages in order. |
| `add` | Add an empty page. |
| `remove` | Remove a page and its visuals. |
| `build` | Lay out a page on a consistent grid: title, slicers, KPI cards, charts (2 per row), table. |
| `suggest` | Suggest visuals for a table from its column types (and sampled source values). |
| `template` | Generate a full dashboard page (KPI cards, slicers, charts, table) from the model. |
| `generate` | Build a themed multi-page report from a JSON data description. |

### Visual

Visuals on a page.

| Command | Description |
|---------|-------------|
| `list` | List visuals on a page with position and fields. |
| `add` | Place one visual. KIND: a built-in type (see 'visual types'), textbox, or a custom visual. |
| `remove` | Remove a visual by id (see 'visual list'). |
| `types` | Visual types and their data roles (! = required, m = summed measure, g = grouping). |
| `format` | Set formatting: OBJECT.PROPERTY=VALUE ... (legend.position=Bottom labels.show=true title.text='Sales'). |
| `color` | Set data colours: whole visual, one series, or one category value. |
| `sort` | Sort a visual by one of its fields. |
| `move` | Move/resize a visual. |

### Visual Custom

Custom visuals: .pbiviz files and AppSource visuals.

| Command | Description |
|---------|-------------|
| `import` | Import a .pbiviz into the report (like Desktop's 'Import a visual from a file'). |
| `add-public` | Register an AppSource visual by its GUID (Desktop downloads it when the report opens). |
| `list` | Custom visuals registered in the report. |
| `remove` | Remove a custom visual (by guid or display name). |

### Auth

Sign in to the Power BI service.

| Command | Description |
|---------|-------------|
| `login` | Device-code sign-in (token cached in ~/.cli-anything-powerbi). |
| `status` | Show auth mode and signed-in account. |
| `logout` | Delete the cached token. |

### Workspace

Workspaces (groups).

| Command | Description |
|---------|-------------|
| `list` | List workspaces you can access. |
| `use` | Set the default workspace (id, name, or 'me'). |
| `current` | Show the default workspace and dataset. |

### Dataset

Datasets (semantic models) in the service.

| Command | Description |
|---------|-------------|
| `list` | List datasets in the workspace. |
| `info` | Show dataset details. |
| `use` | Set the default dataset for refresh/dax commands. |
| `refresh` | Trigger a dataset refresh. |
| `refresh-history` | Show recent refreshes and their status. |

### Report

Reports in the service.

| Command | Description |
|---------|-------------|
| `list` | List reports in the workspace. |

### Dax

Run DAX queries against a published dataset.

| Command | Description |
|---------|-------------|
| `query` | dax query "EVALUATE TOPN(10, Sales)" — uses the executeQueries REST API. |

### Desktop

Power BI Desktop integration.

| Command | Description |
|---------|-------------|
| `find` | Locate the Power BI Desktop executable. |
| `open` | Open a .pbip in Power BI Desktop (defaults to the open project). |

### Session

Session state, undo/redo.

| Command | Description |
|---------|-------------|
| `status` | Show open project, unsaved files, undo depth, context. |
| `undo` | Undo the last model edit (REPL). |
| `redo` | Redo the last undone edit (REPL). |

## Examples

### Author measures in a PBIP project

```bash
cli-anything-powerbi project open ./Sales/Sales.pbip          # remembered for later calls
cli-anything-powerbi --json table list
cli-anything-powerbi measure add Sales "Total Sales" "SUM(Sales[Amount])" --format "#,0.00" --folder KPIs
printf 'VAR n = COUNTROWS(Sales)\nRETURN DIVIDE([Total Sales], n)' | cli-anything-powerbi measure add Sales "Avg Sale" -
cli-anything-powerbi measure update "Avg Sale" --format "0.0" --description "Average per row"
cli-anything-powerbi --json project validate                  # exit 2 if errors
cli-anything-powerbi desktop open
```

### Import and clean data (Power Query)

```bash
cli-anything-powerbi import csv ./orders.csv -t Orders --type "Order Date=date"
cli-anything-powerbi import excel ./products.xlsx --sheet Products     # or --item tblProducts
cli-anything-powerbi clean text Orders trim Product
cli-anything-powerbi clean dedupe Orders
cli-anything-powerbi clean replace Orders Region "" Unknown
cli-anything-powerbi clean set-type Orders Price=decimal
cli-anything-powerbi clean rename Orders Qty=Quantity
cli-anything-powerbi clean add-column Orders "Line Total" "[Quantity] * [Price]" --type decimal
cli-anything-powerbi clean filter Orders "[Quantity] > 0"
cli-anything-powerbi query append Orders OrdersFeb                     # or --as-new "All Orders"
cli-anything-powerbi query merge Orders Products --on Product -e Category
cli-anything-powerbi --json query show Orders     # {"m": ..., "steps": [...], "result": ...}
cli-anything-powerbi query remove-step Orders "Filtered Rows"
```

### Theme and lay out a report page

```bash
cli-anything-powerbi --json theme presets
cli-anything-powerbi theme apply --preset modern            # or: theme apply ./brand.json
cli-anything-powerbi theme create brand.json -n Brand --preset warm --palette "#0F766E,#F59E0B" --font Georgia
cli-anything-powerbi page build "Overview" --title "Sales Overview" --card "[Revenue]" --card "[Orders]"     --chart "column:Orders.Product:[Revenue]:Revenue by product" --chart "donut:Orders.Category:[Units]"     --table "Orders.Order Date,Orders.Product,[Revenue]"
cli-anything-powerbi visual add Overview line -f "[Revenue]" -c "Orders.Order Date" --x 24 --y 400 -W 600 -H 280
cli-anything-powerbi --json visual list Overview
```

### Advanced charts, formatting and custom visuals

```bash
cli-anything-powerbi --json visual types            # roles per type: m = measure, g = grouping, ! = required
cli-anything-powerbi --json visual add Overview combo -c Sales.Product -r "Y=[Revenue]" -r "Y2=[Margin]"
cli-anything-powerbi --json visual add Overview gauge -f "[Revenue]" -r "TargetValue=[Target]"
cli-anything-powerbi visual format Overview <id> legend.position=Bottom labels.show=true
cli-anything-powerbi visual color Overview <id> "#F59E0B" --point East
cli-anything-powerbi visual sort Overview <id> "[Revenue]"
cli-anything-powerbi --json visual custom import ./chart.pbiviz      # -> guid + roles
cli-anything-powerbi visual custom add-public ChicletSlicer1448559807354
cli-anything-powerbi visual props lineChart                        # format objects/properties for a type
cli-anything-powerbi visual add Overview slicer -c People.Gender -t Gender   # title hides the slicer header
cli-anything-powerbi visual add Overview textbox -f "Headline" -f "Second line" --size 11 --no-bold \n    --color "#0F766E" --align center --bg "#FFFFFF" --border "#0F766E" --radius 20 -H 60
cli-anything-powerbi page format Overview --background "#E3F2EF"   # or background.color=... outspace.color=...
```

### Generate dashboards from the model

```bash
cli-anything-powerbi --json page suggest Sales      # kpis, slicers, trend, comparisons, reasons
cli-anything-powerbi page template Overview -t Sales --create-measures --monthly
cli-anything-powerbi page template "By Region" -t Sales -k comparison
cat > dashboard.json <<'EOF'
{"theme": "modern", "create_measures": true, "monthly": true,
 "pages": [{"name": "Overview", "table": "Sales"},
           {"name": "Trends", "table": "Sales", "template": "trend"},
           {"name": "Mix", "table": "Sales", "template": "comparison", "slicers": ["Products.Category"]}]}
EOF
cli-anything-powerbi --json page generate dashboard.json   # add --replace to rebuild
```

### Build a star schema

```bash
cli-anything-powerbi table add Product --dax 'DATATABLE("Product",STRING,"Category",STRING,{{"Widget","Tools"}})' -c Product:string -c Category:string
cli-anything-powerbi relationship add Sales.Product Product.Product   # many -> one
```

### Refresh and query a published dataset

```bash
cli-anything-powerbi auth login
cli-anything-powerbi --json workspace list
cli-anything-powerbi workspace use "Sales Analytics"
cli-anything-powerbi dataset use "Sales"
cli-anything-powerbi --json dataset refresh --wait
cli-anything-powerbi --json dax query "EVALUATE SUMMARIZECOLUMNS(Sales[Product], \"Total\", [Total Sales])" --csv out.csv
```

## For AI Agents

1. **Always use `--json`** for parseable output.
2. **Errors** come back as `{"error": "...", "type": "..."}` on stdout with exit 1 (`project validate` exits 2 when invalid).
3. **Edits auto-save** in one-shot mode; pass `--dry-run` to preview. Run `project validate` after edits.
4. Measure names are model-wide; `measure update/remove` find the table automatically.
5. `clean` / `query merge` / `query append` keep the model's column list in sync with the M output. Raw `query add-step` / `query set` don't, so check with `table show`.
6. M conditions and expressions use Power Query syntax (`[Col] > 0`, `Text.Contains([Name], "x")`). `each` is added automatically; in `add-step`, `{prev}` means the previous step.
7. Types: text, int, number, decimal, date, datetime, bool. Data loads when Power BI refreshes (in Desktop, or with `dataset refresh` after publishing).
8. DAX result rows use keys like `"Sales[Product]"` and `"[Total]"`.
9. Use absolute paths for CSV/Excel imports; the path is stored in the M query.
10. Fields for visuals: `[Measure]`, `Table[Measure]`, `Table.Column`, `sum(Table.Column)` (avg/min/max/count/distinctcount). Numeric columns in values are summed automatically.
11. `page build` makes a consistent grid (title, KPI cards, charts 2 per row, table). Use `--replace` to rebuild a page; the theme supplies the card look, so don't style visuals one by one.
12. Report commands need the PBIR format (new projects have it). Older projects: open and save once in Desktop.
13. For a dashboard, start with `page suggest TABLE` to see the plan, then `page template` (one page) or `page generate spec.json` (theme + pages). Relate dimension tables first so their categories are used. Use `--monthly` for date trends on import tables.
14. Textboxes: each -f is a paragraph; keep height >= the warned minimum (~40 px for one 11pt line) or Desktop clips it. Page colours: `page format PAGE --background #hex`. Never put `objects`/`visualContainerObjects` at the root of visual.json by hand; `project validate` flags it.
15. Charts beyond the basics: check `visual types` for role names and `visual props TYPE` for format properties, then bind with `-r ROLE=FIELD`. Formatting is `object.property=value` (container cards like title/background/border are routed automatically). Custom visuals must be imported or registered before `visual add` can use them; AppSource visuals accept any role name, so check the visual's docs.
16. Not supported: conditional formatting, bookmarks, maps, local DAX against Desktop, deploying PBIP to the service, query parameters.

## Version

1.0.0