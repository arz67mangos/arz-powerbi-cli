"""E2E: Microsoft-serialised TMDL, installed CLI subprocess workflows,
Power BI Desktop discovery, and (opt-in) the live Power BI service."""
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cli_anything.powerbi.core import project as P
from cli_anything.powerbi.utils import powerbi_backend as pbi
from cli_anything.powerbi.tests.conftest import DS_ID, WS_ID

FIXTURE = Path(__file__).parent / "fixtures" / "TomExport.SemanticModel"


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CLI_ANYTHING_FORCE_INSTALLED=1 to require the installed command.
    """
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = name.replace("cli-anything-", "cli_anything.") + "." + name.split("-")[-1] + "_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


# ── Real TMDL written by Microsoft TOM ──

class TestTomFixture:
    def test_parse_tom_export(self):
        p = P.Project(str(FIXTURE))
        info = p.info()
        assert (info["tables"], info["measures"], info["relationships"]) == (2, 2, 1)
        m = {x["name"]: x for x in p.measures()}
        assert m["Total Sales"]["description"] == "Sum of amount"
        assert m["Average Sale"]["expression"] == "VAR n = COUNTROWS(Sales)\nRETURN DIVIDE([Total Sales], n)"
        assert p.validate() == []

    def test_edit_copy_preserves_untouched_files(self, tmp_path):
        dst = tmp_path / "TomExport.SemanticModel"
        shutil.copytree(FIXTURE, dst)
        before = {f.relative_to(dst): f.read_bytes() for f in dst.rglob("*.tmdl")}
        p = P.Project(str(dst))
        p.add_measure("Product", "Products", "COUNTROWS(Product)")
        p.add_table("Dim Region", 'DATATABLE("Region", STRING, {{"EU"}})', [("Region", "string")])
        p.save()
        after = {f.relative_to(dst): f.read_bytes() for f in dst.rglob("*.tmdl")}
        changed = {str(k).replace("\\", "/") for k in after if before.get(k) != after[k]}
        assert changed == {"definition/tables/Product.tmdl", "definition/model.tmdl",
                           "definition/tables/Dim Region.tmdl"}
        assert "ref table 'Dim Region'" in (dst / "definition/model.tmdl").read_text()
        assert P.Project(str(dst)).validate() == []


# ── Real Power BI Desktop install ──

class TestDesktopBackend:
    def test_find_desktop(self):
        exe = pbi.find_desktop()  # hard dependency: fails if Desktop is missing
        assert os.path.exists(exe)
        print(f"\n  Power BI Desktop: {exe}")


# ── Installed CLI ──

class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-powerbi")

    def _run(self, args, check=True, input=None, env=None):
        r = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True,
                           encoding="utf-8", input=input, env={**os.environ, **(env or {})})
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _j(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    @pytest.fixture
    def pbip(self, tmp_path):
        out = self._j(["project", "new", str(tmp_path / "proj"), "-n", "Demo"])
        return out["created"]

    def test_help(self):
        r = self._run(["--help"])
        for g in ["project", "measure", "table", "relationship", "dataset", "dax", "workspace", "auth"]:
            assert g in r.stdout

    def test_project_new_json(self, pbip):
        assert pbip.endswith("Demo.pbip") and os.path.exists(pbip)
        info = self._j(["-p", pbip, "project", "info"])
        assert info["tables"] == 1 and info["columns"] == 3

    def test_measure_authoring_workflow(self, pbip):
        self._run(["-p", pbip, "measure", "add", "Sales", "Total Sales", "SUM(Sales[Amount])",
                   "--format", "#,0.00", "--folder", "KPIs"])
        self._run(["-p", pbip, "measure", "add", "Sales", "Avg Sale", "-"],
                  input="VAR n = COUNTROWS(Sales)\nRETURN DIVIDE([Total Sales], n)")
        self._run(["-p", pbip, "measure", "add", "Sales", "Orders", "COUNTROWS(Sales)"])
        self._run(["-p", pbip, "measure", "update", "Orders", "--format", "0", "--rename", "Order Count"])
        ms = {m["name"]: m for m in self._j(["-p", pbip, "measure", "list"])}
        assert set(ms) == {"Total Sales", "Avg Sale", "Order Count"}
        assert ms["Order Count"]["formatString"] == "0"
        assert ms["Avg Sale"]["expression"].startswith("VAR n")
        assert self._j(["-p", pbip, "project", "validate"])["valid"] is True
        tmdl_file = Path(pbip).parent / "Demo.SemanticModel/definition/tables/Sales.tmdl"
        text = tmdl_file.read_text(encoding="utf-8")
        assert "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n\t\tformatString: #,0.00" in text
        print(f"\n  TMDL: {tmdl_file} ({tmdl_file.stat().st_size:,} bytes)")

    def test_dry_run_does_not_save(self, pbip):
        out = self._j(["-p", pbip, "--dry-run", "measure", "add", "Sales", "X", "1"])
        assert out["measure"] == "X"
        assert self._j(["-p", pbip, "measure", "list"]) == []

    def test_star_schema_workflow(self, pbip):
        self._run(["-p", pbip, "table", "add", "Product", "--dax",
                   'DATATABLE("Product", STRING, "Category", STRING, {{"Widget","Tools"}})',
                   "-c", "Product:string", "-c", "Category:string"])
        rid = self._j(["-p", pbip, "relationship", "add", "Sales.Product", "Product.Product"])["id"]
        assert self._j(["-p", pbip, "relationship", "list"])[0]["to_table"] == "Product"
        blocked = self._run(["--json", "-p", pbip, "table", "remove", "Product"], check=False)
        assert blocked.returncode == 1 and "relationship" in json.loads(blocked.stdout)["error"]
        self._run(["-p", pbip, "relationship", "remove", rid])
        self._run(["-p", pbip, "table", "remove", "Product"])
        assert [t["name"] for t in self._j(["-p", pbip, "table", "list"])] == ["Sales"]

    def test_project_open_is_remembered(self, pbip):
        self._run(["project", "open", pbip])
        assert self._j(["project", "info"])["name"] == "Demo"
        self._run(["project", "close"])
        r = self._run(["--json", "project", "info"], check=False)
        assert r.returncode == 1 and "No project" in json.loads(r.stdout)["error"]

    def test_validate_exit_code(self, pbip):
        self._run(["-p", pbip, "measure", "add", "Sales", "Bad", "SUM(Sales[Amount]"])
        r = self._run(["--json", "-p", pbip, "project", "validate"], check=False)
        assert r.returncode == 2 and json.loads(r.stdout)["valid"] is False

    def test_errors_are_json(self, tmp_path):
        r = self._run(["--json", "-p", str(tmp_path / "nope.pbip"), "project", "info"], check=False)
        assert r.returncode == 1 and "not found" in json.loads(r.stdout)["error"]

    def test_repl_scripted(self, pbip):
        r = self._run(["-p", pbip, "repl"], input=(
            'measure add Sales A "1"\nmeasure add Sales B "2"\nundo\nsave\nquit\n'))
        assert "Undone" in r.stdout
        assert [m["name"] for m in self._j(["-p", pbip, "measure", "list"])] == ["A"]

    def test_desktop_find(self):
        assert os.path.exists(self._j(["desktop", "find"])["desktop"])

    # ── REST via the installed CLI against the stub API ──

    def test_service_workflow(self, stub_api, tmp_path):
        env = {"POWERBI_API_BASE": stub_api.base, "POWERBI_ACCESS_TOKEN": "test-token"}
        assert self._j(["workspace", "list"], env=env)[0]["id"] == WS_ID
        assert self._j(["workspace", "use", "Sales Analytics"], env=env)["workspace_id"] == WS_ID
        assert self._j(["dataset", "list"], env=env)[0]["name"] == "Demo"
        assert self._j(["dataset", "use", "Demo"], env=env)["dataset_id"] == DS_ID
        ref = self._j(["dataset", "refresh", "--wait"], env=env)
        assert ref["accepted"] and ref["result"]["status"] == "Completed"
        assert self._j(["dataset", "refresh-history"], env=env)[0]["requestId"] == "req-123"
        assert self._j(["report", "list"], env=env)[0]["id"] == "r1"
        out = tmp_path / "rows.csv"
        res = self._j(["dax", "query", "EVALUATE Sales", "--csv", str(out)], env=env)
        assert res["row_count"] == 2
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        assert rows[0] == {"Sales[Product]": "Widget", "[Total]": "165.5"}
        print(f"\n  CSV: {out} ({out.stat().st_size:,} bytes)")
        assert all(r["path"].startswith(f"/groups/{WS_ID}") for r in stub_api.requests
                   if "datasets" in r["path"])

    def test_service_failed_refresh_exits_nonzero(self, stub_api):
        stub_api.refresh_status = "Failed"
        env = {"POWERBI_API_BASE": stub_api.base, "POWERBI_ACCESS_TOKEN": "test-token"}
        r = self._run(["--json", "-w", WS_ID, "dataset", "refresh", DS_ID, "--wait"], env=env, check=False)
        assert r.returncode == 1 and "Failed" in r.stdout

    def test_dax_error_json(self, stub_api):
        env = {"POWERBI_API_BASE": stub_api.base, "POWERBI_ACCESS_TOKEN": "test-token"}
        r = self._run(["--json", "-w", WS_ID, "dax", "query", "EVALUATE BAD", "-d", DS_ID],
                      env=env, check=False)
        assert r.returncode == 1 and "HTTP 400" in json.loads(r.stdout)["error"]

    def test_not_signed_in(self):
        env = {"POWERBI_ACCESS_TOKEN": ""}
        r = self._run(["--json", "workspace", "list"], env=env, check=False)
        assert r.returncode == 1 and "auth login" in json.loads(r.stdout)["error"]


# ── Power Query: TMDL serialised by Microsoft TOM ──

PQ_FIXTURE = Path(__file__).parent / "fixtures" / "TomPowerQuery.SemanticModel"


class TestTomPowerQueryFixture:
    """CLI-generated import/clean/merge/append model, re-serialised by Microsoft TOM.
    The same model was refreshed in Power BI Desktop (see TEST.md)."""

    def test_parse_tom_m_partitions(self):
        p = P.Project(str(PQ_FIXTURE))
        assert sorted(p.table_files()) == ["Orders", "OrdersFeb", "Products"]
        q = p.query("Orders")
        names = [s["name"] for s in q["steps"]]
        assert names[:3] == ["Source", "Promoted Headers", "Changed Type"]
        assert {"Merged Queries", "Expanded Products", "Appended Query", "Replaced Value"} <= set(names)
        assert q["result"] == '#"Replaced Value"'
        assert p.query("Products")["steps"][1]["name"] == "Products_Sheet"
        assert p.validate() == []

    def test_edit_tom_copy(self, tmp_path):
        dst = tmp_path / "pq.SemanticModel"
        shutil.copytree(PQ_FIXTURE, dst)
        p = P.Project(str(dst))
        p.set_types("Orders", {"Quantity": "number"})
        p.save()
        p2 = P.Project(str(dst))
        assert p2.query("Orders")["result"] == '#"Changed Type2"'
        assert {c["name"]: c["dataType"] for c in p2.table("Orders")["columns"]}["Quantity"] == "double"
        assert (dst / "definition/tables/Products.tmdl").read_bytes() == \
            (PQ_FIXTURE / "definition/tables/Products.tmdl").read_bytes()


class TestPowerQuerySubprocess:
    CLI_BASE = _resolve_cli("cli-anything-powerbi")

    def _run(self, args, check=True):
        r = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _j(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    @pytest.fixture
    def shop(self, tmp_path):
        (tmp_path / "orders.csv").write_text(
            "OrderID,Order Date,Product,Qty,Price,Region\n1,2026-01-03,Widget ,2,9.99,EU\n"
            "2,2026-01-04,gadget,1,19.5,\n2,2026-01-04,gadget,1,19.5,\n", encoding="utf-8")
        (tmp_path / "feb.csv").write_text(
            "OrderID,Order Date,Product,Quantity,Price,Region\n10,2026-02-01,Widget,3,9.99,APAC\n",
            encoding="utf-8")
        import datetime
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products"
        for r in [("Product", "Category", "Launched"), ("Widget", "Tools", datetime.date(2024, 1, 1)),
                  ("Gadget", "Toys", datetime.date(2025, 6, 1))]:
            ws.append(r)
        wb.save(tmp_path / "products.xlsx")
        pbip = self._j(["project", "new", str(tmp_path / "proj"), "-n", "Shop", "--no-sample"])["created"]
        return tmp_path, pbip

    def test_data_prep_workflow(self, shop):
        d, pbip = shop
        p = ["-p", pbip]
        cols = {c["name"]: c["type"] for c in
                self._j(p + ["import", "csv", str(d / "orders.csv"), "-t", "Orders"])["columns"]}
        assert cols == {"OrderID": "int", "Order Date": "date", "Product": "text", "Qty": "int",
                        "Price": "number", "Region": "text"}
        self._j(p + ["import", "csv", str(d / "feb.csv"), "-t", "Feb"])
        xl = self._j(p + ["import", "excel", str(d / "products.xlsx"), "--sheet", "Products"])
        assert xl["source"] == "Sheet:Products" and xl["columns"][2]["type"] == "date"
        for args in (["clean", "text", "Orders", "trim", "Product"],
                     ["clean", "text", "Orders", "proper", "Product"],
                     ["clean", "dedupe", "Orders"],
                     ["clean", "replace", "Orders", "Region", "", "Unknown"],
                     ["clean", "set-type", "Orders", "Price=decimal"],
                     ["clean", "rename", "Orders", "Qty=Quantity"],
                     ["clean", "add-column", "Orders", "Line Total", "[Quantity] * [Price]", "--type", "decimal"],
                     ["clean", "filter", "Orders", "[Quantity] > 0"]):
            self._j(p + args)
        app = self._j(p + ["query", "append", "Orders", "Feb"])
        assert app["added_columns"] == []          # Feb already uses 'Quantity'
        mer = self._j(p + ["query", "merge", "Orders", "Products", "--on", "Product", "-e", "Category"])
        assert mer["added_columns"] == ["Category"]
        assert self._j(p + ["project", "validate"])["valid"] is True
        tbl = self._j(p + ["table", "show", "Orders"])
        assert [c["name"] for c in tbl["columns"]] == [
            "OrderID", "Order Date", "Product", "Quantity", "Price", "Region", "Line Total", "Category"]
        m = self._run(p + ["query", "show", "Orders"]).stdout
        for frag in ['Csv.Document(File.Contents(', 'Text.Proper, type text', 'Table.Distinct(',
                     'Replacer.ReplaceValue, {"Region"}', '{{"Price", Currency.Type}}',
                     '{{"Qty", "Quantity"}}', 'Table.Combine({#"Filtered Rows", Feb})',
                     'JoinKind.LeftOuter', 'in\n    #"Expanded Products"']:
            assert frag in m, frag
        tmdl_file = Path(pbip).parent / "Shop.SemanticModel/definition/tables/Orders.tmdl"
        print(f"\n  TMDL: {tmdl_file} ({tmdl_file.stat().st_size:,} bytes)")

    def test_steps_edit_and_dry_run(self, shop):
        d, pbip = shop
        p = ["-p", pbip]
        self._j(p + ["import", "csv", str(d / "orders.csv"), "-t", "Orders"])
        self._j(p + ["--dry-run", "clean", "filter", "Orders", "[Qty] > 1"])
        assert [s["name"] for s in self._j(p + ["query", "steps", "Orders"])][-1] == "Changed Type"
        self._j(p + ["query", "add-step", "Orders", "Sorted", 'Table.Sort({prev}, {{"OrderID", Order.Descending}})'])
        assert self._j(p + ["query", "show", "Orders"])["result"] == "Sorted"
        self._j(p + ["query", "remove-step", "Orders", "Sorted"])
        assert self._j(p + ["query", "show", "Orders"])["result"] == '#"Changed Type"'
        r = self._run(["--json"] + p + ["clean", "set-type", "Orders", "Nope=int"], check=False)
        assert r.returncode == 1 and "not in 'Orders'" in json.loads(r.stdout)["error"]
        r = self._run(["--json"] + p + ["clean", "set-type", "Orders", "Qty=varchar"], check=False)
        assert r.returncode == 1 and "Unknown type" in json.loads(r.stdout)["error"]

    def test_append_as_new_and_anti_merge(self, shop):
        d, pbip = shop
        p = ["-p", pbip]
        self._j(p + ["import", "csv", str(d / "orders.csv"), "-t", "Jan"])
        self._j(p + ["import", "csv", str(d / "feb.csv"), "-t", "Feb"])
        new = self._j(p + ["query", "append", "Jan", "Feb", "--as-new", "All Orders"])
        assert new["table"] == "All Orders" and "Quantity" in new["columns"] and "Qty" in new["columns"]
        assert "All Orders" in [t["name"] for t in self._j(p + ["table", "list"])]
        anti = self._j(p + ["query", "merge", "Jan", "Feb", "--on", "OrderID", "--kind", "leftanti"])
        assert anti["added_columns"] == []
        assert self._j(p + ["project", "validate"])["valid"] is True


class TestThemeSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-powerbi")

    def _run(self, args, check=True):
        r = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _j(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    @pytest.fixture
    def demo(self, tmp_path):
        pbip = self._j(["project", "new", str(tmp_path / "proj"), "-n", "Demo"])["created"]
        self._j(["-p", pbip, "measure", "add", "Sales", "Revenue", "SUM(Sales[Amount])"])
        return tmp_path, pbip

    def test_theme_file_roundtrip(self, demo):
        d, pbip = demo
        presets = {x["preset"] for x in self._j(["theme", "presets"])}
        assert {"modern", "dark", "corporate", "warm", "accessible"} <= presets
        assert "colorblind" in {x["palette"] for x in self._j(["theme", "palettes"])}
        out = d / "brand.json"
        made = self._j(["theme", "create", str(out), "-n", "Brand", "--preset", "warm",
                        "--palette", "#112233,#445566", "--radius", "6", "--no-shadow"])
        assert made["dataColors"] == ["#112233", "#445566"] and made["warnings"] == []
        t = json.loads(out.read_text(encoding="utf-8"))
        assert t["visualStyles"]["*"]["*"]["border"][0]["radius"] == 6
        assert self._j(["theme", "validate", str(out)])["valid"] is True
        bad = d / "bad.json"
        bad.write_text('{"dataColors": ["red"]}', encoding="utf-8")
        r = self._run(["--json", "theme", "validate", str(bad)], check=False)
        assert r.returncode == 2 and json.loads(r.stdout)["valid"] is False
        r = self._run(["--json", "-p", pbip, "theme", "apply", str(bad)], check=False)
        assert r.returncode == 1 and "Invalid theme" in json.loads(r.stdout)["error"]
        applied = self._j(["-p", pbip, "theme", "apply", str(out)])
        assert applied["applied"] == "Brand"
        reg = Path(pbip).parent / "Demo.Report/StaticResources/RegisteredResources" / applied["file"]
        assert json.loads(reg.read_text(encoding="utf-8")) == t
        assert self._j(["-p", pbip, "theme", "show"])["custom"] == applied["file"]

    def test_styled_dashboard_workflow(self, demo):
        d, pbip = demo
        p = ["-p", pbip]
        self._j(p + ["--dry-run", "theme", "apply", "--preset", "dark"])
        assert self._j(p + ["theme", "show"])["custom"] is None          # dry run wrote nothing
        self._j(p + ["theme", "apply", "--preset", "modern", "-n", "Modern Cards"])
        res = self._j(p + ["page", "build", "Overview", "--title", "Sales Overview",
                           "--card", "[Revenue]", "--card", "count(Sales.Product)",
                           "--chart", "column:Sales.Product:[Revenue]:Revenue by product",
                           "--chart", "line:Sales.Date:Sales.Amount",
                           "--table", "Sales.Date,Sales.Product,[Revenue]"])
        assert len(res["visuals"]) == 6
        vis = self._j(p + ["visual", "list", "Overview"])
        assert {v["type"] for v in vis} == {"textbox", "card", "clusteredColumnChart", "lineChart", "tableEx"}
        assert "Revenue by product" in {v["title"] for v in vis}
        pages = {x["displayName"]: x for x in self._j(p + ["page", "list"])}
        assert pages["Overview"]["visuals"] == 6 and pages["Overview"]["active"]
        vid = self._j(p + ["visual", "add", "Page 1", "donut", "-f", "[Revenue]", "-c", "Sales.Product",
                           "-t", "Share"])["id"]
        self._j(p + ["visual", "remove", "Page 1", vid])
        r = self._run(["--json"] + p + ["visual", "add", "Page 1", "card", "-f", "[Nope]"], check=False)
        assert r.returncode == 1 and "not found" in json.loads(r.stdout)["error"]
        self._j(p + ["page", "add", "Scratch"])
        self._j(p + ["page", "remove", "Scratch"])
        assert self._j(p + ["project", "validate"])["valid"] is True
        self._j(p + ["measure", "remove", "Revenue"])
        v = self._j(p + ["project", "validate"], check=False)
        assert v["valid"] is False and any("Revenue" in i["message"] for i in v["issues"])
        self._j(p + ["theme", "remove"])
        assert self._j(p + ["theme", "show"])["custom"] is None
        print(f"\n  Report: {Path(pbip).parent / 'Demo.Report'}")


class TestTemplateSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-powerbi")

    def _run(self, args, check=True):
        r = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _j(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    @pytest.fixture
    def retail(self, tmp_path):
        import openpyxl
        prods = ["Widget", "Gadget", "Sprocket", "Gizmo", "Doohickey", "Thing", "Whatsit", "Contraption"]
        lines = ["OrderID,Order Date,Product,Region,Channel,Quantity,Unit Price,Discount Rate"]
        for i in range(60):
            lines.append(f"{1000 + i},2026-0{1 + i % 6}-1{i % 9},{prods[i % 8]},"
                         f"{['North', 'South', 'East', 'West'][i % 4]},{['Online', 'Retail'][i % 2]},"
                         f"{1 + i % 7},{4.25 + i % 5},{0.05 * (i % 3):.2f}")
        (tmp_path / "sales.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products"
        ws.append(["Product", "Category"])
        for i, p in enumerate(prods):
            ws.append([p, ["Tools", "Toys", "Home"][i % 3]])
        wb.save(tmp_path / "products.xlsx")
        pbip = self._j(["project", "new", str(tmp_path / "proj"), "-n", "Retail", "--no-sample"])["created"]
        p = ["-p", pbip]
        self._j(p + ["import", "csv", str(tmp_path / "sales.csv"), "-t", "Sales"])
        self._j(p + ["import", "excel", str(tmp_path / "products.xlsx"), "--sheet", "Products"])
        self._j(p + ["relationship", "add", "Sales.Product", "Products.Product"])
        return tmp_path, pbip

    def test_suggest_and_template(self, retail):
        d, pbip = retail
        p = ["-p", pbip]
        plan = self._j(p + ["page", "suggest", "Sales"])
        assert plan["slicers"][0] == "Sales.Order Date" and plan["trend"]["kind"] == "line"
        cats = {c["column"]: c for c in plan["columns"]}
        assert cats["Category"]["distinct"] == 3 and cats["Category"]["related"]   # sampled from the xlsx
        assert cats["OrderID"]["role"] == "key"
        text = self._run(p + ["page", "suggest", "Sales"]).stdout
        assert "date -> range slicer" in text and "share of total" in text
        self._j(p + ["--dry-run", "page", "template", "Dash", "-t", "Sales", "--create-measures"])
        assert self._j(p + ["measure", "list"]) == []                    # dry run saved nothing
        res = self._j(p + ["page", "template", "Dash", "-t", "Sales", "--create-measures", "--monthly"])
        assert res["measures_created"] == ["Total Quantity", "Average Unit Price", "Average Discount Rate",
                                           "Sales Count"]
        assert len(res["visuals"]) == 1 + 3 + 4 + 4       # title, slicers, cards, trend+2 comparisons+table
        vis = self._j(p + ["visual", "list", "Dash"])
        assert ["Sales.Order Date Month", "Sales.Total Quantity"] in [v["fields"] for v in vis]
        trend = self._j(p + ["page", "template", "Trends", "-t", "Sales", "-k", "trend", "--monthly"])
        assert trend["template"] == "trend"
        r = self._run(["--json"] + p + ["page", "template", "Dash", "-t", "Sales"], check=False)
        assert r.returncode == 1 and "--replace" in json.loads(r.stdout)["error"]
        r = self._run(["--json"] + p + ["page", "template", "X", "-t", "Products", "--monthly"], check=False)
        assert r.returncode == 1 and "no date column" in json.loads(r.stdout)["error"]
        vid = self._j(p + ["visual", "add", "Dash", "slicer", "-f", "Products.Category", "--mode", "Basic"])["id"]
        self._j(p + ["visual", "remove", "Dash", vid])
        self._j(p + ["page", "build", "Hand", "--slicer", "Sales.Region", "--card", "[Sales Count]"])
        assert {v["type"] for v in self._j(p + ["visual", "list", "Hand"])} == {"slicer", "card"}
        assert self._j(p + ["project", "validate"])["valid"] is True

    def test_generate_from_spec(self, retail):
        d, pbip = retail
        p = ["-p", pbip]
        spec = d / "dashboard.json"
        spec.write_text(json.dumps({
            "theme": {"preset": "modern", "palette": "ocean", "name": "Retail"},
            "create_measures": True, "monthly": True,
            "pages": [{"name": "Overview", "table": "Sales", "title": "Retail Sales Overview"},
                      {"name": "Trends", "table": "Sales", "template": "trend"},
                      {"name": "By Region", "table": "Sales", "template": "comparison",
                       "slicers": ["Sales.Order Date", "Products.Category"]}]}), encoding="utf-8")
        res = self._j(p + ["page", "generate", str(spec)])
        assert [x["page"] for x in res["pages"]] == ["Overview", "Trends", "By Region"]
        assert res["theme"].startswith("Retail") and len(res["measures_created"]) == 4
        pages = {x["displayName"]: x for x in self._j(p + ["page", "list"])}
        assert pages["Overview"]["active"] and pages["By Region"]["visuals"] == 1 + 2 + 4 + 3
        assert self._j(p + ["theme", "show"])["theme"]["dataColors"][0] == "#0EA5E9"
        r = self._run(["--json"] + p + ["page", "generate", str(spec)], check=False)
        assert r.returncode == 1 and "--replace" in json.loads(r.stdout)["error"]
        assert len(self._j(p + ["page", "generate", str(spec), "--replace"])["pages"]) == 3
        bad = d / "bad.json"
        bad.write_text('{"pages": [{"table": "Sales"}]}', encoding="utf-8")
        r = self._run(["--json"] + p + ["page", "generate", str(bad)], check=False)
        assert r.returncode == 1 and "needs 'name'" in json.loads(r.stdout)["error"]
        assert self._j(p + ["project", "validate"])["valid"] is True
        print(f"\n  Report: {Path(pbip).parent / 'Retail.Report'}")


class TestVisualsSubprocess:
    CLI_BASE = _resolve_cli("cli-anything-powerbi")
    PBIVIZ = Path(__file__).parent / "fixtures" / "probeViz.pbiviz"
    GUID = "probeViz59BFF8F25AB44BEF9A1BF2109149192E"

    def _run(self, args, check=True):
        r = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _j(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    @pytest.fixture
    def demo(self, tmp_path):
        pbip = self._j(["project", "new", str(tmp_path / "proj"), "-n", "Viz"])["created"]
        self._j(["-p", pbip, "measure", "add", "Sales", "Total", "SUM(Sales[Amount])"])
        return tmp_path, pbip

    def test_advanced_charts_and_formatting(self, demo):
        d, pbip = demo
        p = ["-p", pbip]
        types = {t["type"]: t for t in self._j(p + ["visual", "types"])}
        assert {"combo", "scatter", "matrix", "gauge", "kpi", "waterfall", "treemap", "ribbon"} <= set(types)
        assert types["scatter"]["roles"] == "Category:g, Series:g, X:m!, Y:m!, Size:m"
        combo = self._j(p + ["visual", "add", "Page 1", "combo", "-c", "Sales.Product", "-r", "Y=[Total]",
                             "-r", "Y2=avg(Sales.Amount)", "-t", "Total vs average"])["id"]
        self._j(p + ["visual", "add", "Page 1", "matrix", "-r", "Rows=Sales.Product", "-r", "Values=[Total]",
                     "--x", "440"])
        self._j(p + ["visual", "add", "Page 1", "gauge", "-f", "[Total]", "-r", "TargetValue=sum(Sales.Amount)",
                     "--y", "300"])
        r = self._run(["--json"] + p + ["visual", "add", "Page 1", "scatter", "-r", "X=[Total]"], check=False)
        assert r.returncode == 1 and "needs a Y field" in json.loads(r.stdout)["error"]
        r = self._run(["--json"] + p + ["visual", "add", "Page 1", "column", "-r", "Total"], check=False)
        assert r.returncode == 1 and "ROLE=FIELD" in json.loads(r.stdout)["error"]
        fmt = self._j(p + ["visual", "format", "Page 1", combo, "legend.show=true", "legend.position=Bottom",
                           "labels.show=true", "title.fontSize=14"])
        assert fmt["objects"]["legend"][0]["properties"]["position"]["expr"]["Literal"]["Value"] == "'Bottom'"
        assert "fontSize" in fmt["visualContainerObjects"]["title"][0]["properties"]
        self._j(p + ["visual", "color", "Page 1", combo, "#0EA5E9", "--series", "[Total]"])
        self._j(p + ["visual", "color", "Page 1", combo, "#F59E0B", "--point", "Gadget"])
        assert self._j(p + ["visual", "sort", "Page 1", combo, "[Total]"])["direction"] == "Descending"
        assert self._j(p + ["visual", "move", "Page 1", combo, "--x", "600", "-W", "640"])["width"] == 640
        r = self._run(["--json"] + p + ["visual", "format", "Page 1", combo, "legend"], check=False)
        assert r.returncode == 1 and "object.property" in json.loads(r.stdout)["error"]
        self._j(p + ["--dry-run", "visual", "move", "Page 1", combo, "--x", "1"])
        assert next(v for v in self._j(p + ["visual", "list", "Page 1"]) if v["id"] == combo)["x"] == 600
        assert self._j(p + ["project", "validate"])["valid"] is True

    def test_feedback_fixes(self, demo):
        d, pbip = demo
        p = ["-p", pbip]
        self._j(p + ["visual", "add", "Page 1", "slicer", "-c", "Sales.Product", "-t", "Product"])
        tb = self._j(p + ["visual", "add", "Page 1", "textbox", "-f", "Hello", "-f", "World", "--size", "11",
                          "--no-bold", "--color", "#0F766E", "--align", "center", "--bg", "#FFFFFF",
                          "--border", "#0F766E", "--radius", "20", "-H", "30"])
        assert "clip" in tb["warning"]
        ok = self._j(p + ["visual", "add", "Page 1", "textbox", "-f", "Fits", "--size", "11", "-H", "40"])
        assert "warning" not in ok
        r = self._run(["--json"] + p + ["visual", "add", "Page 1", "textbox", "-f", "x", "--color", "red"],
                      check=False)
        assert r.returncode == 1 and "#RRGGBB" in json.loads(r.stdout)["error"]
        pf = self._j(p + ["page", "format", "Page 1", "--background", "#E3F2EF", "background.transparency=10"])
        assert pf["objects"]["background"][0]["properties"]["transparency"]["expr"]["Literal"]["Value"] == "10D"
        props = {r_["object"]: r_["properties"] for r_ in self._j(p + ["visual", "props", "lineChart"])}
        assert "start" in props["valueAxis"]
        assert self._j(p + ["project", "validate"])["valid"] is True

    def test_check_visuals_and_desktop_cmds(self, demo):
        d, pbip = demo
        p = ["-p", pbip]
        self._j(p + ["visual", "add", "Page 1", "textbox", "-f", "Tight", "--size", "11", "-H", "26"])
        r = self._run(["--json"] + p + ["project", "check-visuals", "Page 1"], check=False)
        out = json.loads(r.stdout)
        assert r.returncode == 0 and out["ok"] is True and "clip" in out["issues"][0]["message"]
        self._j(p + ["page", "add", "Other"])
        assert self._j(p + ["project", "check-visuals", "Other"]) == {"ok": True, "issues": []}
        if os.name == "nt":
            assert isinstance(self._j(["desktop", "windows"]), list)
            r = self._run(["--json"] + p + ["desktop", "screenshot", str(d / "x.png")], check=False)
            assert r.returncode == 1 and "No Power BI Desktop window" in json.loads(r.stdout)["error"]

    def test_custom_visuals(self, demo):
        d, pbip = demo
        p = ["-p", pbip]
        imp = self._j(p + ["visual", "custom", "import", str(self.PBIVIZ)])
        assert imp["guid"] == self.GUID and imp["roles"] == {"category": "g", "measure": "m"}
        self._j(p + ["visual", "custom", "add-public", "ChicletSlicer1448559807354"])
        cvs = {c["guid"]: c for c in self._j(p + ["visual", "custom", "list"])}
        assert cvs[self.GUID]["name"] == "Probe Viz" and cvs["ChicletSlicer1448559807354"]["source"] == "public"
        assert "Probe Viz" in {t["type"] for t in self._j(p + ["visual", "types"])}
        vid = self._j(p + ["visual", "add", "Page 1", "Probe Viz", "-r", "category=Sales.Product",
                           "-r", "measure=[Total]"])["id"]
        stored = Path(pbip).parent / "Viz.Report" / "CustomVisuals" / self.GUID
        assert (stored / "package.json").is_file()
        assert (stored / "resources" / f"{self.GUID}.pbiviz.json").is_file()
        r = self._run(["--json"] + p + ["visual", "add", "Page 1", "Nope Viz", "-f", "[Total]"], check=False)
        assert r.returncode == 1 and "Unknown visual type" in json.loads(r.stdout)["error"]
        r = self._run(["--json"] + p + ["visual", "custom", "remove", "Probe Viz"], check=False)
        assert r.returncode == 1 and "still use" in json.loads(r.stdout)["error"]
        self._j(p + ["visual", "remove", "Page 1", vid])
        self._j(p + ["visual", "custom", "remove", "Probe Viz"])
        assert not stored.exists()
        assert self._j(p + ["project", "validate"])["valid"] is True


# ── Live Power BI service (needs a real sign-in) ──

@pytest.mark.skipif(os.environ.get("POWERBI_LIVE") != "1",
                    reason="set POWERBI_LIVE=1 after 'cli-anything-powerbi auth login'")
class TestLiveService:
    @pytest.fixture(autouse=True)
    def real_home(self, monkeypatch):
        monkeypatch.delenv("POWERBI_API_BASE", raising=False)
        monkeypatch.setenv("CLI_ANYTHING_POWERBI_HOME",
                           os.environ.get("POWERBI_LIVE_HOME", str(Path.home() / ".cli-anything-powerbi")))

    def test_list_workspaces_and_datasets(self):
        ws = pbi.list_workspaces()
        print(f"\n  {len(ws)} workspaces")
        assert isinstance(pbi.list_datasets(), list)

    def test_dax_row(self):
        ds = os.environ.get("POWERBI_LIVE_DATASET")
        if not ds:
            pytest.skip("set POWERBI_LIVE_DATASET=<dataset id> (and POWERBI_LIVE_WORKSPACE)")
        ws = os.environ.get("POWERBI_LIVE_WORKSPACE")
        res = pbi.execute_dax(ds, 'EVALUATE ROW("x", 1)', ws)
        assert res["rows"] == [{"[x]": 1}]
