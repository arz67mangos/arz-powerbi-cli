"""PBIR report editing: custom themes, pages, visuals and grid page layouts.

File shapes mirror what Power BI Desktop writes when it saves a project
(definition/report.json, pages/<id>/page.json, pages/<id>/visuals/<id>/visual.json,
StaticResources/RegisteredResources/<theme>.json).
"""
from __future__ import annotations

import json
import random
import re
import uuid
from pathlib import Path

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/"
S_PBIR = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
S_VERSION = SCHEMA + "definition/versionMetadata/1.0.0/schema.json"
S_REPORT = SCHEMA + "definition/report/3.3.0/schema.json"
S_PAGES = SCHEMA + "definition/pagesMetadata/1.1.0/schema.json"
S_PAGE = SCHEMA + "definition/page/2.1.0/schema.json"
S_VISUAL = SCHEMA + "definition/visualContainer/2.14.0/schema.json"
VERSION_AT_IMPORT = {"visual": "2.14.0", "report": "3.4.0", "page": "2.3.1"}
THEME_DIR = "StaticResources/RegisteredResources"
RP = "@report/"
CV_DIR = "CustomVisuals"  # dirty-set prefix for report files

from cli_anything.powerbi.core import visuals as V

# alias -> (visualType, category role or None, value role); full roles in visuals.TYPES
VISUALS = {k: (v[0], v[2], v[3]) for k, v in V.TYPES.items()}
AGG = {"sum": (0, "Sum"), "avg": (1, "Avg"), "average": (1, "Avg"), "distinctcount": (2, "DistinctCount"),
       "min": (3, "Min"), "max": (4, "Max"), "count": (5, "Count")}
NUMERIC = ("int64", "double", "decimal")


def new_id() -> str:
    return uuid.uuid4().hex[:20]


def create_pbir_report(root: Path, name: str):
    """Minimal PBIR report folder, the same shape Desktop saves."""
    rp = root / f"{name}.Report"
    page = "ReportSection"
    files = {
        "definition.pbir": {"$schema": S_PBIR, "version": "4.0",
                            "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}},
        "definition/version.json": {"$schema": S_VERSION, "version": "2.0.0"},
        "definition/report.json": {"$schema": S_REPORT, "themeCollection": {}},
        "definition/pages/pages.json": {"$schema": S_PAGES, "pageOrder": [page], "activePageName": page},
        f"definition/pages/{page}/page.json": {"$schema": S_PAGE, "name": page, "displayName": "Page 1",
                                               "displayOption": "FitToPage", "height": 720, "width": 1280},
    }
    for rel, obj in files.items():
        p = rp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    return rp


def _lit(v: str) -> dict:
    return {"expr": {"Literal": {"Value": "'" + v.replace("'", "''") + "'"}}}


class ReportMixin:
    """Mixed into Project; report files live in self.rfiles (rel path -> text)."""

    def _load_report(self):
        self.report_dir = None
        self.rfiles: dict[str, str] = {}
        if not self.pbip:
            return
        for art in json.loads(self.pbip.read_text(encoding="utf-8-sig")).get("artifacts", []):
            rel = art.get("report", {}).get("path")
            if rel and (self.pbip.parent / rel).is_dir():
                self.report_dir = (self.pbip.parent / rel).resolve()
                break
        if self.report_dir:
            for f in sorted(self.report_dir.rglob("*")):
                r = f.relative_to(self.report_dir).as_posix()
                if f.is_file() and not r.startswith(".pbi/") and (f.suffix == ".json" or f.name == "definition.pbir"):
                    self.rfiles[r] = f.read_text(encoding="utf-8-sig")

    # ── low level ──
    def _pbir(self):
        from cli_anything.powerbi.core.project import ProjectError
        if not self.report_dir:
            raise ProjectError("No report found (open the .pbip, not the .SemanticModel folder)")
        if "definition/report.json" not in self.rfiles:
            if "report.json" in self.rfiles:
                raise ProjectError(
                    "Report uses the legacy report.json format. Open the .pbip in Power BI Desktop "
                    "and save once (Desktop upgrades it to PBIR), then retry.")
            raise ProjectError(f"No PBIR definition in {self.report_dir}")

    def _rj(self, rel) -> dict:
        return json.loads(self.rfiles[rel])

    def _wj(self, rel, obj):
        self.rfiles[rel] = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
        self.dirty.add(RP + rel)

    def _rdel(self, rel):
        if rel in self.rfiles:
            del self.rfiles[rel]
            self.dirty.add(RP + rel)

    # ── themes ──
    def current_theme(self) -> dict:
        self._pbir()
        tc = self._rj("definition/report.json").get("themeCollection", {})
        out = {"base": tc.get("baseTheme", {}).get("name"), "custom": None, "theme": None}
        ct = tc.get("customTheme")
        if ct:
            out["custom"] = ct["name"]
            body = self.rfiles.get(f"{THEME_DIR}/{ct['name']}")
            out["theme"] = json.loads(body) if body else None
        return out

    def apply_theme(self, theme: dict) -> str:
        self._pbir()
        self.remove_theme(quiet=True)
        rep = self._rj("definition/report.json")
        safe = re.sub(r"[^A-Za-z0-9_-]", "", theme.get("name") or "Theme") or "Theme"
        fname = f"{safe}{random.randint(10**16, 10**17 - 1)}.json"  # Desktop-style unique suffix
        pk = rep.setdefault("resourcePackages", [])
        reg = next((p for p in pk if p.get("type") == "RegisteredResources"), None)
        if reg is None:
            reg = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
            pk.append(reg)
        reg.setdefault("items", []).append({"name": fname, "path": fname, "type": "CustomTheme"})
        rep.setdefault("themeCollection", {})["customTheme"] = {
            "name": fname, "reportVersionAtImport": dict(VERSION_AT_IMPORT), "type": "RegisteredResources"}
        self._wj("definition/report.json", rep)
        self._wj(f"{THEME_DIR}/{fname}", theme)
        return fname

    def remove_theme(self, quiet=False) -> str | None:
        from cli_anything.powerbi.core.project import ProjectError
        self._pbir()
        rep = self._rj("definition/report.json")
        ct = rep.get("themeCollection", {}).pop("customTheme", None)
        if not ct:
            if quiet:
                return None
            raise ProjectError("Report has no custom theme")
        for p in rep.get("resourcePackages", []):
            p["items"] = [i for i in p.get("items", []) if i.get("type") != "CustomTheme"]
        rep["resourcePackages"] = [p for p in rep.get("resourcePackages", []) if p.get("items")]
        if not rep["resourcePackages"]:
            del rep["resourcePackages"]
        self._wj("definition/report.json", rep)
        self._rdel(f"{THEME_DIR}/{ct['name']}")
        return ct["name"]

    # ── pages ──
    def _pages_meta(self) -> dict:
        self._pbir()
        return self._rj("definition/pages/pages.json")

    def pages(self) -> list[dict]:
        meta = self._pages_meta()
        out = []
        for pid in meta.get("pageOrder", []):
            pj = self.rfiles.get(f"definition/pages/{pid}/page.json")
            if not pj:
                continue
            p = json.loads(pj)
            n = sum(1 for r in self.rfiles if r.startswith(f"definition/pages/{pid}/visuals/") and r.endswith("/visual.json"))
            out.append({"id": pid, "displayName": p.get("displayName"), "width": p.get("width"),
                        "height": p.get("height"), "visuals": n, "active": pid == meta.get("activePageName")})
        return out

    def _page(self, ref: str) -> dict:
        from cli_anything.powerbi.core.project import ProjectError
        for p in self.pages():
            if ref in (p["id"],) or (p["displayName"] or "").lower() == ref.lower():
                return p
        raise ProjectError(f"Page '{ref}' not found. Pages: {', '.join(p['displayName'] for p in self.pages())}")

    def add_page(self, display_name: str, width=1280, height=720, active=False) -> str:
        from cli_anything.powerbi.core.project import ProjectError
        meta = self._pages_meta()
        if any((p["displayName"] or "").lower() == display_name.lower() for p in self.pages()):
            raise ProjectError(f"Page '{display_name}' already exists")
        pid = new_id()
        self._wj(f"definition/pages/{pid}/page.json", {
            "$schema": S_PAGE, "name": pid, "displayName": display_name,
            "displayOption": "FitToPage", "height": int(height), "width": int(width)})
        meta.setdefault("pageOrder", []).append(pid)
        if active:
            meta["activePageName"] = pid
        self._wj("definition/pages/pages.json", meta)
        return pid

    def remove_page(self, ref: str):
        from cli_anything.powerbi.core.project import ProjectError
        p = self._page(ref)
        meta = self._pages_meta()
        if len(meta.get("pageOrder", [])) <= 1:
            raise ProjectError("Cannot remove the only page")
        for rel in [r for r in self.rfiles if r.startswith(f"definition/pages/{p['id']}/")]:
            self._rdel(rel)
        meta["pageOrder"].remove(p["id"])
        if meta.get("activePageName") == p["id"]:
            meta["activePageName"] = meta["pageOrder"][0]
        self._wj("definition/pages/pages.json", meta)

    def clear_page(self, ref: str) -> int:
        p = self._page(ref)
        rels = [r for r in self.rfiles if r.startswith(f"definition/pages/{p['id']}/visuals/")]
        for rel in rels:
            self._rdel(rel)
        return len([r for r in rels if r.endswith("visual.json")])

    # ── fields ──
    def field(self, spec: str, value_role: bool) -> dict:
        """'[Measure]', 'Table[Measure]', 'Table.Column', 'sum(Table.Column)' -> projection."""
        from cli_anything.powerbi.core.project import ProjectError
        s = spec.strip()
        agg = None
        m = re.match(r"^(\w+)\((.+)\)$", s)
        if m and m.group(1).lower() in AGG:
            agg, s = m.group(1).lower(), m.group(2).strip()
        m = re.match(r"^(?:'((?:[^']|'')+)'|([^\[\].']+))?\s*\[(.+)\]$", s) or \
            re.match(r"^(?:'((?:[^']|'')+)'|([^\[\].']+))\.(.+)$", s)
        if not m:
            raise ProjectError(f"Bad field '{spec}'. Use [Measure], Table[Measure], Table.Column or sum(Table.Column)")
        table = (m.group(1) or "").replace("''", "'") or (m.group(2) or "").strip() or None
        name = m.group(3).strip()
        tables = {t["name"].lower(): t for t in self.tables()}
        if table is None:
            hits = [x for x in self.measures() if x["name"].lower() == name.lower()]
            if not hits:
                raise ProjectError(f"Measure [{name}] not found")
            table = hits[0]["table"]
        t = tables.get(table.lower())
        if not t:
            raise ProjectError(f"Table '{table}' not found")
        meas = {x["name"].lower(): x["name"] for x in t["measures"]}
        cols = {x["name"].lower(): x for x in t["columns"]}
        src = {"Expression": {"SourceRef": {"Entity": t["name"]}}}
        if name.lower() in meas and not agg:
            prop = meas[name.lower()]
            return {"field": {"Measure": {**src, "Property": prop}},
                    "queryRef": f"{t['name']}.{prop}", "nativeQueryRef": prop}
        if name.lower() not in cols:
            raise ProjectError(f"'{name}' is not a column or measure of '{t['name']}'")
        col = cols[name.lower()]
        prop = col["name"]
        column = {"Column": {**src, "Property": prop}}
        if agg is None and value_role and col.get("dataType") in NUMERIC:
            agg = "sum"
        if agg:
            fn, label = AGG[agg]
            return {"field": {"Aggregation": {"Expression": column, "Function": fn}},
                    "queryRef": f"{label}({t['name']}.{prop})", "nativeQueryRef": f"{label} of {prop}"
                    if label != "Sum" else prop}
        return {"field": column, "queryRef": f"{t['name']}.{prop}", "nativeQueryRef": prop}

    # ── visuals ──
    def field_type(self, spec: str) -> str | None:
        """TMDL dataType of a plain column field, None for measures/aggregations."""
        f = self.field(spec, False)["field"]
        if "Column" not in f:
            return None
        c = f["Column"]
        t = next(t for t in self.tables() if t["name"] == c["Expression"]["SourceRef"]["Entity"])
        return next(x.get("dataType", "string") for x in t["columns"] if x["name"] == c["Property"])

    def visuals(self, page_ref: str) -> list[dict]:
        p = self._page(page_ref)
        out = []
        for rel in sorted(r for r in self.rfiles
                          if r.startswith(f"definition/pages/{p['id']}/visuals/") and r.endswith("/visual.json")):
            v = json.loads(self.rfiles[rel])
            vis = v.get("visual", {})
            fields = [pr.get("queryRef") for role in vis.get("query", {}).get("queryState", {}).values()
                      for pr in role.get("projections", [])]
            title = (vis.get("visualContainerObjects", {}).get("title") or [{}])[0] \
                .get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {}).get("Value", "")
            pos = v.get("position", {})
            out.append({"id": v.get("name"), "type": vis.get("visualType"), "title": title.strip("'"),
                        "x": pos.get("x"), "y": pos.get("y"), "width": pos.get("width"),
                        "height": pos.get("height"), "fields": fields})
        return out

    def add_visual(self, page_ref, kind, fields=(), category=None, x=0, y=0, width=300, height=200,
                   title=None, z=None, mode=None, roles=None) -> str:
        """kind: a built-in alias (visuals.TYPES), or a registered custom visual (guid or name).

        fields/category fill the type's default value/category roles; roles={'Y2': [...]} binds any role.
        mode (slicers only): Dropdown, Between (range slider), Basic (list); default by column type."""
        from cli_anything.powerbi.core.project import ProjectError
        p = self._page(page_ref)
        vtype, rdefs, cat_role, val_role = self.visual_type(kind)
        binds: dict[str, list] = {}
        if category:
            if not cat_role:
                raise ProjectError(f"'{kind}' has no category role; use --role")
            binds.setdefault(cat_role, []).append(category)
        if fields:
            if not val_role:
                raise ProjectError(f"'{kind}' has no default value role; use --role")
            binds.setdefault(val_role, []).extend(fields)
        for r, fs in (roles or {}).items():
            name = next((d for d in rdefs if d.lower() == r.lower()), None) if rdefs else r
            if name is None:
                raise ProjectError(f"'{kind}' has no role '{r}'. Roles: {', '.join(rdefs)}")
            binds.setdefault(name, []).extend([fs] if isinstance(fs, str) else fs)
        if not any(binds.values()):
            raise ProjectError(f"'{kind}' needs at least one field")
        for r, d in rdefs.items():
            if d["required"] and not binds.get(r):
                raise ProjectError(f"'{kind}' needs a category field (--category)" if r == cat_role else
                                   f"'{kind}' needs a {r} field (--role {r}=...)")
        order = list(rdefs) + [r for r in binds if r not in rdefs]
        state = {r: {"projections": [self.field(f, (rdefs.get(r) or {}).get("kind", "a") == "m")
                                     for f in binds[r]]} for r in order if binds.get(r)}
        visual = {"visualType": vtype, "query": {"queryState": state}}
        if vtype == "slicer":
            if mode is None:
                mode = "Between" if self.field_type(binds["Values"][0]) in ("dateTime",) + NUMERIC else "Dropdown"
            visual["objects"] = {"data": [{"properties": {"mode": _lit(mode)}}]}
        if title:
            visual["visualContainerObjects"] = {"title": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}}, "text": _lit(title)}}]}
        return self._write_visual(p, visual, x, y, width, height, z)

    def visual_type(self, kind: str):
        """-> (visualType, {role: {kind, required}}, category role, value role)"""
        from cli_anything.powerbi.core.project import ProjectError
        k = kind.lower()
        if k in V.TYPES:
            vtype, spec, cat, val = V.TYPES[k]
            return vtype, V.roles_of(spec), cat, val
        for guid, cv in self.custom_visuals().items():
            if kind.lower() in (guid.lower(), (cv.get("name") or "").lower()):
                roles = cv.get("roles") or {}
                cat = next((r for r, d in roles.items() if d["kind"] == "g"), None)
                val = next((r for r, d in roles.items() if d["kind"] in ("m", "a")), None)
                return guid, roles, cat, val
        raise ProjectError(f"Unknown visual type '{kind}'. Built-in: {', '.join(V.TYPES)}, textbox; "
                           "custom visuals must be added with 'visual custom import/add-public' first")

    def add_textbox(self, page_ref, text, x=0, y=0, width=600, height=60, size=20, bold=True, z=None) -> str:
        p = self._page(page_ref)
        style = {"fontSize": f"{int(size)}pt"}
        if bold:
            style["fontWeight"] = "bold"
        visual = {"visualType": "textbox", "objects": {"general": [{"properties": {
            "paragraphs": [{"textRuns": [{"value": text, "textStyle": style}]}]}}]}}
        return self._write_visual(p, visual, x, y, width, height, z)

    def _write_visual(self, page, visual, x, y, width, height, z):
        vid = new_id()
        existing = self.visuals(page["id"])
        z = len(existing) * 1000 if z is None else z
        self._wj(f"definition/pages/{page['id']}/visuals/{vid}/visual.json", {
            "$schema": S_VISUAL, "name": vid,
            "position": {"x": round(x, 2), "y": round(y, 2), "z": z, "height": round(height, 2),
                         "width": round(width, 2), "tabOrder": len(existing)},
            "visual": visual})
        return vid

    def remove_visual(self, page_ref, vid):
        from cli_anything.powerbi.core.project import ProjectError
        p = self._page(page_ref)
        rels = [r for r in self.rfiles if r.startswith(f"definition/pages/{p['id']}/visuals/{vid}/")]
        if not rels:
            raise ProjectError(f"Visual '{vid}' not on page '{p['displayName']}'")
        for r in rels:
            self._rdel(r)

    # ── formatting ──
    def _visual_rel(self, page_ref, vid) -> str:
        from cli_anything.powerbi.core.project import ProjectError
        p = self._page(page_ref)
        rel = f"definition/pages/{p['id']}/visuals/{vid}/visual.json"
        if rel not in self.rfiles:
            raise ProjectError(f"Visual '{vid}' not on page '{p['displayName']}'")
        return rel

    def format_visual(self, page_ref, vid, settings, reset=()) -> dict:
        """settings: [(object, property, value)] e.g. ('legend', 'position', 'Bottom').
        Container cards (title, background, border, dropShadow, ...) go to visualContainerObjects."""
        rel = self._visual_rel(page_ref, vid)
        body = self._rj(rel)
        vis = body["visual"]
        for obj in reset:
            for key in ("objects", "visualContainerObjects"):
                vis.get(key, {}).pop(obj, None)
        for obj, prop, value in settings:
            key = "visualContainerObjects" if obj in V.CONTAINER_OBJECTS else "objects"
            entries = vis.setdefault(key, {}).setdefault(obj, [])
            entry = next((e for e in entries if "selector" not in e), None)
            if entry is None:
                entry = {"properties": {}}
                entries.insert(0, entry)
            entry.setdefault("properties", {})[prop] = V.literal(value)
        for key in ("objects", "visualContainerObjects"):
            if key in vis and not vis[key]:
                del vis[key]
        self._wj(rel, body)
        return {k: vis[k] for k in ("objects", "visualContainerObjects") if k in vis}

    def color_visual(self, page_ref, vid, color, series=None, point=None) -> dict:
        """Data colours: all data (no selector), one series (measure/field) or one category value."""
        from cli_anything.powerbi.core.project import ProjectError
        if not V.HEX.match(color):
            raise ProjectError(f"Colour must be #RRGGBB, got {color!r}")
        rel = self._visual_rel(page_ref, vid)
        body = self._rj(rel)
        vis = body["visual"]
        state = vis.get("query", {}).get("queryState", {})
        entry = {"properties": {"fill": V.literal(color)}}
        if series:
            ref = self.field(series, True)["queryRef"]
            if ref not in [pr.get("queryRef") for r in state.values() for pr in r.get("projections", [])]:
                raise ProjectError(f"'{series}' is not a field of this visual")
            entry["selector"] = {"metadata": ref}
            # A lone measure in the main value role with no legend is drawn per category, and Desktop
            # colours its bars with the unselected entry; the metadata entry only reaches the legend.
            spec = V.TYPES.get(V.BY_VISUAL_TYPE.get(vis.get("visualType"), ""))
            main = [pr.get("queryRef") for pr in state.get(spec[3], {}).get("projections", [])] if spec else []
            if main == [ref] and not state.get("Series", {}).get("projections"):
                plain = {"properties": {"fill": V.literal(color)}}
                ents = vis.setdefault("objects", {}).setdefault("dataPoint", [])
                ents[:] = [e for e in ents if "selector" in e] + [plain]
        elif point is not None:
            cat = next((r for r in ("Category", "Group", "Rows", "TrendLine") if r in state), None)
            if not cat:
                raise ProjectError("This visual has no category to colour a single point of")
            entry["selector"] = {"data": [{"scopeId": {"Comparison": {
                "ComparisonKind": 0, "Left": state[cat]["projections"][0]["field"],
                "Right": V.literal(point)["expr"] if not V.HEX.match(point) else {"Literal": {"Value": f"'{point}'"}}}}}]}
        entries = vis.setdefault("objects", {}).setdefault("dataPoint", [])
        entries[:] = [e for e in entries if e.get("selector") != entry.get("selector")] + [entry]
        self._wj(rel, body)
        return entry

    def sort_visual(self, page_ref, vid, spec, descending=True) -> dict:
        rel = self._visual_rel(page_ref, vid)
        body = self._rj(rel)
        f = self.field(spec, True)
        body["visual"].setdefault("query", {})["sortDefinition"] = {
            "sort": [{"field": f["field"], "direction": "Descending" if descending else "Ascending"}],
            "isDefaultSort": False}
        self._wj(rel, body)
        return {"sort": f["queryRef"], "direction": "Descending" if descending else "Ascending"}

    def move_visual(self, page_ref, vid, x=None, y=None, width=None, height=None, z=None) -> dict:
        rel = self._visual_rel(page_ref, vid)
        body = self._rj(rel)
        pos = body.setdefault("position", {})
        for k, v in dict(x=x, y=y, width=width, height=height, z=z).items():
            if v is not None:
                pos[k] = round(v, 2) if k != "z" else int(v)
        self._wj(rel, body)
        return pos

    # ── custom visuals ──
    def custom_visuals(self) -> dict:
        """guid -> {name, source: 'private'|'public', version, roles}"""
        if "definition/report.json" not in self.rfiles:
            return {}
        rep_ = self._rj("definition/report.json")
        out = {g: {"name": g, "source": "public", "version": None, "roles": {}}
               for g in rep_.get("publicCustomVisuals", [])}
        for guid in _private_guids(rep_):
            info = {"name": guid, "source": "private", "version": None, "roles": {}}
            pk = self.rfiles.get(f"{CV_DIR}/{guid}/package.json")
            if pk:
                vis = json.loads(pk).get("visual", {})
                info.update(name=vis.get("displayName") or guid, version=vis.get("version"))
            res = self.rfiles.get(f"{CV_DIR}/{guid}/resources/{guid}.pbiviz.json")
            if res:
                caps = json.loads(res).get("capabilities") or {}
                info["roles"] = {r["name"]: {"kind": {"Grouping": "g", "Measure": "m"}.get(r.get("kind"), "a"),
                                             "required": False} for r in caps.get("dataRoles", [])}
            out[guid] = info
        return out

    def import_custom_visual(self, path: str) -> dict:
        from cli_anything.powerbi.core.project import ProjectError
        self._pbir()
        pkg = V.read_pbiviz(path)
        guid = pkg["guid"]
        if guid in self.custom_visuals() and self.custom_visuals()[guid]["source"] == "public":
            raise ProjectError(f"{guid} is already registered as an AppSource visual")
        for rel in [r for r in self.rfiles if r.startswith(f"{CV_DIR}/{guid}/")]:
            self._rdel(rel)              # re-import replaces the old version
        for name, data in pkg["files"].items():
            self.rfiles[f"{CV_DIR}/{guid}/{name}"] = data.decode("utf-8-sig")
            self.dirty.add(f"{RP}{CV_DIR}/{guid}/{name}")
        rep_ = self._rj("definition/report.json")
        pk = rep_.setdefault("resourcePackages", [])
        pk[:] = [x for x in pk if not (x.get("type") == "CustomVisual" and x.get("name") == guid)]
        meta = f"{guid}.pbiviz.json"     # as Desktop writes it (checked against a Desktop-saved import)
        pk.append({"name": guid, "type": "CustomVisual",
                   "items": [{"name": meta, "path": meta, "type": "CustomVisualMetadata"}]})
        self._wj("definition/report.json", rep_)
        return {"guid": guid, "name": pkg["name"], "version": pkg["version"],
                "roles": {r: d["kind"] for r, d in pkg["roles"].items()}}

    def add_public_visual(self, guid: str) -> str:
        from cli_anything.powerbi.core.project import ProjectError
        self._pbir()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", guid):
            raise ProjectError(f"Bad visual id {guid!r} (use the AppSource visual GUID, e.g. ChicletSlicer1448559807354)")
        rep_ = self._rj("definition/report.json")
        lst = rep_.setdefault("publicCustomVisuals", [])
        if guid not in lst:
            lst.append(guid)
        self._wj("definition/report.json", rep_)
        return guid

    def remove_custom_visual(self, ref: str, force=False) -> str:
        from cli_anything.powerbi.core.project import ProjectError
        cvs = self.custom_visuals()
        guid = next((g for g, c in cvs.items() if ref.lower() in (g.lower(), c["name"].lower())), None)
        if not guid:
            raise ProjectError(f"Custom visual '{ref}' is not in this report")
        used = [r for r, t in self.rfiles.items() if r.endswith("visual.json")
                and json.loads(t).get("visual", {}).get("visualType") == guid]
        if used and not force:
            raise ProjectError(f"{len(used)} visual(s) still use {guid}; remove them or pass --force")
        rep_ = self._rj("definition/report.json")
        if guid in rep_.get("publicCustomVisuals", []):
            rep_["publicCustomVisuals"].remove(guid)
            if not rep_["publicCustomVisuals"]:
                del rep_["publicCustomVisuals"]
        rep_["resourcePackages"] = [x for x in rep_.get("resourcePackages", [])
                                    if not (x.get("type") == "CustomVisual" and x.get("name") == guid)]
        if not rep_["resourcePackages"]:
            del rep_["resourcePackages"]
        self._wj("definition/report.json", rep_)
        for rel in [r for r in self.rfiles if r.startswith(f"{CV_DIR}/{guid}/")] + (used if force else []):
            self._rdel(rel)
        return guid

    # ── layouts ──
    def build_page(self, name, title=None, cards=(), charts=(), table=None, replace=False,
                   margin=24, gap=16, slicers=()) -> dict:
        """Grid layout: title band, slicer band, KPI card row, charts, full-width table.

        charts: chart dicts laid out 2 per row, or a list of rows (each a list of chart dicts)."""
        from cli_anything.powerbi.core.project import ProjectError
        if not (title or cards or charts or table or slicers):
            raise ProjectError("Give at least one of --title, --slicer, --card, --chart, --table")
        if len(slicers) > 5:
            raise ProjectError("At most 5 slicers fit in the slicer band")
        try:
            p = self._page(name)
            if not replace and p["visuals"]:
                raise ProjectError(f"Page '{name}' already has visuals; use --replace")
            self.clear_page(p["id"])
        except ProjectError as e:
            if "not found" not in str(e):
                raise
            p = self._page(self.add_page(name, active=True))
        W, H = p["width"] or 1280, p["height"] or 720
        inner = W - 2 * margin
        y, made = margin, []
        if title:
            made.append(self.add_textbox(p["id"], title, margin, y - 8, inner, 52, size=20))
            y += 52 + gap - 8
        if slicers:
            n = len(slicers)
            sw = (inner - gap * (n - 1)) / n
            for i, f in enumerate(slicers):
                made.append(self.add_visual(p["id"], "slicer", [f], x=margin + i * (sw + gap), y=y,
                                            width=sw, height=84))
            y += 84 + gap
        if cards:
            n = len(cards)
            cw = (inner - gap * (n - 1)) / n
            for i, c in enumerate(cards):
                made.append(self.add_visual(p["id"], "card", [c], x=margin + i * (cw + gap), y=y,
                                            width=cw, height=104))
            y += 104 + gap
        charts = list(charts)
        if charts and isinstance(charts[0], (list, tuple)):
            rows = [list(r) for r in charts if r]
        else:
            rows = [charts[i:i + 2] for i in range(0, len(charts), 2)]
        n_rows = len(rows) + (1 if table else 0)
        if n_rows:
            rh = (H - margin - y - gap * (n_rows - 1)) / n_rows
            if rh < 120:
                raise ProjectError("Too many visuals for one page; split across pages or enlarge it")
            for row in rows:
                cw = (inner - gap * (len(row) - 1)) / len(row)
                for i, ch in enumerate(row):
                    made.append(self.add_visual(p["id"], ch["kind"], ch["values"], ch["category"],
                                                margin + i * (cw + gap), y, cw, rh, ch.get("title")))
                y += rh + gap
            if table:
                made.append(self.add_visual(p["id"], "table", table, x=margin, y=y, width=inner, height=rh))
        return {"page": p["displayName"], "page_id": p["id"], "visuals": made}

    # ── validation ──
    def report_issues(self) -> list[dict]:
        if not self.report_dir or "definition/report.json" not in self.rfiles:
            return []
        issues = []
        tables = {t["name"].lower(): t for t in self.tables()}
        for rel, text in self.rfiles.items():
            if not rel.endswith("visual.json"):
                continue
            for kind, ent, prop in _field_refs(json.loads(text)):
                t = tables.get(ent.lower())
                names = {x["name"].lower() for x in (t["measures"] if t and kind == "Measure" else
                                                     t["columns"] if t else [])}
                if not t or prop.lower() not in names:
                    issues.append({"level": "error", "where": rel,
                                   "message": f"visual uses missing {kind.lower()} {ent}[{prop}]"})
        cvs = self.custom_visuals()
        for rel, text in self.rfiles.items():
            if not rel.endswith("visual.json"):
                continue
            vis = json.loads(text).get("visual", {})
            vt, used = vis.get("visualType", ""), list(vis.get("query", {}).get("queryState", {}))
            if vt in V.BY_VISUAL_TYPE:
                rdefs = V.roles_of(V.TYPES[V.BY_VISUAL_TYPE[vt]][1])
            elif vt in cvs:
                rdefs = cvs[vt]["roles"]
            else:
                if V.CUSTOM_GUID.match(vt):
                    issues.append({"level": "error", "where": rel,
                                   "message": f"custom visual {vt} is not registered in the report"})
                continue
            if rdefs:
                for r in used:
                    if r not in rdefs:
                        issues.append({"level": "error", "where": rel,
                                       "message": f"{vt} has no data role '{r}' (roles: {', '.join(rdefs)})"})
                for r, d in rdefs.items():
                    if d["required"] and r not in used:
                        issues.append({"level": "warning", "where": rel,
                                       "message": f"{vt} is missing its {r} field"})
        ct = json.loads(self.rfiles["definition/report.json"]).get("themeCollection", {}).get("customTheme")
        if ct and f"{THEME_DIR}/{ct['name']}" not in self.rfiles:
            issues.append({"level": "error", "where": "definition/report.json",
                           "message": f"custom theme file {ct['name']} is missing"})
        return issues


def _field_refs(node):
    """Yield (Measure|Column, entity, property) from any visual JSON."""
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            v = node.get(kind)
            if isinstance(v, dict) and "Property" in v:
                ent = v.get("Expression", {}).get("SourceRef", {}).get("Entity")
                if ent:
                    yield kind, ent, v["Property"]
        for v in node.values():
            yield from _field_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _field_refs(v)


def _private_guids(report_json: dict) -> list[str]:
    return [p["name"] for p in report_json.get("resourcePackages", [])
            if p.get("type") == "CustomVisual" and p.get("name")]
