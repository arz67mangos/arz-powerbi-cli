"""Power Query (M): parse/build `let ... in` queries, generate the steps Power BI
Desktop's Query Editor writes, and infer column types from CSV / Excel sources.

ponytail: the M parser understands strings, #"quoted" names, comments, brackets
and nested let/in — enough to split top-level steps. It does not evaluate M;
Power BI Desktop / the service do that on refresh.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

# user type -> (TMDL dataType, M type)
TYPES = {
    "text": ("string", "type text"), "string": ("string", "type text"),
    "int": ("int64", "Int64.Type"), "int64": ("int64", "Int64.Type"), "whole": ("int64", "Int64.Type"),
    "number": ("double", "type number"), "double": ("double", "type number"),
    "decimal": ("decimal", "Currency.Type"), "currency": ("decimal", "Currency.Type"),
    "date": ("dateTime", "type date"), "datetime": ("dateTime", "type datetime"),
    "datetime64": ("dateTime", "type datetime"),
    "bool": ("boolean", "type logical"), "boolean": ("boolean", "type logical"),
    "logical": ("boolean", "type logical"),
}
_TMDL_TO_M = {"string": "type text", "int64": "Int64.Type", "double": "type number",
              "decimal": "Currency.Type", "dateTime": "type datetime", "boolean": "type logical"}
JOIN_KINDS = {"left": "LeftOuter", "right": "RightOuter", "inner": "Inner", "full": "FullOuter",
              "leftanti": "LeftAnti", "rightanti": "RightAnti"}
_KEYWORDS = {"let", "in", "each", "if", "then", "else", "and", "or", "not", "true", "false",
             "null", "type", "meta", "try", "otherwise", "error", "as", "is", "section", "shared"}


def resolve_type(t: str) -> tuple[str, str]:
    try:
        return TYPES[t.lower()]
    except KeyError:
        raise ValueError(f"Unknown type '{t}'. Use one of: {', '.join(sorted(TYPES))}") from None


def m_type_for(tmdl_type: str) -> str:
    return _TMDL_TO_M.get(tmdl_type, "type any")


# ── literals & identifiers ──

def m_str(s: str) -> str:
    return '"' + str(s).replace('"', '""') + '"'


def m_ident(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in _KEYWORDS:
        return name
    return "#" + m_str(name)


def m_list(items) -> str:
    return "{" + ", ".join(m_str(i) for i in items) + "}"


def m_value(v: str, numeric: bool) -> str:
    if v.lower() == "null":
        return "null"
    if numeric:
        try:
            float(v)
            return v
        except ValueError:
            pass
    return m_str(v)


def unident(tok: str) -> str:
    tok = tok.strip()
    if tok.startswith('#"') and tok.endswith('"'):
        return tok[2:-1].replace('""', '"')
    return tok


# ── let/in parsing ──

def _scan(m: str):
    """Yield (index, char, bracket_depth) for code chars outside strings/comments,
    plus ('KW', idx, word) events for whole-word let/in."""
    i, n, depth = 0, len(m), 0
    while i < n:
        c = m[i]
        if c == '"' or (c == "#" and m[i + 1:i + 2] == '"'):
            j = i + (2 if c == "#" else 1)
            while j < n:
                if m[j] == '"':
                    if m[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if m.startswith("//", i):
            j = m.find("\n", i)
            i = n if j == -1 else j
            continue
        if m.startswith("/*", i):
            j = m.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c.isalpha() or c == "_":
            j = i
            while j < n and (m[j].isalnum() or m[j] in "_."):
                j += 1
            word = m[i:j]
            if word in ("let", "in"):
                yield ("KW", i, word, depth)
            i = j
            continue
        yield ("CH", i, c, depth)
        i += 1


def parse_let(m: str) -> tuple[list[tuple[str, str]], str]:
    """Return ([(step_name, expr)], result_expr). A non-let query becomes one 'Source' step."""
    text = m.strip()
    events = list(_scan(text))
    first = next((e for e in events if not (e[0] == "CH" and e[2].isspace())), None)
    if not first or first[0] != "KW" or first[2] != "let":
        return [("Source", text)], "Source"
    letd, cuts, in_at = 0, [], None
    for kind, idx, val, bd in events:
        if kind == "KW" and bd == 0:
            letd += 1 if val == "let" else -1
            if letd == 0:
                in_at = idx
                break
        elif kind == "CH" and val == "," and bd == 0 and letd == 1:
            cuts.append(idx)
    if in_at is None:
        raise ValueError("Malformed M: 'let' without matching 'in'")
    body = text[first[1] + 3:in_at]
    bounds = [-1] + [c - first[1] - 3 for c in cuts] + [len(body)]
    steps = []
    for a, b in zip(bounds, bounds[1:]):
        chunk = body[a + 1:b].strip()
        # ponytail: comments *between* steps are dropped on rewrite; comments inside a step survive
        while chunk.startswith(("//", "/*")):
            end = chunk.find("\n") if chunk.startswith("//") else chunk.find("*/") + 2
            chunk = "" if end <= 0 else chunk[end:].strip()
        mm = re.match(r'^(#"(?:[^"]|"")*"|[A-Za-z_][\w.]*)\s*=\s*(.*)$', chunk, re.S)
        if not mm:
            raise ValueError(f"Cannot parse M step: {chunk[:60]!r}")
        steps.append((unident(mm.group(1)), mm.group(2).strip()))
    return steps, text[in_at + 2:].strip()


def build_let(steps: list[tuple[str, str]], result: str | None = None) -> str:
    result = result or m_ident(steps[-1][0])
    body = ",\n".join(f"    {m_ident(n)} = {e}" for n, e in steps)
    return f"let\n{body}\nin\n    {result}"


def unique_step(steps, base: str) -> str:
    names = {n.lower() for n, _ in steps}
    if base.lower() not in names:
        return base
    i = 1
    while f"{base}{i}".lower() in names:
        i += 1
    return f"{base}{i}"


def references(expr: str, step: str) -> bool:
    pat = re.escape(m_ident(step))
    return re.search(rf"(?<![\w\"]){pat}(?![\w\"])", expr) is not None


def append_step(m: str, name: str, expr_template: str) -> tuple[str, str]:
    """Append a step; `{prev}` in the template becomes the current result step.
    The query's result is repointed to the new step."""
    steps, result = parse_let(m)
    if re.fullmatch(r'#"(?:[^"]|"")*"|[A-Za-z_][\w.]*', result.strip()):
        prev = result.strip()
    else:  # result is an expression, not a step reference: make it a step first
        steps.append((unique_step(steps, "Result"), result))
        prev = m_ident(steps[-1][0])
    name = unique_step(steps, name)
    steps.append((name, expr_template.replace("{prev}", prev)))
    return build_let(steps), name


def remove_step(m: str, name: str) -> str:
    """Remove a step; later references to it are rewired to the previous step."""
    steps, result = parse_let(m)
    idx = next((i for i, (n, _) in enumerate(steps) if n.lower() == name.lower()), None)
    if idx is None:
        raise KeyError(f"Step '{name}' not found. Steps: {', '.join(n for n, _ in steps)}")
    if idx == 0:
        raise ValueError("Cannot remove the first (source) step")
    removed, prev = m_ident(steps[idx][0]), m_ident(steps[idx - 1][0])
    out = []
    for i, (n, e) in enumerate(steps):
        if i == idx:
            continue
        if i > idx:
            e = re.sub(rf"(?<![\w\"]){re.escape(removed)}(?![\w\"])", lambda _: prev, e)
        out.append((n, e))
    if result.strip() == removed:
        result = m_ident(out[-1][0])
    return build_let(out, result)


# ── step builders (Query Editor equivalents) ──

def step_change_types(cols: list[tuple[str, str]]) -> str:
    pairs = ", ".join("{" + m_str(c) + ", " + t + "}" for c, t in cols)
    return "Table.TransformColumnTypes({prev}, {" + pairs + "})"


def step_rename(pairs: list[tuple[str, str]]) -> str:
    return "Table.RenameColumns({prev}, {" + ", ".join("{" + m_str(a) + ", " + m_str(b) + "}" for a, b in pairs) + "})"


def step_remove_columns(cols) -> str:
    return f"Table.RemoveColumns({{prev}}, {m_list(cols)})"


def step_filter(condition: str) -> str:
    cond = condition.strip()
    if not cond.startswith("each ") and not cond.startswith("(") and "=>" not in cond:
        cond = "each " + cond
    return f"Table.SelectRows({{prev}}, {cond})"


def step_replace(col: str, old: str, new: str, text: bool, whole: bool = False) -> str:
    """Text columns replace substrings unless `whole`; blanks/null always match whole cells."""
    sub = text and not whole and old not in ("", "null")
    replacer = "Replacer.ReplaceText" if sub else "Replacer.ReplaceValue"
    return f"Table.ReplaceValue({{prev}}, {m_value(old, not text)}, {m_value(new, not text)}, {replacer}, {m_list([col])})"


def step_transform_text(cols, fn: str) -> str:
    pairs = ", ".join("{" + m_str(c) + ", " + fn + ", type text}" for c in cols)
    return "Table.TransformColumns({prev}, {" + pairs + "})"


def step_distinct(cols=None) -> str:
    return f"Table.Distinct({{prev}}, {m_list(cols)})" if cols else "Table.Distinct({prev})"


def step_fill_down(cols) -> str:
    return f"Table.FillDown({{prev}}, {m_list(cols)})"


def step_add_column(name: str, expr: str, mtype: str) -> str:
    e = expr.strip()
    if not e.startswith("each ") and "=>" not in e:
        e = "each " + e
    return f"Table.AddColumn({{prev}}, {m_str(name)}, {e}, {mtype})"


def step_nested_join(left_keys, right_query: str, right_keys, new_col: str, kind: str) -> str:
    return (f"Table.NestedJoin({{prev}}, {m_list(left_keys)}, {m_ident(right_query)}, "
            f"{m_list(right_keys)}, {m_str(new_col)}, JoinKind.{kind})")


def step_expand(col: str, fields, new_names) -> str:
    return f"Table.ExpandTableColumn({{prev}}, {m_str(col)}, {m_list(fields)}, {m_list(new_names)})"


def step_combine(others) -> str:
    return "Table.Combine({{prev}, " + ", ".join(m_ident(o) for o in others) + "})"


# ── source queries ──

def csv_query(path: str, columns: list[tuple[str, str]], delimiter=",", encoding=65001) -> str:
    p = str(Path(path).resolve())
    steps = [
        ("Source", f"Csv.Document(File.Contents({m_str(p)}), [Delimiter={m_str(delimiter)}, "
                   f"Columns={len(columns)}, Encoding={encoding}, QuoteStyle=QuoteStyle.Csv])"),
        ("Promoted Headers", 'Table.PromoteHeaders(Source, [PromoteAllScalars=true])'),
    ]
    steps.append(("Changed Type", step_change_types([(c, m_type(t)) for c, t in columns])
                  .replace("{prev}", m_ident("Promoted Headers"))))
    return build_let(steps)


def excel_query(path: str, item: str, kind: str, columns: list[tuple[str, str]]) -> str:
    p = str(Path(path).resolve())
    nav = "Sheet" if kind == "Sheet" else "Table"
    var = f"{item}_{nav}"
    steps = [("Source", f"Excel.Workbook(File.Contents({m_str(p)}), null, true)"),
             (var, f"Source{{[Item={m_str(item)},Kind={m_str(kind)}]}}[Data]")]
    prev = var
    if kind == "Sheet":
        steps.append(("Promoted Headers", f"Table.PromoteHeaders({m_ident(var)}, [PromoteAllScalars=true])"))
        prev = "Promoted Headers"
    steps.append(("Changed Type", step_change_types([(c, m_type(t)) for c, t in columns])
                  .replace("{prev}", m_ident(prev))))
    return build_let(steps)


def m_type(tmdl_type_or_user: str) -> str:
    """Accept a user type ('date') or a TMDL type ('dateTime') -> M type."""
    if tmdl_type_or_user.lower() in TYPES:
        return TYPES[tmdl_type_or_user.lower()][1]
    return m_type_for(tmdl_type_or_user)


# ── schema inference ──

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")
_DT_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
               "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M")


def _is(fmt_list, v):
    for f in fmt_list:
        try:
            dt.datetime.strptime(v, f)
            return True
        except ValueError:
            pass
    return False


def infer_text_type(values) -> str:
    """User type name for a column of strings (like Desktop's 'Detect data type')."""
    vals = [v.strip() for v in values if v is not None and v.strip() != ""]
    if not vals:
        return "text"
    checks = [
        ("bool", lambda v: v.lower() in ("true", "false")),
        ("int", lambda v: re.fullmatch(r"[-+]?\d{1,18}", v) is not None),
        ("number", lambda v: re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", v) is not None),
        ("date", lambda v: _is(_DATE_FORMATS, v)),
        ("datetime", lambda v: _is(_DT_FORMATS, v) or _is(_DATE_FORMATS, v)),
    ]
    for name, ok in checks:
        if all(ok(v) for v in vals):
            return name
    return "text"


def sniff_csv(path: str, delimiter: str | None = None, sample_rows: int = 200,
              encoding: str = "utf-8-sig") -> dict:
    with open(path, newline="", encoding=encoding) as f:
        head = f.read(64 * 1024)
        f.seek(0)
        if not delimiter:
            try:
                delimiter = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        rows = []
        for i, r in enumerate(csv.reader(f, delimiter=delimiter)):
            rows.append(r)
            if i >= sample_rows:
                break
    if not rows or not any(h.strip() for h in rows[0]):
        raise ValueError(f"{path}: empty CSV or no header row")
    header = _dedupe([h.strip() or f"Column{i + 1}" for i, h in enumerate(rows[0])])
    data = rows[1:]
    cols = [(h, infer_text_type([r[i] if i < len(r) else "" for r in data])) for i, h in enumerate(header)]
    return {"delimiter": delimiter, "columns": cols, "sample_rows": len(data)}


def sniff_excel(path: str, sheet: str | None = None, table: str | None = None, sample_rows=200) -> dict:
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("Excel import needs openpyxl: pip install openpyxl") from e
    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    if table:
        for ws in wb.worksheets:
            if table in ws.tables:
                ref = ws.tables[table].ref
                rows = [[c.value for c in row] for row in ws[ref]]
                kind, item = "Table", table
                break
        else:
            raise ValueError(f"Excel table '{table}' not found")
    else:
        if sheet and sheet not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet}' not found. Sheets: {', '.join(wb.sheetnames)}")
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        kind, item = "Sheet", ws.title
    rows = [r for r in rows if any(v is not None for v in r)]
    if not rows:
        raise ValueError("Empty worksheet/table")
    header = _dedupe([str(h).strip() if h is not None else f"Column{i + 1}" for i, h in enumerate(rows[0])])
    data = rows[1:sample_rows + 1]
    cols = []
    for i, h in enumerate(header):
        vals = [r[i] for r in data if i < len(r) and r[i] is not None]
        cols.append((h, _infer_native(vals)))
    return {"item": item, "kind": kind, "columns": cols, "sheets": wb.sheetnames}


def _infer_native(vals) -> str:
    if not vals:
        return "text"
    if all(isinstance(v, bool) for v in vals):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "int"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "number"
    if all(isinstance(v, (dt.datetime, dt.date)) for v in vals):
        if all(not isinstance(v, dt.datetime) or v.time() == dt.time(0) for v in vals):
            return "date"
        return "datetime"
    if all(isinstance(v, str) for v in vals):
        return infer_text_type(vals)
    return "text"


def _dedupe(names):
    seen, out = {}, []
    for n in names:
        k = n.lower()
        if k in seen:
            seen[k] += 1
            n = f"{n}_{seen[k]}"
        else:
            seen[k] = 0
        out.append(n)
    return out
