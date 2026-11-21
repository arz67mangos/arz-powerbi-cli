# cli-anything-powerbi — Test Plan & Results

## Test inventory

| File | Planned | Scope |
|---|---|---|
| `test_core.py` | 133 | tmdl parser/editor, project, session/undo, DAX sanity check, REST client vs. stub server |
| `test_full_e2e.py` | 32 | real Microsoft-serialised TMDL fixture, full PBIP workflows, installed-CLI subprocess runs, Power BI Desktop discovery, live Power BI service (opt-in) |

## Unit plan (`test_core.py`)

- **tmdl.py** — quote/unquote names (spaces, embedded `'`), `split_ref`,
  parse single-line / multi-line / fenced measure expressions, descriptions,
  add (insertion point, duplicate rejection, empty expr), update (format/folder
  clear, rename, keeps lineageTag), remove (no stray blank lines), relationships
  parse/add/remove, calculated-table generation.
- **project.py** — `create_project` layout and JSON validity, `locate` for
  .pbip / folder / .SemanticModel, model.bim rejection, measure name uniqueness
  model-wide, relationship column checks, `ref table` sync, table removal blocked
  by relationships, `validate` issues (duplicate, bad reference, unbalanced DAX).
- **session.py** — snapshot/undo/redo, context persistence with `_locked_save_json`.
- **powerbi_backend.py** — against a local stdlib HTTP stub: auth header,
  workspace/dataset name resolution, refresh 202 + RequestId, `wait_refresh`
  polling, executeQueries row shaping, HTTP error surfacing, missing-auth error.

## E2E plan (`test_full_e2e.py`)

- **Real TMDL fixture**: `fixtures/TomExport.SemanticModel` was exported by
  Microsoft TOM (Power BI modeling MCP). Parse it, edit it on a copy, check that
  untouched files stay byte-identical.
- **Desktop backend**: `find_desktop()` returns the installed Power BI Desktop
  executable (hard requirement — fails if not installed).
- **Subprocess (installed `cli-anything-powerbi`)**: `--help`, `project new`,
  JSON output for every read command, auto-save vs `--dry-run`, error JSON + exit
  codes, validate exit code 2, REST commands against the stub server via
  `POWERBI_API_BASE`, DAX → CSV.
- **Live service** (`POWERBI_LIVE=1` + signed in): list workspaces/datasets,
  run `EVALUATE ROW("x", 1)` on `POWERBI_LIVE_DATASET`.

## Workflow scenarios

1. **Measure authoring** — simulates an analyst adding KPIs: new project →
   add 3 measures (one multi-line) → update format → rename → validate → reopen
   and verify persisted TMDL.
2. **Star schema build-out** — add dimension calculated table → relationship
   from fact → validate → attempt to delete dimension (blocked) → remove
   relationship → delete table.
3. **Undo/redo** — mutations, undo past start, redo, new mutation clears redo
   (`test_undo_redo`); REPL script add/add/undo/save verified on disk (`test_repl_scripted`).
4. **Refresh & query ops** — `workspace use` by name → `dataset use` by name →
   `dataset refresh --wait` → `dax query --csv` → CSV content verified.

## Refinement 1 plan: data import and cleaning (Power Query)

- **Unit (`test_core.py`, +29)**: `powerquery.py` covers `let/in` parsing (strings with
  commas, comments, nested `let`, `#""` names, non-let queries, malformed input),
  build round trip, step append (unique names, expression result), step removal with
  reference rewiring, every step builder (incl. replace semantics), type mapping,
  text type inference, CSV sniffing (`;`, duplicate/blank headers, empty file) and Excel
  sniffing (sheet, ListObject, native date/datetime/bool), Excel M shapes. `tmdl.py` covers
  M source get/set (plain + fenced) and column add/remove/rename/retype. `project.py`
  covers CSV import with overrides, schema sync across 8 cleaning steps, errors (unknown
  column, DAX table, relationship-guarded column), merge (expand, clash prefix,
  anti join, bad kind), append (into and `--as-new`), raw step edits and validate.
- **E2E (`test_full_e2e.py`, +5)**: `fixtures/TomPowerQuery.SemanticModel` is a
  CLI-built import/clean/merge/append model **re-serialised by Microsoft TOM**. The tests
  parse it and edit a copy, checking that untouched files stay byte-identical. Installed-CLI
  workflows: the full data-prep pipeline (CSV + Excel → trim/proper/dedupe/replace/type/
  rename/custom/filter → append → merge), with the generated M and column list checked;
  step add/remove, `--dry-run` and error JSON; append-as-new plus an anti-join.
- **Workflow 5, "monthly sales data prep"**: simulates an analyst combining two CSV
  extracts with an Excel product list. Verified in **Power BI Desktop** (manual refresh,
  see results below).

---

## Refinement 2 plan: themes, styling and page layout

- **Unit (`test_core.py`, +17)**: `theme.py` covers palette resolution (names, hex lists,
  bad input), every preset building a valid theme with no font warnings and its card radius
  and shadow, overrides (palette, fonts, colours, radius 0, `card_style=False`), error cases,
  `validate_theme` (5 problem kinds), font warnings and bad JSON files. `report.py` covers
  theme apply/replace/remove with the Desktop-style RegisteredResources layout surviving a
  save and reopen, a missing theme file flagged by validate, the legacy `report.json` refusal,
  page add/remove (duplicates, the last page), field resolution (measure, `T[M]`, auto-sum,
  explicit aggregations, case-insensitive names, four error kinds), visual JSON shape
  (roles, queryRef, position, title), textboxes, visual remove, the `page build` grid
  (margins, gaps, equal widths, full-width table, active page, `--replace`, overflow
  refusal), validate catching a visual whose measure was deleted, and undo of report edits.
  `test_create_project_layout` now checks for the PBIR layout.
- **E2E (`test_full_e2e.py`, +2)**: installed-CLI runs. (1) Theme file round trip:
  `presets`, `palettes`, `create` with overrides, `validate` exit 2 on a bad file, `apply`
  refusing an invalid theme, then applying the file and checking that the RegisteredResources
  copy matches it byte for byte. (2) Styled dashboard workflow: a `--dry-run` apply writes
  nothing, then the modern preset is applied, `page build` makes a 6-visual page, and
  `visual list` and `page list` are checked. Visual add/remove, an error for an unknown
  measure, page add/remove, validate going invalid after a measure is removed, and theme
  remove are also exercised.
- **Workflow 6, "styled sales dashboard"**: an analyst brands the report and lays out an
  overview page. Verified in **Power BI Desktop** (see results).

---

## Refinement 3 plan: report templates

- **Unit (`test_core.py`, +18)**: `suggest.py` covers the column-role rules (9 names and types:
  ID/Key/code, flag, date, year, numeric, category), profiling with cardinality sampled from the
  CSV source and from a related dimension, graceful fallback when the source file is gone,
  the suggestion rules (KPI choice and sum/avg, slicer pick and order, trend, donut vs bar,
  chart-type variety, keys excluded, reasons), existing measures preferred, a table with nothing
  to summarise, `auto_measures` (names, `0.0%`/`#,0` formats, COUNTROWS, idempotent, valid
  model), the three template layouts and their errors, a full template page (slicer modes
  Between/Dropdown, monthly Power Query column added once, everything inside the margins),
  `build_page` slicers and explicit rows, `field_type`, chart-spec parsing, and `generate`
  (theme file, per-page overrides, explicit chart rows, first page active, `--replace` guard,
  automatic pages that skip dimensions, and 3 spec errors).
- **E2E (`test_full_e2e.py`, +2)**: installed-CLI runs on a 60-row CSV plus an xlsx dimension.
  (1) `page suggest` JSON and text, `--dry-run` template saving nothing, `page template`
  with `--create-measures --monthly`, a trend page, the replace and no-date errors, a Basic
  slicer via `visual add --mode`, `page build --slicer`, and validate. (2) `page generate` with a
  3-page themed spec: pages, active page, theme colours, the replace guard, the `--replace` rebuild,
  a bad-spec error, and validate.
- **Workflow 7, "dashboard from a data description"**: an analyst has a sales extract and a
  product list and wants a themed 3-page report without placing visuals by hand. Verified in
  **Power BI Desktop** (see results).

---

## Refinement 4 plan: advanced and custom visuals

- **Unit (`test_core.py`, +22)**: `visuals.py` literal encoding (8 cases: bool, int, decimal,
  colour, text, forced text, quote escaping), setting parsing, role specs, unique visual types,
  custom GUID detection, `.pbiviz` reading (real Microsoft-built fixture, a non-zip, a missing
  resources file). Eight advanced types (combo, scatter, matrix, gauge, kpi, waterfall,
  treemap, stacked column) check role order and that measure roles are summed and grouping
  roles raw. Role errors (missing required, unknown role, no category role, no fields).
  Formatting (objects vs container cards, merge, reset), colours (all/series/point, replacing
  the same selector, bad colour, field not on the visual), sort, move, missing visual. Custom
  visual lifecycle: import, re-import without duplicates, AppSource registration, add by
  name, role checks, save/reopen, remove blocked while in use, `--force`, report.json clean
  afterwards. Role-aware validation: unknown role, missing required role, unregistered custom type.
- **E2E (`test_full_e2e.py`, +2)**: installed CLI. (1) `visual types`, then combo/matrix/gauge
  via `-r`, the missing-role and bad `--role` errors, `format`, `color --series/--point`, `sort`,
  `move`, a bad setting, `--dry-run`, validate. (2) `visual custom import` of the fixture
  `.pbiviz`, `add-public`, `list`, `types` showing it, adding it by display name, the files on disk,
  an unknown type, remove refused while in use, then removed with its folder gone.
- **Bug found by the E2E run**: saving left empty folders behind after files were deleted
  (removed custom visuals, pages, visuals). `save()` now prunes empty folders.
- **Desktop check (2026-11-18): done.**: Desktop's own import of the fixture wrote the same
  `CustomVisuals/<guid>/package.json` + `resources/<guid>.pbiviz.json` files, and one resource
  package item `{name/path: "<guid>.pbiviz.json", type: "CustomVisualMetadata"}`, which the CLI
  now writes too. A CLI-built page with combo, scatter, matrix, gauge, KPI, waterfall, treemap,
  ribbon, stacked column and funnel rendered with data. Legend, data labels, title size and
  ascending sort applied. `visual color` with no flag and with `--point` write the same selectors
  Desktop does. `--series` writes a `metadata` selector (legend, multi-series charts); for a lone
  measure in the main value role with no legend field, Desktop's `metadata` entry only reached the
  legend, so the CLI also writes the unselected entry that colours the bars (not re-rendered).
- **Fix from the Desktop check**: the resource-package items were guessed (`CustomVisualJson`/
  `CustomVisualContent`, one per file); now the single `CustomVisualMetadata` item Desktop writes,
  with a unit assertion on it. Desktop reopened the CLI-written project with the visual intact.

---

## Desktop verify round (2026-11-21): refresh, discard, verify, BOM

Asked for by the Data Fluency session:
- `visual color --point` on a visual with a legend now compares on the Series column (it used the axis).
- `desktop reload --refresh/--no-refresh` (default on) clicks the "Refresh now" bar by exact name and
  waits for the load; it says so when there is no such bar.
- `desktop reload --discard-changes` (opt-in) answers the save prompt with "Don't save" only. Without it
  the command still stops at the prompt. Force-killing Desktop was not added.
- `desktop verify [--pages all|P1,P2] [--out-dir DIR]`: reload, refresh, a PNG per page, check-visuals.
- `project validate` flags files that start with a UTF-8 BOM; saving rewrites them without one.
- Desktop checks (scratch project): refresh took 8.8-11.3 s and the bar went away; verify saved all 3
  pages; with unsaved changes the default reload stopped with the error, and `--discard-changes` clicked
  "Don't save", Desktop exited and reopened.

Tests: +2 (1 unit, plus asserts in the existing desktop unit and E2E tests). Clicking in Desktop isn't
in the automated suite because it launches Desktop.

---

## Desktop loop round (2026-11-21): reload, screenshot, layout lint

Asked for by the Data Fluency session (Desktop doesn't reload on-disk edits, so each pass needed a
manual close/reopen/screenshot):
- `desktop reload [PBIP] [--page P] [--screenshot out.png]`, `desktop screenshot out.png`, `desktop windows`.
- `project check-visuals [PAGE]`: textbox and slicer heights, off-page and partly overlapping visuals,
  filterConfig shape, plus the per-visual validate issues.
- Desktop checks (scratch project): reload closed and reopened Desktop on the chosen page and the PNG
  matched the screen; `labels.percentageLabelPrecision=0` rounds donut percentages (`labelPrecision`
  doesn't); `visual color --point` colours one scatter point.

Tests: +4 (3 unit, 1 E2E). The reload itself isn't in the automated suite because it launches Desktop.

---

## Feedback round (2026-11-21): fixes from building a real CA2 dashboard

Reported by the Data Fluency session after building a one-page dashboard in a Desktop-saved PBIP:
- `visual add slicer -c FIELD` failed; `-c`/`-f` now fill the slicer's `Values` role.
- A slicer with `-t` showed the title and the header ("Gender / Gender"); the header is now hidden.
- Textboxes were fixed at 20pt bold. They now have `--size --bold/--no-bold --color --align --bg --border
  --radius`, one paragraph per `-f`, and a warning when the height is under the size Desktop needs.
- `page format PAGE --background/--wallpaper` or `OBJECT.PROPERTY=VALUE` (no page formatting before).
- `project validate` now flags visual.json root keys the schema doesn't allow (Desktop refused such a file).
- `visual props TYPE` lists common format objects/properties (axis start/end, labelStyle, categoryLabels...).

Tests: +6 (5 unit, 1 E2E). Not rendered in Desktop this round; the header, textbox and page objects
follow the reporting session's hand edits, which Desktop accepted.

---

## Test Results (2026-11-21, after the Desktop verify round; Windows 11, Python 3.14, `CLI_ANYTHING_FORCE_INSTALLED=1`)

```
cli_anything/powerbi/tests/test_core.py::test_quote_roundtrip PASSED     [  0%]
cli_anything/powerbi/tests/test_core.py::test_split_ref PASSED           [  1%]
cli_anything/powerbi/tests/test_core.py::test_parse_table_basics PASSED  [  1%]
cli_anything/powerbi/tests/test_core.py::test_parse_measure_variants PASSED [  2%]
cli_anything/powerbi/tests/test_core.py::test_parse_column_props PASSED  [  2%]
cli_anything/powerbi/tests/test_core.py::test_parse_requires_table PASSED [  3%]
cli_anything/powerbi/tests/test_core.py::test_add_measure_after_last_measure PASSED [  4%]
cli_anything/powerbi/tests/test_core.py::test_add_measure_multiline_and_empty_table PASSED [  4%]
cli_anything/powerbi/tests/test_core.py::test_add_measure_rejects_duplicates_and_empty PASSED [  5%]
cli_anything/powerbi/tests/test_core.py::test_update_measure_keeps_lineage_and_other_props PASSED [  5%]
cli_anything/powerbi/tests/test_core.py::test_update_measure_clear_and_rename PASSED [  6%]
cli_anything/powerbi/tests/test_core.py::test_remove_measure PASSED      [  7%]
cli_anything/powerbi/tests/test_core.py::test_untouched_blocks_preserved PASSED [  7%]
cli_anything/powerbi/tests/test_core.py::test_relationships_roundtrip PASSED [  8%]
cli_anything/powerbi/tests/test_core.py::test_new_table_tmdl PASSED      [  8%]
cli_anything/powerbi/tests/test_core.py::test_create_project_layout PASSED [  9%]
cli_anything/powerbi/tests/test_core.py::test_locate_variants PASSED     [ 10%]
cli_anything/powerbi/tests/test_core.py::test_model_bim_rejected PASSED  [ 10%]
cli_anything/powerbi/tests/test_core.py::test_measure_names_model_wide PASSED [ 11%]
cli_anything/powerbi/tests/test_core.py::test_relationship_column_check_and_table_guard PASSED [ 11%]
cli_anything/powerbi/tests/test_core.py::test_ref_table_sync PASSED      [ 12%]
cli_anything/powerbi/tests/test_core.py::test_save_writes_only_dirty PASSED [ 13%]
cli_anything/powerbi/tests/test_core.py::test_validate PASSED            [ 13%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance[IF(1, "a(b", 2)-None] PASSED [ 14%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance[SUM('My (T)'[c])-None] PASSED [ 14%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance["x""y"-None] PASSED [ 15%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance[(-unclosed '('] PASSED [ 16%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance[)-unbalanced ')'] PASSED [ 16%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance["abc-unterminated string literal] PASSED [ 17%]
cli_anything/powerbi/tests/test_core.py::test_dax_balance[[a-unclosed '['] PASSED [ 17%]
cli_anything/powerbi/tests/test_core.py::test_report_pages PASSED        [ 18%]
cli_anything/powerbi/tests/test_core.py::test_undo_redo PASSED           [ 19%]
cli_anything/powerbi/tests/test_core.py::test_require_without_project PASSED [ 19%]
cli_anything/powerbi/tests/test_core.py::test_context_persistence PASSED [ 20%]
cli_anything/powerbi/tests/test_core.py::test_rest_listing_and_resolution PASSED [ 20%]
cli_anything/powerbi/tests/test_core.py::test_rest_refresh_and_wait PASSED [ 21%]
cli_anything/powerbi/tests/test_core.py::test_rest_enhanced_refresh_body PASSED [ 22%]
cli_anything/powerbi/tests/test_core.py::test_rest_dax PASSED            [ 22%]
cli_anything/powerbi/tests/test_core.py::test_rest_auth_errors PASSED    [ 23%]
cli_anything/powerbi/tests/test_core.py::test_parse_let_handles_strings_comments_nested PASSED [ 23%]
cli_anything/powerbi/tests/test_core.py::test_parse_let_non_let_and_malformed PASSED [ 24%]
cli_anything/powerbi/tests/test_core.py::test_build_roundtrip_and_ident_quoting PASSED [ 25%]
cli_anything/powerbi/tests/test_core.py::test_append_step_and_unique_names PASSED [ 25%]
cli_anything/powerbi/tests/test_core.py::test_append_step_when_result_is_expression PASSED [ 26%]
cli_anything/powerbi/tests/test_core.py::test_remove_step_rewires_references PASSED [ 26%]
cli_anything/powerbi/tests/test_core.py::test_step_builders PASSED       [ 27%]
cli_anything/powerbi/tests/test_core.py::test_resolve_type PASSED        [ 28%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals0-int] PASSED [ 28%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals1-number] PASSED [ 29%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals2-bool] PASSED [ 29%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals3-date] PASSED [ 30%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals4-datetime] PASSED [ 31%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals5-text] PASSED [ 31%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals6-text] PASSED [ 32%]
cli_anything/powerbi/tests/test_core.py::test_infer_text_type[vals7-number] PASSED [ 32%]
cli_anything/powerbi/tests/test_core.py::test_sniff_csv_semicolon_and_dupe_headers PASSED [ 33%]
cli_anything/powerbi/tests/test_core.py::test_sniff_excel_sheet_and_table PASSED [ 34%]
cli_anything/powerbi/tests/test_core.py::test_excel_query_shapes PASSED  [ 34%]
cli_anything/powerbi/tests/test_core.py::test_m_source_get_set_roundtrip PASSED [ 35%]
cli_anything/powerbi/tests/test_core.py::test_m_source_fenced_and_missing PASSED [ 35%]
cli_anything/powerbi/tests/test_core.py::test_column_ops PASSED          [ 36%]
cli_anything/powerbi/tests/test_core.py::test_import_csv_with_override PASSED [ 37%]
cli_anything/powerbi/tests/test_core.py::test_cleaning_keeps_schema_in_sync PASSED [ 37%]
cli_anything/powerbi/tests/test_core.py::test_cleaning_errors PASSED     [ 38%]
cli_anything/powerbi/tests/test_core.py::test_remove_column_blocked_by_relationship PASSED [ 38%]
cli_anything/powerbi/tests/test_core.py::test_merge_expands_and_types PASSED [ 39%]
cli_anything/powerbi/tests/test_core.py::test_append_into_and_as_new PASSED [ 40%]
cli_anything/powerbi/tests/test_core.py::test_query_raw_edit_and_remove_step PASSED [ 40%]
cli_anything/powerbi/tests/test_core.py::test_resolve_palette PASSED     [ 41%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_presets_valid[modern] PASSED [ 41%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_presets_valid[dark] PASSED [ 42%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_presets_valid[corporate] PASSED [ 43%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_presets_valid[warm] PASSED [ 43%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_presets_valid[accessible] PASSED [ 44%]
cli_anything/powerbi/tests/test_core.py::test_build_theme_overrides_and_errors PASSED [ 44%]
cli_anything/powerbi/tests/test_core.py::test_validate_theme_and_fonts PASSED [ 45%]
cli_anything/powerbi/tests/test_core.py::test_apply_and_remove_theme PASSED [ 46%]
cli_anything/powerbi/tests/test_core.py::test_missing_theme_file_reported PASSED [ 46%]
cli_anything/powerbi/tests/test_core.py::test_legacy_report_rejected PASSED [ 47%]
cli_anything/powerbi/tests/test_core.py::test_pages_add_remove PASSED    [ 47%]
cli_anything/powerbi/tests/test_core.py::test_field_resolution PASSED    [ 48%]
cli_anything/powerbi/tests/test_core.py::test_add_visual_shapes PASSED   [ 49%]
cli_anything/powerbi/tests/test_core.py::test_build_page_grid PASSED     [ 49%]
cli_anything/powerbi/tests/test_core.py::test_report_issues_flag_missing_fields PASSED [ 50%]
cli_anything/powerbi/tests/test_core.py::test_report_changes_undo PASSED [ 50%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[OrderID-int64-key] PASSED [ 51%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Customer Key-string-key] PASSED [ 52%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[product_code-string-key] PASSED [ 52%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Paid-boolean-flag] PASSED [ 53%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Order Date-dateTime-date] PASSED [ 53%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Year-int64-time] PASSED [ 54%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Quantity-int64-value] PASSED [ 55%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Unit Price-double-value] PASSED [ 55%]
cli_anything/powerbi/tests/test_core.py::test_column_roles[Region-string-category] PASSED [ 56%]
cli_anything/powerbi/tests/test_core.py::test_profile_samples_source_and_dimensions PASSED [ 56%]
cli_anything/powerbi/tests/test_core.py::test_suggest_rules PASSED       [ 57%]
cli_anything/powerbi/tests/test_core.py::test_suggest_prefers_measures_and_errors PASSED [ 58%]
cli_anything/powerbi/tests/test_core.py::test_auto_measures PASSED       [ 58%]
cli_anything/powerbi/tests/test_core.py::test_layouts PASSED             [ 59%]
cli_anything/powerbi/tests/test_core.py::test_template_page PASSED       [ 59%]
cli_anything/powerbi/tests/test_core.py::test_build_page_slicers_and_rows PASSED [ 60%]
cli_anything/powerbi/tests/test_core.py::test_parse_chart PASSED         [ 61%]
cli_anything/powerbi/tests/test_core.py::test_generate_spec PASSED       [ 61%]
cli_anything/powerbi/tests/test_core.py::test_literal[true-expected0] PASSED [ 62%]
cli_anything/powerbi/tests/test_core.py::test_literal[False-expected1] PASSED [ 62%]
cli_anything/powerbi/tests/test_core.py::test_literal[12-expected2] PASSED [ 63%]
cli_anything/powerbi/tests/test_core.py::test_literal[-1.5-expected3] PASSED [ 64%]
cli_anything/powerbi/tests/test_core.py::test_literal[#0ea5e9-expected4] PASSED [ 64%]
cli_anything/powerbi/tests/test_core.py::test_literal[Bottom-expected5] PASSED [ 65%]
cli_anything/powerbi/tests/test_core.py::test_literal['12'-expected6] PASSED [ 65%]
cli_anything/powerbi/tests/test_core.py::test_literal[it's-expected7] PASSED [ 66%]
cli_anything/powerbi/tests/test_core.py::test_parse_setting_and_roles PASSED [ 67%]
cli_anything/powerbi/tests/test_core.py::test_read_pbiviz PASSED         [ 67%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[combo-roles0-expect_state0] PASSED [ 68%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[scatter-roles1-expect_state1] PASSED [ 68%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[matrix-roles2-expect_state2] PASSED [ 69%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[gauge-roles3-expect_state3] PASSED [ 70%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[kpi-roles4-expect_state4] PASSED [ 70%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[waterfall-roles5-expect_state5] PASSED [ 71%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[treemap-roles6-expect_state6] PASSED [ 71%]
cli_anything/powerbi/tests/test_core.py::test_advanced_types[stackedcolumn-roles7-expect_state7] PASSED [ 72%]
cli_anything/powerbi/tests/test_core.py::test_role_errors PASSED         [ 73%]
cli_anything/powerbi/tests/test_core.py::test_format_color_sort_move PASSED [ 73%]
cli_anything/powerbi/tests/test_core.py::test_custom_visual_lifecycle PASSED [ 74%]
cli_anything/powerbi/tests/test_core.py::test_validate_visual_roles PASSED [ 74%]
cli_anything/powerbi/tests/test_core.py::test_slicer_category_and_title PASSED [ 75%]
cli_anything/powerbi/tests/test_core.py::test_textbox_styling PASSED     [ 76%]
cli_anything/powerbi/tests/test_core.py::test_format_page PASSED         [ 76%]
cli_anything/powerbi/tests/test_core.py::test_validate_visual_root_keys PASSED [ 77%]
cli_anything/powerbi/tests/test_core.py::test_props_for PASSED           [ 77%]
cli_anything/powerbi/tests/test_core.py::test_check_visuals PASSED       [ 78%]
cli_anything/powerbi/tests/test_core.py::test_check_visuals_clean_and_active_page PASSED [ 79%]
cli_anything/powerbi/tests/test_core.py::test_desktop_window_helpers PASSED [ 79%]
cli_anything/powerbi/tests/test_core.py::test_color_point_uses_series_column PASSED [ 80%]
cli_anything/powerbi/tests/test_core.py::test_bom_files_flagged_and_rewritten PASSED [ 80%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTomFixture::test_parse_tom_export PASSED [ 81%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTomFixture::test_edit_copy_preserves_untouched_files PASSED [ 82%]
cli_anything/powerbi/tests/test_full_e2e.py::TestDesktopBackend::test_find_desktop PASSED [ 82%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 83%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_project_new_json PASSED [ 83%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_measure_authoring_workflow PASSED [ 84%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_dry_run_does_not_save PASSED [ 85%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_star_schema_workflow PASSED [ 85%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_project_open_is_remembered PASSED [ 86%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_validate_exit_code PASSED [ 86%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_errors_are_json PASSED [ 87%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_repl_scripted PASSED [ 88%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_desktop_find PASSED [ 88%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_service_workflow PASSED [ 89%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_service_failed_refresh_exits_nonzero PASSED [ 89%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_dax_error_json PASSED [ 90%]
cli_anything/powerbi/tests/test_full_e2e.py::TestCLISubprocess::test_not_signed_in PASSED [ 91%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTomPowerQueryFixture::test_parse_tom_m_partitions PASSED [ 91%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTomPowerQueryFixture::test_edit_tom_copy PASSED [ 92%]
cli_anything/powerbi/tests/test_full_e2e.py::TestPowerQuerySubprocess::test_data_prep_workflow PASSED [ 92%]
cli_anything/powerbi/tests/test_full_e2e.py::TestPowerQuerySubprocess::test_steps_edit_and_dry_run PASSED [ 93%]
cli_anything/powerbi/tests/test_full_e2e.py::TestPowerQuerySubprocess::test_append_as_new_and_anti_merge PASSED [ 94%]
cli_anything/powerbi/tests/test_full_e2e.py::TestThemeSubprocess::test_theme_file_roundtrip PASSED [ 94%]
cli_anything/powerbi/tests/test_full_e2e.py::TestThemeSubprocess::test_styled_dashboard_workflow PASSED [ 95%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTemplateSubprocess::test_suggest_and_template PASSED [ 95%]
cli_anything/powerbi/tests/test_full_e2e.py::TestTemplateSubprocess::test_generate_from_spec PASSED [ 96%]
cli_anything/powerbi/tests/test_full_e2e.py::TestVisualsSubprocess::test_advanced_charts_and_formatting PASSED [ 97%]
cli_anything/powerbi/tests/test_full_e2e.py::TestVisualsSubprocess::test_feedback_fixes PASSED [ 97%]
cli_anything/powerbi/tests/test_full_e2e.py::TestVisualsSubprocess::test_check_visuals_and_desktop_cmds PASSED [ 98%]
cli_anything/powerbi/tests/test_full_e2e.py::TestVisualsSubprocess::test_custom_visuals PASSED [ 98%]
cli_anything/powerbi/tests/test_full_e2e.py::TestLiveService::test_list_workspaces_and_datasets SKIPPED [ 99%]
cli_anything/powerbi/tests/test_full_e2e.py::TestLiveService::test_dax_row SKIPPED [100%]
============ 165 passed, 2 skipped, 1 warning in 247.24s (0:04:07) ============
```

### Summary

- **165 passed, 2 skipped, 0 failed** (100% of runnable tests; 2 new in the Desktop verify round, 4 in the Desktop loop round). The 129 from earlier rounds all still pass; 24 tests are new in refinement 4 (22 unit, 2 E2E). One older test changed: it used `gauge` as its example of an unsupported type.
- Subprocess tests ran the installed `cli-anything-powerbi.exe` (not the module fallback).

### Real-backend verification

- **Microsoft TOM**: CLI-written TMDL loads through the Power BI modeling MCP (`ConnectFolder`):
  - DAX model: 2 tables, 2 measures, 1 relationship; TOM re-serialises it byte-identically (`fixtures/TomExport.SemanticModel`).
  - Power Query model: 3 import tables; TOM's output differs only by a trailing blank line (`fixtures/TomPowerQuery.SemanticModel`).
- **Power BI Desktop**: `desktop open` launched the generated projects.
  - The DAX sample opened with Page 1 and the Sales table.
  - The Power Query project (2 CSVs + 1 xlsx, 14 steps) refreshed without errors. Table view showed the expected 4 rows: trimmed/proper-cased products, duplicate removed, blank region set to "Unknown", decimal Price, renamed Quantity, Line Total = Quantity × Price, February row appended, Category merged from Excel.
  - Styled project (refinement 2): after `theme apply --preset modern` and `page build`, Desktop showed
    white rounded cards with soft shadows on a grey page, a "Sales Overview" title, three KPI cards
    ($89.43 / 3 / 8), an indigo column chart and a donut with titles, and the table, all with correct data after refresh.
    The theme JSON shape came from a theme imported through Desktop's own Import theme dialog.
  - Templates (refinement 3): `page generate` on a 3-page spec (240-order CSV + products xlsx,
    ocean theme, monthly trends) rendered in Desktop after refresh. Overview had a date range slicer,
    Region/Channel dropdowns, KPI cards 1,211 / 28.69 / 3.9% / 240, monthly columns, a region donut,
    product bars and a detail table. Trends had monthly lines for three KPIs. By Region had a
    clustered column of two KPIs and a Category slicer from the related table. A first daily-trend
    version looked noisy, which is why `--monthly` was added.
  - All Desktop checks were done by hand, not automated.
- **Power BI service**: REST code paths run against a stdlib HTTP stub. The live tests (`TestLiveService`) are **skipped**: they need `POWERBI_LIVE=1` and a sign-in.

### Coverage gaps

- No live REST run yet (needs a tenant sign-in).
- The Desktop refresh is a manual check; M evaluation can't run headlessly.
- `auth login` device and browser flows are untested (interactive).
- The REPL is tested in piped mode only; the prompt_toolkit TTY path was not exercised.
- `visual color --series` on a lone-measure chart now also writes the unselected `dataPoint` entry (the Desktop check showed bars ignore `metadata` there); this fix is unit-tested but wasn't re-rendered in Desktop.
- Chart suggestions use name/type heuristics and raw-source sampling; renamed or computed columns have no cardinality. No map visuals (they need Bing in the service).
- Report rendering is checked by hand in Desktop; there's no headless renderer to pixel-test themes.
- Not wrapped: per-visual formatting, bookmarks, custom visuals; query parameters (`expressions.tmdl`), folder/SQL/web sources, unpivot and group-by (`query add-step` covers them). Comments between M steps are dropped on rewrite.
