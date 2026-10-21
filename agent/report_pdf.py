"""Formal PDF report writer (ReportLab). Layout follows an academic/consulting report: navy cover page, table of contents, list of figures,
list of tables, numbered sections with captioned figures and tables, references and appendices, 'Page x of y' footers."""
from __future__ import annotations

import io
import re

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

from .labai import fmt_num
from .labreport import Report

NAVY = colors.HexColor("#1F3864")
AMBER = colors.HexColor("#C9962B")
LIGHT = colors.HexColor("#F2F5FA")
GREY = colors.HexColor("#6B7280")
LINE = colors.HexColor("#D5DBE5")
W, H = A4
LM = RM = 2.2 * cm


def styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle("Body", fontName="Helvetica", fontSize=9.8, leading=14.6, alignment=TA_JUSTIFY, spaceAfter=7, textColor=colors.HexColor("#1f2937"))
    return {
        "body": body,
        "h1": ParagraphStyle("Heading1", parent=body, fontName="Times-Bold", fontSize=18, leading=22, textColor=NAVY, alignment=TA_LEFT, spaceBefore=4, spaceAfter=10),
        "h2": ParagraphStyle("Heading2", parent=body, fontName="Times-Bold", fontSize=12.5, leading=16, textColor=NAVY, alignment=TA_LEFT, spaceBefore=10, spaceAfter=5),
        "sub": ParagraphStyle("SubHead", parent=body, fontName="Times-Bold", fontSize=11.5, leading=15, textColor=NAVY, alignment=TA_LEFT, spaceBefore=9, spaceAfter=4),
        "fig": ParagraphStyle("FigureCaption", parent=body, fontName="Helvetica-Oblique", fontSize=8.6, leading=11, alignment=TA_CENTER, textColor=colors.HexColor("#374151"), spaceBefore=3, spaceAfter=1),
        "tab": ParagraphStyle("TableCaption", parent=body, fontName="Helvetica-Bold", fontSize=8.8, leading=11, alignment=TA_LEFT, textColor=NAVY, spaceBefore=6, spaceAfter=3),
        "src": ParagraphStyle("Source", parent=body, fontName="Helvetica", fontSize=7.6, leading=9.5, alignment=TA_LEFT, textColor=GREY, spaceAfter=8),
        "bullet": ParagraphStyle("Bullet", parent=body, leftIndent=14, bulletIndent=3, spaceAfter=3),
        "cell": ParagraphStyle("Cell", fontName="Helvetica", fontSize=8.1, leading=10, textColor=colors.HexColor("#1f2937")),
        "cellh": ParagraphStyle("CellH", fontName="Helvetica-Bold", fontSize=8.2, leading=10, textColor=colors.white),
        "code": ParagraphStyle("Code", fontName="Courier", fontSize=7.4, leading=9.2, textColor=colors.HexColor("#1f2937"), backColor=LIGHT, borderPadding=5, spaceAfter=8, leftIndent=2),
        "toc1": ParagraphStyle("toc1", fontName="Times-Bold", fontSize=10.5, leading=16, textColor=NAVY, leftIndent=0),
        "toc2": ParagraphStyle("toc2", fontName="Helvetica", fontSize=9.2, leading=13, textColor=colors.HexColor("#374151"), leftIndent=16),
        "lof": ParagraphStyle("lof", fontName="Helvetica", fontSize=9.4, leading=15, textColor=colors.HexColor("#374151"), leftIndent=0),
        "callout": ParagraphStyle("Callout", parent=body, fontName="Helvetica", fontSize=9.3, leading=13.2, alignment=TA_LEFT, spaceAfter=0, textColor=colors.HexColor("#1f2937")),
    }


class ListOf(TableOfContents):
    """TableOfContents that listens for its own notification kind (used for the lists of figures and tables)."""

    def __init__(self, kind: str, **kw):
        super().__init__(**kw)
        self.kind = kind

    def notify(self, kind, stuff):
        if kind == self.kind:
            self.addEntry(*stuff)


class NumberedCanvas(rl_canvas.Canvas):
    """Draws the header/footer once the total page count is known."""
    ctx: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        start = NumberedCanvas.ctx.get("body_start", 2)
        for i, st in enumerate(self._saved):
            self.__dict__.update(st)
            n = i + 1
            if n > 1:
                self.setStrokeColor(LINE)
                self.setLineWidth(.6)
                self.line(LM, H - 1.55 * cm, W - RM, H - 1.55 * cm)
                self.line(LM, 1.55 * cm, W - RM, 1.55 * cm)
                self.setFont("Helvetica", 7.8)
                self.setFillColor(GREY)
                self.drawString(LM, H - 1.3 * cm, NumberedCanvas.ctx.get("short", ""))
                self.drawRightString(W - RM, H - 1.3 * cm, NumberedCanvas.ctx.get("title", "")[:70])
                if n < start:
                    self.drawCentredString(W / 2, 1.05 * cm, _roman(n - 1))
                else:
                    self.drawRightString(W - RM, 1.05 * cm, f"Page {n - start + 1} of {total - start + 1}")
            super().showPage()
        super().save()


def _roman(n: int) -> str:
    vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in vals:
        while n >= v:
            out, n = out + s, n - v
    return out


class Doc(BaseDocTemplate):
    def __init__(self, buf, report: Report, S):
        super().__init__(buf, pagesize=A4, leftMargin=LM, rightMargin=RM, topMargin=2.3 * cm, bottomMargin=2.1 * cm, title=report.title, author=report.prepared_by,
                         subject=report.subtitle)
        self.report, self.S, self._k = report, S, 0
        frame = Frame(LM, 2.1 * cm, W - LM - RM, H - 2.3 * cm - 2.1 * cm, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="Cover", frames=[Frame(0, 0, W, H, id="c")], onPage=self.draw_cover), PageTemplate(id="Body", frames=[frame])])
        self.toc_pages: dict[str, int] = {}
        self.body_start = 2

    # ------------------------------------------------------------------ cover
    def draw_cover(self, c, doc):
        r = self.report
        c.saveState()
        split = H * 0.40
        c.setFillColor(NAVY)
        c.rect(0, split, W, H - split, stroke=0, fill=1)
        c.setFillColor(AMBER)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawCentredString(W / 2, H - 6.2 * cm, "DATA ANALYSIS REPORT")
        c.setFillColor(colors.white)
        y = H - 8.4 * cm
        for ln in _wrap(r.title, "Times-Bold", 27, W - 7 * cm):
            c.setFont("Times-Bold", 27)
            c.drawCentredString(W / 2, y, ln)
            y -= 33
        c.setFont("Times-Bold", 14)
        c.setFillColor(colors.HexColor("#E8EEF8"))
        c.drawCentredString(W / 2, y - 8, r.subtitle)
        c.setStrokeColor(AMBER)
        c.setLineWidth(1.4)
        c.line(W / 2 - 1.6 * cm, y - 24, W / 2 + 1.6 * cm, y - 24)
        c.setFont("Times-Italic", 11)
        c.setFillColor(colors.HexColor("#C7D3EA"))
        c.drawCentredString(W / 2, y - 46, f"Dataset: {r.dataset_name[:80]}")
        yy = split - 2.2 * cm
        c.setFillColor(AMBER)
        c.setFont("Helvetica-Bold", 8.8)
        c.drawCentredString(W / 2, yy, "PREPARED BY")
        c.setFillColor(NAVY)
        c.setFont("Times-Bold", 16)
        c.drawCentredString(W / 2, yy - 20, r.prepared_by)
        c.setFillColor(colors.HexColor("#374151"))
        c.setFont("Helvetica", 10)
        yy -= 44
        for line in [r.organisation, f"Prepared for: {r.prepared_for}" if r.prepared_for else "", f"Analysis engine: {r.model}"]:
            if line:
                c.drawCentredString(W / 2, yy, line[:90])
                yy -= 15
        c.setStrokeColor(LINE)
        c.setLineWidth(.6)
        c.line(LM, 2.7 * cm, W - RM, 2.7 * cm)
        c.setFillColor(GREY)
        c.setFont("Helvetica", 9.5)
        c.drawCentredString(W / 2, 2.0 * cm, r.date)
        c.restoreState()

    # ------------------------------------------------------------------ outline / TOC notifications
    def beforeDocument(self):
        self._k = 0                      # bookmark keys must be identical on every pass or the TOC never settles
        self.toc_pages = {}

    def afterFlowable(self, fl):
        if isinstance(fl, Paragraph):
            name = fl.style.name
            txt = re.sub(r"<[^>]+>", "", fl.getPlainText())
            if name == "BodyStart":
                self.body_start = self.page
                NumberedCanvas.ctx["body_start"] = self.page
            page = self.page - self.body_start + 1
            if name in ("Heading1", "Heading2"):
                self._k += 1
                key = f"k{self._k}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(txt, key, 0 if name == "Heading1" else 1, closed=name == "Heading1")
                if self.page >= self.body_start and self.body_start > 1:
                    self.notify("TOCEntry", (0 if name == "Heading1" else 1, txt, page, key))
                    self.toc_pages[txt] = page
            elif name == "FigureCaption":
                self._k += 1
                self.canv.bookmarkPage(f"k{self._k}")
                self.notify("LOFEntry", (0, txt, page, f"k{self._k}"))
                self.toc_pages[txt] = page
            elif name == "TableCaption":
                self._k += 1
                self.canv.bookmarkPage(f"k{self._k}")
                self.notify("LOTEntry", (0, txt, page, f"k{self._k}"))
                self.toc_pages[txt] = page


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return (lines + [cur])[:4]


def fmt_cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return fmt_num(v) if abs(v) >= 1000 or v != int(v) else f"{int(v):,}"
    return str(v)


def make_table(df: pd.DataFrame, S, widths: list[float] | None = None, max_rows: int = 12) -> Table:
    d = df.head(max_rows).copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]) and (d[c].dropna().dt.day == 1).all():
            d[c] = d[c].dt.strftime('%b %Y')
    cols = list(d.columns)
    data = [[Paragraph(str(c).replace("_", " ").title() if "_" in str(c) else str(c), S["cellh"]) for c in cols]]
    for _, r in d.iterrows():
        data.append([Paragraph(fmt_cell(r[c]).replace("&", "&amp;").replace("<", "&lt;")[:60], S["cell"]) for c in cols])
    avail = W - LM - RM
    if not widths:
        lens = [max(len(str(c)), *(len(fmt_cell(v)) for v in d[c])) if len(d) else len(str(c)) for c in cols]
        lens = [max(l, 9) for l in lens]
        tot = sum(lens)
        widths = [avail * l / tot for l in lens]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
                           ("GRID", (0, 0), (-1, -1), .4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
    return t


def esc(t: str) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_pdf(rep: Report) -> tuple[bytes, dict[str, int]]:
    S = styles()
    sec_n = len(rep.sections)
    tocs = {}

    def story_factory():
        toc = TableOfContents()
        toc.levelStyles = [S["toc1"], S["toc2"]]
        toc.dotsMinLevel = 0
        lof, lot = ListOf("LOFEntry"), ListOf("LOTEntry")
        for x in (lof, lot):
            x.levelStyles = [S["lof"]]
            x.dotsMinLevel = 0
        st = [NextPageTemplate("Body"), PageBreak()]
        st += [Paragraph("Table of Contents", ParagraphStyle("FrontH", parent=S["h1"])), toc, PageBreak()]
        if rep.figures:
            st += [Paragraph("List of Figures", ParagraphStyle("FrontH", parent=S["h1"])), lof, Spacer(1, 18)]
        if rep.tables:
            st += [Paragraph("List of Tables", ParagraphStyle("FrontH", parent=S["h1"])), lot]
        st += [PageBreak(), Paragraph("", ParagraphStyle("BodyStart", parent=S["body"], fontSize=1, leading=1, spaceAfter=0))]
        # ---- 1 executive summary
        st += [Paragraph("Executive Summary", S["h1"])]
        st += [Paragraph(esc(p), S["body"]) for p in rep.exec_summary]
        if rep.sections:
            rows = [[Paragraph("Section", S["cellh"]), Paragraph("Key finding", S["cellh"])]] + [[Paragraph(esc(f"{i + 3}. {s.title}"), S["cell"]), Paragraph(esc(s.key_finding), S["cell"])] for i, s in enumerate(rep.sections)]
            t = Table(rows, colWidths=[(W - LM - RM) * .32, (W - LM - RM) * .68], repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]), ("GRID", (0, 0), (-1, -1), .4, LINE),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            st += [Spacer(1, 6), Paragraph("Summary of key findings", ParagraphStyle("TabLabel", parent=S["tab"])), t, Spacer(1, 6)]
        st += [PageBreak()]
        # ---- 1 introduction
        st += [Paragraph("1. Introduction", S["h1"])] + [Paragraph(esc(p), S["body"]) for p in rep.intro]
        # ---- 2 data and method
        st += [Paragraph("2. Data and Methodology", S["h1"]), Paragraph("2.1 Data sources", S["h2"])]
        if rep.sources is not None and len(rep.sources):
            st += [Paragraph(f"Table {rep.num['sources']}: Uploaded files", S["tab"]), make_table(rep.sources, S), Spacer(1, 6)]
        st += [Paragraph("2.2 Data preparation and privacy screening", S["h2"])] + [Paragraph(esc(p), S["bullet"], bulletText="•") for p in rep.prep_steps]
        st += [Paragraph("2.3 Data quality", S["h2"])]
        if rep.quality is not None and len(rep.quality):
            st += [Paragraph(f"Table {rep.num['quality']}: Data quality summary", S["tab"]), make_table(rep.quality, S), Spacer(1, 6)]
        st += [Paragraph("2.4 Analytical approach", S["h2"])] + [Paragraph(esc(p), S["bullet"], bulletText="•") for p in rep.approach]
        # ---- analysis sections
        for i, s in enumerate(rep.sections):
            num = i + 3
            st += [PageBreak(), Paragraph(f"{num}. {esc(s.title)}", S["h1"])]
            if s.paragraphs:
                st += [Paragraph(esc(s.paragraphs[0]), S["body"])]
            if s.figure:
                img = Image(io.BytesIO(s.figure.png), width=W - LM - RM - 1.6 * cm, height=(W - LM - RM - 1.6 * cm) * 0.5)
                st += [KeepTogether([img, Paragraph(f"Figure {s.figure.id}: {esc(s.figure.caption)}", S["fig"]), Paragraph(f"Source: {esc(s.figure.source)}", ParagraphStyle("srcc", parent=S["src"], alignment=TA_CENTER))])]
            for p in s.paragraphs[1:]:
                st += [Paragraph(esc(p), S["body"])]
            if s.bullets:
                st += [Paragraph(esc(b), S["bullet"], bulletText="•") for b in s.bullets]
            tail = []
            if s.table:
                tail += [Paragraph(f"Table {s.table.id}: {esc(s.table.caption)}", S["tab"]), make_table(s.table.df, S, max_rows=10), Paragraph(f"Source: {esc(s.table.source)}", S["src"])]
            if s.key_finding:
                box = Table([[Paragraph(f"<b>Key finding.</b> {esc(s.key_finding)}", S["callout"])]], colWidths=[W - LM - RM])
                box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEBEFORE", (0, 0), (0, -1), 3, AMBER), ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
                tail += [Spacer(1, 4), box]
            if tail:
                st += [KeepTogether(tail)]          # table and key-finding box travel together so the box is never stranded on its own page
        # ---- conclusions & recommendations
        n = sec_n + 3
        st += [PageBreak(), Paragraph(f"{n}. Conclusions and Recommendations", S["h1"]), Paragraph(f"{n}.1 Conclusions", S["h2"])]
        st += [Paragraph(esc(c), S["bullet"], bulletText="•") for c in rep.conclusions]
        st += [Paragraph(f"{n}.2 Recommendations", S["h2"])]
        for j, r in enumerate(rep.recommendations, 1):
            st += [Paragraph(f"<b>R{j}. {esc(r['title'])}.</b> {esc(r['detail'])}", S["body"])]
        st += [Paragraph(f"{n + 1}. Limitations and Assumptions", S["h1"])] + [Paragraph(esc(x), S["bullet"], bulletText="•") for x in rep.limitations]
        # ---- references
        st += [PageBreak(), Paragraph("References", S["h1"])]
        st += [Paragraph(esc(r_), ParagraphStyle("Ref", parent=S["body"], leftIndent=18, firstLineIndent=-18, alignment=TA_LEFT)) for r_ in rep.references]
        # ---- appendix A  AI usage
        st += [PageBreak(), Paragraph("Appendix A: AI Usage Declaration and Log", S["h1"]), Paragraph("A.1 Tool used", S["sub"]),
               Paragraph(f"Analysis engine: {esc(rep.model)}. Data analysis, calculation, charting and forecasting were performed by deterministic code (SQLite, pandas, Prophet, Matplotlib).", S["body"]),
               Paragraph("A.2 What AI did and did not do", S["sub"]),
               Paragraph("Where an AI service was available it proposed the report structure, drafted the SQL queries and wrote the narrative paragraphs. It did not perform calculations. "
                         "Personal data columns were removed before analysis, and only the aggregated query results shown in this report were passed to the AI service.", S["body"]),
               Paragraph("A.3 Verification", S["sub"]),
               Paragraph("Every SQL query was validated as a single read-only SELECT statement and run with a time and row limit. Each AI-written paragraph was checked against the query results; "
                         "paragraphs citing a figure not present in the data were discarded and replaced by rule-written text. This report should still be reviewed by a person before use.", S["body"])]
        for nline in rep.ai_notes:
            st += [Paragraph(esc(nline), S["bullet"], bulletText="•")]
        if rep.ai_log:
            st += [Paragraph("A.4 Generation log", S["sub"]), Paragraph(f"Table {rep.num['ailog']}: Report generation steps", S["tab"]),
                   make_table(pd.DataFrame([{"Step": x["step"], "Mode": "AI" if x["mode"] == "ai" else "Rules", "Detail": x["detail"][:70], "Elapsed (s)": x["seconds"]} for x in rep.ai_log]), S)]
        sec_notes = [(s.number, s.title, s.author, "; ".join(s.notes)) for s in rep.sections]
        st += [Spacer(1, 8), Paragraph(f"Table {rep.num['authors']}: Authorship of each section", S["tab"]),
               make_table(pd.DataFrame([{"Section": f"{int(a) + 2}. {b}", "Drafted by": "AI" if c == "ai" else "Rules", "Notes": d[:80]} for a, b, c, d in sec_notes]), S)]
        # ---- appendix B queries
        st += [PageBreak(), Paragraph("Appendix B: Query Log", S["h1"]), Paragraph("Each figure and table was produced by the query shown below. Queries are read-only.", S["body"])]
        for i, s in enumerate(rep.sections, 1):
            st += [Paragraph(f"Q{i}. {esc(s.title)}", S["sub"]), Paragraph(esc(s.sql or "Forecast computed with Prophet on the monthly aggregate of the main measure; no SQL query."), S["code"])]
        # ---- appendix C dictionary
        if rep.data_dict is not None and len(rep.data_dict):
            st += [PageBreak(), Paragraph("Appendix C: Data Dictionary", S["h1"]), Paragraph(f"Table {rep.num['dict']}: Columns analysed", S["tab"]), make_table(rep.data_dict, S, max_rows=60)]
        return st

    buf = io.BytesIO()
    short = (rep.organisation or rep.prepared_by)[:40]
    NumberedCanvas.ctx = {"short": short, "title": rep.title, "body_start": 2}
    doc = Doc(buf, rep, S)
    doc.multiBuild(story_factory(), canvasmaker=NumberedCanvas)
    return buf.getvalue(), doc.toc_pages
