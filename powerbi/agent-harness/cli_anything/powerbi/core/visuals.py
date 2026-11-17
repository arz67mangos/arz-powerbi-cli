"""Visual type registry (built-in data roles), formatting literals, custom visual packages.

Role kinds: "g" grouping (columns, not summed), "m" measure (numeric columns auto-summed),
"a" either (table Values). A trailing "!" in the spec marks a required role.
"""
from __future__ import annotations

import json
import re
import zipfile

# alias -> (visualType, roles {name: kind[!]}, legacy category role, legacy value role)
TYPES = {
    "card": ("card", {"Values": "m!"}, None, "Values"),
    "multicard": ("multiRowCard", {"Values": "m!"}, None, "Values"),
    "column": ("clusteredColumnChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "bar": ("clusteredBarChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "stackedcolumn": ("columnChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "stackedbar": ("barChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "column100": ("hundredPercentStackedColumnChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "bar100": ("hundredPercentStackedBarChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "line": ("lineChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "area": ("areaChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "stackedarea": ("stackedAreaChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "combo": ("lineClusteredColumnComboChart", {"Category": "g!", "Y": "m", "Y2": "m", "Series": "g"},
              "Category", "Y"),
    "stackedcombo": ("lineStackedColumnComboChart", {"Category": "g!", "Y": "m", "Y2": "m", "Series": "g"},
                     "Category", "Y"),
    "ribbon": ("ribbonChart", {"Category": "g!", "Y": "m!", "Series": "g"}, "Category", "Y"),
    "pie": ("pieChart", {"Category": "g!", "Y": "m!"}, "Category", "Y"),
    "donut": ("donutChart", {"Category": "g!", "Y": "m!"}, "Category", "Y"),
    "funnel": ("funnel", {"Category": "g!", "Y": "m!"}, "Category", "Y"),
    "waterfall": ("waterfallChart", {"Category": "g!", "Breakdown": "g", "Y": "m!"}, "Category", "Y"),
    "treemap": ("treemap", {"Group": "g!", "Details": "g", "Values": "m!"}, "Group", "Values"),
    "scatter": ("scatterChart", {"Category": "g", "Series": "g", "X": "m!", "Y": "m!", "Size": "m"},
                "Category", "Y"),
    "gauge": ("gauge", {"Y": "m!", "MinValue": "m", "MaxValue": "m", "TargetValue": "m"}, None, "Y"),
    "kpi": ("kpi", {"Indicator": "m!", "TrendLine": "g!", "Goal": "m"}, "TrendLine", "Indicator"),
    "matrix": ("pivotTable", {"Rows": "g", "Columns": "g", "Values": "m!"}, "Rows", "Values"),
    "table": ("tableEx", {"Values": "a!"}, None, "Values"),
    "slicer": ("slicer", {"Values": "g!"}, None, "Values"),
}
BY_VISUAL_TYPE = {v[0]: k for k, v in TYPES.items()}

# formatting cards that live on the visual container (not visual.objects)
CONTAINER_OBJECTS = {"title", "subTitle", "background", "border", "dropShadow", "padding", "divider",
                     "spacing", "visualHeader", "visualTooltip", "visualLink", "stylePreset", "lockAspect"}
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# custom visual guids: AppSource ones end in a 13-digit timestamp, pbiviz tools write a 32-hex suffix
CUSTOM_GUID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*([0-9]{13}|[0-9A-F]{32})(_[A-Za-z]+)?$")


def roles_of(spec: dict) -> dict:
    """{'Y': 'm!'} -> {'Y': {'kind': 'm', 'required': True}}"""
    return {r: {"kind": k.rstrip("!"), "required": k.endswith("!")} for r, k in spec.items()}


def literal(value: str) -> dict:
    """CLI string -> PBIR literal expression (bool, number, colour, text)."""
    v = value.strip()
    if v.lower() in ("true", "false"):
        return {"expr": {"Literal": {"Value": v.lower()}}}
    if re.fullmatch(r"[-+]?\d+", v):
        return {"expr": {"Literal": {"Value": f"{int(v)}L"}}}
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)", v):
        return {"expr": {"Literal": {"Value": f"{float(v)}D"}}}
    if HEX.match(v):
        return {"solid": {"color": {"expr": {"Literal": {"Value": f"'{v.upper()}'"}}}}}
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":   # quoted: force text
        v = v[1:-1]
    return {"expr": {"Literal": {"Value": "'" + v.replace("'", "''") + "'"}}}


def parse_setting(s: str) -> tuple[str, str, str]:
    """'legend.position=Bottom' -> ('legend', 'position', 'Bottom')"""
    m = re.match(r"^\s*(\w+)\.(\w+)\s*=(.*)$", s, re.S)
    if not m:
        raise ValueError(f"Bad setting {s!r}; use object.property=value, e.g. legend.position=Bottom")
    return m.group(1), m.group(2), m.group(3)


# ── custom visual packages (.pbiviz) ──
def read_pbiviz(path: str) -> dict:
    """Open a .pbiviz (zip) and return {guid, name, version, roles, package, files}."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"{path}: not a .pbiviz package ({e})") from e
    with z:
        names = z.namelist()
        if "package.json" not in names:
            raise ValueError(f"{path}: package.json missing - not a .pbiviz")
        pkg = json.loads(z.read("package.json").decode("utf-8-sig"))
        vis = pkg.get("visual") or {}
        guid = vis.get("guid")
        if not guid or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", guid):
            raise ValueError(f"{path}: package.json has no valid visual.guid")
        res = next((n for n in names if n.startswith("resources/") and n.endswith(".pbiviz.json")), None)
        if not res:
            raise ValueError(f"{path}: resources/*.pbiviz.json missing")
        body = json.loads(z.read(res).decode("utf-8-sig"))
        files = {n: z.read(n) for n in names if not n.endswith("/")}
    caps = body.get("capabilities") or {}
    roles = {r["name"]: {"kind": {"Grouping": "g", "Measure": "m"}.get(r.get("kind"), "a"),
                         "required": False, "displayName": r.get("displayName", r["name"])}
             for r in caps.get("dataRoles", []) if r.get("name")}
    return {"guid": guid, "name": vis.get("displayName") or vis.get("name") or guid,
            "version": vis.get("version") or pkg.get("version"), "roles": roles,
            "package": pkg, "files": files}
