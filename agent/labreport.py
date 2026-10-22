"""Autonomous report builder for uploaded data.

Pipeline: profile -> plan sections (AI, or rules without a key) -> run each analysis (validated read-only SQL / Prophet) -> chart ->
formal narrative -> executive summary, conclusions and recommendations -> a `Report` model that the PDF / Word / Excel writers render.

Quality guards: every number-heavy sentence written by the AI is checked against the data it was given; sentences that cite a figure
the data does not contain are replaced by a rule-written paragraph, and the replacement is declared in the AI-usage appendix.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from . import datalab as D
from . import guardrails as G
from .labai import fmt_num, offline_explain, plan_with_ai, stats_text
from .gemini import _json_loads


@dataclass
class Figure:
    id: int
    caption: str
    png: bytes
    source: str


@dataclass
class TableItem:
    id: int
    caption: str
    df: pd.DataFrame
    source: str


@dataclass
class Section:
    number: str
    title: str
    purpose: str
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    key_finding: str = ""
    figure: Figure | None = None
    table: TableItem | None = None
    sql: str | None = None
    full_df: pd.DataFrame | None = None
    kind: str = "ranking"
    author: str = "rules"                     # ai | rules
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    title: str
    subtitle: str
    organisation: str
    prepared_for: str
    prepared_by: str
    date: str
    dataset_name: str
    exec_summary: list[str] = field(default_factory=list)
    intro: list[str] = field(default_factory=list)
    sources: pd.DataFrame | None = None
    prep_steps: list[str] = field(default_factory=list)
    quality: pd.DataFrame | None = None
    approach: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    ai_log: list[dict] = field(default_factory=list)
    ai_notes: list[str] = field(default_factory=list)
    model: str = "Built-in rule engine (no AI key)"
    data_dict: pd.DataFrame | None = None
    forecast_table: pd.DataFrame | None = None
    profile: dict = field(default_factory=dict)
    figures: list[Figure] = field(default_factory=list)
    tables: list[TableItem] = field(default_factory=list)
    num: dict = field(default_factory=dict)


REFERENCES = [
    "Hipp, D. R. (2024). SQLite (Version 3) [Computer software]. SQLite Consortium. https://www.sqlite.org",
    "Hyndman, R. J., & Athanasopoulos, G. (2021). Forecasting: Principles and practice (3rd ed.). OTexts. https://otexts.com/fpp3/",
    "McKinney, W. (2010). Data structures for statistical computing in Python. Proceedings of the 9th Python in Science Conference, 56-61.",
    "Office of the Australian Information Commissioner. (2024). Australian Privacy Principles guidelines. https://www.oaic.gov.au",
    "Taylor, S. J., & Letham, B. (2018). Forecasting at scale. The American Statistician, 72(1), 37-45. https://doi.org/10.1080/00031305.2017.1380080",
]

SECTION_SYSTEM = """Do not state or assume a currency (no 'dollars' or '$') unless the column name says so; write plain numbers. You are a senior data analyst writing one section of a formal analytical report. Use ONLY numbers that appear in the data provided; never invent or estimate figures.
Write in a formal, third-person, past/present tense register ("The analysis indicates...", "X accounted for..."). No first person, no markdown symbols, no emojis.
Return JSON only: {"paragraphs":["two paragraphs of 2-4 sentences each: (1) what the figure/table shows with specific numbers, (2) what it implies for the business"],
"key_finding":"one sentence stating the single most important result with its number","bullets":[]}"""

SUMMARY_SYSTEM = """Do not state or assume a currency (no 'dollars' or '$') unless the column name says so; write plain numbers. You are a senior data analyst finishing a formal report. Use ONLY figures that appear in the findings provided. Formal third-person register, no markdown symbols.
Return JSON only: {"executive_summary":["2 paragraphs: scope and data, then the main findings with key numbers"],
"conclusions":["4 to 5 one-sentence conclusions, each tied to a finding"],
"recommendations":[{"title":"short action title","detail":"1-2 sentences: the action and the finding that justifies it"}],
"limitations":["2 limitations specific to this dataset and analysis"]}
Provide exactly 4 recommendations, ordered by priority."""

PLAN_REPORT_SYSTEM = """You are designing the structure of a formal analytical report for an uploaded dataset. Choose 5 or 6 sections that each yield a clear chart:
rankings of top/bottom performers, trends over time, breakdowns by category, distributions of a key measure, relationships between two measures, and comparisons across groups.
Prefer analyses that matter for decisions (revenue, cost, margin, delivery performance, penalties, suppliers, regions, products). Do not include forecasting (added automatically). For distribution and relationship sections the question must ask for a random sample of up to 2000 rows (ORDER BY RANDOM() LIMIT 2000), never the first N rows.
Return JSON only: {"sections":[{"title":"formal section title","kind":"ranking|trend|breakdown|distribution|relationship|comparison","question":"a precise analytical question answerable with ONE SQL query on the schema, naming the exact columns to use"}]}"""


# --------------------------------------------------------------------------------------------- deterministic plan / SQL
def rule_sections(lab: D.Lab) -> list[dict]:
    p = D.pick_columns(lab)
    t, m, tc = p["table"], p["measure"], p["time"]
    cats, nums = p["categoricals"], [c for c in p["numerics"] if c != m]
    nice = lambda c: c.replace("_", " ")
    out = []
    if not m:
        return out
    if cats:
        c = p["dimension"] or cats[0]
        out.append({"title": f"Performance by {nice(c).title()}", "kind": "ranking", "purpose": f"Rank each {nice(c)} by total {nice(m)} to identify the strongest and weakest performers.",
                    "sql": f"SELECT {c} AS {c}, ROUND(SUM({m}), 2) AS total_{m} FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY total_{m} DESC LIMIT 15"})
    if tc:
        out.append({"title": f"Monthly Trend in {nice(m).title()}", "kind": "trend", "purpose": f"Track total {nice(m)} by month to identify growth, seasonality and peak periods.",
                    "sql": f"SELECT strftime('%Y-%m', {tc}) AS month, ROUND(SUM({m}), 2) AS total_{m} FROM {t} WHERE {tc} IS NOT NULL GROUP BY 1 ORDER BY 1"})
    for c in [x for x in cats if x != (p["dimension"] or cats[0])][:2]:
        if df_nuniq(p["df"], c) <= 30:
            out.append({"title": f"Breakdown by {nice(c).title()}", "kind": "breakdown", "purpose": f"Compare total {nice(m)} and record counts across {nice(c)}.",
                        "sql": f"SELECT {c} AS {c}, ROUND(SUM({m}), 2) AS total_{m}, COUNT(*) AS records FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY total_{m} DESC LIMIT 15"})
    out.append({"title": f"Distribution of {nice(m).title()}", "kind": "distribution", "purpose": f"Show how individual {nice(m)} values are spread, including skew and outliers.",
                "sql": f"SELECT {m} FROM {t} WHERE {m} IS NOT NULL ORDER BY RANDOM() LIMIT 5000"})
    if nums:
        out.append({"title": f"Relationship between {nice(m).title()} and {nice(nums[0]).title()}", "kind": "relationship", "purpose": f"Assess whether {nice(m)} moves with {nice(nums[0])}.",
                    "sql": f"SELECT {nums[0]}, {m} FROM {t} WHERE {m} IS NOT NULL AND {nums[0]} IS NOT NULL ORDER BY RANDOM() LIMIT 3000"})
    return out[:6]


def df_nuniq(df: pd.DataFrame, c: str) -> int:
    return int(df[c].nunique(dropna=True))


# --------------------------------------------------------------------------------------------- number verification
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*(?:\s?[MBK](?![a-z]))?")


def _vals(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text):
        mult = 1.0
        s = m.strip()
        if s and s[-1] in "MBK":
            mult = {"M": 1e6, "B": 1e9, "K": 1e3}[s[-1]]
            s = s[:-1].strip()
        try:
            out.append(float(s.replace(",", "")) * mult)
        except ValueError:
            continue
    return out


def supported(text: str, support: str, extra: list[float]) -> bool:
    """Every figure of four or more digits in `text` must be within 1.5% of some number in the supporting data."""
    have = np.array(_vals(support) + extra, dtype=float)
    for v in _vals(text):
        if abs(v) < 1000 or 1900 <= abs(v) <= 2100:
            continue
        if not len(have) or not np.any(np.abs(have - v) <= 0.015 * abs(v) + 0.5):
            return False
    return True


def derived_numbers(df: pd.DataFrame) -> list[float]:
    out = []
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]):
            s = df[c].dropna()
            if len(s):
                out += [float(s.sum()), float(s.mean()), float(s.min()), float(s.max()), float(s.median()), float(s.max() - s.min()), float(s.head(3).sum())]
                if len(s) > 1:
                    out += [float(s.iloc[-1] - s.iloc[0]), float(s.max() - s.min())]
    return out


# --------------------------------------------------------------------------------------------- narrative
_BIGNUM = re.compile(r"(?<![\w.,/-])(\d{4,})(\.\d+)?(?![\w,/-])")


def tidy_numbers(text: str) -> str:
    """Put thousands separators on large figures in AI text (leave years such as 2026 alone)."""
    def fix(m):
        whole = m.group(1)
        if m.group(2) is None and len(whole) == 4 and 1900 <= int(whole) <= 2100:
            return m.group(0)
        return f"{int(whole):,}{m.group(2) or ''}"
    return _BIGNUM.sub(fix, text)

def rule_narrative(df: pd.DataFrame, kind: str, title: str, purpose: str) -> dict:
    ex = offline_explain(df, title)
    paras = [f"{purpose} {ex['summary']}", " ".join(ex["bullets"][:3])] if ex["bullets"] else [f"{purpose} {ex['summary']}"]
    if kind == "distribution" and len(df.columns) and pd.api.types.is_numeric_dtype(df.iloc[:, 0]):
        s = df.iloc[:, 0].dropna()
        skew = float(s.skew()) if len(s) > 2 else 0
        paras = [f"{purpose} Across {len(s):,} observations the mean was {fmt_num(s.mean())} and the median {fmt_num(s.median())}, with values ranging from {fmt_num(s.min())} to {fmt_num(s.max())}.",
                 f"The distribution is {'right-skewed, with a small number of high values pulling the mean above the median' if skew > 0.5 else 'left-skewed' if skew < -0.5 else 'broadly symmetric'}; "
                 f"the middle 50% of values lie between {fmt_num(s.quantile(.25))} and {fmt_num(s.quantile(.75))}."]
        ex["summary"] = f"The median value was {fmt_num(s.median())} against a mean of {fmt_num(s.mean())}."
    if kind == "relationship" and df.shape[1] >= 2:
        a, b = df.iloc[:, 0].astype(float), df.iloc[:, 1].astype(float)
        r = float(np.corrcoef(a.dropna(), b.loc[a.dropna().index])[0, 1]) if a.notna().sum() > 3 else float("nan")
        strength = "weak" if abs(r) < .3 else "moderate" if abs(r) < .6 else "strong"
        paras = [f"{purpose} The Pearson correlation between {df.columns[0].replace('_', ' ')} and {df.columns[1].replace('_', ' ')} was {r:.2f} across {len(df):,} observations, indicating a {strength} {'positive' if r > 0 else 'negative'} association.",
                 "Correlation does not establish causation; the relationship should be tested with additional variables before being used for decisions."]
        ex["summary"] = f"The correlation was {r:.2f}, a {strength} {'positive' if r > 0 else 'negative'} association."
    return {"paragraphs": [p for p in paras if p.strip()], "key_finding": ex["summary"], "bullets": []}


def ai_narrative(backend, df: pd.DataFrame, title: str, purpose: str, kind: str) -> tuple[dict | None, str | None]:
    raw = backend.complete(SECTION_SYSTEM, f"SECTION: {title}\nPURPOSE: {purpose}\n\n{stats_text(df, 30)}", json_mode=True, temperature=0.2)
    d = _json_loads(raw)
    if not d or not d.get("paragraphs"):
        return None, "empty narrative"
    support = stats_text(df, 200)
    extra = derived_numbers(df)
    text = " ".join(map(str, d["paragraphs"])) + " " + str(d.get("key_finding", ""))
    if not supported(text, support, extra):
        return None, "cited a figure not present in the data"
    clean = lambda t: tidy_numbers(G.sanitize_output(str(t))[0])
    return {"paragraphs": [clean(p) for p in d["paragraphs"]][:3], "key_finding": clean(d.get("key_finding", "")), "bullets": [clean(b) for b in d.get("bullets", [])][:4]}, None


# --------------------------------------------------------------------------------------------- build
def nice_name(files: list[str]) -> str:
    """'wholesale_orders_2025.csv' + 'wholesale_orders_2026.csv' -> 'Wholesale Orders'."""
    stems = []
    for f in files:
        t = re.sub(r"\.[A-Za-z0-9]+$", "", f)
        t = re.sub(r"(?<![A-Za-z])(19|20)\d{2}(?![A-Za-z])", " ", t)
        t = re.sub(r"[_\-.]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip().title()
        if t and t not in stems:
            stems.append(t)
    return " and ".join(stems[:3]) if stems else "Uploaded Data"


def build_report(lab: D.Lab, backend=None, meta: dict | None = None, progress=None) -> Report:
    meta = meta or {}
    say = progress or (lambda *_: None)
    log: list[dict] = []
    t_start = time.time()

    def step(name: str, detail: str = "", mode: str = "rules"):
        log.append({"step": name, "mode": mode, "detail": detail, "seconds": round(time.time() - t_start, 1)})
        say(name, len(log))

    prof = D.profile(lab)
    base = D.pick_columns(lab)
    main = prof["tables"][0] if prof["tables"] else {}
    names = ", ".join(sorted({f["file"] for f in lab.files}))
    ds_name = (meta.get("dataset") or nice_name(sorted({f["file"] for f in lab.files}))).strip()
    now = datetime.now()
    rep = Report(title=(meta.get("title") or f"Data Analysis Report: {ds_name}").strip()[:140],
                 subtitle=(meta.get("subtitle") or "Automated Analytical Report").strip()[:100], organisation=(meta.get("organisation") or "").strip()[:100],
                 prepared_for=(meta.get("prepared_for") or "").strip()[:100], prepared_by=(meta.get("prepared_by") or "AskShelf Data Lab").strip()[:100],
                 date=now.strftime("%-d %B %Y"), dataset_name=ds_name, profile=prof, references=REFERENCES,
                 model=(f"Google Gemini ({backend.active or backend.models[0]})" if backend is not None else "Built-in rule engine (no AI key)"))
    step("Profile the data", f"{prof['rows_total']:,} rows in {len(prof['tables'])} table(s)")

    # ---------------- plan
    plan, how = [], "rules"
    if backend is not None:
        try:
            raw = backend.complete(PLAN_REPORT_SYSTEM, f"SCHEMA:\n{D.schema_text(lab)}", json_mode=True, temperature=0.1)
            ps = (_json_loads(raw) or {}).get("sections") or []
            plan = [{"title": str(s["title"])[:90], "kind": s.get("kind", "ranking"), "purpose": str(s["question"])[:300], "question": str(s["question"])} for s in ps if s.get("title") and s.get("question")][:6]
            how = "ai" if plan else "rules"
        except Exception as e:      # noqa: BLE001
            rep.ai_notes.append(f"The AI could not plan the report structure ({type(e).__name__}); a rule-based structure was used.")
    if not plan:
        plan = rule_sections(lab)
    if not plan:
        raise ValueError("There is no numeric column to analyse in this data, so a report cannot be built.")
    step("Plan the report structure", f"{len(plan)} analysis sections", how)

    # ---------------- run each section (AI work in parallel; charts rendered afterwards on the main thread)
    fallback = {s["kind"]: s for s in rule_sections(lab)}

    def work(i_s):
        i, s = i_s
        sql, df, author, note = s.get("sql"), None, "rules", []
        if backend is not None and s.get("question"):
            try:
                pl = plan_with_ai(backend, lab, s["question"], [])
                sql = pl.get("sql") or sql
                for attempt in range(2):
                    try:
                        df = D.run_select(lab, sql)
                        author = "ai"
                        break
                    except (ValueError, D.UnsafeSQL) as e:
                        if attempt == 0:
                            pl = plan_with_ai(backend, lab, s["question"], [], str(e), sql)
                            sql = pl.get("sql") or sql
                        else:
                            note.append(f"AI query rejected ({str(e)[:80]}); rule-based query used")
            except Exception as e:      # noqa: BLE001
                note.append(f"AI planning unavailable ({type(e).__name__}); rule-based query used")
        if df is None and sql:
            try:
                df = D.run_select(lab, sql)
            except (ValueError, D.UnsafeSQL):
                df = None
        if df is None or df.empty or (s["kind"] not in ("distribution",) and len(df) < 2):
            fb = fallback.get(s["kind"]) or next(iter(fallback.values()), None)
            if fb:
                sql, df, author = fb["sql"], D.run_select(lab, fb["sql"]), "rules"
        if df is None or df.empty:
            return None
        purpose = s["purpose"] if not s.get("question") else s["question"]
        nar, why = None, None
        if backend is not None:
            try:
                nar, why = ai_narrative(backend, df, s["title"], purpose, s["kind"])
            except Exception as e:      # noqa: BLE001
                why = f"narrative unavailable ({type(e).__name__})"
        if nar is None:
            if why:
                note.append(f"Narrative written by rules: {why}")
            nar = rule_narrative(df, s["kind"], s["title"], purpose)
        else:
            author = "ai"
        return {"i": i, "s": s, "sql": sql, "df": df, "nar": nar, "author": author, "notes": note}

    with ThreadPoolExecutor(max_workers=3 if backend is not None else 1) as ex:
        done = [r for r in ex.map(work, enumerate(plan)) if r]
    step("Run the analyses and draft the narrative", f"{len(done)} sections completed", "ai" if backend is not None else "rules")

    fig_n = tab_n = 0
    for k, r in enumerate(sorted(done, key=lambda x: x["i"]), 1):
        df, s = r["df"], r["s"]
        spec = D.chart_spec(df, None)
        kind_hint = {"distribution": {"type": "hist", "x": df.columns[0], "y": None}, "relationship": {"type": "scatter", "x": df.columns[0], "y": df.columns[1] if len(df.columns) > 1 else df.columns[0]}}.get(s["kind"])
        if kind_hint and len(df.columns) >= (2 if s["kind"] == "relationship" else 1):
            spec = {**kind_hint, "color": None}
        sec = Section(number=str(k), title=s["title"], purpose=s["purpose"], paragraphs=r["nar"]["paragraphs"], bullets=r["nar"].get("bullets", []), key_finding=r["nar"].get("key_finding", ""),
                      sql=D.clean_sql(r["sql"] or ""), full_df=df, kind=s["kind"], author=r["author"], notes=r["notes"])
        if spec:
            fig_n += 1
            sec.figure = Figure(fig_n, f"{s['title']}", D.mpl_png(df, spec), f"Analysis of uploaded data ({ds_name}); query Q{k} in Appendix B")
            rep.figures.append(sec.figure)
        if s["kind"] not in ("distribution", "relationship"):
            tab_n += 1
            sec.table = TableItem(tab_n, f"{s['title']}: summary table", df.head(12), f"Analysis of uploaded data; query Q{k} in Appendix B")
            rep.tables.append(sec.table)
        rep.sections.append(sec)

    # ---------------- forecast section
    tc, m = base["time"], base["measure"]
    if tc and m:
        try:
            fr = D.forecast_series(base["df"], tc, m, 6)
            hist, fc = fr["history"], fr["forecast"]
            spec = {"type": "line", "x": "ds", "y": "y", "color": None}
            png = D.mpl_png(hist, spec, xlabel=fr["unit"].capitalize(), ylabel=m.replace("_", " "), forecast=fc)
            k = len(rep.sections) + 1
            fig_n += 1
            tab_n += 1
            tbl = fc.rename(columns={"ds": fr["unit"].capitalize(), "yhat": "Forecast", "lo": "Low (80%)", "hi": "High (80%)"})
            tbl[fr["unit"].capitalize()] = tbl[fr["unit"].capitalize()].dt.strftime("%b %Y")
            ch = fr["change_pct"]
            paras = [f"Future {m.replace('_', ' ')} was projected {fr['periods']} {fr['unit']}s ahead using {fr['method']} fitted to {fr['n_points']} {fr['unit']}s of history. "
                     f"The model expects a total of {fmt_num(fc['yhat'].sum())} over the horizon" + (f", {ch:+.1f}% relative to the preceding {fr['periods']} {fr['unit']}s." if ch is not None else "."),
                     (f"A hold-out back-test on the most recent {max(2, min(6, fr['n_points'] // 4))} {fr['unit']}s produced a mean absolute percentage error of {fr['mape']}%, which should be read as the typical size of forecast error. " if fr["mape"] is not None else "")
                     + "The shaded band shows an 80% prediction range; it indicates uncertainty and is not a guarantee of future results."]
            sec = Section(number=str(k), title=f"Forecast of {m.replace('_', ' ').title()}", purpose="Project the main measure forward to support planning.", paragraphs=paras,
                          key_finding=f"Expected {m.replace('_', ' ')} over the next {fr['periods']} {fr['unit']}s is {fmt_num(fc['yhat'].sum())}" + (f" ({ch:+.1f}% versus the previous {fr['periods']} {fr['unit']}s)." if ch is not None else "."),
                          sql=None, full_df=tbl, kind="forecast", author="rules", notes=[f"Method: {fr['method']}"])
            sec.figure = Figure(fig_n, f"Actual and forecast {m.replace('_', ' ')} by {fr['unit']}", png, f"Prophet forecast on uploaded data ({ds_name})")
            sec.table = TableItem(tab_n, f"Forecast of {m.replace('_', ' ')} with 80% range", tbl, "Prophet forecast; ranges are prediction intervals")
            rep.figures.append(sec.figure)
            rep.tables.append(sec.table)
            rep.sections.append(sec)
            rep.forecast_table = tbl
            step("Forecast the main measure", f"{fr['method']}; back-test MAPE {fr['mape']}%")
        except Exception as e:      # noqa: BLE001
            rep.ai_notes.append(f"The forecast section was skipped: {str(e)[:120]}")

    # ---------------- front/back matter
    findings = "\n".join(f"{s.number}. {s.title}: {s.key_finding}" for s in rep.sections)
    ai_done = False
    if backend is not None:
        try:
            raw = backend.complete(SUMMARY_SYSTEM, f"DATASET: {ds_name}; {prof['rows_total']:,} rows; tables: {', '.join(t['name'] for t in prof['tables'])}\nFINDINGS:\n{findings}", json_mode=True, temperature=0.2)
            d = _json_loads(raw) or {}
            joined = " ".join(map(str, d.get("executive_summary", []))) + " ".join(map(str, d.get("conclusions", []))) + " ".join(str(r) for r in d.get("recommendations", []))
            if d.get("executive_summary") and d.get("recommendations") and supported(joined, findings + "\n" + stats_text(main_df(base), 5), [p.get("rows_total", 0) for p in [prof]]):
                clean = lambda t: tidy_numbers(G.sanitize_output(str(t))[0])
                rep.exec_summary = [clean(x) for x in d["executive_summary"]][:3]
                rep.conclusions = [clean(x) for x in d.get("conclusions", [])][:6]
                rep.recommendations = [{"title": clean(r.get("title", "")), "detail": clean(r.get("detail", ""))} for r in d["recommendations"]][:5]
                rep.limitations = [clean(x) for x in d.get("limitations", [])][:3]
                ai_done = True
            else:
                rep.ai_notes.append("The AI-written summary cited figures not found in the analysis, so a rule-written summary was used.")
        except Exception as e:      # noqa: BLE001
            rep.ai_notes.append(f"The AI could not write the summary ({type(e).__name__}); a rule-written summary was used.")
    if not ai_done:
        rep.exec_summary = rule_summary(rep, prof, main)
        rep.conclusions = [f"{s.key_finding}" for s in rep.sections if s.key_finding][:5]
        rep.recommendations = rule_recommendations(rep)
    step("Write the executive summary and recommendations", "", "ai" if ai_done else "rules")

    mc = next((c for c in main.get("columns", []) if c["name"] == tc), None) if tc else None
    period = f"{mc['min']} to {mc['max']}" if mc else None
    rep.intro = [f"This report presents an automated analysis of the dataset '{ds_name}', comprising {prof['rows_total']:,} records across {len(prof['tables'])} table(s)" + (f" and spanning {period}" if period else "") + ". "
                 "Its purpose is to describe the data, identify the main patterns that matter for decision-making, project the principal measure forward, and recommend actions.",
                 f"The report is organised as follows. Section 2 describes the data and the methods used. Sections 3 to {len(rep.sections) + 2} present the analyses, each with a figure, a summary table and an interpretation. "
                 f"Section {len(rep.sections) + 3} states the conclusions and recommendations, and Section {len(rep.sections) + 4} sets out the limitations of the work."]
    rep.sources = pd.DataFrame([{"File": f["file"], "Sheet": f["sheet"] or "-", "Rows": f"{f['rows']:,}", "Columns": f["columns"]} for f in lab.files])
    rep.prep_steps = ["Column names were standardised to lower-case snake_case and empty rows and columns were removed.",
                      "Text columns that contained numbers (including currency symbols and thousands separators) were converted to numeric type, and date-like text was converted to dates.",
                      lab.fusion_note or "Each uploaded file or sheet was loaded as its own table."]
    if lab.dropped:
        rep.prep_steps.append("Personal or sensitive columns were removed before any analysis: " + "; ".join(f"{d['column']} ({d['file']})" for d in lab.dropped[:8]) + ". No personal data was analysed or sent to any AI service.")
    else:
        rep.prep_steps.append("The privacy screen found no personal or sensitive columns in the uploaded files.")
    rep.quality = pd.DataFrame([{"Table": t["name"], "Rows": f"{t['rows']:,}", "Columns": len(t["columns"]), "Missing cells": f"{t['missing_cells_pct']}%", "Duplicate rows": f"{t['duplicates']:,}",
                                 "Date column": t["time_column"] or "-"} for t in prof["tables"]])
    rep.approach = ["Descriptive analysis was carried out with read-only SQL queries (SQLite) validated to contain a single SELECT statement, with a row limit and a time limit.",
                    "Charts and summary tables were produced directly from query results; no figure in this report was typed or estimated by the language model.",
                    "The forecast used Prophet, an additive time-series model with trend and seasonal components, and was evaluated by hold-out back-testing." if rep.forecast_table is not None else "No time column was available, so no forecast was produced.",
                    ("An AI model (" + rep.model + ") proposed the analysis plan, drafted the SQL and wrote the narrative; each narrative was checked against the query results before inclusion." if backend is not None
                     else "The analysis plan, SQL and narrative were produced by the built-in rule engine; no AI service was used.")]
    cols = []
    for t in prof["tables"]:
        for c in t["columns"]:
            rng = f"{c.get('min')} to {c.get('max')}" if c["kind"] in ("numeric", "datetime") and c.get("min") is not None else (", ".join(x[0] for x in c.get("top", [])[:3]) if c.get("top") else "")
            cols.append({"Table": t["name"], "Column": c["name"], "Type": c["kind"], "Missing": f"{c['missing_pct']}%", "Distinct": c["unique"], "Range or examples": rng[:48]})
    rep.data_dict = pd.DataFrame(cols)
    miss = max((t["missing_cells_pct"] for t in prof["tables"]), default=0)
    rep.limitations = rep.limitations + [
        f"The analysis is descriptive and depends on the quality of the uploaded data; up to {miss}% of cells were missing in the most incomplete table." if miss else "The analysis is descriptive and depends on the quality of the uploaded data.",
        "Associations reported here do not establish cause and effect.",
        "Forecasts extrapolate historical patterns and cannot anticipate events outside the history; they are shown with ranges and a back-test error.",
        "Columns identified as personal data were removed, so any analysis that would require them could not be performed.",
        "Automatically generated analysis should be reviewed by a person who knows the business before it is used for decisions."]
    n = 2
    for sec in rep.sections:
        if sec.table:
            n += 1
            sec.table.id = n
    rep.num = {"sources": 1, "quality": 2, "ailog": n + 1, "authors": n + 2, "dict": n + 3}
    rep.ai_log = log
    step("Assemble the report", f"{len(rep.sections)} sections, {len(rep.figures)} figures, {len(rep.tables)} tables")
    rep.ai_log = log
    return rep


def main_df(base: dict) -> pd.DataFrame:
    return base["df"].head(5)


def rule_summary(rep: Report, prof: dict, main: dict) -> list[str]:
    lead = [s.key_finding for s in rep.sections if s.key_finding][:3]
    return [f"This report analyses '{rep.dataset_name}' ({prof['rows_total']:,} records). It examines {', '.join(s.title.lower() for s in rep.sections[:4])}" + (" and projects the main measure forward." if rep.forecast_table is not None else "."),
            "The main findings are as follows. " + " ".join(lead)]


def rule_recommendations(rep: Report) -> list[dict]:
    recs = []
    for s in rep.sections:
        if s.kind == "ranking" and len(recs) < 4:
            recs.append({"title": "Concentrate effort on the leading performers", "detail": f"{s.key_finding} Protect these relationships and investigate what the leaders do differently."})
        elif s.kind == "trend":
            recs.append({"title": "Plan around peak and low periods", "detail": f"{s.key_finding} Align staffing, stock and cash flow to the seasonal pattern."})
        elif s.kind == "forecast":
            recs.append({"title": "Use the forecast range for planning", "detail": f"{s.key_finding} Plan to the range rather than a single figure."})
        elif s.kind == "breakdown" and not any("segments" in r["title"] for r in recs):
            recs.append({"title": "Review under-performing segments", "detail": f"{s.key_finding} Examine the lowest-ranked segments for pricing, service or supply problems."})
    if len(recs) < 3:
        recs.append({"title": "Extend the data", "detail": "Add more history and related fields (costs, delivery dates) so that future analyses can explain drivers rather than describe patterns."})
    return recs[:5]
