# cli-anything-powerbi

Command-line harness for Power BI: edit **PBIP / TMDL** projects (tables, DAX
measures, relationships), import and clean data with **Power Query (M)** (CSV/Excel,
types, merges, appends), and drive the **Power BI REST API** (workspaces,
datasets, refresh, DAX queries). Every command supports `--json` for agents.

## Requirements

- Python ≥ 3.10
- **Power BI Desktop** (to open/render projects): `winget install Microsoft.PowerBI`
  or the Microsoft Store. Save projects with *File > Save as > Power BI project (.pbip)*
  and enable *Options > Preview features > Store semantic model using TMDL format*.
- A Power BI / Fabric account for service commands.

## Install

```bash
cd powerbi/agent-harness
pip install -e .
cli-anything-powerbi --help
```

If `cli-anything-powerbi` is not found, add pip's scripts folder to PATH
(on Windows user installs: `%APPDATA%\Python\Python3XX\Scripts`).

## Auth (service commands)

| Method | How |
|---|---|
| User sign-in | `cli-anything-powerbi auth login` (device code) or `auth login --browser` |
| Service principal | set `POWERBI_TENANT_ID`, `POWERBI_CLIENT_ID`, `POWERBI_CLIENT_SECRET` |
| Raw token | set `POWERBI_ACCESS_TOKEN` |

Tokens are cached in `~/.cli-anything-powerbi/token_cache.json`
(`CLI_ANYTHING_POWERBI_HOME` overrides the folder).

## Usage

```bash
# Projects (one-shot commands auto-save; add --dry-run to preview)
cli-anything-powerbi project new ./demo -n Demo
cli-anything-powerbi project open ./demo/Demo.pbip      # remembered for later commands
cli-anything-powerbi measure add Sales "Total Sales" "SUM(Sales[Amount])" --format "#,0.00" --folder KPIs
cli-anything-powerbi measure update "Total Sales" --description "Gross revenue"
cli-anything-powerbi table add Product --dax 'DATATABLE("Product",STRING,{{"Widget"}})' -c Product:string
cli-anything-powerbi relationship add Sales.Product Product.Product
cli-anything-powerbi --json project validate              # exit 2 on errors
cli-anything-powerbi desktop open                         # open in Power BI Desktop

# Data import & cleaning (Power Query M, refreshed by Power BI)
cli-anything-powerbi import csv ./orders.csv -t Orders              # types inferred; --type Qty=int
cli-anything-powerbi import excel ./products.xlsx --sheet Products  # or --item tblProducts
cli-anything-powerbi clean text Orders trim Product
cli-anything-powerbi clean dedupe Orders
cli-anything-powerbi clean replace Orders Region "" Unknown
cli-anything-powerbi clean set-type Orders Price=decimal "Order Date=date"
cli-anything-powerbi clean rename Orders Qty=Quantity
cli-anything-powerbi clean add-column Orders "Line Total" "[Quantity] * [Price]" --type decimal
cli-anything-powerbi clean filter Orders "[Quantity] > 0"
cli-anything-powerbi query append Orders OrdersFeb                  # or --as-new "All Orders"
cli-anything-powerbi query merge Orders Products --on Product -e Category
cli-anything-powerbi query steps Orders
cli-anything-powerbi query show Orders                              # full M (Advanced Editor)
cli-anything-powerbi query remove-step Orders "Filtered Rows"

# Themes & layout (PBIR report)
cli-anything-powerbi theme presets                                  # modern, dark, corporate, warm, accessible
cli-anything-powerbi theme apply --preset modern                    # rounded shadowed cards on a tinted page
cli-anything-powerbi theme create brand.json -n Brand --palette "#0F766E,#F59E0B" --font Georgia --radius 8
cli-anything-powerbi theme apply brand.json                         # any theme JSON file
cli-anything-powerbi page build "Sales Overview" --title "Sales Overview" \
    --card "[Revenue]" --card "[Orders]" \
    --chart "column:Orders.Product:[Revenue]:Revenue by product" \
    --chart "donut:Orders.Category:[Units]" --table "Orders.Order Date,Orders.Product,[Revenue]"
cli-anything-powerbi visual add "Sales Overview" line -f "[Revenue]" -c "Orders.Order Date" --x 24 --y 400
cli-anything-powerbi visual list "Sales Overview"

# Dashboard templates (chart types picked from column types)
cli-anything-powerbi page suggest Sales                            # ranked visuals + reasons
cli-anything-powerbi page template Overview -t Sales --create-measures --monthly
cli-anything-powerbi page template Trends -t Sales -k trend --monthly
cli-anything-powerbi page generate dashboard.json                  # theme + several pages
cli-anything-powerbi page build Mix --slicer Sales.Region --card "[Sales Count]"

# Advanced charts, formatting, custom visuals
cli-anything-powerbi visual types                                  # every type and its data roles
cli-anything-powerbi visual add Overview combo -c Sales.Month -r "Y=[Revenue]" -r "Y2=[Margin %]"
cli-anything-powerbi visual add Overview scatter -r "X=[Revenue]" -r "Y=[Units]" -r Category=Sales.Product
cli-anything-powerbi visual add Overview matrix -r Rows=Sales.Region -r Columns=Sales.Channel -r "Values=[Revenue]"
cli-anything-powerbi visual format Overview <id> legend.position=Bottom labels.show=true title.fontSize=14
cli-anything-powerbi visual color Overview <id> "#0EA5E9" --series "[Revenue]"
cli-anything-powerbi visual sort Overview <id> "[Revenue]"
cli-anything-powerbi visual custom import ./MyVisual.pbiviz         # roles read from the package
cli-anything-powerbi visual add Overview "My Visual" -r category=Sales.Product -r "measure=[Revenue]"
cli-anything-powerbi visual props lineChart                        # format objects/properties for a type
cli-anything-powerbi visual add Overview slicer -c People.Gender -t Gender   # title hides the slicer header
cli-anything-powerbi visual add Overview textbox -f "Headline" -f "Second line" --size 11 --no-bold \n    --color "#0F766E" --align center --bg "#FFFFFF" --border "#0F766E" --radius 20 -H 60
cli-anything-powerbi page format Overview --background "#E3F2EF"   # or background.color=... outspace.color=...
cli-anything-powerbi project check-visuals Overview               # clipped text, overlaps, off-page, filterConfig
cli-anything-powerbi desktop reload --page Overview --screenshot shot.png   # close (no save), reopen, refresh data, capture
cli-anything-powerbi desktop verify --out-dir shots     # reload + refresh, PNG of every page, check-visuals
cli-anything-powerbi desktop screenshot shot.png                  # capture the open Desktop window

# Service
cli-anything-powerbi auth login
cli-anything-powerbi workspace list
cli-anything-powerbi workspace use "Sales Analytics"
cli-anything-powerbi dataset use "Demo"
cli-anything-powerbi dataset refresh --wait
cli-anything-powerbi --json dax query "EVALUATE TOPN(10, Sales)" --csv top10.csv

# REPL (default with no subcommand): undo / redo / save / status / quit
cli-anything-powerbi -p ./demo/Demo.pbip
```

## Tests

```bash
pip install -e .[test]
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/powerbi/tests -v -s
# live service tests (optional, after auth login):
POWERBI_LIVE=1 POWERBI_LIVE_DATASET=<id> POWERBI_LIVE_WORKSPACE=<id> python -m pytest -k Live
```

## Limits

Report editing covers themes, pages, 25 visual types, formatting, colours, sorting and custom
visuals, but not conditional formatting, bookmarks or maps. Custom-visual storage and the
advanced types were checked in Desktop. There's no local DAX
against a running Desktop instance, no PBIP deploy, and `model.bim` (TMSL) models are
rejected. See `POWERBI.md`.
