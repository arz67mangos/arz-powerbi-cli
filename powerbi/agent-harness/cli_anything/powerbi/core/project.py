"""PBIP project: locate, create, inspect, edit and validate the TMDL semantic model.

Edits are buffered in memory (Project.files) and written by save(), so the CLI
can support --dry-run and REPL undo/redo.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from cli_anything.powerbi.core import powerquery as pq
from cli_anything.powerbi.core import tmdl
from cli_anything.powerbi.core.report import RP, ReportMixin, create_pbir_report


class ProjectError(Exception):
    pass


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def locate(path: str) -> tuple[Path | None, Path]:
    """Return (pbip_file or None, semantic_model_dir) for a .pbip file, a folder
    holding one, or a *.SemanticModel folder."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ProjectError(f"Path not found: {p}")
    if p.is_dir() and p.name.endswith(".SemanticModel"):
        return None, p
    if p.is_dir():
        pbips = sorted(p.glob("*.pbip"))
        if len(pbips) == 1:
            p = pbips[0]
        elif not pbips:
            models = sorted(p.glob("*.SemanticModel"))
            if len(models) == 1:
                return None, models[0]
            raise ProjectError(f"No .pbip or single *.SemanticModel found in {p}")
        else:
            raise ProjectError(f"Several .pbip files in {p}; pass one explicitly")
    if p.suffix.lower() != ".pbip":
        raise ProjectError(f"Not a .pbip file: {p}")
    return p, _model_dir_for(p)


def _model_dir_for(pbip: Path) -> Path:
    root = pbip.parent
    # Follow report -> definition.pbir -> datasetReference.byPath when present.
    try:
        for art in json.loads(_read(pbip)).get("artifacts", []):
            rpt = art.get("report", {}).get("path")
            pbir = root / rpt / "definition.pbir" if rpt else None
            if pbir and pbir.exists():
                ref = json.loads(_read(pbir)).get("datasetReference", {}).get("byPath")
                if ref and ref.get("path"):
                    cand = (pbir.parent / ref["path"]).resolve()
                    if cand.is_dir():
                        return cand
    except json.JSONDecodeError as e:
        raise ProjectError(f"Invalid JSON in project files: {e}") from e
    cand = root / f"{pbip.stem}.SemanticModel"
    if cand.is_dir():
        return cand
    models = sorted(root.glob("*.SemanticModel"))
    if len(models) == 1:
        return models[0]
    raise ProjectError(f"Cannot find the semantic model folder for {pbip}")


class PowerQueryMixin:
    """Query Editor operations. Each appends an M step to the table's partition
    and updates the TMDL column list to match the query output."""

    def _m_text(self, table):
        rel = self._table_rel(table)
        try:
            return rel, tmdl.get_m_source(self.files[rel])
        except ValueError as e:
            raise ProjectError(f"Table '{table}': {e} (DAX calculated tables have no query)") from e

    def _source_cols(self, table) -> dict:
        """lower name -> (name, dataType) for query-backed (non-calculated) columns."""
        return {c["name"].lower(): (c["name"], c.get("dataType", "string"))
                for c in self.table(table)["columns"] if not c.get("expression")}

    def _need(self, table, cols) -> list[str]:
        have = self._source_cols(table)
        missing = [c for c in cols if c.lower() not in have]
        if missing:
            raise ProjectError(f"Column(s) not in '{table}': {', '.join(missing)}. "
                               f"Columns: {', '.join(v[0] for v in have.values())}")
        return [have[c.lower()][0] for c in cols]

    def _step(self, table, base, template, schema=None) -> str:
        rel, m = self._m_text(table)
        try:
            m2, name = pq.append_step(m, base, template)
        except ValueError as e:
            raise ProjectError(f"Table '{table}': {e}") from e
        text = tmdl.set_m_source(self.files[rel], m2)
        if schema:
            text = schema(text)
        self._set(rel, text)
        return name

    # ── import ──
    def import_query(self, name, m, columns: list[tuple[str, str]]) -> str:
        if any(t.lower() == name.lower() for t in self.table_files()):
            raise ProjectError(f"Table '{name}' already exists")
        safe = re.sub(r'[<>:"/\\|?*]', "_", name)
        rel = f"tables/{safe}.tmdl"
        cols = [(c, pq.resolve_type(t)[0]) for c, t in columns]
        self._set(rel, tmdl.new_m_table_tmdl(name, m, cols))
        model = self.files.get("model.tmdl", "")
        if "\nref table " in model:
            self._set("model.tmdl", model.rstrip("\n") + f"\nref table {tmdl.quote_name(name)}\n")
        return rel

    def import_csv(self, path, table=None, delimiter=None, overrides=None, encoding=65001) -> dict:
        info = pq.sniff_csv(path, delimiter)
        cols = _override(info["columns"], overrides)
        name = table or Path(path).stem
        rel = self.import_query(name, pq.csv_query(path, cols, info["delimiter"], encoding), cols)
        return {"table": name, "file": rel, "delimiter": info["delimiter"],
                "columns": [{"name": c, "type": t} for c, t in cols]}

    def import_excel(self, path, sheet=None, xl_table=None, table=None, overrides=None) -> dict:
        info = pq.sniff_excel(path, sheet, xl_table)
        cols = _override(info["columns"], overrides)
        name = table or info["item"]
        rel = self.import_query(name, pq.excel_query(path, info["item"], info["kind"], cols), cols)
        return {"table": name, "file": rel, "source": f"{info['kind']}:{info['item']}",
                "columns": [{"name": c, "type": t} for c, t in cols]}

    # ── query inspection / raw edits ──
    def query(self, table) -> dict:
        _, m = self._m_text(table)
        steps, result = pq.parse_let(m)
        return {"table": table, "m": m, "result": result,
                "steps": [{"name": n, "expression": e} for n, e in steps]}

    def set_query(self, table, m):
        pq.parse_let(m)  # raises on malformed let/in
        rel, _ = self._m_text(table)
        self._set(rel, tmdl.set_m_source(self.files[rel], m))

    def add_step(self, table, name, expr) -> str:
        return self._step(table, name, expr)

    def remove_step(self, table, name):
        rel, m = self._m_text(table)
        try:
            self._set(rel, tmdl.set_m_source(self.files[rel], pq.remove_step(m, name)))
        except (KeyError, ValueError) as e:
            raise ProjectError(str(e).strip("'\"")) from e

    # ── cleaning ──
    def set_types(self, table, mapping: dict[str, str]) -> str:
        cols = self._need(table, list(mapping))
        typed = [(c, pq.resolve_type(t)) for c, t in zip(cols, mapping.values())]

        def schema(text):
            for c, (tm, _) in typed:
                text = tmdl.set_column_type(text, c, tm)
            return text
        return self._step(table, "Changed Type", pq.step_change_types([(c, m) for c, (_, m) in typed]), schema)

    def rename_columns(self, table, pairs: list[tuple[str, str]]) -> str:
        olds = self._need(table, [a for a, _ in pairs])
        pairs = list(zip(olds, [b for _, b in pairs]))

        def schema(text):
            for a, b in pairs:
                text = tmdl.rename_column(text, a, b)
            return text
        return self._step(table, "Renamed Columns", pq.step_rename(pairs), schema)

    def remove_columns(self, table, cols) -> str:
        cols = self._need(table, cols)
        used = [r for r in self.relationships()
                for side in ("from", "to")
                if (r.get(f"{side}_table") or "").lower() == table.lower()
                and (r.get(f"{side}_column") or "").lower() in [c.lower() for c in cols]]
        if used:
            raise ProjectError(f"Column used by relationship {used[0]['id']}; remove it first")

        def schema(text):
            for c in cols:
                text = tmdl.remove_column(text, c)
            return text
        return self._step(table, "Removed Columns", pq.step_remove_columns(cols), schema)

    def filter_rows(self, table, condition) -> str:
        return self._step(table, "Filtered Rows", pq.step_filter(condition))

    def replace_values(self, table, col, old, new, whole=False) -> str:
        (c,) = self._need(table, [col])
        is_text = self._source_cols(table)[c.lower()][1] == "string"
        return self._step(table, "Replaced Value", pq.step_replace(c, old, new, is_text, whole))

    TEXT_OPS = {"trim": ("Trimmed Text", "Text.Trim"), "clean": ("Cleaned Text", "Text.Clean"),
                "upper": ("Uppercased Text", "Text.Upper"), "lower": ("Lowercased Text", "Text.Lower"),
                "proper": ("Capitalized Each Word", "Text.Proper")}

    def transform_text(self, table, cols, op) -> str:
        cols = self._need(table, cols)
        base, fn = self.TEXT_OPS[op]
        return self._step(table, base, pq.step_transform_text(cols, fn))

    def remove_duplicates(self, table, cols=None) -> str:
        cols = self._need(table, cols) if cols else None
        return self._step(table, "Removed Duplicates", pq.step_distinct(cols))

    def fill_down(self, table, cols) -> str:
        return self._step(table, "Filled Down", pq.step_fill_down(self._need(table, cols)))

    def add_custom_column(self, table, name, expr, typ="text") -> str:
        if name.lower() in self._source_cols(table):
            raise ProjectError(f"Column '{name}' already exists in '{table}'")
        tm, mt = pq.resolve_type(typ)
        return self._step(table, "Added Custom", pq.step_add_column(name, expr, mt),
                          lambda text: tmdl.add_column(text, name, tm))

    # ── combine ──
    def merge(self, left, right, on, right_on=None, kind="left", expand=None, prefix=None) -> dict:
        right_name = tmdl.parse_table(self.files[self._table_rel(right)])["name"]
        self._m_text(right_name)  # right must be a query too
        lk = self._need(left, on)
        rk = self._need(right_name, right_on or on)
        if len(lk) != len(rk):
            raise ProjectError("--on and --right-on need the same number of columns")
        jk = pq.JOIN_KINDS.get(kind.lower())
        if not jk:
            raise ProjectError(f"Join kind must be one of {', '.join(pq.JOIN_KINDS)}")
        rcols = self._source_cols(right_name)
        fields = self._need(right_name, expand) if expand else \
            [v[0] for k, v in rcols.items() if k not in [x.lower() for x in rk]]
        left_cols = self._source_cols(left)
        names = []
        for f in fields:
            n = f"{prefix}.{f}" if prefix else f
            if n.lower() in left_cols or n.lower() in [x.lower() for x in names]:
                n = f"{right_name}.{f}"
            names.append(n)
        nested = right_name
        s1 = self._step(left, "Merged Queries", pq.step_nested_join(lk, right_name, rk, nested, jk))
        if jk in ("LeftAnti", "RightAnti"):
            s2 = self._step(left, "Removed Merge Column", pq.step_remove_columns([nested]))
            return {"steps": [s1, s2], "added_columns": []}

        def schema(text):
            for f, n in zip(fields, names):
                text = tmdl.add_column(text, n, rcols[f.lower()][1])
            return text
        s2 = self._step(left, f"Expanded {right_name}", pq.step_expand(nested, fields, names), schema)
        return {"steps": [s1, s2], "added_columns": names}

    def append(self, target, others, as_new=None) -> dict:
        others = [tmdl.parse_table(self.files[self._table_rel(o)])["name"] for o in others]
        for o in others:
            self._m_text(o)
        if as_new:
            tgt = tmdl.parse_table(self.files[self._table_rel(target)])["name"]
            self._m_text(tgt)
            cols, seen = [], set()
            for t in [tgt] + others:
                for k, (n, dtype) in self._source_cols(t).items():
                    if k not in seen:
                        seen.add(k)
                        cols.append((n, dtype))
            m = pq.build_let([("Source", "Table.Combine({" + ", ".join(pq.m_ident(t) for t in [tgt] + others) + "})")])
            safe = re.sub(r'[<>:"/\\|?*]', "_", as_new)
            if any(t.lower() == as_new.lower() for t in self.table_files()):
                raise ProjectError(f"Table '{as_new}' already exists")
            self._set(f"tables/{safe}.tmdl", tmdl.new_m_table_tmdl(as_new, m, cols))
            return {"table": as_new, "columns": [c for c, _ in cols]}
        mine = self._source_cols(target)
        extra = []
        for o in others:
            for k, (n, dtype) in self._source_cols(o).items():
                if k not in mine and k not in [e[0].lower() for e in extra]:
                    extra.append((n, dtype))

        def schema(text):
            for n, dtype in extra:
                text = tmdl.add_column(text, n, dtype)
            return text
        step = self._step(target, "Appended Query", pq.step_combine(others), schema)
        return {"step": step, "added_columns": [n for n, _ in extra]}


class Project(PowerQueryMixin, ReportMixin):
    def __init__(self, path: str):
        self.pbip, self.model_dir = locate(path)
        self.definition = self.model_dir / "definition"
        if not self.definition.is_dir():
            if (self.model_dir / "model.bim").exists():
                raise ProjectError(
                    "This semantic model uses model.bim (TMSL JSON). Re-save it in TMDL: "
                    "Power BI Desktop > Options > Preview features > "
                    "'Store semantic model using TMDL format'.")
            raise ProjectError(f"Missing TMDL definition folder: {self.definition}")
        self.name = self.pbip.stem if self.pbip else self.model_dir.name.removesuffix(".SemanticModel")
        self.files: dict[str, str] = {}   # rel path (posix) -> text, all .tmdl under definition/
        for f in sorted(self.definition.rglob("*.tmdl")):
            self.files[f.relative_to(self.definition).as_posix()] = _read(f)
        self.dirty: set[str] = set()
        self._load_report()

    # ── persistence ──
    def snapshot(self):
        return dict(self.files), set(self.dirty), dict(self.rfiles)

    def restore(self, snap):
        self.files, self.dirty, self.rfiles = dict(snap[0]), set(snap[1]), dict(snap[2])

    def save(self) -> list[str]:
        written = []
        for key in sorted(self.dirty):
            if key.startswith(RP):
                rel, store, dest = key[len(RP):], self.rfiles, self.report_dir / key[len(RP):]
            else:
                rel, store, dest = key, self.files, self.definition / key
            if rel in store:
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Desktop writes UTF-8 (BOM-less) with CRLF on Windows; keep LF-normalised text
                dest.write_text(store[rel], encoding="utf-8")
            elif dest.exists():
                dest.unlink()
                root = self.report_dir if key.startswith(RP) else self.definition
                d = dest.parent
                while d != root and root in d.parents and not any(d.iterdir()):
                    d.rmdir()   # drop folders left empty (removed pages, visuals, custom visuals)
                    d = d.parent
            written.append(str(dest))
        self.dirty.clear()
        return written

    def _set(self, rel, text):
        self.files[rel] = text
        self.dirty.add(rel)

    # ── tables ──
    def table_files(self) -> dict[str, str]:
        """table name -> rel path"""
        out = {}
        for rel, text in self.files.items():
            if rel.startswith("tables/"):
                try:
                    out[tmdl.parse_table(text)["name"]] = rel
                except ValueError:
                    continue
        return out

    def _table_rel(self, table: str) -> str:
        for name, rel in self.table_files().items():
            if name.lower() == table.lower():
                return rel
        raise ProjectError(f"Table '{table}' not found. Tables: {', '.join(self.table_files()) or '(none)'}")

    def table(self, name: str) -> dict:
        return tmdl.parse_table(self.files[self._table_rel(name)])

    def tables(self) -> list[dict]:
        return [tmdl.parse_table(self.files[rel]) for rel in self.table_files().values()]

    def add_table(self, name: str, dax: str, columns: list[tuple[str, str]]) -> str:
        if any(t.lower() == name.lower() for t in self.table_files()):
            raise ProjectError(f"Table '{name}' already exists")
        safe = re.sub(r'[<>:"/\\|?*]', "_", name)
        rel = f"tables/{safe}.tmdl"
        self._set(rel, tmdl.new_table_tmdl(name, dax, columns))
        model = self.files.get("model.tmdl", "")
        if "\nref table " in model:  # keep TOM's table-order refs in sync
            self._set("model.tmdl", model.rstrip("\n") + f"\nref table {tmdl.quote_name(name)}\n")
        return rel

    def remove_table(self, name: str):
        rel = self._table_rel(name)
        name = tmdl.parse_table(self.files[rel])["name"]
        if any(name in (r.get("from_table"), r.get("to_table")) for r in self.relationships()):
            raise ProjectError(f"Table '{name}' is used by a relationship; remove that first")
        del self.files[rel]
        self.dirty.add(rel)
        model = self.files.get("model.tmdl", "")
        ref = f"ref table {tmdl.quote_name(name)}"
        if ref in model.splitlines():
            self._set("model.tmdl", "\n".join(l for l in model.splitlines() if l != ref) + "\n")

    # ── measures ──
    def measures(self, table: str | None = None) -> list[dict]:
        out = []
        for t in self.tables():
            if table and t["name"].lower() != table.lower():
                continue
            out += [{"table": t["name"], **m} for m in t["measures"]]
        return out

    def find_measure_table(self, name: str) -> str:
        hits = [m["table"] for m in self.measures() if m["name"].lower() == name.lower()]
        if not hits:
            raise ProjectError(f"Measure '{name}' not found")
        return hits[0]

    def add_measure(self, table, name, expression, **kw):
        if any(m["name"].lower() == name.lower() for m in self.measures()):
            raise ProjectError(f"Measure '{name}' already exists (measure names are model-wide)")
        rel = self._table_rel(table)
        try:
            self._set(rel, tmdl.add_measure(self.files[rel], name, expression, **kw))
        except ValueError as e:
            raise ProjectError(str(e)) from e

    def update_measure(self, name, table=None, **kw):
        rel = self._table_rel(table or self.find_measure_table(name))
        try:
            self._set(rel, tmdl.update_measure(self.files[rel], name, **kw))
        except KeyError as e:
            raise ProjectError(str(e).strip("'\"")) from e

    def remove_measure(self, name, table=None):
        rel = self._table_rel(table or self.find_measure_table(name))
        self._set(rel, tmdl.remove_measure(self.files[rel], name))

    # ── relationships ──
    def relationships(self) -> list[dict]:
        return tmdl.parse_relationships(self.files.get("relationships.tmdl", ""))

    def add_relationship(self, from_ref, to_ref, cross_filter=None, active=True) -> str:
        for ref in (from_ref, to_ref):
            t, c = tmdl.split_ref(ref)
            cols = [x["name"].lower() for x in self.table(t)["columns"]]
            if c.lower() not in cols:
                raise ProjectError(f"Column '{c}' not found in table '{t}'")
        text, rid = tmdl.add_relationship(self.files.get("relationships.tmdl", ""),
                                          from_ref, to_ref, cross_filter, active)
        self._set("relationships.tmdl", text)
        return rid

    def remove_relationship(self, rid):
        try:
            self._set("relationships.tmdl",
                      tmdl.remove_relationship(self.files.get("relationships.tmdl", ""), rid))
        except KeyError as e:
            raise ProjectError(str(e).strip("'\"")) from e

    # ── inspection ──
    def info(self) -> dict:
        tables = self.tables()
        return {
            "name": self.name,
            "pbip": str(self.pbip) if self.pbip else None,
            "semantic_model": str(self.model_dir),
            "tables": len(tables),
            "columns": sum(len(t["columns"]) for t in tables),
            "measures": sum(len(t["measures"]) for t in tables),
            "relationships": len(self.relationships()),
            "unsaved_files": sorted(self.dirty),
        }

    def validate(self) -> list[dict]:
        issues = []

        def err(where, msg, level="error"):
            issues.append({"level": level, "where": where, "message": msg})

        if not (self.model_dir / "definition.pbism").exists():
            err("definition.pbism", "missing")
        if "model.tmdl" not in self.files:
            err("model.tmdl", "missing")
        if "database.tmdl" not in self.files:
            err("database.tmdl", "missing", "warning")
        cols: dict[str, set] = {}
        seen_measures: dict[str, str] = {}
        for rel, text in self.files.items():
            if not rel.startswith("tables/"):
                continue
            try:
                t = tmdl.parse_table(text)
            except ValueError as e:
                err(rel, str(e))
                continue
            cols[t["name"].lower()] = {c["name"].lower() for c in t["columns"]}
            if not t["partitions"]:
                err(rel, "table has no partition", "warning")
            if any(p.get("type") == "m" for p in t["partitions"]):
                try:
                    pq.parse_let(tmdl.get_m_source(text))
                except ValueError as e:
                    err(rel, f"Power Query: {e}")
            for m in t["measures"]:
                key = m["name"].lower()
                if key in seen_measures:
                    err(rel, f"duplicate measure name '{m['name']}' (also in {seen_measures[key]})")
                seen_measures[key] = t["name"]
                if key in cols[t["name"].lower()]:
                    err(rel, f"measure '{m['name']}' clashes with a column name")
                bad = _unbalanced(m.get("expression") or "")
                if bad:
                    err(rel, f"measure '{m['name']}': {bad}")
        issues += self.report_issues()
        for r in self.relationships():
            for side in ("from", "to"):
                t, c = r.get(f"{side}_table"), r.get(f"{side}_column")
                if t is None:
                    err("relationships.tmdl", f"relationship {r['id']} lacks {side}Column")
                elif t.lower() not in cols:
                    err("relationships.tmdl", f"relationship {r['id']}: unknown table '{t}'")
                elif c.lower() not in cols[t.lower()]:
                    err("relationships.tmdl", f"relationship {r['id']}: unknown column '{t}'[{c}]")
        return issues


def _override(cols, overrides):
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    for k, v in overrides.items():
        pq.resolve_type(v)
        if k not in [c.lower() for c, _ in cols]:
            raise ProjectError(f"--type given for unknown column '{k}'")
    return [(c, overrides.get(c.lower(), t)) for c, t in cols]


def _unbalanced(expr: str) -> str | None:
    """Cheap DAX sanity check: brackets/quotes balance outside string literals."""
    pairs = {")": "(", "]": "["}
    stack = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == '"':
            j = expr.find('"', i + 1)
            while j != -1 and j + 1 < n and expr[j + 1] == '"':
                j = expr.find('"', j + 2)
            if j == -1:
                return "unterminated string literal"
            i = j + 1
            continue
        if ch == "'":
            j = expr.find("'", i + 1)
            if j == -1:
                return "unterminated quoted table name"
            i = j + 1
            continue
        if ch == "[":
            j = expr.find("]", i + 1)
            if j == -1:
                return "unclosed '['"
            i = j + 1
            continue
        if ch == "(":
            stack.append(ch)
        elif ch == ")":
            if not stack:
                return "unbalanced ')'"
            stack.pop()
        elif ch == "]":
            return "unbalanced ']'"
        i += 1
    return "unclosed '('" if stack else None


# ── new project ─────────────────────────────────────────────────────────

_SAMPLE_DAX = '''DATATABLE(
	"Date", DATETIME, "Product", STRING, "Amount", DOUBLE,
	{
		{ "2026-01-01", "Widget", 120 },
		{ "2026-01-02", "Gadget", 80 },
		{ "2026-01-03", "Widget", 45.5 }
	}
)'''


def create_project(folder: str, name: str, sample: bool = True, overwrite: bool = False) -> Path:
    """Write a minimal PBIP (report + TMDL semantic model) that Power BI Desktop opens."""
    root = Path(folder).expanduser().resolve()
    pbip = root / f"{name}.pbip"
    if pbip.exists() and not overwrite:
        raise ProjectError(f"{pbip} already exists (use --overwrite)")
    sm, rp = root / f"{name}.SemanticModel", root / f"{name}.Report"
    (sm / "definition" / "tables").mkdir(parents=True, exist_ok=True)
    rp.mkdir(parents=True, exist_ok=True)

    def wjson(p: Path, obj):
        p.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")

    wjson(pbip, {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })
    wjson(sm / "definition.pbism", {"version": "4.0", "settings": {}})
    wjson(sm / ".platform", _platform("SemanticModel", name))
    (sm / "definition" / "database.tmdl").write_text(
        "database\n\tcompatibilityLevel: 1567\n", encoding="utf-8")
    (sm / "definition" / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n"
        "\t\treturnErrorValuesAsNull\n", encoding="utf-8")
    if sample:
        (sm / "definition" / "tables" / "Sales.tmdl").write_text(
            tmdl.new_table_tmdl("Sales", _SAMPLE_DAX,
                                [("Date", "dateTime"), ("Product", "string"), ("Amount", "double")]),
            encoding="utf-8")
    create_pbir_report(root, name)
    wjson(rp / ".platform", _platform("Report", name))
    (root / ".gitignore").write_text("**/.pbi/localSettings.json\n**/.pbi/cache.abf\n", encoding="utf-8")
    return pbip


def _platform(kind, name):
    import uuid
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": kind, "displayName": name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }


def list_report_pages(pbip: Path | None) -> list[dict]:
    """Pages from a legacy report.json or PBIR definition/pages folder."""
    if not pbip:
        return []
    out = []
    for art in json.loads(_read(pbip)).get("artifacts", []):
        rpt = art.get("report", {}).get("path")
        if not rpt:
            continue
        rdir = pbip.parent / rpt
        legacy = rdir / "report.json"
        if legacy.exists():
            for s in json.loads(_read(legacy)).get("sections", []):
                out.append({"name": s.get("name"), "displayName": s.get("displayName"),
                            "visuals": len(s.get("visualContainers", []))})
        for page in sorted((rdir / "definition" / "pages").glob("*/page.json")):
            meta = json.loads(_read(page))
            out.append({"name": meta.get("name"), "displayName": meta.get("displayName"),
                        "visuals": len(list((page.parent / "visuals").glob("*/visual.json")))})
    return out

