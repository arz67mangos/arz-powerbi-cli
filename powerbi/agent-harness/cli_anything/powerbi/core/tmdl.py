"""Line-based TMDL reader/editor.

Power BI Desktop writes TMDL with tab indentation: the object header sits at
depth N, its properties at N+1 and a multi-line expression at N+2 (or inside
``` fences). This module edits text in place so everything it does not
understand (annotations, lineage tags, partitions...) is preserved verbatim.

ponytail: not a full TMDL grammar — covers tables, columns, measures and
relationships, the objects agents edit. Use TOM / Tabular Editor for the rest.
"""
from __future__ import annotations

import re
import uuid

_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
_HEADER = re.compile(
    r"^(?P<kind>[A-Za-z]+)\s+(?P<name>'(?:[^']|'')*'|[^\s=:]+)(?:\s*=\s*(?P<expr>.*))?$"
)
FENCE = "```"


def depth(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def quote_name(name: str) -> str:
    if _PLAIN_NAME.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def unquote_name(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def split_ref(ref: str) -> tuple[str, str]:
    """'My Table'.Col  or  Table.'My Col'  ->  (table, column)."""
    m = re.match(r"^('(?:[^']|'')*'|[^.]+)\.(.+)$", ref.strip())
    if not m:
        raise ValueError(f"Bad column reference {ref!r}; expected Table.Column")
    return unquote_name(m.group(1)), unquote_name(m.group(2))


def join_ref(table: str, column: str) -> str:
    return f"{quote_name(table)}.{quote_name(column)}"


def parse_header(line: str):
    m = _HEADER.match(line.strip())
    if not m:
        return None
    return m.group("kind"), unquote_name(m.group("name")), m.group("expr")


# ── Generic block scanning ──────────────────────────────────────────────

def child_blocks(lines: list[str], d: int) -> list[dict]:
    """Objects whose header is at depth d. Each block spans [start, end)
    including leading /// description lines, excluding trailing blanks."""
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or depth(line) != d:
            i += 1
            continue
        start = i
        desc = []
        while i < n and depth(lines[i]) == d and lines[i].strip().startswith("///"):
            desc.append(lines[i].strip()[3:].strip())
            i += 1
        if i >= n or depth(lines[i]) != d:
            continue
        hdr = parse_header(lines[i])
        is_prop = re.match(r"^[A-Za-z]+\s*:", lines[i].strip()) or hdr is None
        header_idx = i
        i += 1
        fenced = hdr and (hdr[2] or "").strip() == FENCE
        while i < n:
            ln = lines[i]
            if fenced:
                if ln.strip() == FENCE:
                    fenced = False
                i += 1
                continue
            if ln.strip() and depth(ln) <= d:
                break
            if ln.strip() == FENCE:
                fenced = True
            i += 1
        end = i
        while end > header_idx + 1 and not lines[end - 1].strip():
            end -= 1
        if not is_prop:
            blocks.append({
                "kind": hdr[0], "name": hdr[1], "start": start,
                "header": header_idx, "end": end, "description": "\n".join(desc) or None,
            })
    return blocks


def _body(lines, blk, d):
    """Split a block body into (expression_lines, property_lines)."""
    hdr = parse_header(lines[blk["header"]])
    first = (hdr[2] or "").strip() if hdr else ""
    body = lines[blk["header"] + 1:blk["end"]]
    expr: list[str] = []
    i = 0
    if first == FENCE:
        while i < len(body) and body[i].strip() != FENCE:
            expr.append(body[i])
            i += 1
        i += 1  # closing fence
    else:
        if first:
            expr.append("\t" * (d + 2) + first)
        while i < len(body) and (not body[i].strip() or depth(body[i]) >= d + 2):
            expr.append(body[i])
            i += 1
    # dedent
    while expr and not expr[-1].strip():
        expr.pop()
    ind = min((depth(x) for x in expr if x.strip()), default=0)
    expr_text = "\n".join(x[ind:] if x.strip() else "" for x in expr)
    return expr_text if (first or expr) else None, body[i:]


def _props(prop_lines, d) -> dict:
    props = {}
    for ln in prop_lines:
        if depth(ln) != d + 1 or not ln.strip():
            continue
        s = ln.strip()
        m = re.match(r"^([A-Za-z]+)\s*:\s*(.*)$", s)
        if m:
            props[m.group(1)] = m.group(2)
        elif re.match(r"^[A-Za-z]+$", s):
            props[s] = True
    return props


# ── Table files ─────────────────────────────────────────────────────────

def parse_table(text: str) -> dict:
    lines = text.splitlines()
    tbl = next((b for b in child_blocks(lines, 0) if b["kind"] == "table"), None)
    if tbl is None:
        raise ValueError("No 'table' declaration found")
    out = {"name": tbl["name"], "description": tbl["description"],
           "columns": [], "measures": [], "partitions": [], "hierarchies": []}
    inner = lines[:]  # depth-1 children of the table
    for b in child_blocks(inner, 1):
        expr, rest = _body(lines, b, 1)
        props = _props(rest, 1)
        item = {"name": b["name"], "description": b["description"], **props}
        if b["kind"] == "measure":
            item["expression"] = expr
            out["measures"].append(item)
        elif b["kind"] == "column":
            if expr:
                item["expression"] = expr
            out["columns"].append(item)
        elif b["kind"] == "partition":
            hdr = parse_header(lines[b["header"]])
            item["type"] = (hdr[2] or "").strip() or None
            out["partitions"].append(item)
        elif b["kind"] == "hierarchy":
            out["hierarchies"].append(item)
    return out


def _find(lines, kind, name):
    for b in child_blocks(lines, 1):
        if b["kind"] == kind and b["name"].lower() == name.lower():
            return b
    return None


def _measure_block(name, expression, format_string=None, display_folder=None,
                   description=None, extra_props: list[str] | None = None) -> list[str]:
    out = []
    if description:
        out += [f"\t/// {d}" for d in description.splitlines()]
    expr_lines = expression.strip().splitlines() or [""]
    if len(expr_lines) == 1:
        out.append(f"\tmeasure {quote_name(name)} = {expr_lines[0].strip()}")
    else:
        out.append(f"\tmeasure {quote_name(name)} =")
        out += ["\t\t\t" + x if x.strip() else "" for x in expr_lines]
    if format_string:
        out.append(f"\t\tformatString: {format_string}")
    if display_folder:
        out.append(f"\t\tdisplayFolder: {display_folder}")
    out += extra_props if extra_props is not None else [f"\t\tlineageTag: {uuid.uuid4()}"]
    return out


def _insert_point(lines) -> int:
    """After the last measure, else before the first column/partition, else EOF."""
    kids = child_blocks(lines, 1)
    measures = [b for b in kids if b["kind"] == "measure"]
    if measures:
        return measures[-1]["end"]
    others = [b for b in kids if b["kind"] in ("column", "partition", "hierarchy")]
    if others:
        return others[0]["start"]
    return len(lines)


def _splice(lines, at, block):
    """Insert block at index `at`, keeping one blank line around it."""
    new = block[:]
    if at > 0 and lines[at - 1].strip():
        new = [""] + new
    if at < len(lines) and lines[at].strip():
        new = new + [""]
    return lines[:at] + new + lines[at:]


def _join(lines):
    return "\n".join(lines).rstrip("\n") + "\n"


def add_measure(text, name, expression, format_string=None, display_folder=None,
                description=None) -> str:
    lines = text.splitlines()
    if _find(lines, "measure", name) or _find(lines, "column", name):
        raise ValueError(f"'{name}' already exists in this table")
    if not expression or not expression.strip():
        raise ValueError("Measure expression must not be empty")
    blk = _measure_block(name, expression, format_string, display_folder, description)
    return _join(_splice(lines, _insert_point(lines), blk))


def remove_measure(text, name) -> str:
    lines = text.splitlines()
    b = _find(lines, "measure", name)
    if not b:
        raise KeyError(f"Measure '{name}' not found")
    end = b["end"]
    while end < len(lines) and not lines[end].strip():
        end += 1
    return _join(lines[:b["start"]] + lines[end:])


_UNSET = object()


def update_measure(text, name, expression=None, format_string=_UNSET,
                   display_folder=_UNSET, description=_UNSET, new_name=None) -> str:
    """Change selected parts of a measure; '' clears an optional property."""
    lines = text.splitlines()
    b = _find(lines, "measure", name)
    if not b:
        raise KeyError(f"Measure '{name}' not found")
    expr, rest = _body(lines, b, 1)
    props = _props(rest, 1)
    keep = [ln for ln in rest
            if not re.match(r"^\t\t(formatString|displayFolder)\s*:", ln)]
    while keep and not keep[-1].strip():
        keep.pop()
    fs = props.get("formatString") if format_string is _UNSET else format_string
    df = props.get("displayFolder") if display_folder is _UNSET else display_folder
    desc = b["description"] if description is _UNSET else description
    blk = _measure_block(new_name or b["name"], expression or expr or "", fs, df, desc, keep)
    return _join(lines[:b["start"]] + blk + lines[b["end"]:])


def new_table_tmdl(name: str, dax: str, columns: list[tuple[str, str]]) -> str:
    """A calculated table (DAX partition) — needs no data source."""
    out = [f"table {quote_name(name)}", f"\tlineageTag: {uuid.uuid4()}", ""]
    for col, dtype in columns:
        out += [f"\tcolumn {quote_name(col)}", f"\t\tdataType: {dtype}",
                f"\t\tlineageTag: {uuid.uuid4()}",
                "\t\tsummarizeBy: " + ("sum" if dtype in ("int64", "double", "decimal") else "none"),
                f"\t\tsourceColumn: [{col}]", ""]
    out += [f"\tpartition {quote_name(name)} = calculated", "\t\tmode: import", "\t\tsource ="]
    out += ["\t\t\t\t" + x for x in dax.strip().splitlines()]
    return _join(out)


# ── relationships.tmdl ──────────────────────────────────────────────────

def parse_relationships(text: str) -> list[dict]:
    lines = text.splitlines()
    rels = []
    for b in child_blocks(lines, 0):
        if b["kind"] != "relationship":
            continue
        props = _props(lines[b["header"] + 1:b["end"]], 0)
        r = {"id": b["name"], **props}
        for side in ("fromColumn", "toColumn"):
            if side in props:
                t, c = split_ref(props[side])
                r[side.replace("Column", "") + "_table"] = t
                r[side.replace("Column", "") + "_column"] = c
        rels.append(r)
    return rels


def add_relationship(text: str, from_ref: str, to_ref: str, cross_filter=None,
                     active=True) -> tuple[str, str]:
    ft, fc = split_ref(from_ref)
    tt, tc = split_ref(to_ref)
    rid = str(uuid.uuid4())
    blk = [f"relationship {rid}"]
    if cross_filter:
        blk.append(f"\tcrossFilteringBehavior: {cross_filter}")
    if not active:
        blk.append("\tisActive: false")
    blk += [f"\tfromColumn: {join_ref(ft, fc)}", f"\ttoColumn: {join_ref(tt, tc)}"]
    lines = text.splitlines()
    return _join(_splice(lines, len(lines), blk) if lines else blk), rid


def remove_relationship(text: str, rid: str) -> str:
    lines = text.splitlines()
    for b in child_blocks(lines, 0):
        if b["kind"] == "relationship" and b["name"] == rid:
            end = b["end"]
            while end < len(lines) and not lines[end].strip():
                end += 1
            return _join(lines[:b["start"]] + lines[end:])
    raise KeyError(f"Relationship '{rid}' not found")


# ── Power Query (M) partitions & column schema ──────────────────────────

NUMERIC = ("int64", "double", "decimal")


def _m_partition(lines):
    for b in child_blocks(lines, 1):
        if b["kind"] == "partition":
            hdr = parse_header(lines[b["header"]])
            if (hdr[2] or "").strip() == "m":
                return b
    return None


def _source_span(lines, b):
    """(source_line_idx, first_expr_idx, end_idx, fenced) inside an M partition."""
    for j in range(b["header"] + 1, b["end"]):
        s = lines[j].strip()
        if depth(lines[j]) == 2 and re.match(r"^source\s*=", s):
            fenced = s.endswith(FENCE)
            k = j + 1
            while k < b["end"]:
                ln = lines[k]
                if fenced:
                    if ln.strip() == FENCE:
                        return j, j + 1, k + 1, True
                elif ln.strip() and depth(ln) <= 2:
                    break
                k += 1
            while k > j + 1 and not lines[k - 1].strip():
                k -= 1
            return j, j + 1, k, fenced
    raise ValueError("M partition has no 'source =' expression")


def get_m_source(text: str) -> str:
    lines = text.splitlines()
    b = _m_partition(lines)
    if not b:
        raise ValueError("Table has no Power Query (M) partition")
    _, a, e, fenced = _source_span(lines, b)
    body = lines[a:e - 1] if fenced else lines[a:e]
    ind = min((depth(x) for x in body if x.strip()), default=0)
    return "\n".join(x[ind:] if x.strip() else "" for x in body)


def set_m_source(text: str, m: str) -> str:
    lines = text.splitlines()
    b = _m_partition(lines)
    if not b:
        raise ValueError("Table has no Power Query (M) partition")
    j, _, e, _ = _source_span(lines, b)
    new = ["\t\tsource ="] + ["\t\t\t\t" + x if x.strip() else "" for x in m.strip("\n").splitlines()]
    return _join(lines[:j] + new + lines[e:])


def _column_block(name, dtype, source_column=None) -> list[str]:
    return [f"\tcolumn {quote_name(name)}", f"\t\tdataType: {dtype}",
            f"\t\tlineageTag: {uuid.uuid4()}",
            "\t\tsummarizeBy: " + ("sum" if dtype in NUMERIC else "none"),
            f"\t\tsourceColumn: {source_column or name}", "",
            "\t\tannotation SummarizationSetBy = Automatic"]


def add_column(text: str, name: str, dtype: str) -> str:
    lines = text.splitlines()
    if _find(lines, "column", name):
        raise ValueError(f"Column '{name}' already exists")
    kids = child_blocks(lines, 1)
    cols = [k for k in kids if k["kind"] == "column"]
    parts = [k for k in kids if k["kind"] == "partition"]
    at = cols[-1]["end"] if cols else (parts[0]["start"] if parts else len(lines))
    return _join(_splice(lines, at, _column_block(name, dtype)))


def remove_column(text: str, name: str) -> str:
    lines = text.splitlines()
    b = _find(lines, "column", name)
    if not b:
        raise KeyError(f"Column '{name}' not found")
    end = b["end"]
    while end < len(lines) and not lines[end].strip():
        end += 1
    return _join(lines[:b["start"]] + lines[end:])


def rename_column(text: str, old: str, new: str) -> str:
    lines = text.splitlines()
    b = _find(lines, "column", old)
    if not b:
        raise KeyError(f"Column '{old}' not found")
    if _find(lines, "column", new) and new.lower() != old.lower():
        raise ValueError(f"Column '{new}' already exists")
    lines[b["header"]] = f"\tcolumn {quote_name(new)}"
    for k in range(b["header"] + 1, b["end"]):
        if re.match(r"^\t\tsourceColumn\s*:", lines[k]):
            lines[k] = f"\t\tsourceColumn: {new}"
    return _join(lines)


def set_column_type(text: str, name: str, dtype: str) -> str:
    lines = text.splitlines()
    b = _find(lines, "column", name)
    if not b:
        raise KeyError(f"Column '{name}' not found")
    for k in range(b["header"] + 1, b["end"]):
        if re.match(r"^\t\tdataType\s*:", lines[k]):
            lines[k] = f"\t\tdataType: {dtype}"
        elif re.match(r"^\t\tsummarizeBy\s*:", lines[k]):
            lines[k] = "\t\tsummarizeBy: " + ("sum" if dtype in NUMERIC else "none")
    return _join(lines)


def new_m_table_tmdl(name: str, m: str, columns: list[tuple[str, str]]) -> str:
    out = [f"table {quote_name(name)}", f"\tlineageTag: {uuid.uuid4()}", ""]
    for col, dtype in columns:
        out += _column_block(col, dtype) + [""]
    out += [f"\tpartition {quote_name(name)} = m", "\t\tmode: import", "\t\tsource ="]
    out += ["\t\t\t\t" + x if x.strip() else "" for x in m.strip("\n").splitlines()]
    out += ["", "\tannotation PBI_ResultType = Table"]
    return _join(out)
