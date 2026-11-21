"""cli-anything-powerbi: PBIP/TMDL project editing + Power BI REST API."""
from __future__ import annotations

import functools
import json
import os
import shlex
import sys
import time
from pathlib import Path

import click

from cli_anything.powerbi import __version__
from cli_anything.powerbi.core import project as proj_mod
from cli_anything.powerbi.core import theme as theme_mod
from cli_anything.powerbi.core import suggest as suggest_mod
from cli_anything.powerbi.core import visuals as visuals_mod
from cli_anything.powerbi.core.session import Session
from cli_anything.powerbi.utils import powerbi_backend as pbi

_session = Session()
_repl_mode = False
_json = False


def get_session() -> Session:
    return _session


# ── output ──────────────────────────────────────────────────────────────

def _short(v, width=60):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= width else s[:width - 1] + "…"


def emit(data, cols: list[str] | None = None, msg: str | None = None):
    """JSON in --json mode, otherwise a table / key-value listing / message."""
    if _json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    if msg:
        click.secho("✓ " + msg, fg="green")
    if isinstance(data, list):
        if not data:
            click.echo("(none)")
            return
        cols = cols or list(data[0].keys())
        rows = [[_short(r.get(c, "")) for c in cols] for r in data]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
        click.echo("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
        click.echo("  ".join("─" * w for w in widths))
        for r in rows:
            click.echo("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    elif isinstance(data, dict) and not msg:
        for k, v in data.items():
            if isinstance(v, str) and "\n" in v:
                click.echo(f"{k}:")
                for ln in v.splitlines():
                    click.echo(f"    {ln}")
            else:
                click.echo(f"{k}: {v if isinstance(v, str) else json.dumps(v, default=str)}")


_ERRORS = (proj_mod.ProjectError, pbi.PowerBIError, RuntimeError, ValueError, KeyError, OSError)


def fail(e: Exception):
    """Report an error as JSON {"error": ...} or a red message; exit 1 outside the REPL."""
    msg = str(e).strip("'\"") if isinstance(e, KeyError) else str(e)
    if _json:
        click.echo(json.dumps({"error": msg, "type": type(e).__name__}))
    else:
        click.secho(f"✗ {msg}", fg="red", err=True)
    if not _repl_mode:
        sys.exit(1)


def handle(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except _ERRORS as e:
            fail(e)
    return wrapper


def _ws(ctx) -> str | None:
    """Workspace id from --workspace, else the saved context (None = My workspace)."""
    ref = ctx.find_root().params.get("workspace")
    if ref:
        return pbi.resolve_workspace(ref)
    return (Session.context().get("workspace") or {}).get("id")


def _ds(ctx, ref: str | None) -> str:
    if ref:
        return pbi.resolve_dataset(ref, _ws(ctx))
    saved = (Session.context().get("dataset") or {}).get("id")
    if not saved:
        raise RuntimeError("No dataset given. Pass one or run 'dataset use <name|id>'.")
    return saved


def _mutate():
    s = get_session()
    s.snapshot()
    return s.project


# ── root group ──────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--project", "-p", "project_path", envvar="POWERBI_PROJECT",
              help=".pbip file, its folder, or a *.SemanticModel folder")
@click.option("--workspace", "-w", envvar="POWERBI_WORKSPACE",
              help="Workspace id or name ('me' = My workspace); default from 'workspace use'")
@click.option("--dry-run", is_flag=True, help="Run without saving project changes to disk")
@click.version_option(__version__)
@click.pass_context
def cli(ctx, use_json, project_path, workspace, dry_run):
    """Power BI from the command line: edit PBIP/TMDL projects and drive the
    Power BI service (workspaces, datasets, refresh, DAX queries).

    Run without a subcommand to start the interactive REPL."""
    global _json
    _json = use_json
    s = get_session()
    path = project_path or (None if s.has_project() else Session.context().get("project"))
    if path and (not s.has_project() or os.path.abspath(path) != os.path.abspath(s.project_path)):
        try:
            s.open(path)
        except proj_mod.ProjectError as e:
            if project_path:  # explicit path must work; a stale saved one is ignored
                fail(e)
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.result_callback()
def auto_save_on_exit(result, use_json, project_path, workspace, dry_run, **kw):
    """Auto-save the project after one-shot mutations unless --dry-run."""
    if _repl_mode or dry_run:
        return
    s = get_session()
    if s.has_project() and s._modified and s.project_path:
        try:
            s.save_session()
        except Exception as e:  # noqa: BLE001
            click.echo(f"Warning: Auto-save failed: {e}", err=True)


# ── project ─────────────────────────────────────────────────────────────

@cli.group()
def project():
    """Create, open and inspect PBIP projects."""


@project.command("new")
@click.argument("folder")
@click.option("--name", "-n", required=True, help="Project name (file stem)")
@click.option("--no-sample", is_flag=True, help="Skip the sample 'Sales' calculated table")
@click.option("--overwrite", is_flag=True)
@handle
def project_new(folder, name, no_sample, overwrite):
    """Create a minimal PBIP (report + TMDL semantic model)."""
    p = proj_mod.create_project(folder, name, sample=not no_sample, overwrite=overwrite)
    get_session().open(str(p))
    emit({"created": str(p), **get_session().project.info()}, msg=f"Created {p}")


@project.command("open")
@click.argument("path")
@handle
def project_open(path):
    """Open a project and remember it for later one-shot commands."""
    p = get_session().open(path)
    Session.set_context(project=str(p.pbip or p.model_dir))
    emit(p.info(), msg=f"Opened {p.name}")


@project.command("close")
@handle
def project_close():
    """Forget the remembered project."""
    s = get_session()
    s.project = s.project_path = None
    Session.set_context(project=None)
    emit({"closed": True}, msg="Closed")


@project.command("info")
@handle
def project_info():
    """Summary counts for the open project."""
    emit(get_session().require().info())


@project.command("check-visuals")
@click.argument("page_name", required=False)
@handle
def project_check_visuals(page_name):
    """Layout lint: clipped textboxes/slicers, off-page or overlapping visuals, bad filterConfig, wrong roles."""
    issues = get_session().require().check_visuals(page_name)
    errors = [i for i in issues if i["level"] == "error"]
    if _json:
        emit({"ok": not errors, "issues": issues})
    elif issues:
        emit(issues, cols=["level", "where", "message"])
    else:
        click.secho("✓ no layout problems found", fg="green")
    if errors and not _repl_mode:
        sys.exit(2)


@project.command("validate")
@handle
def project_validate():
    """Structural checks: names, references, DAX bracket balance."""
    issues = get_session().require().validate()
    errors = [i for i in issues if i["level"] == "error"]
    if _json:
        emit({"valid": not errors, "issues": issues})
    elif issues:
        emit(issues, cols=["level", "where", "message"])
    else:
        click.secho("✓ valid", fg="green")
    if errors and not _repl_mode:
        sys.exit(2)


@project.command("pages")
@handle
def project_pages():
    """List report pages."""
    emit(proj_mod.list_report_pages(get_session().require().pbip))


@project.command("save")
@handle
def project_save():
    """Write pending changes (REPL; one-shot commands auto-save)."""
    written = get_session().save_session()
    emit({"written": written}, msg=f"Saved {len(written)} file(s)")


# ── table ───────────────────────────────────────────────────────────────

@cli.group()
def table():
    """Tables in the semantic model."""


@table.command("list")
@handle
def table_list():
    """List tables with column/measure counts."""
    emit([{"name": t["name"], "columns": len(t["columns"]), "measures": len(t["measures"]),
           "partitions": ",".join(p.get("type") or "?" for p in t["partitions"])}
          for t in get_session().require().tables()])


@table.command("show")
@click.argument("name")
@handle
def table_show(name):
    """Columns, measures and partitions of one table."""
    t = get_session().require().table(name)
    if _json:
        return emit(t)
    click.secho(f"table {t['name']}", bold=True)
    click.echo("\ncolumns:")
    emit([{"name": c["name"], "dataType": c.get("dataType", ""), "hidden": c.get("isHidden", "")}
          for c in t["columns"]])
    click.echo("\nmeasures:")
    emit([{"name": m["name"], "expression": m.get("expression", "")} for m in t["measures"]])


@table.command("add")
@click.argument("name")
@click.option("--dax", required=True, help="DAX table expression (calculated table)")
@click.option("--column", "-c", "columns", multiple=True, required=True,
              help="name:dataType (string, int64, double, decimal, dateTime, boolean)")
@handle
def table_add(name, dax, columns):
    """Add a calculated table defined by DAX."""
    cols = []
    for c in columns:
        n, _, t = c.partition(":")
        cols.append((n, t or "string"))
    rel = _mutate().add_table(name, dax, cols)
    emit({"table": name, "file": rel}, msg=f"Added table {name}")


@table.command("remove")
@click.argument("name")
@handle
def table_remove(name):
    """Remove a table (blocked while a relationship uses it)."""
    _mutate().remove_table(name)
    emit({"removed": name}, msg=f"Removed table {name}")


# ── measure ─────────────────────────────────────────────────────────────

@cli.group()
def measure():
    """DAX measures."""


@measure.command("list")
@click.option("--table", "-t")
@handle
def measure_list(table):
    """List measures, optionally for one table."""
    emit(get_session().require().measures(table),
         cols=["table", "name", "expression", "formatString", "displayFolder"])


@measure.command("show")
@click.argument("name")
@handle
def measure_show(name):
    """Show one measure with all properties."""
    m = [x for x in get_session().require().measures() if x["name"].lower() == name.lower()]
    if not m:
        raise proj_mod.ProjectError(f"Measure '{name}' not found")
    emit(m[0])


def _expr(expression, file):
    if file:
        return click.open_file(file, encoding="utf-8").read()
    if expression == "-":
        return sys.stdin.read()
    return expression


@measure.command("add")
@click.argument("table")
@click.argument("name")
@click.argument("expression", required=False)
@click.option("--file", "-f", type=click.Path(exists=True, dir_okay=False), help="Read DAX from file")
@click.option("--format", "format_string", help='Format string, e.g. "#,0.00" or "0.0%"')
@click.option("--folder", "display_folder", help="Display folder")
@click.option("--description", "-d")
@handle
def measure_add(table, name, expression, file, format_string, display_folder, description):
    """Add a measure: measure add Sales "Total Sales" "SUM(Sales[Amount])"."""
    expr = _expr(expression, file)
    if not expr:
        raise ValueError("Provide EXPRESSION, '-' for stdin, or --file")
    _mutate().add_measure(table, name, expr, format_string=format_string,
                          display_folder=display_folder, description=description)
    emit({"table": table, "measure": name, "expression": expr.strip()}, msg=f"Added [{name}]")


@measure.command("update")
@click.argument("name")
@click.option("--expression", "-e")
@click.option("--file", "-f", type=click.Path(exists=True, dir_okay=False))
@click.option("--format", "format_string", help="'' clears it")
@click.option("--folder", "display_folder", help="'' clears it")
@click.option("--description", "-d", help="'' clears it")
@click.option("--rename")
@handle
def measure_update(name, expression, file, format_string, display_folder, description, rename):
    """Change a measure's expression, format, folder, description or name."""
    kw = {k: v for k, v in dict(format_string=format_string, display_folder=display_folder,
                                description=description).items() if v is not None}
    kw = {k: (v or None) for k, v in kw.items()}
    p = _mutate()
    p.update_measure(name, expression=_expr(expression, file), new_name=rename, **kw)
    emit({"updated": rename or name}, msg=f"Updated [{rename or name}]")


@measure.command("remove")
@click.argument("name")
@handle
def measure_remove(name):
    """Remove a measure."""
    _mutate().remove_measure(name)
    emit({"removed": name}, msg=f"Removed [{name}]")


# ── relationship ────────────────────────────────────────────────────────

@cli.group()
def relationship():
    """Relationships between tables."""


@relationship.command("list")
@handle
def rel_list():
    """List relationships."""
    emit(get_session().require().relationships(),
         cols=["id", "fromColumn", "toColumn", "crossFilteringBehavior", "isActive"])


@relationship.command("add")
@click.argument("from_ref")
@click.argument("to_ref")
@click.option("--both", is_flag=True, help="Bi-directional cross filtering")
@click.option("--inactive", is_flag=True)
@handle
def rel_add(from_ref, to_ref, both, inactive):
    """Add FROM (many side, e.g. Sales.ProductKey) -> TO (one side, e.g. Product.ProductKey)."""
    rid = _mutate().add_relationship(from_ref, to_ref, "bothDirections" if both else None,
                                     active=not inactive)
    emit({"id": rid, "from": from_ref, "to": to_ref}, msg=f"Added relationship {rid}")


@relationship.command("remove")
@click.argument("rel_id")
@handle
def rel_remove(rel_id):
    """Remove a relationship by id."""
    _mutate().remove_relationship(rel_id)
    emit({"removed": rel_id}, msg="Removed relationship")


# ── import (Get Data) ───────────────────────────────────────────────────

def _pairs(items, what="COL=VALUE") -> list[tuple[str, str]]:
    out = []
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise ValueError(f"Expected {what}, got {it!r}")
        out.append((k.strip(), v.strip()))
    return out


@cli.group("import")
def import_():
    """Get Data: create Power Query tables from CSV / Excel files."""


@import_.command("csv")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--table", "-t", help="Table name (default: file name)")
@click.option("--delimiter", help="Default: sniffed from the file")
@click.option("--type", "types", multiple=True, help="Override inferred type: COL=TYPE (repeatable)")
@click.option("--encoding", default=65001, show_default=True, help="Windows code page (65001 = UTF-8)")
@handle
def import_csv(path, table, delimiter, types, encoding):
    """Import a CSV: Csv.Document -> Promote Headers -> Changed Type (types inferred)."""
    res = _mutate().import_csv(path, table, delimiter, dict(_pairs(types, "COL=TYPE")), encoding)
    emit(res, msg=f"Imported {res['table']} ({len(res['columns'])} columns)")
    if not _json:
        emit(res["columns"])


@import_.command("excel")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--sheet", help="Worksheet name (default: first sheet)")
@click.option("--item", "xl_table", help="Excel table (ListObject) name instead of a sheet")
@click.option("--table", "-t", help="Power BI table name (default: sheet/table name)")
@click.option("--type", "types", multiple=True, help="Override inferred type: COL=TYPE")
@handle
def import_excel(path, sheet, xl_table, table, types):
    """Import an Excel sheet or table via Excel.Workbook (types inferred from cells)."""
    res = _mutate().import_excel(path, sheet, xl_table, table, dict(_pairs(types, "COL=TYPE")))
    emit(res, msg=f"Imported {res['table']} from {res['source']}")
    if not _json:
        emit(res["columns"])


# ── query (Power Query editor) ──────────────────────────────────────────

@cli.group()
def query():
    """View and edit a table's Power Query (M) steps; merge and append queries."""


@query.command("show")
@click.argument("table")
@handle
def query_show(table):
    """Print the full M query (Advanced Editor view)."""
    q = get_session().require().query(table)
    if _json:
        return emit(q)
    click.echo(q["m"])


@query.command("steps")
@click.argument("table")
@handle
def query_steps(table):
    """List applied steps."""
    emit(get_session().require().query(table)["steps"], cols=["name", "expression"])


@query.command("set")
@click.argument("table")
@click.option("--file", "-f", type=click.Path(exists=True, dir_okay=False), help="Read M from file")
@click.argument("m", required=False)
@handle
def query_set(table, file, m):
    """Replace the whole M query (M, '-' for stdin, or --file). Column list is not changed."""
    text = _expr(m, file)
    if not text:
        raise ValueError("Provide M, '-' for stdin, or --file")
    _mutate().set_query(table, text)
    emit({"table": table, "steps": len(get_session().project.query(table)["steps"])},
         msg=f"Replaced query for {table}")


@query.command("add-step")
@click.argument("table")
@click.argument("name")
@click.argument("expression")
@handle
def query_add_step(table, name, expression):
    """Append a raw M step; {prev} = previous step, e.g. 'Table.Sort({prev}, {{"Date", Order.Ascending}})'."""
    step = _mutate().add_step(table, name, expression)
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@query.command("remove-step")
@click.argument("table")
@click.argument("name")
@handle
def query_remove_step(table, name):
    """Remove a step; later steps are rewired to the one before it."""
    _mutate().remove_step(table, name)
    emit({"table": table, "removed": name}, msg=f"Removed step '{name}'")


@query.command("merge")
@click.argument("left")
@click.argument("right")
@click.option("--on", "on", multiple=True, required=True, help="Key column in LEFT (repeatable)")
@click.option("--right-on", multiple=True, help="Key column in RIGHT (default: same as --on)")
@click.option("--kind", default="left", show_default=True,
              type=click.Choice(["left", "right", "inner", "full", "leftanti", "rightanti"]))
@click.option("--expand", "-e", multiple=True, help="RIGHT columns to expand (default: all non-key)")
@click.option("--prefix", help="Prefix for expanded columns (e.g. Product -> Product.Category)")
@handle
def query_merge(left, right, on, right_on, kind, expand, prefix):
    """Merge Queries: join RIGHT into LEFT and expand its columns."""
    res = _mutate().merge(left, right, list(on), list(right_on) or None, kind, list(expand) or None, prefix)
    emit(res, msg=f"Merged {right} into {left}: +{len(res['added_columns'])} column(s)")


@query.command("append")
@click.argument("target")
@click.argument("others", nargs=-1, required=True)
@click.option("--as-new", help="Create a new table instead of appending into TARGET")
@handle
def query_append(target, others, as_new):
    """Append Queries: stack OTHERS under TARGET (Table.Combine)."""
    res = _mutate().append(target, list(others), as_new)
    emit(res, msg=f"Appended {', '.join(others)} " + (f"as new table {as_new}" if as_new else f"into {target}"))


# ── clean (Transform tab) ───────────────────────────────────────────────

@cli.group()
def clean():
    """Data cleaning steps (each adds a Power Query step and updates column metadata)."""


@clean.command("set-type")
@click.argument("table")
@click.argument("assignments", nargs=-1, required=True)
@handle
def clean_set_type(table, assignments):
    """Change column types, e.g. Amount=decimal Date=date (text|int|number|decimal|date|datetime|bool)."""
    step = _mutate().set_types(table, dict(_pairs(assignments, "COL=TYPE")))
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("rename")
@click.argument("table")
@click.argument("assignments", nargs=-1, required=True)
@handle
def clean_rename(table, assignments):
    """Rename columns: clean rename Sales "cust id=Customer ID"."""
    step = _mutate().rename_columns(table, _pairs(assignments, "OLD=NEW"))
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("remove-columns")
@click.argument("table")
@click.argument("columns", nargs=-1, required=True)
@handle
def clean_remove_columns(table, columns):
    """Remove columns from the query and the model."""
    step = _mutate().remove_columns(table, list(columns))
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("filter")
@click.argument("table")
@click.argument("condition")
@handle
def clean_filter(table, condition):
    """Keep rows matching an M condition: clean filter Sales '[Amount] > 0 and [Region] <> null'."""
    step = _mutate().filter_rows(table, condition)
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("replace")
@click.argument("table")
@click.argument("column")
@click.argument("old")
@click.argument("new")
@click.option("--whole", is_flag=True, help="Match entire cell contents (text columns)")
@handle
def clean_replace(table, column, old, new, whole):
    """Replace values: substring for text (unless --whole); blank '' and 'null' match whole cells."""
    step = _mutate().replace_values(table, column, old, new, whole)
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("text")
@click.argument("table")
@click.argument("op", type=click.Choice(["trim", "clean", "upper", "lower", "proper"]))
@click.argument("columns", nargs=-1, required=True)
@handle
def clean_text(table, op, columns):
    """Text transforms: clean text Sales trim Product Region."""
    step = _mutate().transform_text(table, list(columns), op)
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("dedupe")
@click.argument("table")
@click.argument("columns", nargs=-1)
@handle
def clean_dedupe(table, columns):
    """Remove duplicate rows (optionally judged on COLUMNS only)."""
    step = _mutate().remove_duplicates(table, list(columns) or None)
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("fill-down")
@click.argument("table")
@click.argument("columns", nargs=-1, required=True)
@handle
def clean_fill_down(table, columns):
    """Fill nulls with the value above."""
    step = _mutate().fill_down(table, list(columns))
    emit({"table": table, "step": step}, msg=f"Added step '{step}'")


@clean.command("add-column")
@click.argument("table")
@click.argument("name")
@click.argument("expression")
@click.option("--type", "typ", default="text", show_default=True)
@handle
def clean_add_column(table, name, expression, typ):
    """Add a custom column: clean add-column Sales Net '[Amount] * 0.9' --type number."""
    step = _mutate().add_custom_column(table, name, expression, typ)
    emit({"table": table, "step": step, "column": name}, msg=f"Added column {name}")


# ── theme ───────────────────────────────────────────────────────────────

def _theme_options(f):
    for opt in reversed([
        click.option("--preset", default="modern", show_default=True,
                     type=click.Choice(list(theme_mod.PRESETS)), help="Base look"),
        click.option("--palette", help="Palette name or comma-separated #RRGGBB list"),
        click.option("--font", help="Body/label font (e.g. 'Segoe UI')"),
        click.option("--heading-font", help="Title/callout font"),
        click.option("--foreground", help="Text colour #RRGGBB"),
        click.option("--card", "background", help="Card (visual) background #RRGGBB"),
        click.option("--page", "page_background", help="Page/wallpaper colour #RRGGBB"),
        click.option("--border", help="Card border colour #RRGGBB"),
        click.option("--radius", type=int, help="Card corner radius 0-40"),
        click.option("--shadow/--no-shadow", default=None, help="Card drop shadow"),
        click.option("--no-cards", is_flag=True, help="Palette/fonts only, no card visualStyles"),
    ]):
        f = opt(f)
    return f


def _build_theme(name, preset, palette, font, heading_font, foreground, background,
                 page_background, border, radius, shadow, no_cards):
    return theme_mod.build_theme(name, preset, palette, font, heading_font, foreground, background,
                                 page_background, border, radius, shadow, card_style=not no_cards)


@cli.group()
def theme():
    """Report themes: palettes, fonts and the card-style look."""


@theme.command("presets")
@handle
def theme_presets():
    """List built-in theme presets."""
    emit([{"preset": k, "palette": v["palette"], "font": v["heading_font"], "page": v["page"],
           "card": v["card"], "radius": v["radius"], "shadow": v["shadow"]}
          for k, v in theme_mod.PRESETS.items()])


@theme.command("palettes")
@handle
def theme_palettes():
    """List named colour palettes."""
    emit([{"palette": k, "colors": ",".join(v)} for k, v in theme_mod.PALETTES.items()])


@theme.command("create")
@click.argument("out", type=click.Path(dir_okay=False))
@click.option("--name", "-n", default="Modern", show_default=True)
@_theme_options
@handle
def theme_create(out, name, **opts):
    """Write a theme JSON file (importable via Design > Themes > Import theme)."""
    t = _build_theme(name, **opts)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(t, f, indent=2)
    emit({"theme": os.path.abspath(out), "name": name, "dataColors": t["dataColors"],
          "warnings": theme_mod.font_warnings(t)}, msg=f"Wrote {out}")


@theme.command("apply")
@click.argument("file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "-n", default="Modern", show_default=True, help="Theme name (generated themes)")
@_theme_options
@handle
def theme_apply(file, name, **opts):
    """Apply a theme JSON FILE, or generate one from --preset/--palette/... and apply it."""
    if file:
        t = theme_mod.load_theme_file(file)
    else:
        t = _build_theme(name, **opts)
    issues = theme_mod.validate_theme(t)
    if issues:
        raise ValueError("Invalid theme: " + "; ".join(issues))
    fname = _mutate().apply_theme(t)
    emit({"applied": t.get("name"), "file": fname, "warnings": theme_mod.font_warnings(t)},
         msg=f"Applied theme {t.get('name')}")


@theme.command("show")
@handle
def theme_show():
    """Show the report's current custom theme."""
    cur = get_session().require().current_theme()
    if _json:
        return emit(cur)
    t = cur["theme"] or {}
    emit({"custom_theme": cur["custom"] or "(none)", "name": t.get("name", ""),
          "dataColors": ", ".join(t.get("dataColors", [])[:8]),
          "fonts": ", ".join(sorted({s.get("fontFace", "") for s in t.get("textClasses", {}).values()})),
          "card_style": "visualStyles" in t})


@theme.command("remove")
@handle
def theme_remove():
    """Remove the custom theme (back to the default theme)."""
    name = _mutate().remove_theme()
    emit({"removed": name}, msg=f"Removed theme {name}")


@theme.command("validate")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@handle
def theme_validate(file):
    """Check a theme JSON file."""
    t = theme_mod.load_theme_file(file)
    issues = theme_mod.validate_theme(t)
    emit({"valid": not issues, "issues": issues, "warnings": theme_mod.font_warnings(t)})
    if issues and not _repl_mode:
        sys.exit(2)


# ── page ────────────────────────────────────────────────────────────────

@cli.group()
def page():
    """Report pages and consistent grid layouts."""


@page.command("list")
@handle
def page_list():
    """List pages in order."""
    emit(get_session().require().pages(), cols=["id", "displayName", "width", "height", "visuals", "active"])


@page.command("add")
@click.argument("name")
@click.option("--width", default=1280, show_default=True)
@click.option("--height", default=720, show_default=True)
@click.option("--active", is_flag=True, help="Make it the page the report opens on")
@handle
def page_add(name, width, height, active):
    """Add an empty page."""
    pid = _mutate().add_page(name, width, height, active)
    emit({"page": name, "id": pid}, msg=f"Added page {name}")


@page.command("remove")
@click.argument("name")
@handle
def page_remove(name):
    """Remove a page and its visuals."""
    _mutate().remove_page(name)
    emit({"removed": name}, msg=f"Removed page {name}")


@page.command("format")
@click.argument("name")
@click.argument("settings", nargs=-1)
@click.option("--background", help="Page canvas colour #RRGGBB (same as background.color=...)")
@click.option("--wallpaper", help="Wallpaper colour around the canvas #RRGGBB (outspace.color=...)")
@handle
def page_format(name, settings, background, wallpaper):
    """Page formatting: OBJECT.PROPERTY=VALUE ... e.g. background.color=#E3F2EF background.transparency=0."""
    parsed = [visuals_mod.parse_setting(s) for s in settings]
    if background:
        parsed.append(("background", "color", background))
    if wallpaper:
        parsed.append(("outspace", "color", wallpaper))
    if not parsed:
        raise ValueError("Give OBJECT.PROPERTY=VALUE settings, --background or --wallpaper")
    emit({"page": name, "objects": _mutate().format_page(name, parsed)}, msg=f"Formatted page {name}")


def _chart_spec(spec: str) -> dict:
    """TYPE:CATEGORY:VALUE[,VALUE...][:Title] e.g. column:Orders.Category:[Total Sales]:Sales by category"""
    return suggest_mod.parse_chart(spec)


@page.command("build")
@click.argument("name")
@click.option("--title", help="Page heading text box")
@click.option("--card", "cards", multiple=True, help="KPI card field, e.g. '[Total Sales]' (repeatable)")
@click.option("--chart", "charts", multiple=True,
              help="TYPE:CATEGORY:VALUES[:Title], e.g. 'column:Orders.Category:[Total Sales]' (repeatable)")
@click.option("--table", "table_fields", help="Comma-separated fields for a full-width table")
@click.option("--slicer", "slicers", multiple=True,
              help="Slicer field, e.g. 'Orders.Region' (repeatable; dates get a range slider)")
@click.option("--replace", is_flag=True, help="Clear an existing page's visuals first")
@handle
def page_build(name, title, cards, charts, table_fields, slicers, replace):
    """Lay out a page on a consistent grid: title, slicers, KPI cards, charts (2 per row), table."""
    table = [f.strip() for f in table_fields.split(",")] if table_fields else None
    res = _mutate().build_page(name, title, list(cards), [_chart_spec(c) for c in charts], table, replace,
                               slicers=list(slicers))
    emit(res, msg=f"Built page {res['page']} with {len(res['visuals'])} visual(s)")


@page.command("suggest")
@click.argument("table")
@handle
def page_suggest(table):
    """Suggest visuals for a table from its column types (and sampled source values)."""
    plan = suggest_mod.suggest(get_session().require(), table)
    if _json:
        return emit(plan)
    emit([{"visual": s["visual"], "category": s.get("category", ""), "fields": ", ".join(s["fields"]),
           "reason": s["reason"]} for s in plan["suggestions"]],
         cols=["visual", "category", "fields", "reason"])


@page.command("template")
@click.argument("name")
@click.option("--table", "-t", "table", required=True, help="Table the page summarises")
@click.option("--kind", "-k", default="overview", show_default=True,
              type=click.Choice(list(suggest_mod.TEMPLATES)),
              help="overview: trend + comparisons + detail; trend: lines over time; comparison: by category")
@click.option("--title", help="Heading text (default: the page name; '' for none)")
@click.option("--create-measures", is_flag=True, help="Add SUM/AVERAGE/COUNTROWS measures if the table has none")
@click.option("--monthly", is_flag=True, help="Trend by month (adds '<Date> Month' in Power Query)")
@click.option("--replace", is_flag=True, help="Clear an existing page's visuals first")
@handle
def page_template(name, table, kind, title, create_measures, monthly, replace):
    """Generate a full dashboard page (KPI cards, slicers, charts, table) from the model."""
    res = suggest_mod.template(_mutate(), name, table, kind, title, replace, create_measures, monthly=monthly)
    emit(res, msg=f"Built {kind} page {res['page']} with {len(res['visuals'])} visual(s)"
         + (f"; created measures {', '.join(res['measures_created'])}" if res["measures_created"] else ""))


@page.command("generate")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--replace", is_flag=True, help="Rebuild pages that already have visuals")
@handle
def page_generate(spec, replace):
    """Build a themed multi-page report from a JSON data description."""
    data = suggest_mod.load_spec(spec)
    res = suggest_mod.generate(_mutate(), data, os.path.dirname(os.path.abspath(spec)), replace)
    if _json:
        return emit(res)
    emit([{"page": p["page"], "template": p["template"], "table": p["table"], "visuals": len(p["visuals"])}
          for p in res["pages"]], cols=["page", "template", "table", "visuals"])


# ── visual ──────────────────────────────────────────────────────────────

@cli.group()
def visual():
    """Visuals on a page."""


@visual.command("list")
@click.argument("page_name")
@handle
def visual_list(page_name):
    """List visuals on a page with position and fields."""
    emit(get_session().require().visuals(page_name),
         cols=["id", "type", "title", "x", "y", "width", "height", "fields"])


@visual.command("add")
@click.argument("page_name")
@click.argument("kind")
@click.option("--field", "-f", "fields", multiple=True,
              help="Value field: [Measure], Table[Measure], Table.Column, sum(Table.Column); textbox: the text")
@click.option("--category", "-c", help="Axis/legend field for charts")
@click.option("--role", "-r", "roles", multiple=True,
              help="ROLE=FIELD for any data role, e.g. Y2=[Margin], X=sum(Sales.Qty), Rows=Sales.Region")
@click.option("--x", default=24.0, show_default=True)
@click.option("--y", default=24.0, show_default=True)
@click.option("--width", "-W", default=400.0, show_default=True)
@click.option("--height", "-H", default=260.0, show_default=True)
@click.option("--title", "-t")
@click.option("--mode", type=click.Choice(["Dropdown", "Between", "Basic"]),
              help="Slicer style (default: Between for dates/numbers, else Dropdown)")
@click.option("--size", type=float, default=20, show_default=True, help="Textbox font size (pt)")
@click.option("--bold/--no-bold", default=True, show_default=True, help="Textbox bold text")
@click.option("--color", help="Textbox text colour #RRGGBB")
@click.option("--align", type=click.Choice(["left", "center", "right"]), help="Textbox alignment")
@click.option("--bg", help="Textbox background #RRGGBB")
@click.option("--border", help="Textbox border colour #RRGGBB")
@click.option("--radius", type=int, help="Textbox border corner radius")
@handle
def visual_add(page_name, kind, fields, category, roles, x, y, width, height, title, mode,
               size, bold, color, align, bg, border, radius):
    """Place one visual. KIND: a built-in type (see 'visual types'), textbox, or a custom visual.

    Textbox: each -f is one paragraph; style with --size --color --align --bg --border --radius."""
    p = _mutate()
    bound = {}
    for r in roles:
        if "=" not in r:
            raise ValueError(f"Bad --role {r!r}; use ROLE=FIELD")
        k, v = r.split("=", 1)
        bound.setdefault(k.strip(), []).append(v.strip())
    if kind.lower() == "textbox":
        if not fields:
            raise ValueError("textbox needs --field 'text'")
        vid = p.add_textbox(page_name, list(fields), x, y, width, height, size=size, bold=bold, color=color,
                            align=align, bg=bg, border=border, radius=radius)
        need = p.textbox_min_height(size, len(fields))
        if height < need:
            out = {"page": page_name, "id": vid, "type": kind,
                   "warning": f"height {height:g}px is under ~{need}px; Desktop will clip {size:g}pt text"}
            if not _json:
                click.secho("! " + out["warning"], fg="yellow", err=True)
            emit(out, msg=f"Added {kind} {vid}")
            return
    else:
        vid = p.add_visual(page_name, kind, list(fields), category, x, y, width, height, title, mode=mode,
                           roles=bound)
    emit({"page": page_name, "id": vid, "type": kind}, msg=f"Added {kind} {vid}")


@visual.command("props")
@click.argument("kind")
@handle
def visual_props(kind):
    """Common formatting objects/properties for a type, for 'visual format' (not exhaustive)."""
    emit([{"object": o, "properties": p_} for o, p_ in visuals_mod.props_for(kind).items()],
         cols=["object", "properties"])


@visual.command("remove")
@click.argument("page_name")
@click.argument("visual_id")
@handle
def visual_remove(page_name, visual_id):
    """Remove a visual by id (see 'visual list')."""
    _mutate().remove_visual(page_name, visual_id)
    emit({"removed": visual_id}, msg=f"Removed visual {visual_id}")


@visual.command("types")
@handle
def visual_types():
    """Visual types and their data roles (! = required, m = summed measure, g = grouping)."""
    rows = [{"type": k, "visualType": v[0], "source": "built-in",
             "roles": ", ".join(r + ("" if k_.rstrip("!") == "a" else f":{k_.rstrip('!')}") +
                                ("!" if k_.endswith("!") else "") for r, k_ in v[1].items())}
            for k, v in visuals_mod.TYPES.items()]
    if get_session().has_project():
        for g, c in get_session().require().custom_visuals().items():
            rows.append({"type": c["name"], "visualType": g, "source": c["source"],
                         "roles": ", ".join(f"{r}:{d['kind']}" for r, d in c["roles"].items()) or "(any)"})
    emit(rows, cols=["type", "visualType", "source", "roles"])


@visual.command("format")
@click.argument("page_name")
@click.argument("visual_id")
@click.argument("settings", nargs=-1)
@click.option("--reset", multiple=True, help="Drop a formatting card first, e.g. --reset legend")
@handle
def visual_format(page_name, visual_id, settings, reset):
    """Set formatting: OBJECT.PROPERTY=VALUE ... (legend.position=Bottom labels.show=true title.text='Sales')."""
    if not settings and not reset:
        raise ValueError("Give OBJECT.PROPERTY=VALUE settings or --reset OBJECT")
    res = _mutate().format_visual(page_name, visual_id, [visuals_mod.parse_setting(x) for x in settings], reset)
    emit({"id": visual_id, **res}, msg=f"Formatted {visual_id}")


@visual.command("color")
@click.argument("page_name")
@click.argument("visual_id")
@click.argument("color")
@click.option("--series", help="Colour one series (a field of the visual, e.g. '[Revenue]')")
@click.option("--point", help="Colour one category value, e.g. 'East'")
@handle
def visual_color(page_name, visual_id, color, series, point):
    """Set data colours: whole visual, one series, or one category value."""
    if series and point:
        raise ValueError("Use --series or --point, not both")
    res = _mutate().color_visual(page_name, visual_id, color, series, point)
    emit({"id": visual_id, "dataPoint": res}, msg=f"Coloured {visual_id} {color}")


@visual.command("sort")
@click.argument("page_name")
@click.argument("visual_id")
@click.argument("field")
@click.option("--asc", is_flag=True, help="Ascending (default descending)")
@handle
def visual_sort(page_name, visual_id, field, asc):
    """Sort a visual by one of its fields."""
    res = _mutate().sort_visual(page_name, visual_id, field, descending=not asc)
    emit({"id": visual_id, **res}, msg=f"Sorted {visual_id} by {res['sort']} {res['direction']}")


@visual.command("move")
@click.argument("page_name")
@click.argument("visual_id")
@click.option("--x", type=float)
@click.option("--y", type=float)
@click.option("--width", "-W", type=float)
@click.option("--height", "-H", type=float)
@click.option("--z", type=int, help="Stacking order (higher is in front)")
@handle
def visual_move(page_name, visual_id, x, y, width, height, z):
    """Move/resize a visual."""
    if all(v is None for v in (x, y, width, height, z)):
        raise ValueError("Give at least one of --x --y -W -H --z")
    pos = _mutate().move_visual(page_name, visual_id, x, y, width, height, z)
    emit({"id": visual_id, **pos}, msg=f"Moved {visual_id}")


@visual.group("custom")
def visual_custom():
    """Custom visuals: .pbiviz files and AppSource visuals."""


@visual_custom.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@handle
def custom_import(file):
    """Import a .pbiviz into the report (like Desktop's 'Import a visual from a file')."""
    res = _mutate().import_custom_visual(file)
    emit(res, msg=f"Imported {res['name']} ({res['guid']}); roles: {', '.join(res['roles']) or 'none'}")


@visual_custom.command("add-public")
@click.argument("guid")
@handle
def custom_add_public(guid):
    """Register an AppSource visual by its GUID (Desktop downloads it when the report opens)."""
    emit({"added": _mutate().add_public_visual(guid)}, msg=f"Registered AppSource visual {guid}")


@visual_custom.command("list")
@handle
def custom_list():
    """Custom visuals registered in the report."""
    cvs = get_session().require().custom_visuals()
    emit([{"guid": g, "name": c["name"], "source": c["source"], "version": c["version"] or "",
           "roles": ", ".join(c["roles"]) or "(unknown)"} for g, c in cvs.items()],
         cols=["guid", "name", "source", "version", "roles"])


@visual_custom.command("remove")
@click.argument("ref")
@click.option("--force", is_flag=True, help="Also delete visuals that use it")
@handle
def custom_remove(ref, force):
    """Remove a custom visual (by guid or display name)."""
    emit({"removed": _mutate().remove_custom_visual(ref, force)}, msg=f"Removed custom visual {ref}")


# ── auth ────────────────────────────────────────────────────────────────

@cli.group()
def auth():
    """Sign in to the Power BI service."""


@auth.command("login")
@click.option("--browser", is_flag=True, help="Interactive browser sign-in instead of device code")
@click.option("--tenant", help="Tenant id or domain (default: organizations)")
@handle
def auth_login(browser, tenant):
    """Device-code sign-in (token cached in ~/.cli-anything-powerbi)."""
    emit(pbi.login(interactive=browser, tenant=tenant, echo=lambda m: click.echo(m, err=True)),
         msg="Signed in")


@auth.command("status")
@handle
def auth_status():
    """Show auth mode and signed-in account."""
    emit(pbi.status())


@auth.command("logout")
@handle
def auth_logout():
    """Delete the cached token."""
    emit(pbi.logout(), msg="Signed out")


# ── workspace ───────────────────────────────────────────────────────────

@cli.group()
def workspace():
    """Workspaces (groups)."""


@workspace.command("list")
@click.option("--filter", "filter_name", help="Name contains")
@handle
def ws_list(filter_name):
    """List workspaces you can access."""
    emit(pbi.list_workspaces(filter_name), cols=["id", "name", "type", "isOnDedicatedCapacity"])


@workspace.command("use")
@click.argument("ref")
@handle
def ws_use(ref):
    """Set the default workspace (id, name, or 'me')."""
    wid = pbi.resolve_workspace(ref)
    name = "My workspace" if wid is None else next(
        (w["name"] for w in pbi.list_workspaces() if w["id"] == wid), ref)
    Session.set_context(workspace={"id": wid, "name": name} if wid else None, dataset=None)
    emit({"workspace_id": wid, "name": name}, msg=f"Using workspace {name}")


@workspace.command("current")
@handle
def ws_current():
    """Show the default workspace and dataset."""
    c = Session.context()
    emit({"workspace": c.get("workspace") or {"id": None, "name": "My workspace"},
          "dataset": c.get("dataset")})


# ── dataset ─────────────────────────────────────────────────────────────

@cli.group()
def dataset():
    """Datasets (semantic models) in the service."""


@dataset.command("list")
@click.pass_context
@handle
def ds_list(ctx):
    """List datasets in the workspace."""
    emit(pbi.list_datasets(_ws(ctx)), cols=["id", "name", "configuredBy", "isRefreshable"])


@dataset.command("info")
@click.argument("ref", required=False)
@click.pass_context
@handle
def ds_info(ctx, ref):
    """Show dataset details."""
    emit(pbi.get_dataset(_ds(ctx, ref), _ws(ctx)))


@dataset.command("use")
@click.argument("ref")
@click.pass_context
@handle
def ds_use(ctx, ref):
    """Set the default dataset for refresh/dax commands."""
    ws = _ws(ctx)
    did = pbi.resolve_dataset(ref, ws)
    info = pbi.get_dataset(did, ws)
    Session.set_context(dataset={"id": did, "name": info.get("name")})
    emit({"dataset_id": did, "name": info.get("name")}, msg=f"Using dataset {info.get('name')}")


@dataset.command("refresh")
@click.argument("ref", required=False)
@click.option("--wait", is_flag=True, help="Poll until the refresh finishes")
@click.option("--timeout", default=1800, show_default=True)
@click.option("--type", "refresh_type",
              type=click.Choice(["Full", "ClearValues", "Calculate", "DataOnly", "Automatic", "Defragment"]),
              help="Enhanced refresh type (Premium/Fabric capacity)")
@click.pass_context
@handle
def ds_refresh(ctx, ref, wait, timeout, refresh_type):
    """Trigger a dataset refresh."""
    ws = _ws(ctx)
    did = _ds(ctx, ref)
    res = pbi.refresh(did, ws, refresh_type)
    if wait:
        res["result"] = pbi.wait_refresh(did, ws, timeout=timeout)
        if res["result"].get("status") != "Completed":
            emit(res)
            raise pbi.PowerBIError(f"Refresh ended with status {res['result'].get('status')}")
    emit(res, msg="Refresh " + (res.get("result", {}).get("status", "requested")))


@dataset.command("refresh-history")
@click.argument("ref", required=False)
@click.option("--top", default=10, show_default=True)
@click.pass_context
@handle
def ds_history(ctx, ref, top):
    """Show recent refreshes and their status."""
    emit(pbi.refresh_history(_ds(ctx, ref), _ws(ctx), top),
         cols=["requestId", "refreshType", "status", "startTime", "endTime"])


# ── report ──────────────────────────────────────────────────────────────

@cli.group()
def report():
    """Reports in the service."""


@report.command("list")
@click.pass_context
@handle
def report_list(ctx):
    """List reports in the workspace."""
    emit(pbi.list_reports(_ws(ctx)), cols=["id", "name", "datasetId", "webUrl"])


# ── dax ─────────────────────────────────────────────────────────────────

@cli.group()
def dax():
    """Run DAX queries against a published dataset."""


@dax.command("query")
@click.argument("query", required=False)
@click.option("--file", "-f", type=click.Path(exists=True, dir_okay=False))
@click.option("--dataset", "-d", "ref", help="Dataset id or name (default: 'dataset use')")
@click.option("--csv", "csv_path", type=click.Path(dir_okay=False), help="Also write rows to CSV")
@click.pass_context
@handle
def dax_query(ctx, query, file, ref, csv_path):
    """dax query "EVALUATE TOPN(10, Sales)" — uses the executeQueries REST API."""
    q = _expr(query, file)
    if not q:
        raise ValueError("Provide QUERY, '-' for stdin, or --file")
    res = pbi.execute_dax(_ds(ctx, ref), q, _ws(ctx))
    if csv_path:
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=res["columns"])
            w.writeheader()
            w.writerows(res["rows"])
        res["csv"] = os.path.abspath(csv_path)
    emit(res if _json else res["rows"])


# ── desktop ─────────────────────────────────────────────────────────────

@cli.group()
def desktop():
    """Power BI Desktop integration."""


@desktop.command("find")
@handle
def desktop_find():
    """Locate the Power BI Desktop executable."""
    emit({"desktop": pbi.find_desktop()})


@desktop.command("open")
@click.argument("path", required=False)
@handle
def desktop_open(path):
    """Open a .pbip in Power BI Desktop (defaults to the open project)."""
    if not path:
        p = get_session().require()
        path = str(p.pbip or p.model_dir)
    emit(pbi.open_in_desktop(path), msg="Launched Power BI Desktop")


def _desktop_target(path):
    """-> (pbip path, window-title stem) from PATH or the open project."""
    if not path:
        p = get_session().require()
        path = str(p.pbip or p.model_dir)
    return path, Path(path).stem


def _reload(path, page, wait_s, settle, refresh, discard):
    """Close (optionally discarding unsaved GUI edits), reopen, wait, refresh. -> (result, stem)."""
    path, stem = _desktop_target(path)
    if page:
        p = proj_mod.Project(path)
        p.set_active_page(page)
        p.save()
    pids = sorted({w["pid"] for w in pbi.desktop_windows(stem)})
    discarded = False
    try:
        closed = pbi.close_desktop(stem, timeout=min(wait_s, 60))
    except pbi.PowerBIError:
        if not discard:
            raise
        discarded = any([pbi.discard_and_close(stem, pid) for pid in pids])
        if not discarded:
            raise pbi.PowerBIError(f"Desktop ({stem}) didn't close and no 'Don't save' button was found; "
                                   "close it by hand, then run the command again.")
        end = time.time() + 60
        while time.time() < end and any(pbi._pid_alive(x) for x in pids):
            time.sleep(0.5)
        if any(pbi._pid_alive(x) for x in pids):
            raise pbi.PowerBIError(f"Desktop ({stem}) is still open after choosing 'Don't save'")
        closed = pids
    opened = pbi.open_in_desktop(path)
    win = pbi.wait_for_desktop(stem, timeout=wait_s, settle=settle)
    res = {"closed_pids": closed, "discarded_changes": discarded, "opened": opened["opened"],
           "pid": win["pid"], "title": win["title"]}
    if refresh:
        r = pbi.refresh_desktop(stem)
        res["refreshed"] = r["refreshed"]
        res["refresh_note"] = r.get("note") or f"refreshed in {r['seconds']}s"
        if not r["refreshed"]:
            click.secho(f"Note: {r['note']}", fg="yellow", err=True)
    return res, stem


@desktop.command("reload")
@click.argument("path", required=False)
@click.option("--page", help="Open on this page (sets the report's active page first)")
@click.option("--wait", "wait_s", default=120.0, show_default=True, help="Max seconds to wait for close/open")
@click.option("--settle", default=15.0, show_default=True, help="Seconds to let visuals render after opening")
@click.option("--refresh/--no-refresh", default=True, show_default=True,
              help="Click the report's 'Refresh now' bar (exact name, via UI Automation) and wait for the load")
@click.option("--discard-changes", is_flag=True,
              help="If Desktop asks to save, click ONLY 'Don't save' (drops edits made in the Desktop GUI)")
@click.option("--screenshot", "shot", type=click.Path(dir_okay=False), help="Then save a PNG of the window")
@handle
def desktop_reload(path, page, wait_s, settle, refresh, discard_changes, shot):
    """Close Desktop for this .pbip, reopen it, wait for it to load and refresh the data.

    Desktop doesn't pick up edits made on disk; this does the close/reopen. If Desktop asks to save,
    the command stops (choose 'Don't save' there to keep the CLI's edits) unless --discard-changes."""
    res, stem = _reload(path, page, wait_s, settle, refresh, discard_changes)
    if shot:
        res.update(pbi.screenshot_desktop(stem, shot))
    emit(res, msg=f"Reloaded {stem} in Power BI Desktop" + ("" if not refresh else f" ({res['refresh_note']})"))


@desktop.command("verify")
@click.argument("path", required=False)
@click.option("--pages", default="all", show_default=True, help="'all' or comma-separated page names")
@click.option("--out-dir", type=click.Path(file_okay=False), default="verify", show_default=True)
@click.option("--wait", "wait_s", default=120.0, show_default=True)
@click.option("--settle", default=15.0, show_default=True)
@click.option("--page-settle", default=5.0, show_default=True, help="Seconds to let each page render")
@click.option("--discard-changes", is_flag=True, help="If Desktop asks to save, click ONLY 'Don't save'")
@handle
def desktop_verify(path, pages, out_dir, wait_s, settle, page_settle, discard_changes):
    """Reload + refresh, screenshot every page, and run check-visuals. Prints the PNG paths."""
    res, stem = _reload(path, None, wait_s, settle, True, discard_changes)
    proj = proj_mod.Project(_desktop_target(path)[0])
    names = [p["displayName"] for p in proj.pages()] if pages == "all" else         [proj._page(n.strip())["displayName"] for n in pages.split(",")]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    shots = []
    for i, name in enumerate(names, 1):
        if not pbi.show_page(stem, name):
            raise pbi.PowerBIError(f"Couldn't switch Desktop to page '{name}'")
        time.sleep(page_settle)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        shots.append(pbi.screenshot_desktop(stem, str(Path(out_dir) / f"{i:02d}-{safe}.png"))["screenshot"])
    issues = proj.check_visuals()
    errors = [x for x in issues if x["level"] == "error"]
    res.update(screenshots=shots, ok=not errors, issues=issues)
    if _json:
        emit(res)
    else:
        click.echo(f"Reloaded {stem} ({res.get('refresh_note')})")
        for s_ in shots:
            click.echo(s_)
        if issues:
            emit(issues, cols=["level", "where", "message"])
        else:
            click.secho("✓ no layout problems found", fg="green")
    if errors and not _repl_mode:
        sys.exit(2)


@desktop.command("screenshot")
@click.argument("out", type=click.Path(dir_okay=False))
@click.option("--file", "path", help=".pbip whose window to capture (default: the open project)")
@handle
def desktop_screenshot(out, path):
    """Save a PNG of the Power BI Desktop window showing this report (as it is on screen now)."""
    path, stem = _desktop_target(path)
    emit(pbi.screenshot_desktop(stem, out), msg=f"Saved {out}")


@desktop.command("windows")
@handle
def desktop_windows_cmd():
    """List open Power BI Desktop windows."""
    emit([{"pid": w["pid"], "title": w["title"]} for w in pbi.desktop_windows()], cols=["pid", "title"])


# ── session ─────────────────────────────────────────────────────────────

@cli.group()
def session():
    """Session state, undo/redo."""


@session.command("status")
@handle
def session_status():
    """Show open project, unsaved files, undo depth, context."""
    s = get_session()
    emit({"project": s.project_path, "modified": s._modified,
          "unsaved_files": sorted(s.project.dirty) if s.project else [],
          "undo_depth": len(s._undo), "redo_depth": len(s._redo),
          "context": Session.context()})


@session.command("undo")
@handle
def session_undo():
    """Undo the last model edit (REPL)."""
    ok = get_session().undo()
    emit({"undone": ok}, msg="Undone" if ok else "Nothing to undo")


@session.command("redo")
@handle
def session_redo():
    """Redo the last undone edit (REPL)."""
    ok = get_session().redo()
    emit({"redone": ok}, msg="Redone" if ok else "Nothing to redo")


# ── REPL ────────────────────────────────────────────────────────────────

@cli.command()
def repl():
    """Interactive session (edits stay in memory until 'project save')."""
    from cli_anything.powerbi.utils.repl_skin import ReplSkin
    global _repl_mode
    _repl_mode = True
    sticky_json = _json
    skin = ReplSkin("powerbi", version=__version__)
    skin.print_banner()
    pt = None
    if sys.stdin.isatty():  # piped scripts (agents, tests) use plain input()
        try:
            pt = skin.create_prompt_session()
        except Exception:  # noqa: BLE001 - e.g. no Windows console buffer
            pt = None
    while True:
        s = get_session()
        try:
            line = skin.get_input(pt, project_name=s.project.name if s.project else "",
                                  modified=s._modified)
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in ("quit", "exit", "q"):
            if s._modified:
                skin.warning("Unsaved changes discarded (use 'project save' first)")
            break
        if line.lower() == "help":
            skin.help({g: c.help.splitlines()[0] if c.help else "" for g, c in cli.commands.items()})
            skin.info("Shortcuts: undo, redo, save, status, quit")
            continue
        alias = {"undo": "session undo", "redo": "session redo",
                 "save": "project save", "status": "session status"}
        line = alias.get(line, line)
        try:
            args = shlex.split(line, posix=os.name != "nt")
            if os.name == "nt":
                args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
            cli.main(args=(["--json"] if sticky_json else []) + args,
                     prog_name="cli-anything-powerbi", standalone_mode=False)
        except click.exceptions.Exit:
            pass
        except click.ClickException as e:
            skin.error(e.format_message())
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            skin.error(f"{type(e).__name__}: {e}")
    skin.print_goodbye()


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cli()


if __name__ == "__main__":
    main()
