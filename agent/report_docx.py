"""Word (.docx) report writer. Same structure and numbering as the PDF; table of contents and lists of figures/tables are pre-filled with
the PDF's page numbers (right-click > Update Field in Word refreshes them against Word's own pagination)."""
from __future__ import annotations

import io

import pandas as pd
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .labreport import Report
from .report_pdf import fmt_cell

NAVY = RGBColor(0x1F, 0x38, 0x64)
AMBER = RGBColor(0xC9, 0x96, 0x2B)
GREY = RGBColor(0x6B, 0x72, 0x80)


def _shade(cell, hex_fill: str):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _borders(table, color="D5DBE5"):
    tbl = table._tbl
    pr = tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:space"), "0")
        e.set(qn("w:color"), color)
        b.append(e)
    pr.append(b)


def _field(par, instr: str, placeholder: str = "1", size: float | None = None, color: RGBColor | None = None):
    r = par.add_run()
    for kind, text in (("begin", None), (None, instr), ("separate", None), ("text", placeholder), ("end", None)):
        if kind in ("begin", "separate", "end"):
            c = OxmlElement("w:fldChar")
            c.set(qn("w:fldCharType"), kind)
            r._r.append(c)
        elif kind == "text":
            t = OxmlElement("w:t")
            t.text = text
            r._r.append(t)
        else:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = text
            r._r.append(it)
    if size:
        r.font.size = Pt(size)
    if color:
        r.font.color.rgb = color
    return r


def build_docx(rep: Report, pages: dict[str, int] | None = None) -> bytes:
    pages = pages or {}
    doc = Document()
    st = doc.styles
    st["Normal"].font.name = "Calibri"
    st["Normal"].font.size = Pt(10.5)
    st["Normal"].paragraph_format.space_after = Pt(6)
    st["Normal"].paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 18), ("Heading 2", 13)):
        h = st[name]
        h.font.name = "Cambria"
        h.font.size = Pt(size)
        h.font.bold = True
        h.font.color.rgb = NAVY
        h.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
        h.paragraph_format.space_after = Pt(6)
        h.element.rPr.rFonts.set(qn("w:eastAsia"), "Cambria")
    cap = st["Caption"]
    cap.font.name, cap.font.size, cap.font.italic, cap.font.bold = "Calibri", Pt(9), True, False
    cap.font.color.rgb = RGBColor(0x37, 0x41, 0x51)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.3), Cm(2.1)
    sec.different_first_page_header_footer = True
    hp = sec.header.paragraphs[0]
    hp.text = ""
    r = hp.add_run((rep.organisation or rep.prepared_by)[:40])
    r.font.size, r.font.color.rgb = Pt(8), GREY
    hp.paragraph_format.tab_stops.add_tab_stop(Cm(16.6), WD_TAB_ALIGNMENT.RIGHT)
    r = hp.add_run("\t" + rep.title[:70])
    r.font.size, r.font.color.rgb = Pt(8), GREY
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = fp.add_run("Page ")
    r.font.size, r.font.color.rgb = Pt(8), GREY
    _field(fp, "PAGE", "1", 8, GREY)
    r = fp.add_run(" of ")
    r.font.size, r.font.color.rgb = Pt(8), GREY
    _field(fp, "NUMPAGES", "1", 8, GREY)

    def para(text="", style=None, bold=False, italic=False, size=None, color=None, align=None, after=None):
        p = doc.add_paragraph(style=style)
        if text:
            r = p.add_run(text)
            r.bold, r.italic = bold, italic
            if size:
                r.font.size = Pt(size)
            if color:
                r.font.color.rgb = color
        if align is not None:
            p.alignment = align
        if after is not None:
            p.paragraph_format.space_after = Pt(after)
        return p

    def body(text):
        p = para(text, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        return p

    def bullet(text):
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(text)
        p.paragraph_format.space_after = Pt(3)
        return p

    def table(df: pd.DataFrame, max_rows=12, widths=None):
        d = df.head(max_rows).copy()
        for c in d.columns:
            if pd.api.types.is_datetime64_any_dtype(d[c]) and (d[c].dropna().dt.day == 1).all():
                d[c] = d[c].dt.strftime("%b %Y")
        t = doc.add_table(rows=1, cols=len(d.columns))
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        _borders(t)
        for j, c in enumerate(d.columns):
            cell = t.rows[0].cells[j]
            _shade(cell, "1F3864")
            cell.text = ""
            run = cell.paragraphs[0].add_run(str(c).replace("_", " ").title() if "_" in str(c) else str(c))
            run.bold, run.font.size, run.font.color.rgb = True, Pt(8.5), RGBColor(255, 255, 255)
        for i, (_, row) in enumerate(d.iterrows()):
            cells = t.add_row().cells
            for j, c in enumerate(d.columns):
                cells[j].text = ""
                run = cells[j].paragraphs[0].add_run(fmt_cell(row[c])[:60])
                run.font.size = Pt(8.5)
                if i % 2:
                    _shade(cells[j], "F2F5FA")
        for row in t.rows:
            for cell in row.cells:
                cell.paragraphs[0].paragraph_format.space_after = Pt(2)
        doc.add_paragraph().paragraph_format.space_after = Pt(2)
        return t

    def caption(kind: str, n: int, text: str, above=False):
        p = doc.add_paragraph(style="Caption")
        p.add_run(f"{kind} ")
        _field(p, f"SEQ {kind} \\* ARABIC", str(n))
        p.add_run(f": {text}")
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT if above else WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = above
        return p

    def source(text: str, center=False):
        p = para(f"Source: {text}", size=8, color=GREY, after=8, align=WD_ALIGN_PARAGRAPH.CENTER if center else None)
        return p

    def page_break():
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def toc_line(text: str, page, level=0, bold=False):
        p = doc.add_paragraph()
        p.paragraph_format.tab_stops.add_tab_stop(Cm(16.6), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        p.paragraph_format.left_indent = Cm(0.6 * level)
        p.paragraph_format.space_after = Pt(3)
        r = p.add_run(text + "\t" + (str(page) if page else ""))
        r.font.size = Pt(10 if level == 0 else 9.5)
        r.bold = bold or level == 0
        if level == 0:
            r.font.color.rgb = NAVY
        return p

    # ---------------------------------------------------------------- cover (navy block as a one-cell table)
    ct = doc.add_table(rows=1, cols=1)
    ct.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = ct.rows[0].cells[0]
    _shade(cell, "1F3864")
    cell.width = Cm(16.6)
    cell.text = ""
    def cp(text, size, color, bold=False, italic=False, font="Calibri", before=0, after=6):
        p = cell.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(before), Pt(after)
        r = p.add_run(text)
        r.font.size, r.bold, r.italic, r.font.name = Pt(size), bold, italic, font
        r.font.color.rgb = color
    cp("", 12, RGBColor(255, 255, 255), before=60)
    cp("DATA ANALYSIS REPORT", 10, RGBColor(0xE0, 0xB4, 0x4C), bold=True, before=70, after=26)
    cp(rep.title, 28, RGBColor(255, 255, 255), bold=True, font="Cambria", after=14)
    cp(rep.subtitle, 15, RGBColor(0xE8, 0xEE, 0xF8), bold=True, font="Cambria", after=18)
    cp(f"Dataset: {rep.dataset_name[:80]}", 11, RGBColor(0xC7, 0xD3, 0xEA), italic=True, font="Cambria", after=90)
    cell.paragraphs[0]._element.getparent().remove(cell.paragraphs[0]._element)
    para("", after=40)
    para("PREPARED BY", bold=True, size=9, color=AMBER, align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
    para(rep.prepared_by, bold=True, size=16, color=NAVY, align=WD_ALIGN_PARAGRAPH.CENTER, after=6)
    for line in [rep.organisation, f"Prepared for: {rep.prepared_for}" if rep.prepared_for else "", f"Analysis engine: {rep.model}"]:
        if line:
            para(line, size=10, align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
    para("", after=70)
    para(rep.date, size=10, color=GREY, align=WD_ALIGN_PARAGRAPH.CENTER)
    page_break()

    # ---------------------------------------------------------------- front matter
    doc.add_paragraph("Table of Contents", style="Heading 1")
    n = len(rep.sections)
    entries = [("Executive Summary", 0), ("1. Introduction", 0), ("2. Data and Methodology", 0), ("2.1 Data sources", 1), ("2.2 Data preparation and privacy screening", 1), ("2.3 Data quality", 1), ("2.4 Analytical approach", 1)]
    entries += [(f"{i + 3}. {s.title}", 0) for i, s in enumerate(rep.sections)]
    entries += [(f"{n + 3}. Conclusions and Recommendations", 0), (f"{n + 3}.1 Conclusions", 1), (f"{n + 3}.2 Recommendations", 1), (f"{n + 4}. Limitations and Assumptions", 0), ("References", 0),
                ("Appendix A: AI Usage Declaration and Log", 0), ("Appendix B: Query Log", 0), ("Appendix C: Data Dictionary", 0)]
    for t, lv in entries:
        toc_line(t, pages.get(t), lv)
    page_break()
    if rep.figures:
        doc.add_paragraph("List of Figures", style="Heading 1")
        for f in rep.figures:
            k = f"Figure {f.id}: {f.caption}"
            toc_line(k, pages.get(k), 1, bold=False).paragraph_format.left_indent = Cm(0)
        para("", after=10)
    if rep.tables:
        doc.add_paragraph("List of Tables", style="Heading 1")
        for t_ in [(rep.num["sources"], "Uploaded files"), (rep.num["quality"], "Data quality summary")] + [(x.id, x.caption) for x in rep.tables] + [(rep.num["ailog"], "Report generation steps"), (rep.num["authors"], "Authorship of each section"), (rep.num["dict"], "Columns analysed")]:
            k = f"Table {t_[0]}: {t_[1]}"
            toc_line(k, pages.get(k), 1, bold=False).paragraph_format.left_indent = Cm(0)
    page_break()

    # ---------------------------------------------------------------- body
    doc.add_paragraph("Executive Summary", style="Heading 1")
    for p in rep.exec_summary:
        body(p)
    if rep.sections:
        para("Summary of key findings", bold=True, size=9.5, color=NAVY, after=3)
        table(pd.DataFrame([{"Section": f"{i + 3}. {s.title}", "Key finding": s.key_finding} for i, s in enumerate(rep.sections)]))
    page_break()
    doc.add_paragraph("1. Introduction", style="Heading 1")
    for p in rep.intro:
        body(p)
    doc.add_paragraph("2. Data and Methodology", style="Heading 1")
    doc.add_paragraph("2.1 Data sources", style="Heading 2")
    if rep.sources is not None and len(rep.sources):
        caption("Table", rep.num["sources"], "Uploaded files", above=True)
        table(rep.sources)
    doc.add_paragraph("2.2 Data preparation and privacy screening", style="Heading 2")
    for p in rep.prep_steps:
        bullet(p)
    doc.add_paragraph("2.3 Data quality", style="Heading 2")
    if rep.quality is not None and len(rep.quality):
        caption("Table", rep.num["quality"], "Data quality summary", above=True)
        table(rep.quality)
    doc.add_paragraph("2.4 Analytical approach", style="Heading 2")
    for p in rep.approach:
        bullet(p)
    for i, s in enumerate(rep.sections):
        page_break()
        doc.add_paragraph(f"{i + 3}. {s.title}", style="Heading 1")
        if s.paragraphs:
            body(s.paragraphs[0])
        if s.figure:
            p = para(align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
            p.add_run().add_picture(io.BytesIO(s.figure.png), width=Cm(15.2))
            p.paragraph_format.keep_with_next = True
            caption("Figure", s.figure.id, s.figure.caption)
            source(s.figure.source, center=True)
        for q in s.paragraphs[1:]:
            body(q)
        for b in s.bullets:
            bullet(b)
        if s.table:
            caption("Table", s.table.id, s.table.caption, above=True)
            table(s.table.df)
            source(s.table.source)
        if s.key_finding:
            t = doc.add_table(rows=1, cols=1)
            _borders(t, "F2F5FA")
            c = t.rows[0].cells[0]
            _shade(c, "F2F5FA")
            c.text = ""
            r = c.paragraphs[0].add_run("Key finding. ")
            r.bold, r.font.size, r.font.color.rgb = True, Pt(10), NAVY
            r2 = c.paragraphs[0].add_run(s.key_finding)
            r2.font.size = Pt(10)
    page_break()
    doc.add_paragraph(f"{n + 3}. Conclusions and Recommendations", style="Heading 1")
    doc.add_paragraph(f"{n + 3}.1 Conclusions", style="Heading 2")
    for c in rep.conclusions:
        bullet(c)
    doc.add_paragraph(f"{n + 3}.2 Recommendations", style="Heading 2")
    for j, r_ in enumerate(rep.recommendations, 1):
        p = doc.add_paragraph()
        a = p.add_run(f"R{j}. {r_['title']}. ")
        a.bold = True
        p.add_run(r_["detail"])
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    doc.add_paragraph(f"{n + 4}. Limitations and Assumptions", style="Heading 1")
    for x in rep.limitations:
        bullet(x)
    page_break()
    doc.add_paragraph("References", style="Heading 1")
    for r_ in rep.references:
        p = para(r_)
        p.paragraph_format.left_indent = Cm(0.8)
        p.paragraph_format.first_line_indent = Cm(-0.8)
    page_break()
    doc.add_paragraph("Appendix A: AI Usage Declaration and Log", style="Heading 1")
    for h_, txt in (("A.1 Tool used", f"Analysis engine: {rep.model}. Data analysis, calculation, charting and forecasting were performed by deterministic code (SQLite, pandas, Prophet, Matplotlib)."),
                    ("A.2 What AI did and did not do", "Where an AI service was available it proposed the report structure, drafted the SQL queries and wrote the narrative paragraphs. It did not perform calculations. Personal data columns were removed before analysis, and only the aggregated query results shown in this report were passed to the AI service."),
                    ("A.3 Verification", "Every SQL query was validated as a single read-only SELECT statement and run with a time and row limit. Each AI-written paragraph was checked against the query results; paragraphs citing a figure not present in the data were discarded and replaced by rule-written text. This report should still be reviewed by a person before use.")):
        p = doc.add_paragraph()
        r = p.add_run(h_)
        r.bold, r.font.size, r.font.color.rgb, r.font.name = True, Pt(11.5), NAVY, "Cambria"
        body(txt)
    for line in rep.ai_notes:
        bullet(line)
    if rep.ai_log:
        p = doc.add_paragraph()
        r = p.add_run("A.4 Generation log")
        r.bold, r.font.size, r.font.color.rgb, r.font.name = True, Pt(11.5), NAVY, "Cambria"
        caption("Table", rep.num["ailog"], "Report generation steps", above=True)
        table(pd.DataFrame([{"Step": x["step"], "Mode": "AI" if x["mode"] == "ai" else "Rules", "Detail": x["detail"][:70], "Elapsed (s)": x["seconds"]} for x in rep.ai_log]))
    caption("Table", rep.num["authors"], "Authorship of each section", above=True)
    table(pd.DataFrame([{"Section": f"{i + 3}. {s.title}", "Drafted by": "AI" if s.author == "ai" else "Rules", "Notes": "; ".join(s.notes)[:80]} for i, s in enumerate(rep.sections)]))
    page_break()
    doc.add_paragraph("Appendix B: Query Log", style="Heading 1")
    body("Each figure and table was produced by the query shown below. Queries are read-only.")
    for i, s in enumerate(rep.sections, 1):
        p = doc.add_paragraph()
        r = p.add_run(f"Q{i}. {s.title}")
        r.bold, r.font.size, r.font.color.rgb, r.font.name = True, Pt(11), NAVY, "Cambria"
        q = para(s.sql or "Forecast computed with Prophet on the monthly aggregate of the main measure; no SQL query.", size=8.5)
        for run in q.runs:
            run.font.name = "Courier New"
    if rep.data_dict is not None and len(rep.data_dict):
        page_break()
        doc.add_paragraph("Appendix C: Data Dictionary", style="Heading 1")
        caption("Table", rep.num["dict"], "Columns analysed", above=True)
        table(rep.data_dict, max_rows=60)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
