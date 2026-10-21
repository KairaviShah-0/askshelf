"""Data Lab tests: upload, privacy screening, safe SQL, planning, forecasting, report writers and the API (built-in mode, no AI key)."""
import io
from pathlib import Path

import pandas as pd
import pytest

from agent import datalab as D
from agent import labai, labreport, report_docx, report_pdf, report_xlsx

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture(scope="module")
def lab():
    lb = D.Lab("pytest")
    for f in sorted(SAMPLE.glob("*.csv")):
        lb.add_file(f.name, f.read_bytes())
    return lb


def test_files_fuse_and_personal_columns_are_dropped(lab):
    assert list(lab.tables) == ["combined_data"] and len(lab.tables["combined_data"]) > 2000
    assert "source_file" in lab.tables["combined_data"].columns
    assert "customer_email" not in lab.tables["combined_data"].columns and {d["column"] for d in lab.dropped} == {"customer_email"}
    assert D.profile(lab)["dropped"][0]["files"] and D.profile(lab)["tables"][0]["time_column"] == "order_date"


def test_pii_detected_by_values_not_just_names():
    df = pd.DataFrame({"contact": [f"user{i}@example.com" for i in range(20)], "amount": range(20)})
    lb = D.Lab("pii")
    lb.add_file("x.csv", df.to_csv(index=False).encode())
    assert "contact" not in lb.tables["x"].columns


def test_excel_and_messy_values_are_cleaned():
    buf = io.BytesIO()
    pd.DataFrame({"Order Date": ["2026-01-05", "2026-02-03"] * 5, "Sales $": ["$1,200", "$900"] * 5, "Region": ["N", "S"] * 5}).to_excel(buf, index=False)
    lb = D.Lab("xl")
    lb.add_file("Book 1.xlsx", buf.getvalue())
    t = next(iter(lb.tables.values()))
    assert list(t.columns) == ["order_date", "sales", "region"] and pd.api.types.is_numeric_dtype(t["sales"]) and t["sales"].sum() == 10500 and str(t["order_date"].dtype).startswith("datetime")


@pytest.mark.parametrize("sql", ["DROP TABLE combined_data", "SELECT 1; DELETE FROM combined_data", "PRAGMA table_info(combined_data)", "SELECT * FROM sqlite_master",
                                 "UPDATE combined_data SET region='x'", "ATTACH DATABASE 'x' AS y", "SELECT load_extension('x')"])
def test_unsafe_sql_is_refused(lab, sql):
    with pytest.raises((D.UnsafeSQL, ValueError)):
        D.run_select(lab, sql)
    assert len(lab.tables["combined_data"]) > 2000          # data untouched


def test_select_and_cte_run_with_limits(lab):
    assert len(D.run_select(lab, "WITH a AS (SELECT region, SUM(sales_amount) s FROM combined_data GROUP BY 1) SELECT * FROM a")) == 4
    assert len(D.run_select(lab, "SELECT * FROM combined_data", max_rows=50)) == 50


def test_rule_based_questions(lab):
    top = labai.lab_answer(lab, "Top 3 suppliers by sales amount", [], None)
    assert top["kind"] == "sql" and top["table"]["total_rows"] == 3 and top["chart"] and "supplier" in top["sql"]
    trend = labai.lab_answer(lab, "monthly sales trend", [], None)
    assert trend["table"]["total_rows"] == 24 and "strftime" in trend["sql"]
    assert labai.lab_answer(lab, "show me customer ID 45", [], None)["blocked"]
    assert "Upload" in labai.lab_answer(D.Lab("empty"), "top suppliers", [], None)["error"]


def test_prophet_forecast_and_grouped(lab):
    p = D.pick_columns(lab)
    assert p["measure"] == "sales_amount"                     # prefers revenue-like columns over quantity
    r = D.forecast_series(p["df"], p["time"], "sales_amount", 6)
    assert r["unit"] == "month" and len(r["forecast"]) == 6 and r["mape"] is not None and (r["forecast"]["lo"] <= r["forecast"]["hi"]).all()
    assert len(D.forecast_groups(p["df"], p["time"], "sales_amount", "region", 3, 4, "MS")) == 12
    f = labai.lab_answer(lab, "forecast sales for the next 3 months", [], None)
    assert f["kind"] == "forecast" and f["forecast"]["unit"] == "month"


def test_numbers_check_catches_invented_figures():
    support = "total=1,234,567 mean=4,000"
    assert labreport.supported("Sales were 1.23M and the mean 4,000.", support, [])
    assert not labreport.supported("Sales were 9,876,543.", support, [])
    assert labreport.tidy_numbers("sales of 3469376.93 in 2026") == "sales of 3,469,376.93 in 2026"


@pytest.fixture(scope="module")
def report(lab):
    return labreport.build_report(lab, None, {"organisation": "Test Co", "prepared_for": "Board"})


def test_report_structure(report):
    assert report.title == "Data Analysis Report: Wholesale Orders" and len(report.sections) >= 5 and report.forecast_table is not None
    assert report.figures and report.tables and report.recommendations and report.limitations and report.references and report.ai_log
    assert all(s.key_finding and s.paragraphs for s in report.sections)
    assert report.num["sources"] == 1 and report.num["quality"] == 2


def test_report_pdf_docx_xlsx(report):
    pdf, pages = report_pdf.build_pdf(report)
    assert pdf[:4] == b"%PDF" and len(pdf) > 100_000 and any("Executive" in k for k in pages)
    from docx import Document
    d = Document(io.BytesIO(report_docx.build_docx(report, pages)))
    heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
    assert "Executive Summary" in heads and "References" in heads and any(h.startswith("Appendix B") for h in heads) and len(d.inline_shapes) == len(report.figures)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(report_xlsx.report_xlsx(report)))
    assert wb.sheetnames[0] == "Summary" and "Queries" in wb.sheetnames and len(wb.sheetnames) >= 6
    assert openpyxl.load_workbook(io.BytesIO(report_xlsx.table_xlsx(pd.DataFrame({"a": [1, 2]}), "T", "SELECT 1", "q"))).sheetnames == ["Result", "About"]
