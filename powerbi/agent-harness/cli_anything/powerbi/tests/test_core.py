"""Unit tests: synthetic TMDL, no Power BI install or network needed."""
import json

import pytest

from cli_anything.powerbi.core import tmdl
from cli_anything.powerbi.core import project as P
from cli_anything.powerbi.core.session import Session
from cli_anything.powerbi.utils import powerbi_backend as pbi
from cli_anything.powerbi.tests.conftest import DS_ID, WS_ID

TABLE = """table 'Fact Sales'
\tlineageTag: t1

\t/// Revenue
\t/// in USD
\tmeasure Revenue = SUM('Fact Sales'[Amount])
\t\tformatString: #,0
\t\tlineageTag: m1

\tmeasure 'Rev ''LY''' =
\t\t\tCALCULATE(
\t\t\t\t[Revenue],
\t\t\t\tSAMEPERIODLASTYEAR('Date'[Date])
\t\t\t)
\t\tdisplayFolder: Time
\t\tlineageTag: m2

\tmeasure Fenced = ```
\t\t\tVAR x = 1
\t\t\tRETURN x
\t\t\t```
\t\tlineageTag: m3

\tcolumn Amount
\t\tdataType: double
\t\tisHidden
\t\tsourceColumn: Amount

\tcolumn Margin = [Amount] * 0.2
\t\tdataType: double

\tpartition 'Fact Sales' = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet x = 1 in x
"""


# ── names ──

def test_quote_roundtrip():
    for n in ["Sales", "Total Sales", "Rev 'LY'", "a.b", "1st"]:
        assert tmdl.unquote_name(tmdl.quote_name(n)) == n
    assert tmdl.quote_name("Sales_2") == "Sales_2"
    assert tmdl.quote_name("Total Sales") == "'Total Sales'"


def test_split_ref():
    assert tmdl.split_ref("'Fact Sales'.'Product Key'") == ("Fact Sales", "Product Key")
    assert tmdl.split_ref("Sales.Key") == ("Sales", "Key")
    with pytest.raises(ValueError):
        tmdl.split_ref("nodot")


# ── parsing ──

def test_parse_table_basics():
    t = tmdl.parse_table(TABLE)
    assert t["name"] == "Fact Sales"
    assert [m["name"] for m in t["measures"]] == ["Revenue", "Rev 'LY'", "Fenced"]
    assert [c["name"] for c in t["columns"]] == ["Amount", "Margin"]
    assert t["partitions"][0]["type"] == "m"


def test_parse_measure_variants():
    ms = {m["name"]: m for m in tmdl.parse_table(TABLE)["measures"]}
    assert ms["Revenue"]["expression"] == "SUM('Fact Sales'[Amount])"
    assert ms["Revenue"]["description"] == "Revenue\nin USD"
    assert ms["Revenue"]["formatString"] == "#,0"
    assert ms["Rev 'LY'"]["expression"].splitlines()[0] == "CALCULATE("
    assert ms["Rev 'LY'"]["expression"].splitlines()[1] == "\t[Revenue],"
    assert ms["Rev 'LY'"]["displayFolder"] == "Time"
    assert ms["Fenced"]["expression"] == "VAR x = 1\nRETURN x"


def test_parse_column_props():
    cs = {c["name"]: c for c in tmdl.parse_table(TABLE)["columns"]}
    assert cs["Amount"]["isHidden"] is True
    assert cs["Amount"]["dataType"] == "double"
    assert cs["Margin"]["expression"] == "[Amount] * 0.2"


def test_parse_requires_table():
    with pytest.raises(ValueError):
        tmdl.parse_table("model Model\n")


# ── editing ──

def test_add_measure_after_last_measure():
    out = tmdl.add_measure(TABLE, "Units", "COUNTROWS('Fact Sales')", format_string="0")
    t = tmdl.parse_table(out)
    assert [m["name"] for m in t["measures"]][-1] == "Units"
    lines = out.splitlines()
    assert lines.index("\tmeasure Units = COUNTROWS('Fact Sales')") < lines.index("\tcolumn Amount")
    assert "\n\n\n" not in out


def test_add_measure_multiline_and_empty_table():
    base = "table T\n\tlineageTag: x\n"
    out = tmdl.add_measure(base, "M", "VAR a = 1\nRETURN a", description="d")
    m = tmdl.parse_table(out)["measures"][0]
    assert m["expression"] == "VAR a = 1\nRETURN a" and m["description"] == "d"
    assert "lineageTag" in m


def test_add_measure_rejects_duplicates_and_empty():
    with pytest.raises(ValueError):
        tmdl.add_measure(TABLE, "revenue", "1")
    with pytest.raises(ValueError):
        tmdl.add_measure(TABLE, "Amount", "1")  # clashes with column
    with pytest.raises(ValueError):
        tmdl.add_measure(TABLE, "X", "  ")


def test_update_measure_keeps_lineage_and_other_props():
    out = tmdl.update_measure(TABLE, "Revenue", expression="SUMX('Fact Sales', [Amount])",
                              format_string="0.00")
    m = {x["name"]: x for x in tmdl.parse_table(out)["measures"]}["Revenue"]
    assert m["expression"] == "SUMX('Fact Sales', [Amount])"
    assert m["formatString"] == "0.00" and m["lineageTag"] == "m1"
    assert m["description"] == "Revenue\nin USD"


def test_update_measure_clear_and_rename():
    out = tmdl.update_measure(TABLE, "Rev 'LY'", display_folder=None, description=None,
                              new_name="Revenue LY")
    m = {x["name"]: x for x in tmdl.parse_table(out)["measures"]}["Revenue LY"]
    assert "displayFolder" not in m and m["lineageTag"] == "m2"
    assert m["expression"].startswith("CALCULATE(")


def test_remove_measure():
    out = tmdl.remove_measure(TABLE, "Rev 'LY'")
    names = [m["name"] for m in tmdl.parse_table(out)["measures"]]
    assert names == ["Revenue", "Fenced"]
    assert "\n\n\n" not in out
    with pytest.raises(KeyError):
        tmdl.remove_measure(TABLE, "Nope")


def test_untouched_blocks_preserved():
    out = tmdl.add_measure(TABLE, "Units", "1")
    assert "\tpartition 'Fact Sales' = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet x = 1 in x" in out


def test_relationships_roundtrip():
    text, rid = tmdl.add_relationship("", "'Fact Sales'.Key", "Product.Key", "bothDirections", active=False)
    rels = tmdl.parse_relationships(text)
    assert rels[0]["id"] == rid and rels[0]["from_table"] == "Fact Sales"
    assert rels[0]["isActive"] == "false" and rels[0]["crossFilteringBehavior"] == "bothDirections"
    text2, _ = tmdl.add_relationship(text, "A.x", "B.y")
    assert len(tmdl.parse_relationships(text2)) == 2
    assert tmdl.parse_relationships(tmdl.remove_relationship(text2, rid))[0]["from_table"] == "A"


def test_new_table_tmdl():
    t = tmdl.parse_table(tmdl.new_table_tmdl("Dim Date", "CALENDARAUTO()", [("Date", "dateTime")]))
    assert t["name"] == "Dim Date" and t["columns"][0]["dataType"] == "dateTime"
    assert t["partitions"][0]["type"] == "calculated"


# ── project ──

@pytest.fixture
def proj(tmp_path):
    P.create_project(str(tmp_path / "p"), "Demo")
    return P.Project(str(tmp_path / "p" / "Demo.pbip"))


def test_create_project_layout(tmp_path):
    pbip = P.create_project(str(tmp_path), "Demo")
    for rel in ["Demo.pbip", "Demo.Report/definition.pbir", "Demo.Report/definition/report.json",
                "Demo.Report/definition/version.json", "Demo.Report/definition/pages/pages.json",
                "Demo.SemanticModel/definition.pbism", "Demo.SemanticModel/definition/model.tmdl",
                "Demo.SemanticModel/definition/database.tmdl",
                "Demo.SemanticModel/definition/tables/Sales.tmdl"]:
        assert (tmp_path / rel).is_file(), rel
    assert not (tmp_path / "Demo.Report/report.json").exists()
    for js in ["Demo.pbip", "Demo.Report/definition.pbir", "Demo.Report/definition/report.json"]:
        json.loads((tmp_path / js).read_text())
    with pytest.raises(P.ProjectError):
        P.create_project(str(tmp_path), "Demo")
    assert pbip.name == "Demo.pbip"


def test_locate_variants(tmp_path):
    P.create_project(str(tmp_path), "Demo")
    sm = tmp_path / "Demo.SemanticModel"
    assert P.locate(str(tmp_path / "Demo.pbip"))[1] == sm
    assert P.locate(str(tmp_path))[1] == sm
    assert P.locate(str(sm)) == (None, sm)
    with pytest.raises(P.ProjectError):
        P.locate(str(tmp_path / "missing"))


def test_model_bim_rejected(tmp_path):
    sm = tmp_path / "Old.SemanticModel"
    sm.mkdir()
    (sm / "model.bim").write_text("{}")
    with pytest.raises(P.ProjectError, match="TMDL"):
        P.Project(str(sm))


def test_measure_names_model_wide(proj):
    proj.add_table("Product", 'DATATABLE("Product", STRING, {{"A"}})', [("Product", "string")])
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    with pytest.raises(P.ProjectError, match="model-wide"):
        proj.add_measure("Product", "total", "1")
    assert proj.find_measure_table("TOTAL") == "Sales"


def test_relationship_column_check_and_table_guard(proj):
    proj.add_table("Product", 'DATATABLE("Product", STRING, {{"A"}})', [("Product", "string")])
    with pytest.raises(P.ProjectError, match="Column"):
        proj.add_relationship("Sales.Nope", "Product.Product")
    rid = proj.add_relationship("Sales.Product", "Product.Product")
    with pytest.raises(P.ProjectError, match="relationship"):
        proj.remove_table("Product")
    proj.remove_relationship(rid)
    proj.remove_table("Product")
    assert "tables/Product.tmdl" in proj.dirty and "Product" not in proj.table_files()


def test_ref_table_sync(proj):
    proj.files["model.tmdl"] += "\nref table Sales\n"
    proj.add_table("Dim X", "{1}", [("Value", "int64")])
    assert "ref table 'Dim X'" in proj.files["model.tmdl"]
    proj.remove_table("Dim X")
    assert "ref table 'Dim X'" not in proj.files["model.tmdl"]


def test_save_writes_only_dirty(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    written = proj.save()
    assert len(written) == 1 and written[0].endswith("Sales.tmdl")
    assert P.Project(str(proj.pbip)).measures()[0]["name"] == "Total"
    assert proj.save() == []


def test_validate(proj):
    assert proj.validate() == []
    proj.add_measure("Sales", "Bad", "SUM(Sales[Amount]")
    proj.files["relationships.tmdl"] = "relationship r1\n\tfromColumn: Sales.Nope\n\ttoColumn: X.Y\n"
    msgs = [i["message"] for i in proj.validate()]
    assert any("unclosed '('" in m for m in msgs)
    assert any("unknown column" in m for m in msgs) and any("unknown table" in m for m in msgs)


@pytest.mark.parametrize("expr,bad", [
    ('IF(1, "a(b", 2)', None), ("SUM('My (T)'[c])", None), ('"x""y"', None),
    ("(", "unclosed '('"), (")", "unbalanced ')'"), ('"abc', "unterminated string literal"),
    ("[a", "unclosed '['"),
])
def test_dax_balance(expr, bad):
    assert P._unbalanced(expr) == bad


def test_report_pages(proj):
    pages = P.list_report_pages(proj.pbip)
    assert pages == [{"name": "ReportSection", "displayName": "Page 1", "visuals": 0}]


# ── session ──

def test_undo_redo(proj, tmp_path):
    s = Session()
    s.open(str(proj.pbip))
    s.snapshot(); s.project.add_measure("Sales", "A", "1")
    s.snapshot(); s.project.add_measure("Sales", "B", "2")
    assert len(s.project.measures()) == 2
    assert s.undo() and len(s.project.measures()) == 1
    assert s.undo() and s.project.measures() == [] and not s._modified
    assert not s.undo()
    assert s.redo() and [m["name"] for m in s.project.measures()] == ["A"]
    s.snapshot(); s.project.add_measure("Sales", "C", "3")
    assert not s.redo()  # new mutation clears redo


def test_require_without_project():
    with pytest.raises(RuntimeError, match="No project"):
        Session().require()


def test_context_persistence(isolated_home):
    Session.set_context(workspace={"id": "w", "name": "W"}, dataset={"id": "d"})
    assert Session.context()["workspace"]["name"] == "W"
    Session.set_context(dataset=None)
    assert "dataset" not in Session.context()
    assert json.loads((isolated_home / "session.json").read_text())["workspace"]["id"] == "w"


# ── REST client against the stub ──

def test_rest_listing_and_resolution(stub_api):
    assert pbi.list_workspaces()[0]["name"] == "Sales Analytics"
    assert pbi.resolve_workspace("sales analytics") == WS_ID
    assert pbi.resolve_workspace("me") is None
    assert pbi.resolve_dataset("demo", WS_ID) == DS_ID
    assert pbi.list_reports(WS_ID)[0]["datasetId"] == DS_ID
    assert stub_api.requests[0]["auth"] == "Bearer test-token"
    with pytest.raises(pbi.PowerBIError, match="not found"):
        pbi.resolve_workspace("Nope")


def test_rest_refresh_and_wait(stub_api):
    res = pbi.refresh(DS_ID, WS_ID)
    assert res == {"dataset_id": DS_ID, "accepted": True, "request_id": "req-123"}
    assert stub_api.requests[-1]["body"] == {"notifyOption": "NoNotification"}
    stub_api.unknown_polls = 2
    sleeps = []
    assert pbi.wait_refresh(DS_ID, WS_ID, sleep=sleeps.append)["status"] == "Completed"
    assert len(sleeps) == 2


def test_rest_enhanced_refresh_body(stub_api):
    pbi.refresh(DS_ID, None, "Full")
    assert stub_api.requests[-1]["body"] == {"type": "Full"}
    assert stub_api.requests[-1]["path"] == f"/datasets/{DS_ID}/refreshes"


def test_rest_dax(stub_api):
    res = pbi.execute_dax(DS_ID, "EVALUATE Sales", WS_ID)
    assert res["columns"] == ["Sales[Product]", "[Total]"] and res["row_count"] == 2
    with pytest.raises(pbi.PowerBIError, match="HTTP 400"):
        pbi.execute_dax(DS_ID, "EVALUATE BAD", WS_ID)


def test_rest_auth_errors(stub_api, monkeypatch):
    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", "wrong")
    with pytest.raises(pbi.PowerBIError, match="HTTP 401"):
        pbi.list_workspaces()
    monkeypatch.delenv("POWERBI_ACCESS_TOKEN")
    with pytest.raises(pbi.PowerBIError, match="auth login"):
        pbi.get_token()
    assert pbi.status()["signed_in"] is False


# ── Power Query (M) ──

from cli_anything.powerbi.core import powerquery as pq

M_SAMPLE = '''let
    // load, with a comma, in a comment
    Source = Csv.Document(File.Contents("C:\\d\\a, b.csv"), [Delimiter=","]),
    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Fn = (x) => let y = x + 1 in y,
    #"Say ""Hi""" = Table.AddColumn(#"Promoted Headers", "In", each "let, in", type text)
in
    #"Say ""Hi"""'''


def test_parse_let_handles_strings_comments_nested():
    steps, result = pq.parse_let(M_SAMPLE)
    assert [n for n, _ in steps] == ["Source", "Promoted Headers", "Fn", 'Say "Hi"']
    assert steps[2][1] == "(x) => let y = x + 1 in y"
    assert result == '#"Say ""Hi"""'


def test_parse_let_non_let_and_malformed():
    assert pq.parse_let('Sql.Database("s", "d")') == ([("Source", 'Sql.Database("s", "d")')], "Source")
    with pytest.raises(ValueError):
        pq.parse_let("let A = 1, B = 2")


def test_build_roundtrip_and_ident_quoting():
    steps, _ = pq.parse_let(M_SAMPLE)
    again, _ = pq.parse_let(pq.build_let(steps))
    assert again == steps
    assert pq.m_ident("Source") == "Source" and pq.m_ident("each") == '#"each"'
    assert pq.m_ident("My Step") == '#"My Step"'


def test_append_step_and_unique_names():
    m, name = pq.append_step("let\n    Source = 1\nin\n    Source", "Changed Type", "f({prev})")
    m, name2 = pq.append_step(m, "Changed Type", "g({prev})")
    steps, result = pq.parse_let(m)
    assert (name, name2) == ("Changed Type", "Changed Type1")
    assert steps[-1] == ("Changed Type1", 'g(#"Changed Type")') and result == '#"Changed Type1"'


def test_append_step_when_result_is_expression():
    m, _ = pq.append_step("let\n    Source = 1\nin\n    Source + 1", "Next", "f({prev})")
    steps, _ = pq.parse_let(m)
    assert [n for n, _ in steps] == ["Source", "Result", "Next"] and steps[-1][1] == "f(Result)"


def test_remove_step_rewires_references():
    m = pq.build_let([("Source", "1"), ("A", "f(Source)"), ("B", "g(A, A)")])
    steps, result = pq.parse_let(pq.remove_step(m, "A"))
    assert steps == [("Source", "1"), ("B", "g(Source, Source)")] and result == "B"
    steps, result = pq.parse_let(pq.remove_step(m, "B"))
    assert result == "A"
    with pytest.raises(ValueError):
        pq.remove_step(m, "Source")
    with pytest.raises(KeyError):
        pq.remove_step(m, "Nope")


def test_step_builders():
    assert pq.step_change_types([("A", "type text")]) == 'Table.TransformColumnTypes({prev}, {{"A", type text}})'
    assert pq.step_filter("[x] > 1") == "Table.SelectRows({prev}, each [x] > 1)"
    assert pq.step_filter("each [x] > 1") == "Table.SelectRows({prev}, each [x] > 1)"
    assert "Replacer.ReplaceText" in pq.step_replace("R", "a", "b", text=True)
    assert "Replacer.ReplaceValue" in pq.step_replace("R", "", "b", text=True)
    assert "Replacer.ReplaceValue" in pq.step_replace("R", "a", "b", text=True, whole=True)
    assert pq.step_replace("N", "0", "null", text=False).startswith("Table.ReplaceValue({prev}, 0, null,")
    assert pq.step_combine(["Feb", "My Q"]) == 'Table.Combine({{prev}, Feb, #"My Q"})'
    assert pq.step_nested_join(["k"], "Dim", ["k"], "Dim", "Inner").endswith('"Dim", JoinKind.Inner)')


def test_resolve_type():
    assert pq.resolve_type("Date") == ("dateTime", "type date")
    assert pq.resolve_type("decimal") == ("decimal", "Currency.Type")
    with pytest.raises(ValueError, match="Unknown type"):
        pq.resolve_type("varchar")


@pytest.mark.parametrize("vals,expected", [
    (["1", "2", ""], "int"), (["1.5", "2"], "number"), (["true", "FALSE"], "bool"),
    (["2026-01-01", "2026-02-03"], "date"), (["2026-01-01 10:00:00"], "datetime"),
    (["a", "1"], "text"), (["", " "], "text"), (["00123456789012345678901"], "number"),
])
def test_infer_text_type(vals, expected):
    assert pq.infer_text_type(vals) == expected


def test_sniff_csv_semicolon_and_dupe_headers(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("id;Name;name;\n1;a;b;\n2;c;d;\n", encoding="utf-8")
    info = pq.sniff_csv(str(f))
    assert info["delimiter"] == ";"
    assert [c for c, _ in info["columns"]] == ["id", "Name", "name_1", "Column4"]
    assert info["columns"][0][1] == "int"
    (tmp_path / "e.csv").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        pq.sniff_csv(str(tmp_path / "e.csv"))


def _xlsx(path):
    import datetime
    import openpyxl
    from openpyxl.worksheet.table import Table
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in [("Key", "When", "Amt", "Ok"), ("A", datetime.date(2026, 1, 1), 1.5, True),
              ("B", datetime.datetime(2026, 1, 2, 9, 30), 2, False)]:
        ws.append(r)
    ws.add_table(Table(displayName="tblData", ref="A1:D3"))
    wb.save(path)
    return path


def test_sniff_excel_sheet_and_table(tmp_path):
    p = _xlsx(tmp_path / "x.xlsx")
    s = pq.sniff_excel(str(p))
    assert s["kind"] == "Sheet" and s["item"] == "Data"
    assert dict(s["columns"]) == {"Key": "text", "When": "datetime", "Amt": "number", "Ok": "bool"}
    t = pq.sniff_excel(str(p), table="tblData")
    assert t["kind"] == "Table" and t["item"] == "tblData"
    with pytest.raises(ValueError):
        pq.sniff_excel(str(p), sheet="Nope")


def test_excel_query_shapes():
    m = pq.excel_query("C:/x.xlsx", "tblData", "Table", [("A", "text")])
    assert 'Source{[Item="tblData",Kind="Table"]}[Data]' in m and "PromoteHeaders" not in m
    m = pq.excel_query("C:/x.xlsx", "Data", "Sheet", [("A", "text")])
    assert "Table.PromoteHeaders(Data_Sheet" in m


# ── TMDL M partitions & columns ──

def test_m_source_get_set_roundtrip():
    text = tmdl.new_m_table_tmdl("T", pq.build_let([("Source", "1")]), [("A", "string")])
    assert tmdl.get_m_source(text) == "let\n    Source = 1\nin\n    Source"
    text2 = tmdl.set_m_source(text, "let\n    Source = 2,\n    X = Source\nin\n    X")
    assert tmdl.get_m_source(text2).endswith("in\n    X")
    assert "annotation PBI_ResultType = Table" in text2
    assert tmdl.parse_table(text2)["columns"][0]["name"] == "A"


def test_m_source_fenced_and_missing():
    text = ("table T\n\tpartition T = m\n\t\tmode: import\n\t\tsource = ```\n\t\t\t\tlet\n"
            "\t\t\t\t    S = 1\n\t\t\t\tin\n\t\t\t\t    S\n\t\t\t\t```\n")
    assert tmdl.get_m_source(text) == "let\n    S = 1\nin\n    S"
    with pytest.raises(ValueError, match="Power Query"):
        tmdl.get_m_source(TABLE.replace("= m\n", "= calculated\n"))


def test_column_ops():
    t = tmdl.new_m_table_tmdl("T", "let\n    S = 1\nin\n    S", [("A", "string"), ("B", "int64")])
    t = tmdl.set_column_type(t, "a", "double")
    t = tmdl.rename_column(t, "B", "Bee")
    t = tmdl.add_column(t, "C", "boolean")
    t = tmdl.remove_column(t, "A")
    cols = {c["name"]: c for c in tmdl.parse_table(t)["columns"]}
    assert list(cols) == ["Bee", "C"]
    assert cols["Bee"]["sourceColumn"] == "Bee" and cols["C"]["dataType"] == "boolean"
    assert tmdl.get_m_source(t)  # partition intact
    with pytest.raises(ValueError):
        tmdl.add_column(t, "c", "string")


# ── project-level Power Query ops ──

@pytest.fixture
def pqproj(tmp_path):
    P.create_project(str(tmp_path / "p"), "Shop", sample=False)
    (tmp_path / "o.csv").write_text("Id,Prod,Qty,Region\n1,a ,2,EU\n2,b,1,\n", encoding="utf-8")
    (tmp_path / "f.csv").write_text("Id,Prod,Qty,Extra\n3,a,4,x\n", encoding="utf-8")
    (tmp_path / "d.csv").write_text("Prod,Cat\na,Tools\nb,Toys\n", encoding="utf-8")
    p = P.Project(str(tmp_path / "p" / "Shop.pbip"))
    p.import_csv(str(tmp_path / "o.csv"), "Orders")
    p.import_csv(str(tmp_path / "f.csv"), "Feb")
    p.import_csv(str(tmp_path / "d.csv"), "Dim")
    return p


def test_import_csv_with_override(pqproj, tmp_path):
    res = pqproj.import_csv(str(tmp_path / "o.csv"), "O2", overrides={"qty": "decimal"})
    assert {c["name"]: c["type"] for c in res["columns"]}["Qty"] == "decimal"
    assert pqproj.table("O2")["partitions"][0]["type"] == "m"
    with pytest.raises(P.ProjectError, match="already exists"):
        pqproj.import_csv(str(tmp_path / "o.csv"), "o2")
    with pytest.raises(P.ProjectError, match="unknown column"):
        pqproj.import_csv(str(tmp_path / "o.csv"), "O3", overrides={"nope": "int"})


def test_cleaning_keeps_schema_in_sync(pqproj):
    pqproj.set_types("Orders", {"qty": "number"})
    pqproj.rename_columns("Orders", [("Prod", "Product")])
    pqproj.transform_text("Orders", ["Product"], "trim")
    pqproj.add_custom_column("Orders", "Double", "[Qty] * 2", "number")
    pqproj.remove_columns("Orders", ["Region"])
    pqproj.filter_rows("Orders", "[Qty] > 0")
    pqproj.remove_duplicates("Orders", ["Id"])
    pqproj.fill_down("Orders", ["Product"])
    cols = {c["name"]: c["dataType"] for c in pqproj.table("Orders")["columns"]}
    assert cols == {"Id": "int64", "Product": "string", "Qty": "double", "Double": "double"}
    names = [s["name"] for s in pqproj.query("Orders")["steps"]]
    assert names[3:] == ["Changed Type1", "Renamed Columns", "Trimmed Text", "Added Custom",
                         "Removed Columns", "Filtered Rows", "Removed Duplicates", "Filled Down"]
    assert pqproj.validate() == []


def test_cleaning_errors(pqproj):
    with pytest.raises(P.ProjectError, match="not in 'Orders'"):
        pqproj.set_types("Orders", {"Nope": "int"})
    with pytest.raises(P.ProjectError, match="already exists"):
        pqproj.add_custom_column("Orders", "qty", "1")
    pqproj.add_table("Calc", "{1}", [("Value", "int64")])
    with pytest.raises(P.ProjectError, match="calculated"):
        pqproj.filter_rows("Calc", "true")


def test_remove_column_blocked_by_relationship(pqproj):
    pqproj.add_relationship("Orders.Prod", "Dim.Prod")
    with pytest.raises(P.ProjectError, match="relationship"):
        pqproj.remove_columns("Dim", ["Prod"])


def test_merge_expands_and_types(pqproj):
    res = pqproj.merge("Orders", "dim", ["Prod"])
    assert res["added_columns"] == ["Cat"]
    q = pqproj.query("Orders")
    assert q["steps"][-2]["expression"] == (
        'Table.NestedJoin(#"Changed Type", {"Prod"}, Dim, {"Prod"}, "Dim", JoinKind.LeftOuter)')
    assert {c["name"]: c["dataType"] for c in pqproj.table("Orders")["columns"]}["Cat"] == "string"
    res2 = pqproj.merge("Orders", "Dim", ["Prod"], expand=["Cat"])  # clash -> prefixed
    assert res2["added_columns"] == ["Dim.Cat"]
    anti = pqproj.merge("Feb", "Dim", ["Prod"], kind="leftanti")
    assert anti["added_columns"] == [] and "Removed Merge Column" in anti["steps"]
    with pytest.raises(P.ProjectError, match="Join kind"):
        pqproj.merge("Orders", "Dim", ["Prod"], kind="cross")


def test_append_into_and_as_new(pqproj):
    res = pqproj.append("Orders", ["Feb"])
    assert res["added_columns"] == ["Extra"]
    assert pqproj.query("Orders")["steps"][-1]["expression"] == 'Table.Combine({#"Changed Type", Feb})'
    new = pqproj.append("Feb", ["Dim"], as_new="All")
    assert new["columns"] == ["Id", "Prod", "Qty", "Extra", "Cat"]
    assert pqproj.query("All")["steps"][0]["expression"] == "Table.Combine({Feb, Dim})"


def test_query_raw_edit_and_remove_step(pqproj):
    step = pqproj.add_step("Orders", "Sorted", 'Table.Sort({prev}, {{"Id", Order.Descending}})')
    assert pqproj.query("Orders")["result"] == "Sorted" and step == "Sorted"
    pqproj.remove_step("Orders", "Sorted")
    assert pqproj.query("Orders")["result"] == '#"Changed Type"'
    with pytest.raises(ValueError):
        pqproj.set_query("Orders", "let A = 1")
    pqproj.files["tables/Orders.tmdl"] = tmdl.set_m_source(pqproj.files["tables/Orders.tmdl"], "let\n    A = 1")
    assert any("Power Query" in i["message"] for i in pqproj.validate())


# ── themes (core/theme.py) ──
import re

from cli_anything.powerbi.core import theme as TH
from cli_anything.powerbi.core import report as R


def test_resolve_palette():
    assert TH.resolve_palette("Ocean") == TH.PALETTES["ocean"]
    assert TH.resolve_palette("#aabbcc, #112233") == ["#AABBCC", "#112233"]
    assert TH.resolve_palette(None) == TH.PALETTES["indigo"]
    for bad in ("nope", "#12345", ""):
        with pytest.raises(ValueError):
            TH.resolve_palette(bad)


@pytest.mark.parametrize("preset", list(TH.PRESETS))
def test_build_theme_presets_valid(preset):
    t = TH.build_theme("X", preset)
    assert TH.validate_theme(t) == []
    assert TH.font_warnings(t) == []
    assert t["dataColors"] == TH.PALETTES[TH.PRESETS[preset]["palette"]]
    card = t["visualStyles"]["*"]["*"]
    assert card["border"][0]["radius"] == TH.PRESETS[preset]["radius"]
    assert card["dropShadow"][0]["show"] is TH.PRESETS[preset]["shadow"]
    assert t["visualStyles"]["textbox"]["*"]["background"] == [{"show": False}]


def test_build_theme_overrides_and_errors():
    t = TH.build_theme("Brand", palette="#FF0000,#00FF00", font="Georgia", heading_font="Georgia",
                       background="#fafafa", page_background="#eeeeee", radius=0, shadow=False)
    assert t["dataColors"] == ["#FF0000", "#00FF00"] and t["background"] == "#FAFAFA"
    assert t["textClasses"]["label"]["fontFace"] == "Georgia"
    assert t["visualStyles"]["page"]["*"]["background"][0]["color"]["solid"]["color"] == "#eeeeee"
    assert "visualStyles" not in TH.build_theme("Flat", card_style=False)
    for kw in ({"preset": "nope"}, {"foreground": "red"}, {"radius": 99}):
        with pytest.raises(ValueError):
            TH.build_theme("Bad", **kw)


def test_validate_theme_and_fonts(tmp_path):
    assert TH.validate_theme([]) == ["theme must be a JSON object"]
    issues = TH.validate_theme({"dataColors": ["blue"], "foreground": "#12",
                                "textClasses": {"title": {"fontSize": "big"}}, "visualStyles": []})
    assert len(issues) == 5   # name, dataColors, foreground, fontSize, visualStyles
    assert TH.font_warnings({"textClasses": {"t": {"fontFace": "Comic Neue"}}}) == [
        "font 'Comic Neue' may not render in the Power BI service"]
    f = tmp_path / "t.json"
    f.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        TH.load_theme_file(str(f))


# ── report pages / visuals (core/report.py) ──
def test_apply_and_remove_theme(proj):
    assert proj.current_theme()["custom"] is None
    f1 = proj.apply_theme(TH.build_theme("My Theme!"))
    assert re.fullmatch(r"MyTheme\d{17}\.json", f1)
    f2 = proj.apply_theme(TH.build_theme("Second", "dark"))   # replaces, never stacks
    rep = json.loads(proj.rfiles["definition/report.json"])
    items = [i for p in rep["resourcePackages"] for i in p["items"]]
    assert items == [{"name": f2, "path": f2, "type": "CustomTheme"}]
    assert f"{R.THEME_DIR}/{f1}" not in proj.rfiles
    assert proj.current_theme()["theme"]["name"] == "Second"
    proj.save()
    reopened = P.Project(str(proj.pbip))
    assert reopened.current_theme()["custom"] == f2
    assert (reopened.report_dir / R.THEME_DIR / f2).is_file()
    assert reopened.remove_theme() == f2
    reopened.save()
    assert not (reopened.report_dir / R.THEME_DIR / f2).exists()
    assert "resourcePackages" not in json.loads(reopened.rfiles["definition/report.json"])
    with pytest.raises(P.ProjectError, match="no custom theme"):
        reopened.remove_theme()


def test_missing_theme_file_reported(proj):
    f = proj.apply_theme(TH.build_theme("T"))
    del proj.rfiles[f"{R.THEME_DIR}/{f}"]
    assert any("theme file" in i["message"] for i in proj.validate())


def test_legacy_report_rejected(proj):
    proj.rfiles = {"report.json": "{}"}
    with pytest.raises(P.ProjectError, match="legacy"):
        proj.pages()


def test_pages_add_remove(proj):
    assert [p["displayName"] for p in proj.pages()] == ["Page 1"]
    pid = proj.add_page("Detail", 1920, 1080, active=True)
    pages = {p["displayName"]: p for p in proj.pages()}
    assert pages["Detail"]["width"] == 1920 and pages["Detail"]["active"]
    with pytest.raises(P.ProjectError, match="already exists"):
        proj.add_page("detail")
    proj.remove_page("Page 1")
    assert [p["id"] for p in proj.pages()] == [pid]
    with pytest.raises(P.ProjectError, match="only page"):
        proj.remove_page("Detail")


def test_field_resolution(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    assert proj.field("[Total]", True)["field"]["Measure"]["Property"] == "Total"
    assert proj.field("Sales[Total]", True)["queryRef"] == "Sales.Total"
    auto = proj.field("Sales.Amount", True)        # numeric column in a value role -> Sum
    assert auto["field"]["Aggregation"]["Function"] == 0 and auto["queryRef"] == "Sum(Sales.Amount)"
    assert "Column" in proj.field("Sales.Amount", False)["field"]   # category role: raw column
    assert proj.field("avg(Sales.Amount)", True)["field"]["Aggregation"]["Function"] == 1
    assert proj.field("count(sales.product)", True)["queryRef"] == "Count(Sales.Product)"
    for bad, msg in (("[Nope]", "not found"), ("Nope.X", "not found"),
                     ("Sales.Nope", "not a column"), ("junk", "Bad field")):
        with pytest.raises(P.ProjectError, match=msg):
            proj.field(bad, True)


def test_add_visual_shapes(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    vid = proj.add_visual("Page 1", "column", ["[Total]"], "Sales.Product", 10, 20, 400, 300, "By product")
    v = proj.visuals("Page 1")
    assert [x["id"] for x in v] == [vid] and v[0]["type"] == "clusteredColumnChart"
    page_id = proj.pages()[0]["id"]
    body = json.loads(proj.rfiles[f"definition/pages/{page_id}/visuals/{vid}/visual.json"])
    q = body["visual"]["query"]["queryState"]
    assert q["Category"]["projections"][0]["queryRef"] == "Sales.Product"
    assert q["Y"]["projections"][0]["queryRef"] == "Sales.Total"
    assert body["position"]["x"] == 10 and body["position"]["width"] == 400
    assert "By product" in json.dumps(body["visual"]["visualContainerObjects"]["title"])
    tb = proj.add_textbox("Page 1", "Hello")
    assert "Hello" in proj.rfiles[f"definition/pages/{page_id}/visuals/{tb}/visual.json"]
    for args, msg in ((("pie", ["[Total]"]), "category"), (("card", []), "at least one"),
                      (("sparkle", ["[Total]"]), "Unknown visual type")):
        with pytest.raises(P.ProjectError, match=msg):
            proj.add_visual("Page 1", *args)
    proj.remove_visual("Page 1", vid)
    assert len(proj.visuals("Page 1")) == 1
    with pytest.raises(P.ProjectError, match="not on page"):
        proj.remove_visual("Page 1", vid)


def test_build_page_grid(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    charts = [{"kind": "column", "category": "Sales.Product", "values": ["[Total]"], "title": "A"},
              {"kind": "line", "category": "Sales.Date", "values": ["[Total]"]},
              {"kind": "bar", "category": "Sales.Product", "values": ["Sales.Amount"]}]
    res = proj.build_page("Overview", title="Overview", cards=["[Total]", "count(Sales.Product)"],
                          charts=charts, table=["Sales.Date", "Sales.Product", "[Total]"])
    assert len(res["visuals"]) == 1 + 2 + 3 + 1
    pos = {v["id"]: v for v in proj.visuals("Overview")}
    boxes = [pos[i] for i in res["visuals"]]
    for b in boxes:                                       # everything inside the 24px margins
        assert b["x"] >= 24 - 0.01 and b["x"] + b["width"] <= 1280 - 24 + 0.01
        assert b["y"] + b["height"] <= 720 - 24 + 0.01
    c1, c2 = boxes[1], boxes[2]
    assert c1["width"] == c2["width"] and c2["x"] - (c1["x"] + c1["width"]) == pytest.approx(16)
    assert boxes[3]["width"] == boxes[4]["width"] and boxes[5]["width"] == boxes[6]["width"] == 1232
    assert next(p for p in proj.pages() if p["displayName"] == "Overview")["active"]
    with pytest.raises(P.ProjectError, match="--replace"):
        proj.build_page("Overview", title="Again")
    assert len(proj.build_page("Overview", title="Again", replace=True)["visuals"]) == 1
    assert len(proj.visuals("Overview")) == 1
    with pytest.raises(P.ProjectError, match="Too many"):
        proj.build_page("Crowded", charts=charts * 4, replace=True)


def test_report_issues_flag_missing_fields(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    proj.add_visual("Page 1", "card", ["[Total]"])
    assert proj.validate() == []
    proj.remove_measure("Total")
    assert any("missing measure Sales[Total]" in i["message"] for i in proj.validate())


def test_report_changes_undo(proj):
    s = Session()
    s.project = proj
    s.snapshot()
    proj.apply_theme(TH.build_theme("U"))
    proj.add_page("Extra")
    assert s.undo()
    assert s.project.current_theme()["custom"] is None
    assert [p["displayName"] for p in s.project.pages()] == ["Page 1"]


# ── report templates (core/suggest.py) ──
from cli_anything.powerbi.core import suggest as SG

PRODUCTS = ["Widget", "Gadget", "Sprocket", "Gizmo", "Doohickey", "Thing", "Whatsit", "Contraption"]


@pytest.fixture
def retail(tmp_path):
    P.create_project(str(tmp_path / "p"), "Retail", sample=False)
    rows = ["OrderID,Order Date,Product,Region,Channel,Quantity,Unit Price,Discount Rate"]
    for i in range(40):
        rows.append(f"{100 + i},2026-0{1 + i % 6}-{10 + i % 9},{PRODUCTS[i % 8]},"
                    f"{['North', 'South', 'East', 'West'][i % 4]},{['Online', 'Retail'][i % 2]},"
                    f"{1 + i % 5},{9.5 + i % 3},{0.05 * (i % 3)}")
    (tmp_path / "sales.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (tmp_path / "prod.csv").write_text(
        "Product,Category\n" + "\n".join(f"{p},{['Tools', 'Toys'][i % 2]}" for i, p in enumerate(PRODUCTS)) + "\n",
        encoding="utf-8")
    p = P.Project(str(tmp_path / "p" / "Retail.pbip"))
    p.import_csv(str(tmp_path / "sales.csv"), "Sales")
    p.import_csv(str(tmp_path / "prod.csv"), "Products")
    p.add_relationship("Sales.Product", "Products.Product")
    return p


@pytest.mark.parametrize("name,dtype,role", [
    ("OrderID", "int64", "key"), ("Customer Key", "string", "key"), ("product_code", "string", "key"),
    ("Paid", "boolean", "flag"), ("Order Date", "dateTime", "date"), ("Year", "int64", "time"),
    ("Quantity", "int64", "value"), ("Unit Price", "double", "value"), ("Region", "string", "category"),
])
def test_column_roles(name, dtype, role):
    assert SG._role({"name": name, "dataType": dtype}) == role


def test_profile_samples_source_and_dimensions(retail, tmp_path):
    prof = {(c["table"], c["column"]): c for c in SG.profile(retail, "Sales")["columns"]}
    assert prof[("Sales", "Region")]["distinct"] == 4 and prof[("Sales", "Channel")]["distinct"] == 2
    assert prof[("Sales", "Product")]["distinct"] == 8
    assert prof[("Sales", "Unit Price")]["agg"] == "avg" and prof[("Sales", "Quantity")]["agg"] == "sum"
    assert prof[("Products", "Category")]["related"] and ("Products", "Product") not in prof
    (tmp_path / "sales.csv").unlink()            # source gone -> no cardinality, still works
    assert all(c["distinct"] is None for c in SG.profile(retail, "Sales")["columns"] if c["table"] == "Sales")


def test_suggest_rules(retail):
    plan = SG.suggest(retail, "Sales")
    assert plan["kpis"] == ["sum(Sales.Quantity)", "avg(Sales.Unit Price)", "avg(Sales.Discount Rate)",
                            "distinctcount(Sales.OrderID)"]
    assert plan["slicers"] == ["Sales.Order Date", "Sales.Region", "Sales.Channel"]
    assert plan["trend"]["kind"] == "line" and plan["trend"]["category"] == "Sales.Order Date"
    kinds = {c["category"]: c["kind"] for c in plan["comparisons"]}
    assert kinds == {"Sales.Region": "donut", "Sales.Product": "bar"}   # second picks a different type
    assert "Sales.OrderID" not in plan["detail"]
    assert {s["visual"] for s in plan["suggestions"]} == {"card", "slicer", "line", "donut", "bar", "table"}
    assert any("4 values" in s["reason"] for s in plan["suggestions"])


def test_suggest_prefers_measures_and_errors(retail):
    retail.add_measure("Sales", "Revenue", "SUMX(Sales, Sales[Quantity] * Sales[Unit Price])")
    assert SG.suggest(retail, "Sales")["kpis"] == ["[Revenue]"]
    retail.add_table("Empty", '{"a"}', [("Label", "string")])
    with pytest.raises(SG.TemplateError, match="no measures"):
        SG.suggest(retail, "Empty")


def test_auto_measures(retail):
    made = SG.auto_measures(retail, "Sales")
    assert made == ["Total Quantity", "Average Unit Price", "Average Discount Rate", "Sales Count"]
    ms = {m["name"]: m for m in retail.measures("Sales")}
    assert ms["Average Discount Rate"]["formatString"] == "0.0%"
    assert ms["Total Quantity"]["formatString"] == "#,0"
    assert ms["Sales Count"]["expression"] == "COUNTROWS('Sales')"
    assert SG.auto_measures(retail, "Sales") == []        # table has measures now
    assert retail.validate() == []


def test_layouts(retail):
    plan = SG.suggest(retail, "Sales")
    ov = SG.layout(plan, "overview")
    assert [[c["kind"] for c in r] for r in ov] == [["line", "donut"], ["bar", "table"]]
    tr = SG.layout(plan, "trend")
    assert [c["kind"] for c in tr[0]] == ["line"] and len(tr[1]) == 2
    cmp_ = SG.layout(plan, "comparison")
    assert cmp_[0][0]["kind"] == "column" and len(cmp_[0][0]["values"]) == 2
    assert [c["kind"] for c in cmp_[1]] == ["table"]
    with pytest.raises(SG.TemplateError, match="Template must be"):
        SG.layout(plan, "fancy")
    with pytest.raises(SG.TemplateError, match="no date"):
        SG.layout({**plan, "trend": None}, "trend")


def test_template_page(retail):
    res = SG.template(retail, "Dash", "Sales", create_measures=True, monthly=True)
    assert res["measures_created"][0] == "Total Quantity" and res["kpis"][0] == "[Total Quantity]"
    vis = retail.visuals("Dash")
    types = sorted(v["type"] for v in vis)
    assert types.count("slicer") == 3 and types.count("card") == 4 and "tableEx" in types
    trend = next(v for v in vis if v["type"] == "clusteredColumnChart")   # monthly overview trend
    assert trend["fields"][0] == "Sales.Order Date Month"
    assert "Date.StartOfMonth([Order Date])" in retail.query("Sales")["m"]
    page_id = retail.pages()[-1]["id"]
    slicers = [json.loads(retail.rfiles[f"definition/pages/{page_id}/visuals/{v['id']}/visual.json"])
               for v in vis if v["type"] == "slicer"]
    modes = sorted(s["visual"]["objects"]["data"][0]["properties"]["mode"]["expr"]["Literal"]["Value"]
                   for s in slicers)
    assert modes == ["'Between'", "'Dropdown'", "'Dropdown'"]
    for v in vis:
        assert v["y"] + v["height"] <= 720 - 24 + 0.01
    assert retail.validate() == []
    SG.template(retail, "Dash", "Sales", kind="trend", replace=True, monthly=True)  # idempotent month col
    assert retail.query("Sales")["m"].count("Date.StartOfMonth") == 1


def test_build_page_slicers_and_rows(proj):
    res = proj.build_page("S", slicers=["Sales.Product", "Sales.Date"],
                          charts=[[{"kind": "bar", "category": "Sales.Product", "values": ["Sales.Amount"]}]])
    vis = {v["id"]: v for v in proj.visuals("S")}
    s1, s2, bar = (vis[i] for i in res["visuals"])
    assert s1["type"] == s2["type"] == "slicer" and s1["height"] == 84
    assert bar["width"] == 1232 and bar["y"] == 24 + 84 + 16          # explicit one-chart row
    assert proj.field_type("Sales.Date") == "dateTime" and proj.field_type("sum(Sales.Amount)") is None
    with pytest.raises(P.ProjectError, match="At most 5"):
        proj.build_page("T", slicers=["Sales.Product"] * 6)


def test_parse_chart():
    assert SG.parse_chart("bar:Sales.Product:[A],[B]:Top: products") == {
        "kind": "bar", "category": "Sales.Product", "values": ["[A]", "[B]"], "title": "Top: products"}
    assert SG.parse_chart({"kind": "table", "values": ["Sales.Product"]})["category"] is None
    for bad in ("bar:x", "gauge:a:b", {"kind": "bar"}):
        with pytest.raises(SG.TemplateError):
            SG.parse_chart(bad)


def test_generate_spec(retail, tmp_path):
    (tmp_path / "brand.json").write_text(json.dumps(TH.build_theme("Brand", "warm")), encoding="utf-8")
    res = SG.generate(retail, {
        "theme": "brand.json", "create_measures": True, "monthly": True,
        "pages": [{"name": "Overview", "table": "Sales", "title": "Retail"},
                  {"name": "Mix", "table": "Sales", "template": "comparison", "slicers": ["Products.Category"],
                   "charts": ["bar:Products.Category:[Total Quantity]", ["donut:Sales.Channel:[Sales Count]",
                                                                         "column:Sales.Region:[Total Quantity]"]],
                   "detail": ["Sales.Product", "[Total Quantity]"]}]}, base_dir=str(tmp_path))
    assert retail.current_theme()["theme"]["name"] == "Brand"
    assert [p["page"] for p in res["pages"]] == ["Overview", "Mix"] and len(res["measures_created"]) == 4
    mix = retail.visuals("Mix")
    assert sorted(v["type"] for v in mix if v["type"] not in ("card", "textbox")) == [
        "clusteredBarChart", "clusteredColumnChart", "donutChart", "slicer", "tableEx"]
    assert next(p for p in retail.pages() if p["active"])["displayName"] == "Overview"
    with pytest.raises(P.ProjectError, match="--replace"):
        SG.generate(retail, {"pages": [{"name": "Overview", "table": "Sales"}]})
    assert len(SG.generate(retail, {"theme": "dark", "pages": [{"name": "Overview", "table": "Sales"}]},
                           replace=True)["pages"]) == 1
    auto = SG.generate(retail, {})            # no pages: one overview per fact table, dims skipped
    assert [p["page"] for p in auto["pages"]] == ["Sales Overview"]
    for bad, msg in (({"colour": 1}, "Spec keys"), ({"pages": [{"name": "X"}]}, "needs 'name' and 'table'"),
                     ({"theme": {"preset": "nope"}}, "preset")):
        with pytest.raises(ValueError, match=msg):
            SG.generate(retail, bad)


# ── advanced visuals, formatting, custom visuals (core/visuals.py) ──
import zipfile
from pathlib import Path

from cli_anything.powerbi.core import visuals as VZ

PBIVIZ = Path(__file__).parent / "fixtures" / "probeViz.pbiviz"
PROBE = "probeViz59BFF8F25AB44BEF9A1BF2109149192E"


@pytest.mark.parametrize("value,expected", [
    ("true", {"expr": {"Literal": {"Value": "true"}}}),
    ("False", {"expr": {"Literal": {"Value": "false"}}}),
    ("12", {"expr": {"Literal": {"Value": "12L"}}}),
    ("-1.5", {"expr": {"Literal": {"Value": "-1.5D"}}}),
    ("#0ea5e9", {"solid": {"color": {"expr": {"Literal": {"Value": "'#0EA5E9'"}}}}}),
    ("Bottom", {"expr": {"Literal": {"Value": "'Bottom'"}}}),
    ("'12'", {"expr": {"Literal": {"Value": "'12'"}}}),
    ("it's", {"expr": {"Literal": {"Value": "'it''s'"}}}),
])
def test_literal(value, expected):
    assert VZ.literal(value) == expected


def test_parse_setting_and_roles():
    assert VZ.parse_setting("legend.position=Bottom") == ("legend", "position", "Bottom")
    assert VZ.parse_setting("title.text=a=b") == ("title", "text", "a=b")
    with pytest.raises(ValueError, match="object.property"):
        VZ.parse_setting("legend=Bottom")
    assert VZ.roles_of({"Y": "m!", "Series": "g"}) == {"Y": {"kind": "m", "required": True},
                                                       "Series": {"kind": "g", "required": False}}
    assert all(VZ.BY_VISUAL_TYPE[v[0]] == k for k, v in VZ.TYPES.items())   # visualTypes unique
    assert VZ.CUSTOM_GUID.match(PROBE) and VZ.CUSTOM_GUID.match("ChicletSlicer1448559807354")
    assert not VZ.CUSTOM_GUID.match("clusteredColumnChart")


def test_read_pbiviz(tmp_path):
    info = VZ.read_pbiviz(str(PBIVIZ))
    assert info["guid"] == PROBE and info["name"] == "Probe Viz" and info["version"] == "1.0.0.0"
    assert info["roles"]["category"]["kind"] == "g" and info["roles"]["measure"]["kind"] == "m"
    bad = tmp_path / "bad.pbiviz"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a .pbiviz"):
        VZ.read_pbiviz(str(bad))
    with zipfile.ZipFile(tmp_path / "nores.pbiviz", "w") as z:
        z.writestr("package.json", json.dumps({"visual": {"guid": "abc123"}}))
    with pytest.raises(ValueError, match="resources"):
        VZ.read_pbiviz(str(tmp_path / "nores.pbiviz"))


def _visual(proj, vid, page="Page 1"):
    pid = next(p["id"] for p in proj.pages() if p["displayName"] == page)
    return json.loads(proj.rfiles[f"definition/pages/{pid}/visuals/{vid}/visual.json"])["visual"]


@pytest.mark.parametrize("kind,roles,expect_state", [
    ("combo", {"Y": ["[Total]"], "Y2": ["sum(Sales.Amount)"]}, ["Category", "Y", "Y2"]),
    ("scatter", {"X": ["[Total]"], "Y": ["Sales.Amount"], "Category": ["Sales.Product"]}, ["Category", "X", "Y"]),
    ("matrix", {"Rows": ["Sales.Product"], "Columns": ["Sales.Date"], "Values": ["[Total]"]},
     ["Rows", "Columns", "Values"]),
    ("gauge", {"Y": ["[Total]"], "targetvalue": ["sum(Sales.Amount)"]}, ["Y", "TargetValue"]),
    ("kpi", {"Indicator": ["[Total]"], "TrendLine": ["Sales.Date"]}, ["Indicator", "TrendLine"]),
    ("waterfall", {"Y": ["[Total]"]}, ["Category", "Y"]),
    ("treemap", {"Values": ["[Total]"]}, ["Group", "Values"]),
    ("stackedcolumn", {"Series": ["Sales.Date"]}, ["Category", "Y", "Series"]),
])
def test_advanced_types(proj, kind, roles, expect_state):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    cat = "Sales.Product" if VZ.TYPES[kind][2] in ("Category", "Group") and kind != "scatter" else None
    fields = ["[Total]"] if kind == "stackedcolumn" else []
    vid = proj.add_visual("Page 1", kind, fields, cat, roles=roles)
    vis = _visual(proj, vid)
    assert vis["visualType"] == VZ.TYPES[kind][0]
    assert list(vis["query"]["queryState"]) == expect_state
    for r, d in VZ.roles_of(VZ.TYPES[kind][1]).items():     # measure roles summed, groupings raw
        for pr in vis["query"]["queryState"].get(r, {}).get("projections", []):
            if d["kind"] == "g":
                assert "Column" in pr["field"]
            else:
                assert "Column" not in pr["field"]
    assert proj.validate() == []


def test_role_errors(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    for kind, kw, msg in (("scatter", {"roles": {"X": ["[Total]"]}}, "needs a Y field"),
                          ("kpi", {"fields": ["[Total]"]}, "needs a category field"),
                          ("column", {"fields": ["[Total]"], "category": "Sales.Product",
                                      "roles": {"Size": ["[Total]"]}}, "has no role 'Size'"),
                          ("gauge", {"category": "Sales.Product"}, "no category role"),
                          ("matrix", {}, "at least one field")):
        with pytest.raises(P.ProjectError, match=msg):
            proj.add_visual("Page 1", kind, **kw)


def test_format_color_sort_move(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    vid = proj.add_visual("Page 1", "column", ["[Total]"], "Sales.Product", title="T")
    proj.format_visual("Page 1", vid, [("legend", "show", "true"), ("legend", "position", "Bottom"),
                                       ("labels", "fontSize", "9"), ("title", "fontColor", "#112233")])
    vis = _visual(proj, vid)
    assert vis["objects"]["legend"][0]["properties"]["position"] == VZ.literal("Bottom")
    assert vis["objects"]["labels"][0]["properties"]["fontSize"]["expr"]["Literal"]["Value"] == "9L"
    t = vis["visualContainerObjects"]["title"][0]["properties"]          # container card, merged
    assert "text" in t and t["fontColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#112233'"
    proj.format_visual("Page 1", vid, [], reset=["legend"])
    assert "legend" not in _visual(proj, vid)["objects"]
    proj.color_visual("Page 1", vid, "#FF0000")
    proj.color_visual("Page 1", vid, "#00FF00", series="[Total]")
    proj.color_visual("Page 1", vid, "#0000FF", point="Widget")
    proj.color_visual("Page 1", vid, "#00FF01", series="[Total]")           # replaces same selector
    dp = _visual(proj, vid)["objects"]["dataPoint"]
    assert len(dp) == 3 and dp[-1]["selector"] == {"metadata": "Sales.Total"}
    plain = next(e for e in dp if "selector" not in e)          # lone series: bars need the plain entry
    assert plain["properties"]["fill"] == VZ.literal("#00FF01")
    for kind, roles, series in (("column", {"Y": ["[Total]", "avg(Sales.Amount)"]}, "[Total]"),  # 2 series
                                ("combo", {"Y": ["[Total]"], "Y2": ["avg(Sales.Amount)"]}, "avg(Sales.Amount)"),
                                ("column", {"Y": ["[Total]"], "Series": ["Sales.Date"]}, "[Total]")):  # legend
        v2 = proj.add_visual("Page 1", kind, [], "Sales.Product", roles=roles)
        proj.color_visual("Page 1", v2, "#123456", series=series)
        assert [("selector" in e) for e in _visual(proj, v2)["objects"]["dataPoint"]] == [True]
    cmp_ = next(e for e in dp if "data" in e.get("selector", {}))["selector"]["data"][0]["scopeId"]["Comparison"]
    assert cmp_["Left"]["Column"]["Property"] == "Product" and cmp_["Right"]["Literal"]["Value"] == "'Widget'"
    for kw, msg in (({"color": "red"}, "RRGGBB"), ({"color": "#123456", "series": "Sales.Amount"}, "not a field")):
        with pytest.raises(P.ProjectError, match=msg):
            proj.color_visual("Page 1", vid, **kw)
    assert proj.sort_visual("Page 1", vid, "[Total]") == {"sort": "Sales.Total", "direction": "Descending"}
    sd = _visual(proj, vid)["query"]["sortDefinition"]
    assert sd["sort"][0]["direction"] == "Descending" and "Measure" in sd["sort"][0]["field"]
    pos = proj.move_visual("Page 1", vid, x=100.456, width=500, z=7)
    assert pos["x"] == 100.46 and pos["width"] == 500 and pos["z"] == 7
    with pytest.raises(P.ProjectError, match="not on page"):
        proj.format_visual("Page 1", "nope", [("legend", "show", "true")])


def test_custom_visual_lifecycle(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    res = proj.import_custom_visual(str(PBIVIZ))
    assert res == {"guid": PROBE, "name": "Probe Viz", "version": "1.0.0.0",
                   "roles": {"category": "g", "measure": "m"}}
    proj.import_custom_visual(str(PBIVIZ))                                   # re-import: no duplicates
    rep = json.loads(proj.rfiles["definition/report.json"])
    assert [p["name"] for p in rep["resourcePackages"] if p["type"] == "CustomVisual"] == [PROBE]
    assert rep["resourcePackages"][-1]["items"] == [   # what Desktop writes for an imported .pbiviz
        {"name": f"{PROBE}.pbiviz.json", "path": f"{PROBE}.pbiviz.json", "type": "CustomVisualMetadata"}]
    proj.add_public_visual("ChicletSlicer1448559807354")
    proj.add_public_visual("ChicletSlicer1448559807354")
    cvs = proj.custom_visuals()
    assert cvs[PROBE]["source"] == "private" and cvs["ChicletSlicer1448559807354"]["source"] == "public"
    vid = proj.add_visual("Page 1", "probe viz", roles={"category": ["Sales.Product"], "measure": ["[Total]"]})
    assert _visual(proj, vid)["visualType"] == PROBE
    proj.add_visual("Page 1", "ChicletSlicer1448559807354", roles={"Category": ["Sales.Product"]})  # any role
    with pytest.raises(P.ProjectError, match="no role 'bogus'"):
        proj.add_visual("Page 1", PROBE, roles={"bogus": ["[Total]"]})
    assert proj.validate() == []
    proj.save()
    again = P.Project(str(proj.pbip))
    assert (again.report_dir / "CustomVisuals" / PROBE / "package.json").is_file()
    assert again.custom_visuals()[PROBE]["roles"]["measure"]["kind"] == "m"
    with pytest.raises(P.ProjectError, match="still use"):
        again.remove_custom_visual("Probe Viz")
    assert again.remove_custom_visual("Probe Viz", force=True) == PROBE
    assert again.visuals("Page 1")[0]["type"] == "ChicletSlicer1448559807354"
    again.remove_custom_visual("ChicletSlicer1448559807354", force=True)
    rep = json.loads(again.rfiles["definition/report.json"])
    assert "publicCustomVisuals" not in rep and "resourcePackages" not in rep
    assert not any(r.startswith("CustomVisuals/") for r in again.rfiles)
    with pytest.raises(P.ProjectError, match="Bad visual id"):
        again.add_public_visual("not a guid!")


def test_validate_visual_roles(proj):
    proj.add_measure("Sales", "Total", "SUM(Sales[Amount])")
    vid = proj.add_visual("Page 1", "scatter", roles={"X": ["[Total]"], "Y": ["[Total]"]})
    pid = proj.pages()[0]["id"]
    rel = f"definition/pages/{pid}/visuals/{vid}/visual.json"
    body = json.loads(proj.rfiles[rel])
    body["visual"]["query"]["queryState"]["Bogus"] = body["visual"]["query"]["queryState"].pop("Y")
    proj.rfiles[rel] = json.dumps(body)
    msgs = [(i["level"], i["message"]) for i in proj.validate()]
    assert ("error", "scatterChart has no data role 'Bogus' (roles: Category, Series, X, Y, Size)") in msgs
    assert ("warning", "scatterChart is missing its Y field") in msgs
    body["visual"]["visualType"] = PROBE                                  # never registered
    proj.rfiles[rel] = json.dumps(body)
    assert any("not registered" in m for _, m in [(i["level"], i["message"]) for i in proj.validate()])


# ── feedback round: slicer -c, slicer titles, textbox styling, page format, root keys, props ──
def test_slicer_category_and_title(proj):
    vid = proj.add_visual("Page 1", "slicer", category="Sales.Product", title="Product")
    vis = _visual(proj, vid)
    assert list(vis["query"]["queryState"]) == ["Values"]
    assert vis["objects"]["header"][0]["properties"]["show"] == VZ.literal("false")   # no "Product / Product"
    assert "header" not in _visual(proj, proj.add_visual("Page 1", "slicer", ["Sales.Product"]))["objects"]


def test_textbox_styling(proj):
    vid = proj.add_textbox("Page 1", ["Line one", "Line two"], height=80, size=11, bold=False,
                           color="#0f766e", align="center", bg="#FFFFFF", border="#0F766E", radius=20)
    vis = _visual(proj, vid)
    paras = vis["objects"]["general"][0]["properties"]["paragraphs"]
    assert [p["textRuns"][0]["value"] for p in paras] == ["Line one", "Line two"]
    assert paras[0]["horizontalTextAlignment"] == "center"
    assert paras[0]["textRuns"][0]["textStyle"] == {"fontSize": "11pt", "color": "#0F766E"}
    box = vis["visualContainerObjects"]
    assert box["background"][0]["properties"]["transparency"]["expr"]["Literal"]["Value"] == "0D"
    assert box["border"][0]["properties"]["radius"]["expr"]["Literal"]["Value"] == "20L"
    assert proj.textbox_min_height(11) <= 40 < proj.textbox_min_height(20)
    for kw, msg in (({"color": "teal"}, "--color"), ({"align": "justify"}, "--align")):
        with pytest.raises(P.ProjectError, match=msg):
            proj.add_textbox("Page 1", "x", **kw)


def test_format_page(proj):
    objs = proj.format_page("Page 1", [("background", "color", "#e3f2ef"), ("outspace", "color", "#FFFFFF")])
    bg = objs["background"][0]["properties"]
    assert bg["color"] == VZ.literal("#E3F2EF") and bg["transparency"]["expr"]["Literal"]["Value"] == "0D"
    proj.format_page("Page 1", [("background", "transparency", "50")])
    pid = proj.pages()[0]["id"]
    page = json.loads(proj.rfiles[f"definition/pages/{pid}/page.json"])
    assert len(page["objects"]["background"]) == 1
    assert page["objects"]["background"][0]["properties"]["transparency"]["expr"]["Literal"]["Value"] == "50D"
    assert proj.validate() == []


def test_validate_visual_root_keys(proj):
    vid = proj.add_textbox("Page 1", "Hi", bg="#FFFFFF")
    pid = proj.pages()[0]["id"]
    rel = f"definition/pages/{pid}/visuals/{vid}/visual.json"
    body = json.loads(proj.rfiles[rel])
    body["visualContainerObjects"] = body["visual"].pop("visualContainerObjects")   # the hand-edit mistake
    proj.rfiles[rel] = json.dumps(body)
    msg = next(i["message"] for i in proj.validate() if i["where"] == rel)
    assert "visualContainerObjects" in msg and "inside \"visual\"" in msg


def test_props_for():
    assert "start" in VZ.props_for("line")["valueAxis"] and "radius" in VZ.props_for("lineChart")["border"]
    assert "labelStyle" in VZ.props_for("pie")["labels"]
    assert "show" in VZ.props_for("card")["categoryLabels"]
    assert "--size" in VZ.props_for("textbox")["(text)"]
    assert all(VZ.props_for(k) for k in VZ.TYPES)
    with pytest.raises(ValueError, match="Unknown visual type"):
        VZ.props_for("sparkle")
