"""Data Lab: bring-your-own-data analysis.

Users upload Excel / CSV files (several can be fused into one table). Files are cleaned, screened for personal data, loaded into an
in-memory SQLite database (no database server needed) and profiled. Questions are answered either by an AI-written SELECT query
(validated read-only, row- and time-limited) or by Prophet forecasting.

Everything here is deterministic except the SQL text itself, which comes from the language model (or a rule-based fallback when
there is no AI key) and is *always* run through `run_select`, which refuses anything that is not a single read-only SELECT.
"""
from __future__ import annotations

import io
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MAX_UPLOAD_MB = 25
MAX_ROWS_PER_FILE = 500_000
MAX_RESULT_ROWS = 5_000
SQL_TIMEOUT_S = 6.0
ALLOWED_EXT = (".csv", ".tsv", ".xlsx", ".xls")

NAVY = "#1f3864"
PALETTE = ["#1f3864", "#2a78d6", "#1baf7a", "#eda100", "#e87ba4", "#7a5af8", "#eb6834", "#64748b"]

# --------------------------------------------------------------------------------------------- privacy screening of uploads
_PII_NAME = re.compile(r"(^|_)(e_?mail|phone|mobile|tel(ephone)?|ssn|tfn|passport|licen[cs]e|dob|birth|address|street|postcode|zip|"
                       r"first_?name|last_?name|full_?name|surname|customer_?name|client_?name|employee_?(id|name)|staff_?(id|name)|"
                       r"salary|wage|pay(roll)?|password|card_?(no|number)|iban|account_?(no|number))($|_)")
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{8,16}$")


def pii_reason(name: str, s: pd.Series) -> str | None:
    if _PII_NAME.search(name):
        return "column name suggests personal or sensitive data"
    sample = s.dropna().astype(str).head(200)
    if len(sample) >= 5:
        if (sample.map(lambda v: bool(_EMAIL.match(v.strip())))).mean() > 0.5:
            return "values look like email addresses"
        if (s.dtype == object or pd.api.types.is_string_dtype(s)) and (sample.map(lambda v: bool(_PHONE.match(v.strip())) and sum(c.isdigit() for c in v) >= 8)).mean() > 0.6:
            return "values look like phone numbers"
    return None


# --------------------------------------------------------------------------------------------- cleaning
def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower()
    s = re.sub(r"_+", "_", s) or "col"
    return ("c_" + s) if s[0].isdigit() else s


def unique_names(names: list[str]) -> list[str]:
    seen, out = {}, []
    for n in names:
        k = snake(n)
        seen[k] = seen.get(k, 0) + 1
        out.append(k if seen[k] == 1 else f"{k}_{seen[k]}")
    return out


_MONEY = re.compile(r"[\s$,£€%]")


def clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all").copy()
    df.columns = unique_names(list(df.columns))
    for c in df.columns:
        s = df[c]
        if s.dtype == object or pd.api.types.is_string_dtype(s):
            st = s.astype(str).str.strip().replace({"": np.nan, "nan": np.nan, "None": np.nan, "NaT": np.nan})
            num = pd.to_numeric(st.str.replace(_MONEY, "", regex=True), errors="coerce")
            nn = st.notna().sum()
            if nn and num.notna().sum() / nn >= 0.9 and not re.search(r"(date|time|day|month|year|id$|code|zip|post)", c):
                df[c] = num
                continue
            if nn and (re.search(r"(date|time|_dt$|^dt_|created|updated|month|period)", c) or nn > 5):
                try:
                    dt = pd.to_datetime(st, errors="coerce", format="mixed", dayfirst=False)
                except (TypeError, ValueError):
                    dt = pd.to_datetime(st, errors="coerce")
                if dt.notna().sum() / nn >= 0.85 and st.str.contains(r"\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}|[A-Za-z]{3,9}\s+\d{4}", regex=True, na=False).mean() > 0.8:
                    df[c] = dt
                    continue
            df[c] = st
    return df


def kind_of(s: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_numeric_dtype(s):
        return "numeric" if s.nunique(dropna=True) > 2 or s.dropna().isin([0, 1]).mean() < 1 else "boolean"
    n, u = len(s.dropna()), s.nunique(dropna=True)
    return "categorical" if u <= max(50, 0.2 * n) else "text"


# --------------------------------------------------------------------------------------------- session store
@dataclass
class Lab:
    session: str
    frames: list[dict] = field(default_factory=list)          # raw cleaned frames: {file, sheet, df}
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    conn: sqlite3.Connection | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    fusion_note: str = ""
    created: float = field(default_factory=time.time)
    last_result: dict | None = None
    qa_log: list[dict] = field(default_factory=list)
    cache: dict = field(default_factory=dict)

    # ---------------- ingest
    def add_file(self, filename: str, content: bytes) -> dict:
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        if ext not in ALLOWED_EXT:
            raise ValueError(f"'{filename}' is not supported. Upload .xlsx, .xls or .csv files.")
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"'{filename}' is larger than {MAX_UPLOAD_MB} MB.")
        if ext in (".csv", ".tsv"):
            raw = {"": _read_csv(content, "\t" if ext == ".tsv" else None)}
        else:
            raw = pd.read_excel(io.BytesIO(content), sheet_name=None, engine="openpyxl" if ext == ".xlsx" else "xlrd")
        added = []
        for sheet, df in raw.items():
            if df is None or df.empty:
                continue
            if len(df) > MAX_ROWS_PER_FILE:
                df = df.head(MAX_ROWS_PER_FILE)
            df = clean_frame(df)
            if df.empty or len(df.columns) < 1:
                continue
            keep = []
            for c in df.columns:
                why = pii_reason(c, df[c])
                if why:
                    self.dropped.append({"file": filename, "sheet": sheet, "column": c, "reason": why})
                else:
                    keep.append(c)
            df = df[keep]
            if df.empty:
                continue
            self.frames.append({"file": filename, "sheet": sheet, "df": df})
            self.files.append({"file": filename, "sheet": sheet or "", "rows": int(len(df)), "columns": int(len(df.columns))})
            added.append(sheet or filename)
        if not added:
            raise ValueError(f"'{filename}' has no usable data (empty, or every column was personal data).")
        self.rebuild()
        return {"file": filename, "sheets": added}

    def rebuild(self) -> None:
        """Fuse frames that share the same columns into one table; give every other frame its own table. Load into SQLite."""
        groups: dict[tuple, list[dict]] = {}
        for f in self.frames:
            groups.setdefault(tuple(sorted(f["df"].columns)), []).append(f)
        tables, notes, used = {}, [], set()

        def tname(base: str) -> str:
            n = snake(base)[:40] or "data"
            k, i = n, 2
            while k in used:
                k, i = f"{n}_{i}", i + 1
            used.add(k)
            return k
        for sig, fs in groups.items():
            if len(fs) > 1:
                df = pd.concat([x["df"].assign(source_file=x["file"] + (f" [{x['sheet']}]" if x["sheet"] else "")) for x in fs], ignore_index=True)
                stems = {snake(x["file"].rsplit(".", 1)[0]) for x in fs}
                nm = tname(next(iter(stems)) if len(stems) == 1 else "combined_data")
                tables[nm] = df
                notes.append(f"{len(fs)} files with identical columns were combined into one table '{nm}' ({len(df):,} rows; the 'source_file' column shows where each row came from).")
            else:
                x = fs[0]
                tables[tname(x["file"].rsplit(".", 1)[0] + ("_" + x["sheet"] if x["sheet"] else ""))] = x["df"]
        self.tables, self.fusion_note = tables, " ".join(notes)
        if self.conn is not None:
            self.conn.close()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        for nm, df in tables.items():
            out = df.copy()
            for c in out.columns:
                if pd.api.types.is_datetime64_any_dtype(out[c]):
                    out[c] = out[c].dt.strftime("%Y-%m-%d")
            out.to_sql(nm, self.conn, index=False)
        self.cache.clear()

    def reset(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.frames, self.tables, self.dropped, self.files, self.conn = [], {}, [], [], None
        self.fusion_note, self.last_result, self.qa_log = "", None, []
        self.cache.clear()

    @property
    def loaded(self) -> bool:
        return bool(self.tables)


def _read_csv(content: bytes, sep: str | None) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(content), sep=sep, engine="python", encoding=enc)
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
    return pd.DataFrame()


_LABS: dict[str, Lab] = {}
_LABS_LOCK = threading.Lock()


def get_lab(session: str) -> Lab:
    with _LABS_LOCK:
        if session not in _LABS:
            if len(_LABS) >= 40:                                     # evict the oldest session
                _LABS.pop(min(_LABS, key=lambda k: _LABS[k].created))
            _LABS[session] = Lab(session)
        return _LABS[session]


# --------------------------------------------------------------------------------------------- profiling
def detect_time(df: pd.DataFrame) -> str | None:
    cands = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if not cands:
        return None
    pref = [c for c in cands if re.search(r"(order|sale|invoice|transaction|created|date)", c)]
    return (pref or cands)[0]


def profile(lab: Lab) -> dict:
    tables = []
    for nm, df in lab.tables.items():
        cols = []
        for c in df.columns:
            s, k = df[c], kind_of(df[c])
            info = {"name": c, "kind": k, "missing_pct": round(float(s.isna().mean() * 100), 1), "unique": int(s.nunique(dropna=True))}
            if k == "numeric":
                info |= {"min": _r(s.min()), "max": _r(s.max()), "mean": _r(s.mean()), "median": _r(s.median()), "sum": _r(s.sum())}
            elif k == "datetime":
                info |= {"min": str(s.min())[:10], "max": str(s.max())[:10]}
            elif k in ("categorical", "boolean"):
                info["top"] = [[str(i), int(v)] for i, v in s.value_counts().head(5).items()]
            cols.append(info)
        tc = detect_time(df)
        tables.append({"name": nm, "rows": int(len(df)), "columns": cols, "time_column": tc, "duplicates": int(df.duplicated().sum()),
                       "missing_cells_pct": round(float(df.isna().mean().mean() * 100), 1),
                       "measures": [c["name"] for c in cols if c["kind"] == "numeric"], "dimensions": [c["name"] for c in cols if c["kind"] == "categorical"],
                       "sample": _records(df.head(8))})
    seen: dict[str, dict] = {}
    for d in lab.dropped:                                    # one entry per column, even if several files had it
        e = seen.setdefault(d["column"], {"column": d["column"], "reason": d["reason"], "files": []})
        e["files"].append(d["file"])
    return {"loaded": lab.loaded, "files": lab.files, "tables": tables, "dropped": list(seen.values()), "fusion_note": lab.fusion_note,
            "rows_total": int(sum(len(d) for d in lab.tables.values()))}


def _r(v):
    try:
        v = float(v)
        return None if np.isnan(v) else round(v, 2)
    except (TypeError, ValueError):
        return None


def _records(df: pd.DataFrame) -> dict:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(out.notna(), None)
    return {"columns": list(out.columns), "data": [[(round(v, 2) if isinstance(v, float) else v) for v in row] for row in out.values.tolist()]}


def schema_text(lab: Lab, max_cols: int = 60) -> str:
    """Compact schema + example values for the SQL-writing prompt."""
    lines = []
    for nm, df in lab.tables.items():
        lines.append(f"TABLE {nm} ({len(df):,} rows)")
        for c in list(df.columns)[:max_cols]:
            k = kind_of(df[c])
            ex = ""
            if k in ("categorical", "boolean"):
                ex = " e.g. " + ", ".join(repr(str(v)) for v in df[c].dropna().unique()[:5])
            elif k == "datetime":
                ex = f" range {str(df[c].min())[:10]} to {str(df[c].max())[:10]} (stored as TEXT 'YYYY-MM-DD')"
            elif k == "numeric":
                ex = f" range {_r(df[c].min())} to {_r(df[c].max())}"
            lines.append(f"  - {c} [{k}]{ex}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- safe SQL execution
_FORBIDDEN = re.compile(r"\b(sqlite_\w*|insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex|truncate|load_extension|begin|commit|rollback|savepoint|release)\b", re.I)


class UnsafeSQL(ValueError):
    pass


def clean_sql(sql: str) -> str:
    s = re.sub(r"^```(?:sql)?|```$", "", sql.strip(), flags=re.M).strip()
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s).strip().rstrip(";").strip()
    return s


def validate_select(sql: str) -> str:
    s = clean_sql(sql)
    if not s:
        raise UnsafeSQL("The query is empty.")
    bare = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", s)
    if ";" in bare:
        raise UnsafeSQL("Only a single query is allowed.")
    if not re.match(r"^(select|with)\b", bare, re.I):
        raise UnsafeSQL("Only SELECT queries are allowed.")
    bad = _FORBIDDEN.search(bare)
    if bad:
        raise UnsafeSQL(f"'{bad.group(1)}' is not allowed: queries are read-only and limited to your uploaded tables.")
    return s


def run_select(lab: Lab, sql: str, max_rows: int = MAX_RESULT_ROWS) -> pd.DataFrame:
    """Validate and run a read-only query with an authoriser, a time limit and a row cap."""
    s = validate_select(sql)
    if not lab.conn:
        raise ValueError("No data loaded yet.")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
    with lab.lock:
        conn = lab.conn
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        t0 = time.time()
        conn.set_progress_handler(lambda: 1 if time.time() - t0 > SQL_TIMEOUT_S else 0, 20000)
        try:
            cur = conn.execute(s)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows)
        except sqlite3.Error as e:
            raise ValueError(f"{e}") from e
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_authorizer(None)
    df = pd.DataFrame(rows, columns=cols)
    for c in df.columns:
        if (df[c].dtype == object or pd.api.types.is_string_dtype(df[c])) and re.search(r"(date|month|day|period|week|quarter|year)", c.lower()):
            d = pd.to_datetime(df[c], errors="coerce", format="mixed")
            if d.notna().mean() > 0.9 and df[c].astype(str).str.match(r"^\d{4}-\d{2}(-\d{2})?$").mean() > 0.9:
                df[c] = d
    return df


# --------------------------------------------------------------------------------------------- rule-based planner (no AI key)
_STOP = set("a an the of in on by for to and or is are was were what which who how much many show me give tell our my total all each per from with as at it this that top best worst bottom".split())
_MEASURE_PRIORITY = [re.compile(x) for x in (r"(revenue|sales|income|turnover)", r"(amount|total|value|spend)", r"(profit|margin)", r"(price|cost)", r"(qty|quantity|units|volume)", r"(count|score)")]
_MEASURE_HINT = re.compile("|".join(p.pattern for p in _MEASURE_PRIORITY))


def _best_measure(nums: list[str]) -> str | None:
    for pat in _MEASURE_PRIORITY:
        for c in nums:
            if pat.search(c):
                return c
    return None


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", q.lower()) if w not in _STOP]


def _match(cols: list[str], words: list[str]) -> str | None:
    best, bs = None, 0
    for c in cols:
        toks = set(c.split("_"))
        sc = sum(1 for w in words if w in toks or (len(w) > 3 and any(t.startswith(w[:4]) or w.startswith(t[:4]) for t in toks if len(t) > 3)))
        if sc > bs:
            best, bs = c, sc
    return best


def pick_columns(lab: Lab, question: str = "") -> dict:
    nm, df = max(lab.tables.items(), key=lambda kv: len(kv[1]))
    prof_kinds = {c: kind_of(df[c]) for c in df.columns}
    nums = [c for c, k in prof_kinds.items() if k == "numeric" and not re.search(r"(^id$|_id$|^id_|code|zip|post|year|month)", c)]
    cats = [c for c, k in prof_kinds.items() if k == "categorical"]
    w = _words(question)
    measure = _match(nums, w) or _best_measure(nums) or (nums[0] if nums else None)
    by = re.search(r"\b(?:by|per|across|for each|each)\s+([a-z_ ]+?)(?:\s+(?:in|for|over|during|and|last|this)\b|[?.,]|$)", question.lower())
    dim = _match(cats, _words(by.group(1))) if by else None
    dim = dim or _match(cats, [x for x in w if x not in (measure or "").split("_")]) or (cats[0] if cats else None)
    return {"table": nm, "df": df, "measure": measure, "dimension": dim, "time": detect_time(df), "numerics": nums, "categoricals": cats}


def heuristic_plan(lab: Lab, question: str) -> dict:
    q = question.lower()
    p = pick_columns(lab, question)
    t, m, d, tc = p["table"], p["measure"], p["dimension"], p["time"]
    n = re.search(r"\b(?:top|best|highest|bottom|lowest|worst)\s+(\d+)", q) or re.search(r"\b(\d+)\s+(?:best|top|highest|lowest|worst)", q)
    limit = int(n.group(1)) if n else 10
    agg = "AVG" if re.search(r"\b(average|avg|mean)\b", q) else "COUNT" if re.search(r"\b(count|how many|number of)\b", q) else "SUM"
    desc = "ASC" if re.search(r"\b(bottom|lowest|worst|least|smallest)\b", q) else "DESC"
    expr = "COUNT(*)" if agg == "COUNT" or not m else f"{agg}({m})"
    alias = f"{agg.lower()}_{m}" if m and agg != "COUNT" else "records"
    if re.search(r"\b(forecast|predict|projection|project|next \d+|will be)\b", q) and tc and m:
        mo = re.search(r"(\d+)\s*(month|week|day|year|quarter)", q)
        return {"action": "forecast", "value": m, "time": tc, "group": d if re.search(r"\b(by|per|each)\b", q) and d else None, "periods": int(mo.group(1)) if mo else 6,
                "freq": FREQ_WORDS[mo.group(2)] if mo else None, "title": f"Forecast of {m.replace('_', ' ')}", "mode": "rules"}
    if tc and re.search(r"\b(trend|over time|monthly|by month|per month|each month|quarterly|by quarter|yearly|by year|growth|seasonal)", q):
        fmt = "%Y" if re.search(r"year", q) else "%Y-%m"
        label = "year" if fmt == "%Y" else "month"
        return {"action": "sql", "sql": f"SELECT strftime('{fmt}', {tc}) AS {label}, {expr} AS {alias} FROM {t} WHERE {tc} IS NOT NULL GROUP BY 1 ORDER BY 1", "title": f"{alias.replace('_', ' ').title()} by {label}", "mode": "rules"}
    if d:
        return {"action": "sql", "sql": f"SELECT {d}, {expr} AS {alias} FROM {t} WHERE {d} IS NOT NULL GROUP BY {d} ORDER BY {alias} {desc} LIMIT {limit}", "title": f"{alias.replace('_', ' ').title()} by {d.replace('_', ' ')}", "mode": "rules"}
    if m:
        return {"action": "sql", "sql": f"SELECT COUNT(*) AS records, SUM({m}) AS total_{m}, AVG({m}) AS average_{m}, MIN({m}) AS min_{m}, MAX({m}) AS max_{m} FROM {t}", "title": f"Summary of {m}", "mode": "rules"}
    return {"action": "sql", "sql": f"SELECT * FROM {t} LIMIT 20", "title": "Data sample", "mode": "rules"}


# --------------------------------------------------------------------------------------------- charts
def chart_spec(df: pd.DataFrame, hint: dict | None = None) -> dict | None:
    if df is None or df.empty or len(df.columns) == 0:
        return None
    kinds = {c: kind_of(df[c]) if len(df) > 1 else ("numeric" if pd.api.types.is_numeric_dtype(df[c]) else "categorical") for c in df.columns}
    time = next((c for c in df.columns if kinds[c] == "datetime"), None)
    nums = [c for c in df.columns if kinds[c] == "numeric"]
    cats = [c for c in df.columns if kinds[c] in ("categorical", "text", "boolean") and df[c].nunique() <= 40]
    if hint and hint.get("x") in df.columns and hint.get("y") in df.columns and hint.get("type") in ("bar", "line", "scatter", "hist"):
        return {"type": hint["type"], "x": hint["x"], "y": hint["y"], "color": hint.get("color") if hint.get("color") in df.columns else None}
    if len(df) == 1:
        return None
    if time and nums:
        col = next((c for c in cats if c != time), None) if len(nums) == 1 and len(df.columns) >= 3 else None
        return {"type": "line", "x": time, "y": nums[0], "color": col}
    if cats and nums:
        return {"type": "bar", "x": cats[0], "y": nums[0], "color": None}
    if len(nums) >= 2:
        return {"type": "scatter", "x": nums[0], "y": nums[1], "color": None}
    if len(nums) == 1 and len(df) > 5:
        return {"type": "hist", "x": nums[0], "y": None, "color": None}
    return None


def _prep_bar(df: pd.DataFrame, spec: dict, limit: int = 20) -> pd.DataFrame:
    d = df[[spec["x"], spec["y"]]].dropna().drop_duplicates().copy()      # identical repeated rows are not extra data
    d[spec["x"]] = d[spec["x"]].astype(str)
    d = d.groupby(spec["x"], as_index=False, sort=False)[spec["y"]].sum()
    return d.sort_values(spec["y"], ascending=False).head(limit) if len(d) > 8 else d


def plotly_fig(df: pd.DataFrame, spec: dict, title: str = ""):
    import plotly.graph_objects as go
    fig = go.Figure()
    x, y = spec["x"], spec.get("y")
    if spec["type"] == "bar":
        d = _prep_bar(df, spec)
        horizontal = len(d) > 6 or d[x].str.len().max() > 12
        if horizontal:
            d = d.iloc[::-1]
            fig.add_trace(go.Bar(y=d[x], x=d[y], orientation="h", marker=dict(color=PALETTE[1], cornerradius=4), hovertemplate="%{y}: %{x:,.2f}<extra></extra>"))
        else:
            fig.add_trace(go.Bar(x=d[x], y=d[y], marker=dict(color=PALETTE[1], cornerradius=4), hovertemplate="%{x}: %{y:,.2f}<extra></extra>"))
    elif spec["type"] == "line":
        if spec.get("color"):
            for i, (g, d) in enumerate(df.groupby(spec["color"])):
                d = d.sort_values(x)
                fig.add_trace(go.Scatter(x=d[x], y=d[y], mode="lines+markers", name=str(g), line=dict(color=PALETTE[i % len(PALETTE)], width=2.4)))
        else:
            d = df.sort_values(x)
            fig.add_trace(go.Scatter(x=d[x], y=d[y], mode="lines+markers", line=dict(color=PALETTE[1], width=2.6), marker=dict(size=6), fill="tozeroy", fillcolor="rgba(42,120,214,.10)"))
    elif spec["type"] == "scatter":
        d = df[[x, y]].dropna().head(3000)
        fig.add_trace(go.Scatter(x=d[x], y=d[y], mode="markers", marker=dict(color=PALETTE[1], size=7, opacity=.6)))
    else:
        fig.add_trace(go.Histogram(x=df[x].dropna(), marker=dict(color=PALETTE[1]), nbinsx=30))
    fig.update_layout(title=dict(text=title, x=0, font=dict(size=15)), height=380, margin=dict(l=8, r=16, t=56, b=8), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, system-ui, sans-serif", size=12, color="#52514e"), showlegend=bool(spec.get("color")), bargap=0.3,
                      xaxis=dict(showgrid=False, automargin=True, title=x.replace("_", " ") if spec["type"] != "bar" else None), yaxis=dict(gridcolor="#e1e0d9", automargin=True))
    return fig


def mpl_png(df: pd.DataFrame, spec: dict, xlabel: str | None = None, ylabel: str | None = None, size=(7.2, 3.6), forecast: pd.DataFrame | None = None) -> bytes:
    """Publication-style static chart for the PDF / Word reports."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})
    fig, ax = plt.subplots(figsize=size, dpi=200)
    x, y = spec["x"], spec.get("y")
    fmt = FuncFormatter(lambda v, _: f"{v / 1e9:.1f}B" if abs(v) >= 1e9 else f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:.0f}K" if abs(v) >= 1e4 else f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}".rstrip("0").rstrip("."))
    if spec["type"] == "bar":
        d = _prep_bar(df, spec, 15)
        if len(d) > 6 or d[x].str.len().max() > 12:
            d = d.iloc[::-1]
            bars = ax.barh(d[x].str.slice(0, 32), d[y], color=NAVY)
            ax.xaxis.set_major_formatter(fmt)
            for b, v in zip(bars, d[y]):
                ax.text(b.get_width(), b.get_y() + b.get_height() / 2, " " + fmt(v, 0), va="center", fontsize=7.5, color="#333")
            ax.set_xlabel(ylabel or y.replace("_", " ")); ax.set_ylabel("")
        else:
            bars = ax.bar(d[x], d[y], color=NAVY, width=.6)
            ax.yaxis.set_major_formatter(fmt)
            for b, v in zip(bars, d[y]):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height(), fmt(v, 0), ha="center", va="bottom", fontsize=7.5, color="#333")
            ax.set_xlabel(xlabel or x.replace("_", " ")); ax.set_ylabel(ylabel or y.replace("_", " "))
    elif spec["type"] == "line":
        if spec.get("color"):
            for i, (g, d) in enumerate(df.groupby(spec["color"])):
                d = d.sort_values(x)
                ax.plot(d[x], d[y], marker="o", ms=3, lw=1.8, color=PALETTE[i % len(PALETTE)], label=str(g))
            ax.legend(frameon=False, fontsize=8, ncol=3)
        else:
            d = df.sort_values(x)
            ax.plot(d[x], d[y], marker="o", ms=3.5, lw=2, color=NAVY, label="Actual")
            ax.fill_between(d[x], d[y], alpha=.08, color=NAVY)
        if forecast is not None and len(forecast):
            ax.plot(forecast["ds"], forecast["yhat"], lw=2, ls="--", color="#d97706", label="Forecast")
            ax.fill_between(forecast["ds"], forecast["lo"], forecast["hi"], color="#d97706", alpha=.15, label="80% range")
            ax.legend(frameon=False, fontsize=8)
        ax.yaxis.set_major_formatter(fmt)
        ax.set_xlabel(xlabel or x.replace("_", " ")); ax.set_ylabel(ylabel or y.replace("_", " "))
        fig.autofmt_xdate()
    elif spec["type"] == "scatter":
        d = df[[x, y]].dropna().head(3000)
        ax.scatter(d[x], d[y], s=10, alpha=.55, color=NAVY)
        ax.xaxis.set_major_formatter(fmt); ax.yaxis.set_major_formatter(fmt)
        ax.set_xlabel(xlabel or x.replace("_", " ")); ax.set_ylabel(ylabel or y.replace("_", " "))
    else:
        ax.hist(df[x].dropna(), bins=30, color=NAVY, edgecolor="white")
        ax.xaxis.set_major_formatter(fmt)
        ax.set_xlabel(xlabel or x.replace("_", " ")); ax.set_ylabel("Frequency")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.spines["left"].set_color("#bbb"); ax.spines["bottom"].set_color("#bbb")
    ax.grid(axis="y" if spec["type"] != "bar" or ax.get_xlabel() != "" else "x", color="#e6e6e6", lw=.7)
    ax.set_axisbelow(True)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------- forecasting (Prophet, with a plain fallback)
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)


FREQ_WORDS = {"day": "D", "week": "W", "month": "MS", "quarter": "QS", "year": "YS"}
FREQ_NAME = {"D": "day", "W": "week", "MS": "month", "QS": "quarter", "YS": "year"}


def _series(df: pd.DataFrame, time_col: str, value_col: str, freq: str | None = None) -> tuple[pd.Series, str]:
    d = df[[time_col, value_col]].dropna().copy()
    d[time_col] = pd.to_datetime(d[time_col])
    if d.empty:
        return pd.Series(dtype=float), "MS"
    span = (d[time_col].max() - d[time_col].min()).days
    if not freq:
        freq = "MS" if span >= 240 else "W" if span >= 70 else "D"
    s = d.set_index(time_col)[value_col].astype(float).resample(freq).sum()
    last = d[time_col].max()
    if freq == "MS" and len(s) > 12 and last.day < 0.8 * last.days_in_month:
        s = s.iloc[:-1]                                   # last month is clearly incomplete: do not let it drag the trend down
    return s, freq


def forecast_series(df: pd.DataFrame, time_col: str, value_col: str, periods: int = 6, freq: str | None = None) -> dict:
    s, freq = _series(df, time_col, value_col, freq)
    n = len(s)
    if n < 8:
        raise ValueError(f"Need at least 8 time periods to forecast; this data has {n} {FREQ_NAME.get(freq, 'period')}s. Try a shorter period (for example weeks).")
    periods = int(max(1, min(periods, 24)))
    hist = pd.DataFrame({"ds": s.index, "y": s.values})

    def fit_predict(train: pd.DataFrame, h: int):
        from prophet import Prophet
        logging.getLogger("cmdstanpy").disabled = True
        logging.getLogger("prophet").disabled = True
        m = Prophet(yearly_seasonality=(len(train) >= 24 and freq == "MS") or (freq in ("D", "W") and len(train) >= 400),
                    weekly_seasonality=(freq == "D" and len(train) >= 28), daily_seasonality=False, interval_width=0.8)
        m.fit(train)
        f = m.predict(m.make_future_dataframe(h, freq=freq))
        return f.tail(h)[["ds", "yhat", "yhat_lower", "yhat_upper"]].rename(columns={"yhat_lower": "lo", "yhat_upper": "hi"})

    k = max(2, min(6, n // 4))
    method = "Prophet (additive trend + seasonality)"
    try:
        fc = fit_predict(hist, periods)
        bt = fit_predict(hist.iloc[:-k], k)
        act = hist.tail(k)["y"].values
        mape = float(np.mean(np.abs((act - bt["yhat"].values) / np.where(act == 0, np.nan, act))) * 100) if (act != 0).any() else float("nan")
    except Exception:       # noqa: BLE001 - Prophet missing or failed: fall back to a linear trend so the feature still works
        method = "Linear trend with residual range (Prophet unavailable)"
        x = np.arange(n)
        a, b = np.polyfit(x, s.values, 1)
        sd = float((s.values - (a * x + b)).std()) or 1.0
        fx = np.arange(n, n + periods)
        idx = pd.date_range(s.index[-1], periods=periods + 1, freq=freq)[1:]
        fc = pd.DataFrame({"ds": idx, "yhat": a * fx + b, "lo": a * fx + b - 1.28 * sd, "hi": a * fx + b + 1.28 * sd})
        a2, b2 = np.polyfit(x[:-k], s.values[:-k], 1)
        act = s.values[-k:]
        mape = float(np.mean(np.abs((act - (a2 * x[-k:] + b2)) / np.where(act == 0, np.nan, act))) * 100)
    fc[["yhat", "lo", "hi"]] = fc[["yhat", "lo", "hi"]].clip(lower=0 if (s >= 0).all() else None)
    first, last = float(s.tail(min(periods, n)).sum()), float(fc["yhat"].sum())
    return {"history": hist, "forecast": fc.reset_index(drop=True), "mape": None if np.isnan(mape) else round(mape, 1), "method": method, "freq": freq, "unit": FREQ_NAME.get(freq, "period"),
            "n_points": n, "periods": periods, "change_pct": None if first == 0 else round((last / first - 1) * 100, 1)}


def forecast_groups(df: pd.DataFrame, time_col: str, value_col: str, group_col: str, periods: int = 6, top: int = 5, freq: str | None = None) -> pd.DataFrame:
    totals = df.groupby(group_col)[value_col].sum().sort_values(ascending=False).head(top).index
    rows = []
    for g in totals:
        try:
            r = forecast_series(df[df[group_col] == g], time_col, value_col, periods, freq)
        except ValueError:
            continue
        f = r["forecast"].assign(group=str(g))
        rows.append(f[["group", "ds", "yhat", "lo", "hi"]])
    if not rows:
        raise ValueError("No group has enough history to forecast (need at least 8 periods each).")
    return pd.concat(rows, ignore_index=True)
