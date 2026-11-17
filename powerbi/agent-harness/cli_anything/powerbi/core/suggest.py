"""Dashboard templates: profile a table, suggest visuals by column type, lay out pages.

Rules (the same calls an analyst makes in Desktop):
  date column            -> line chart trend, range slicer
  numeric (not a key)    -> KPI card (Sum; Avg for price/rate/% style names)
  text/bool, <= 6 values -> donut (share of total), dropdown slicer
  text, more values      -> bar chart (horizontal, room for labels)
  year/month/quarter     -> column chart (ordered time buckets)
  ID / Key / Code        -> excluded from charts; count of rows instead
Distinct counts are sampled from the CSV/Excel source when the table has one.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from cli_anything.powerbi.core import tmdl
from cli_anything.powerbi.core.report import NUMERIC

KEY_CASED = re.compile(r"(ID|Id|Key|GUID|SKU)$")
KEY_WORD = re.compile(r"(^|[\s_])(id|key|code|sku|guid|number|no)$")
TIME_PARTS = {"year", "month", "quarter", "week", "day", "month name", "weekday", "fiscal year",
              "month number", "year month"}
AVG_HINT = re.compile(r"price|rate|pct|percent|ratio|avg|average|score|\bage\b|%", re.I)
PCT_HINT = re.compile(r"rate|pct|percent|%", re.I)
GEO = re.compile(r"country|region|state|province|city|territory|continent|market|location", re.I)
TEMPLATES = ("overview", "trend", "comparison")
DONUT_MAX, SLICER_MAX, CHART_MAX = 6, 30, 50


class TemplateError(ValueError):
    pass


# ── profiling ──
def _role(col) -> str:
    name, dt = col["name"], col.get("dataType", "string")
    low = name.lower().strip()
    if KEY_CASED.search(name) or KEY_WORD.search(low):
        return "key"
    if dt == "dateTime":
        return "date"
    if low in TIME_PARTS:
        return "time"
    if dt in NUMERIC:
        return "value"
    if dt == "boolean":
        return "flag"
    return "category"


def _source_sample(proj, table, limit=1000) -> dict[str, set]:
    """Distinct raw values per source column (pre-cleaning) for CSV/Excel-backed tables, else {}."""
    # ponytail: samples the raw file, so renamed columns go unmeasured; fine for chart choice
    try:
        m = tmdl.get_m_source(proj.files[proj._table_rel(table)])
    except ValueError:
        return {}
    path = re.search(r'File\.Contents\("((?:[^"]|"")*)"\)', m)
    if not path or not Path(path.group(1).replace('""', '"')).is_file():
        return {}
    f = path.group(1).replace('""', '"')
    rows = []
    try:
        if "Csv.Document" in m:
            d = re.search(r'Delimiter="((?:[^"]|"")*)"', m)
            with open(f, newline="", encoding="utf-8-sig") as fh:
                for i, r in enumerate(csv.reader(fh, delimiter=d.group(1) if d else ",")):
                    rows.append(r)
                    if i >= limit:
                        break
        elif "Excel.Workbook" in m:
            import openpyxl
            it = re.search(r'\[Item="((?:[^"]|"")*)",Kind="(\w+)"\]', m)
            wb = openpyxl.load_workbook(f, read_only=False, data_only=True)
            name, kind = it.group(1).replace('""', '"'), it.group(2)
            if kind == "Table":
                ws = next(w for w in wb.worksheets if name in w.tables)
                rows = [[c.value for c in r] for r in ws[ws.tables[name].ref]]
            else:
                rows = [list(r) for r in wb[name].iter_rows(values_only=True)]
            rows = rows[:limit + 1]
    except (OSError, StopIteration, KeyError, AttributeError, csv.Error, ImportError):
        return {}
    if not rows:
        return {}
    head = [str(h).strip() for h in rows[0]]
    return {h: {str(r[i]).strip() for r in rows[1:] if i < len(r) and r[i] not in (None, "")}
            for i, h in enumerate(head)}


def profile(proj, table: str) -> dict:
    t = proj.table(table)
    sample = _source_sample(proj, t["name"])
    cols = []
    for c in t["columns"]:
        if c.get("isHidden"):
            continue
        role = _role(c)
        vals = sample.get(c["name"])
        prof = {"table": t["name"], "column": c["name"], "dataType": c.get("dataType", "string"),
                "role": role, "distinct": len(vals) if vals is not None else None}
        if role == "value":
            prof["agg"] = "avg" if AVG_HINT.search(c["name"]) else "sum"
        cols.append(prof)
    # categories from dimension tables this table points at (many -> one)
    for r in proj.relationships():
        if r.get("from_table", "").lower() == t["name"].lower() and r.get("to_table"):
            try:
                dim = proj.table(r["to_table"])
            except Exception:
                continue
            dsample = _source_sample(proj, dim["name"])
            for c in dim["columns"]:
                if not c.get("isHidden") and _role(c) in ("category", "flag", "time") \
                        and c["name"].lower() != r.get("to_column", "").lower():
                    vals = dsample.get(c["name"])
                    cols.append({"table": dim["name"], "column": c["name"],
                                 "dataType": c.get("dataType", "string"), "role": _role(c),
                                 "distinct": len(vals) if vals is not None else None, "related": True})
    return {"table": t["name"], "columns": cols, "measures": [m["name"] for m in t["measures"]]}


def _ref(c) -> str:
    t = c["table"]
    if re.search(r"[\[\].']", t):
        return "'" + t.replace("'", "''") + f"'.{c['column']}"
    return f"{t}.{c['column']}"


def _cat_score(c) -> tuple:
    d = c["distinct"]
    return (0 if c["role"] == "flag" or GEO.search(c["column"]) else 1,
            0 if c["role"] == "category" else 1,
            d if d is not None else 25, bool(c.get("related")), c["column"])


def _label(field: str) -> str:
    m = re.match(r"^(\w+)\((.+)\)$", field)
    if m:
        name = m.group(2).split(".", 1)[-1].strip("'[]")
        return {"sum": "Total", "avg": "Average", "count": "Count of",
                "distinctcount": "Count of", "min": "Min", "max": "Max"}[m.group(1)] + f" {name}"
    return field.split("[")[-1].rstrip("]")


def _chart_for(c) -> str:
    if c["role"] == "time":
        return "column"
    d = c["distinct"]
    return "donut" if d is not None and d <= DONUT_MAX else "bar"


def suggest(proj, table: str) -> dict:
    prof = profile(proj, table)
    T = prof["table"]
    cols = prof["columns"]
    kpis = [f"[{m}]" for m in prof["measures"]][:4]   # measure names are model-wide
    reasons = {k: "existing measure" for k in kpis}
    if not kpis:
        for c in [c for c in cols if c["role"] == "value" and not c.get("related")][:3]:
            f = f"{c['agg']}({_ref(c)})"
            kpis.append(f)
            reasons[f] = f"numeric column -> {'average' if c['agg'] == 'avg' else 'sum'}"
        key = next((c for c in cols if c["role"] == "key" and not c.get("related")), None)
        if key and len(kpis) < 4:
            f = f"distinctcount({_ref(key)})"
            kpis.append(f)
            reasons[f] = "ID column -> count of rows"
    if not kpis:
        raise TemplateError(f"'{T}' has no measures, numeric columns or ID column to summarise")
    dates = [c for c in cols if c["role"] == "date" and not c.get("related")]
    cats = sorted([c for c in cols if c["role"] in ("category", "flag", "time")
                   and (c["distinct"] is None or 1 < c["distinct"] <= CHART_MAX)], key=_cat_score)
    slicer_cats = [c for c in cats if c["role"] != "time" and (c["distinct"] or 0) <= SLICER_MAX]
    if len(cats) > 2:   # second comparison: prefer a different chart type for variety
        alt = next((c for c in cats[1:] if _chart_for(c) != _chart_for(cats[0])), cats[1])
        cats.remove(alt)
        cats.insert(1, alt)
    primary = kpis[0]
    slicers = ([_ref(dates[0])] if dates else []) + \
              [_ref(c) for c in slicer_cats][:3 - bool(dates)]
    trend = {"kind": "line", "category": _ref(dates[0]), "values": [primary],
             "title": f"{_label(primary)} over time"} if dates else None
    comps = [{"kind": _chart_for(c), "category": _ref(c), "values": [primary],
              "title": f"{_label(primary)} by {c['column']}"} for c in cats[:2]]
    detail_vals = [c for c in cols if c["role"] == "value" and not c.get("related")]
    detail = ([_ref(dates[0])] if dates else []) + [_ref(c) for c in cats[:3]] + \
             ([k for k in kpis if "(" not in k] or [_ref(c) for c in detail_vals])[:3]
    sug = [{"visual": "card", "fields": [k], "reason": reasons[k]} for k in kpis]
    sug += [{"visual": "slicer", "fields": [s],
             "reason": "date -> range slicer" if dates and s == _ref(dates[0]) else "low-cardinality text -> dropdown"}
            for s in slicers]
    if trend:
        sug.append({"visual": "line", "category": trend["category"], "fields": trend["values"],
                    "reason": "date column -> trend over time"})
    for ch, c in zip(comps, cats):
        why = {"donut": f"{c['distinct']} values -> share of total", "column": "time bucket -> ordered columns",
               "bar": f"{c['distinct'] if c['distinct'] is not None else 'unknown #'} values -> ranked bars"}
        sug.append({"visual": ch["kind"], "category": ch["category"], "fields": ch["values"],
                    "reason": why[ch["kind"]]})
    sug.append({"visual": "table", "fields": detail, "reason": "detail rows"})
    return {"table": T, "columns": cols, "kpis": kpis, "slicers": slicers, "trend": trend,
            "comparisons": comps, "detail": detail, "suggestions": sug}


# ── measures ──
def auto_measures(proj, table: str) -> list[str]:
    """SUM/AVERAGE measures for numeric columns and a row count, when the table has none."""
    t = proj.table(table)
    if t["measures"]:
        return []
    existing = {m["name"].lower() for m in proj.measures()} | \
               {c["name"].lower() for x in proj.tables() for c in x["columns"]}
    q = "'" + t["name"].replace("'", "''") + "'"
    made = []
    for c in t["columns"]:
        if c.get("isHidden") or _role(c) != "value":
            continue
        avg = bool(AVG_HINT.search(c["name"]))
        name = f"{'Average' if avg else 'Total'} {c['name']}"
        if name.lower() in existing:
            continue
        fmt = "0.0%" if PCT_HINT.search(c["name"]) else "#,0.00" if avg or c.get("dataType") != "int64" else "#,0"
        proj.add_measure(t["name"], name, f"{'AVERAGE' if avg else 'SUM'}({q}[{c['name']}])", format_string=fmt)
        made.append(name)
    name = f"{t['name']} Count"
    if name.lower() not in existing:
        proj.add_measure(t["name"], name, f"COUNTROWS({q})", format_string="#,0")
        made.append(name)
    return made


# ── templates ──
def layout(plan: dict, kind: str) -> list[list[dict]]:
    """Chart rows for a template kind."""
    if kind not in TEMPLATES:
        raise TemplateError(f"Template must be one of: {', '.join(TEMPLATES)}")
    trend, comps, kpis = plan["trend"], plan["comparisons"], plan["kpis"]
    table = {"kind": "table", "category": None, "values": plan["detail"], "title": None}
    if kind == "trend":
        if not trend:
            raise TemplateError(f"'{plan['table']}' has no date column for a trend page")
        more = [{**trend, "values": [k], "title": f"{_label(k)} over time"} for k in kpis[1:3]]
        return [[trend], more or [table]]
    if kind == "comparison":
        if not comps:
            raise TemplateError(f"'{plan['table']}' has no category columns to compare")
        first = {**comps[0], "kind": "column" if comps[0]["kind"] == "donut" else comps[0]["kind"],
                 "values": [k for k in kpis[:2]],
                 "title": " vs ".join(_label(k) for k in kpis[:2]) + f" by {comps[0]['category'].split('.')[-1]}"}
        return [[first] + comps[1:2], [table]]
    top = [c for c in [trend] + comps if c]
    if len(top) >= 3:
        return [top[:2], [top[2], table]]
    return [top, [table]] if top else [[table]]


def month_column(proj, table: str) -> str:
    """Add '<Date> Month' = Date.StartOfMonth([Date]) in Power Query (idempotent); returns its field ref."""
    t = proj.table(table)
    date = next((c for c in t["columns"] if not c.get("isHidden") and _role(c) == "date"), None)
    if not date:
        raise TemplateError(f"'{t['name']}' has no date column for monthly trends")
    name = f"{date['name']} Month"
    if not any(c["name"].lower() == name.lower() for c in t["columns"]):
        try:
            proj.add_custom_column(t["name"], name, f"Date.StartOfMonth([{date['name']}])", "date")
        except Exception as e:
            raise TemplateError(f"--monthly needs a Power Query (import) table: {e}") from e
    return _ref({"table": t["name"], "column": name})


def template(proj, page, table, kind="overview", title=None, replace=False, create_measures=False,
             overrides=None, monthly=False) -> dict:
    o = overrides or {}
    made = auto_measures(proj, table) if create_measures else []
    month = month_column(proj, table) if monthly else None
    plan = suggest(proj, table)
    if month and plan["trend"]:
        plan["trend"]["category"] = month
        plan["trend"]["kind"] = "column" if kind == "overview" else "line"
    if o.get("table"):
        plan["detail"] = o["table"]
    rows = o.get("charts") or layout(plan, kind)
    res = proj.build_page(page, title=title if title is not None else page,
                          cards=o.get("kpis", plan["kpis"]), charts=rows,
                          slicers=o.get("slicers", plan["slicers"]),
                          table=o.get("table") if o.get("charts") else None, replace=replace)
    res.update(template=kind, table=plan["table"], measures_created=made,
               slicers=o.get("slicers", plan["slicers"]), kpis=o.get("kpis", plan["kpis"]))
    return res


def parse_chart(spec) -> dict:
    """'TYPE:CATEGORY:VALUE[,VALUE][:Title]' or a dict -> chart dict."""
    if isinstance(spec, dict):
        if "kind" not in spec or "values" not in spec:
            raise TemplateError(f"Chart needs 'kind' and 'values': {spec}")
        return {"category": None, "title": None, **spec}
    parts = spec.split(":")
    if len(parts) < 3:
        raise TemplateError(f"Bad chart {spec!r}; use TYPE:CATEGORY:VALUE[,VALUE][:Title]")
    kind = parts[0].strip().lower()
    if kind not in ("column", "bar", "line", "area", "pie", "donut"):
        raise TemplateError(f"Chart type must be column, bar, line, area, pie or donut (got {kind})")
    return {"kind": kind, "category": parts[1].strip(),
            "values": [v.strip() for v in parts[2].split(",") if v.strip()],
            "title": ":".join(parts[3:]).strip() or None}


SPEC_KEYS = {"theme", "create_measures", "monthly", "replace", "pages"}
PAGE_KEYS = {"name", "table", "template", "title", "kpis", "slicers", "charts", "detail"}


def generate(proj, spec: dict, base_dir=".", replace=False) -> dict:
    """Build a themed multi-page report from a JSON data description (see POWERBI.md)."""
    from cli_anything.powerbi.core import theme as theme_mod
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
        raise TemplateError(f"Spec keys must be among {sorted(SPEC_KEYS)}; got {sorted(set(spec) - SPEC_KEYS)}"
                            if isinstance(spec, dict) else "Spec must be a JSON object")
    out = {"theme": None, "pages": [], "measures_created": []}
    th = spec.get("theme")
    if th:
        if isinstance(th, str) and th.lower().endswith(".json"):
            t = theme_mod.load_theme_file(str(Path(base_dir) / th))
        elif isinstance(th, str):
            t = theme_mod.build_theme(th.title(), th)
        else:
            t = theme_mod.build_theme(**{"name": "Report Theme", **th})
        bad = theme_mod.validate_theme(t)
        if bad:
            raise TemplateError("Invalid theme: " + "; ".join(bad))
        out["theme"] = proj.apply_theme(t)
    pages = spec.get("pages")
    if pages is None:   # one overview per table that has something to summarise
        dims = {r.get("to_table", "").lower() for r in proj.relationships()}
        pages = []
        for t in proj.tables():
            if t["name"].lower() in dims and not t["measures"]:
                continue
            if t["measures"] or any(_role(c) in ("value", "key") for c in t["columns"]):
                pages.append({"name": f"{t['name']} Overview", "table": t["name"]})
        if not pages:
            raise TemplateError("No table has measures or numeric columns; add 'pages' to the spec")
    for pg in pages:
        extra = set(pg) - PAGE_KEYS
        if extra or "name" not in pg or "table" not in pg:
            raise TemplateError(f"Each page needs 'name' and 'table' (keys: {sorted(PAGE_KEYS)}); bad: {pg}")
        ov = {k: pg[k] for k in ("kpis", "slicers") if k in pg}
        if "charts" in pg:
            ov["charts"] = [[parse_chart(c) for c in row] if isinstance(row, list) else [parse_chart(row)]
                            for row in pg["charts"]]
        if "detail" in pg:
            ov["table"] = pg["detail"]
        res = template(proj, pg["name"], pg["table"], pg.get("template", "overview"), pg.get("title"),
                       replace=replace or bool(spec.get("replace")),
                       create_measures=bool(spec.get("create_measures")), overrides=ov,
                       monthly=bool(spec.get("monthly")))
        out["measures_created"] += res["measures_created"]
        out["pages"].append({k: res[k] for k in ("page", "page_id", "template", "table", "visuals")})
    if out["pages"]:   # open on the first generated page
        meta = proj._rj("definition/pages/pages.json")
        meta["activePageName"] = out["pages"][0]["page_id"]
        proj._wj("definition/pages/pages.json", meta)
    return out


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as e:
            raise TemplateError(f"{path}: invalid JSON ({e})") from e
