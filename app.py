"""Modern Furniture Co. AI Analytics Agent - Streamlit web app.

Pages: Ask the Agent (chat) | Executive Dashboard | Predictive Lab | What-If Simulator | Data & Trust
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import time
import uuid

import plotly.graph_objects as go
import streamlit as st

from agent import data, feedback, guardrails, llm, predict as P, report
from agent import tools as T
from agent import tools_pro as PRO
from agent.config import APP_NAME, DEFAULT_MODELS, INK_2, MAX_QUESTIONS_PER_SESSION, MUTED, RESTRICTED_FIELDS, SURFACE
from agent.knowledge import SUGGESTED

st.set_page_config(page_title="Modern Furniture Co. | AI Analytics Agent", page_icon=":material/chair:", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
.stApp, .stMarkdown, .stButton button, .stTextInput input, .stTextArea textarea, [data-testid="stSidebar"] label,
[data-testid="stMetric"], .kpi, .hero, .insight, .action, .caution {font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;}
[data-testid="stIconMaterial"], span[class*="material"], .material-symbols-rounded, .material-icons {font-family: 'Material Symbols Rounded' !important;}
.block-container {padding-top: 2.6rem; max-width: 1240px;}
h1, h2, h3 {letter-spacing: -0.02em;}
.hero {background: linear-gradient(120deg,#0f1b2d 0%,#1c3f73 58%,#2a78d6 100%); color:#fff; padding:1.5rem 1.9rem; border-radius:18px; margin-bottom:1.1rem;
       box-shadow: 0 8px 24px rgba(15,27,45,.18);}
.hero .eyebrow {font-size:.72rem; letter-spacing:.14em; text-transform:uppercase; color:#9fc3f2; font-weight:600;}
.hero h1 {color:#fff; font-size:1.85rem; margin:.15rem 0 .25rem; font-weight:700;}
.hero p {color:#d3e2f8; margin:0; font-size:.98rem;}
.hero .meta {margin-top:.7rem; font-size:.8rem; color:#9fc3f2;}
.kpi {background:#fff; border:1px solid #e6e4dc; border-radius:14px; padding:.85rem 1rem; box-shadow:0 1px 2px rgba(16,24,40,.05); height:100%;}
.kpi .l {font-size:.68rem; text-transform:uppercase; letter-spacing:.07em; color:#6b6a65; font-weight:600;}
.kpi .v {font-size:1.55rem; font-weight:700; color:#0b0b0b; line-height:1.25;}
.kpi .d {font-size:.78rem; font-weight:600;}
.up {color:#12805c;} .down {color:#c0392b;} .flat {color:#6b6a65;}
.insight, .action, .caution {padding:.6rem .9rem; border-radius:0 10px 10px 0; margin:.35rem 0; font-size:.92rem; line-height:1.45; color:#1f1f1d;}
.insight {border-left:4px solid #2a78d6; background:#f2f7fd;}
.action  {border-left:4px solid #1baf7a; background:#eff9f4;}
.caution {border-left:4px solid #eda100; background:#fff8e8;}
.sect {font-size:.72rem; letter-spacing:.09em; text-transform:uppercase; color:#898781; margin:.9rem 0 .2rem; font-weight:700;}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
.stButton > button {text-align:left; justify-content:flex-start; border-radius: 10px; height:auto; padding:.5rem .7rem;}
.stButton > button p {white-space: normal !important; text-align:left; overflow: visible !important; text-overflow: clip !important;}
.cat {font-size:.7rem; letter-spacing:.09em; text-transform:uppercase; color:#898781; margin-bottom:.25rem; font-weight:700;}
.badge {display:inline-block; padding:.12rem .6rem; border-radius:999px; font-size:.74rem; background:#e5f1ff; color:#1c5cab; border:1px solid #b9d7fa; font-weight:600;}
.badge.off {background:#fff3d6; color:#8a5a00; border-color:#f3dcaa;}
.pillrow span {display:inline-block; margin:.15rem .3rem .15rem 0; padding:.15rem .6rem; border-radius:999px; background:#f0efe9; color:#52514e; font-size:.76rem;}
[data-testid="stMetric"] {background:#f3f2ee; border-radius:12px; padding:.7rem .9rem;}
[data-testid="stSidebar"] .stRadio label {font-weight:500;}
button[data-baseweb="tab"] {font-weight:600;}
</style>
""",
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------------------------------ configuration
def _secret(name: str, default: str | None = None) -> str | None:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:       # no secrets file present
        pass
    return os.environ.get(name, default)


@st.cache_resource(show_spinner=False)
def get_backend(api_key: str | None, models: tuple[str, ...]):
    if not api_key:
        return None
    try:
        return llm.GeminiBackend(api_key, list(models))
    except Exception:       # SDK missing or bad key format: fall back to built-in mode
        return None


@st.cache_resource(show_spinner="Loading the simulator data and training the models...")
def load_data():
    ds = data.load()
    for warm in (P.late_model, P.win_model, P.baseline, P.price_elasticity):      # warm the models once per server
        warm()
    return ds


@st.cache_resource(show_spinner=False)
def _cached_tool(name: str, args_json: str):
    return T.run_tool(name, json.loads(args_json))


def tool(name: str, **args):
    """Run a vetted tool with server-side caching (the dataset is fixed, so results never go stale)."""
    return _cached_tool(name, json.dumps(args, sort_keys=True, default=str))


ds = load_data()
API_KEY = _secret("GEMINI_API_KEY")
MODELS = tuple(m.strip() for m in (_secret("GEMINI_MODELS") or ",".join(DEFAULT_MODELS)).split(",") if m.strip())
MAX_Q = int(_secret("MAX_QUESTIONS_PER_SESSION", str(MAX_QUESTIONS_PER_SESSION)) or MAX_QUESTIONS_PER_SESSION)
backend = get_backend(API_KEY, MODELS)

ss = st.session_state
ss.setdefault("messages", [])
ss.setdefault("queued", None)
ss.setdefault("n_questions", 0)
ss.setdefault("rated", set())
PAGES = ["Ask the Agent", "Executive Dashboard", "Predictive Lab", "What-If Simulator", "Data & Trust"]
_qp = st.query_params.get("page", "")
ss.setdefault("page", next((p_ for p_ in PAGES if p_.lower().startswith(str(_qp).lower())), "Ask the Agent") if _qp else "Ask the Agent")


# ------------------------------------------------------------------------------------------ small helpers
def esc(text: str) -> str:
    """Stop Streamlit treating $...$ as LaTeX maths (all our figures contain dollar signs)."""
    return str(text).replace("$", "\\$")


def h(text: str) -> str:
    """Escape text for our HTML cards ($ becomes an entity so it can never be read as maths)."""
    return _html.escape(str(text)).replace("$", "&#36;")


def queue(q: str):
    ss.queued = q
    ss.page = "Ask the Agent"


def goto(page: str):
    ss.page = page


def history_for_llm() -> list[dict]:
    hist = []
    for m in ss.messages[-8:]:
        if m["role"] == "user":
            hist.append({"role": "user", "text": m["content"]})
        else:
            hist.append({"role": "assistant", "text": m["reply"].text[:400]})
    return hist


def hero(title: str, subtitle: str, meta: str | None = None):
    st.markdown(f"<div class='hero'><div class='eyebrow'>Modern Furniture Co.</div><h1>{h(title)}</h1><p>{h(subtitle)}</p>"
                + (f"<div class='meta'>{h(meta)}</div>" if meta else "") + "</div>", unsafe_allow_html=True)


def kpi(label: str, value: str, delta: str | None = None, good: str | None = None) -> str:
    cls = "flat"
    if delta:
        up = delta.strip().startswith("+")
        cls = ("up" if up else "down") if good != "down" else ("down" if up else "up")
    d = f"<div class='d {cls}'>{h(delta)}</div>" if delta else "<div class='d flat'>&nbsp;</div>"
    return f"<div class='kpi'><div class='l'>{h(label)}</div><div class='v'>{h(value)}</div>{d}</div>"


def kpi_row(items: list[tuple]):
    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        col.markdown(kpi(*it), unsafe_allow_html=True)


def callouts(insights=(), actions=(), cautions=()):
    if insights:
        st.markdown("<div class='sect'>What stands out</div>", unsafe_allow_html=True)
        for i in insights:
            st.markdown(f"<div class='insight'>{h(i)}</div>", unsafe_allow_html=True)
    if actions:
        st.markdown("<div class='sect'>What I would do</div>", unsafe_allow_html=True)
        for a in actions:
            st.markdown(f"<div class='action'>{h(a)}</div>", unsafe_allow_html=True)
    for c in cautions:
        st.markdown(f"<div class='caution'>{h(c)}</div>", unsafe_allow_html=True)


def show_table(df, key: str, name: str = "table", height: int | str = "auto"):
    if df is None or not len(df):
        return
    st.dataframe(df, hide_index=True, width="stretch", height=height if height != "auto" else min(38 + 35 * len(df), 420))
    st.download_button("Download CSV", df.to_csv(index=False).encode(), file_name=f"{name}.csv", mime="text/csv", key=f"dl-{key}",
                       icon=":material/download:", type="tertiary")


def chart(fig, key: str):
    if fig is not None:
        st.plotly_chart(fig, key=key, config={"displayModeBar": False}, width="stretch")


# ------------------------------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### :material/chair: Modern Furniture Co.")
    st.caption("AI Analytics Agent")
    st.radio("Navigate", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    mode_label = "Generative AI mode" if backend else "Built-in mode (no AI key)"
    st.markdown(f"<span class='badge {'' if backend else 'off'}'>{mode_label}</span>", unsafe_allow_html=True)
    if backend and getattr(backend, "active", None):
        st.caption(f"Model: {backend.active}")
    self_review = st.toggle("Self-review answers", value=True, disabled=backend is None,
                            help="A second AI pass (the 'critic') checks each draft for leaked details, wrong numbers and over-claimed causes before you see it.")
    st.caption(f"Questions used: {ss.n_questions}/{MAX_Q}")
    if st.button("Clear conversation", icon=":material/refresh:", width="stretch"):
        ss.messages, ss.queued, ss.n_questions, ss.rated = [], None, 0, set()
        st.rerun()
    st.divider()
    with st.expander("Data and privacy", icon=":material/shield:"):
        st.markdown("**Data used** - simulator CSVs: orders, financial ledger, design log, HR events and roster, inventory log.")
        st.markdown("**Never shown** - I only report aggregates. These are excluded before the AI sees anything:")
        for k, v in RESTRICTED_FIELDS.items():
            st.markdown(f"- {v}")
        st.markdown("If you ask for restricted detail I will refer you to your manager.")
    with st.expander("Feedback insights", icon=":material/insights:"):
        fs = feedback.summary()
        if fs["total"] == 0:
            st.caption("No feedback yet. Rate an answer to start the log.")
        else:
            a, b, c = st.columns(3)
            a.metric("Helpful", fs["up"])
            b.metric("Not helpful", fs["down"])
            c.metric("Reports", fs["issues"] + fs["definitions"])
            df = fs["df"]
            df = df[df["comment"] != ""].tail(5)[["kind", "category", "comment"]]
            if len(df):
                st.caption("Latest comments")
                st.dataframe(df, hide_index=True, width="stretch")

page = ss.page


# ============================================================================================ PAGE: ASK THE AGENT
def render_reply(msg: dict, is_last: bool, stream: bool = False):
    reply: llm.AgentReply = msg["reply"]
    mid = msg["id"]
    if reply.blocked:
        st.warning(reply.text, icon=":material/shield:")
    else:
        if reply.notice:
            st.info(reply.notice, icon=":material/info:")
        first_kpi = next((r for r in reply.results if r.kpis), None)
        if first_kpi:
            kpi_row([(label, value, delta) for label, value, delta in first_kpi.kpis])
        if stream:
            def gen():
                for tok in re.findall(r"\S+\s*|\n+", reply.text):
                    yield esc(tok)
                    time.sleep(0.006)
            st.write_stream(gen())
        else:
            st.markdown(esc(reply.text))
        charts = []
        for r in reply.results:
            if r.chart is not None:
                charts.append((r.title, r.chart))
            charts += r.extra_charts
        if len(charts) == 1:
            chart(charts[0][1], f"chart-{mid}-0")
        elif charts:
            tabs = st.tabs([c[0] for c in charts])
            for j, (tab, (_, fig)) in enumerate(zip(tabs, charts)):
                with tab:
                    chart(fig, f"chart-{mid}-{j}")
    if reply.results or reply.trace:
        with st.expander("Data behind this answer and how it was made", icon=":material/fact_check:"):
            for i, r in enumerate(reply.results):
                st.markdown(esc(f"**{r.title}** - period: {r.period}"))
                st.caption("Data files: " + ", ".join(r.sources) + (f" | Settings: {r.args}" if r.args else ""))
                if r.confidence:
                    st.markdown(f"<div class='caution'>{h('Method confidence: ' + r.confidence)}</div>", unsafe_allow_html=True)
                if r.table is not None and len(r.table):
                    show_table(r.table, f"{mid}-{i}", r.tool)
                for j, (cap, tdf) in enumerate(r.extra_tables):
                    st.caption(esc(cap))
                    show_table(tdf, f"{mid}-{i}-x{j}", f"{r.tool}_{j}")
                for n in r.notes:
                    st.caption(esc(f"Note: {n}"))
                st.divider()
            st.markdown("**Steps**")
            for s in reply.trace:
                st.markdown(esc(f"- **{s['step']}** - {s['detail']}"))
            if reply.critic:
                v = reply.critic["verdict"]
                st.markdown(esc(f"**Self-review:** {v}" + (f" - {'; '.join(reply.critic['issues'])}" if reply.critic["issues"] else "")))
    if not reply.blocked:
        _feedback_row(msg)
    if is_last and reply.follow_ups:
        st.caption("Ask next")
        for i, q in enumerate(reply.follow_ups):
            st.button(q, key=f"fu-{mid}-{i}", on_click=queue, args=(q,), icon=":material/arrow_forward:", type="tertiary")


def _log_rating(msg_id: str):
    val = ss.get(f"fb-{msg_id}")
    if val is None or msg_id in ss.rated:
        return
    ss.rated.add(msg_id)
    m = next(m for m in ss.messages if m["id"] == msg_id)
    feedback.log("rating", rating="up" if val == 1 else "down", question=m["question"],
                 tools_used=",".join(r.tool for r in m["reply"].results), mode=m["reply"].mode)


def _feedback_row(msg: dict):
    mid = msg["id"]
    left, right = st.columns([1, 3])
    with left:
        st.feedback("thumbs", key=f"fb-{mid}", on_change=_log_rating, args=(mid,))
    with right:
        with st.popover("Report a problem or teach the agent a term", icon=":material/rate_review:"):
            with st.form(f"form-{mid}", clear_on_submit=True):
                cat = st.selectbox("What was the issue?", feedback.CATEGORIES, key=f"cat-{mid}")
                txt = st.text_area("Details (do not include personal information)", max_chars=500, key=f"txt-{mid}",
                                   placeholder="For example: 'lead time should exclude weekends' or 'the margin figure looks too high'")
                if st.form_submit_button("Send feedback"):
                    kind = "definition" if cat.startswith("Term") else "issue"
                    feedback.log(kind, category=cat, comment=txt, question=msg["question"],
                                 tools_used=",".join(r.tool for r in msg["reply"].results), mode=msg["reply"].mode)
                    st.success("Thanks - logged for the team to review.")


def chat_home():
    hero("Ask anything about the business", "Plain-English questions in, charts, tables, forecasts and recommendations out. Every number is traceable.",
         f"Data: {data.coverage_text()}")
    st.markdown("<div class='pillrow'><span>Forecasts with honest ranges</span><span>Delivery-risk model</span><span>What-if simulator</span>"
                "<span>Profit bridge</span><span>Anomaly scan</span><span>Privacy by design</span></div>", unsafe_allow_html=True)
    st.markdown("##### Try a question")
    items = [(cat, q) for cat, qs in SUGGESTED.items() for q in qs]
    for row in range(0, len(items), 3):
        cols = st.columns(3)
        for col, (cat, q) in zip(cols, items[row:row + 3]):
            with col.container(border=True):
                st.markdown(f"<div class='cat'>{cat}</div>", unsafe_allow_html=True)
                st.button(q, key=f"sg-{cat}-{q[:20]}", on_click=queue, args=(q,), width="stretch", type="tertiary")
    st.caption("You can also type your own question below. I only report aggregates; restricted details such as customer or employee identifiers are never shown.")


def page_chat():
    limit_hit = ss.n_questions >= MAX_Q
    typed = st.chat_input("Ask about production, profit, people, sales, risk, forecasts or 'what if...'", disabled=limit_hit, max_chars=800)
    prompt = typed or ss.queued
    ss.queued = None
    if prompt and not limit_hit:
        ss.n_questions += 1
    else:
        prompt = None

    if not ss.messages and not prompt:
        chat_home()
    else:
        st.markdown(f"### {APP_NAME}")
        last_idx = len(ss.messages) - 1
        for idx, m in enumerate(ss.messages):
            with st.chat_message(m["role"], avatar=":material/person:" if m["role"] == "user" else ":material/chair:"):
                if m["role"] == "user":
                    st.markdown(esc(m["content"]))
                else:
                    render_reply(m, is_last=idx == last_idx and not prompt)

    if limit_hit:
        st.warning("This demo session has reached its question limit. Use 'Clear conversation' in the sidebar to start again.")

    if prompt:
        user_msg = {"role": "user", "content": prompt, "id": uuid.uuid4().hex}
        hist = history_for_llm()
        ss.messages.append(user_msg)
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(esc(prompt))
        with st.chat_message("assistant", avatar=":material/chair:"):
            with st.spinner("Analysing the data and running the models..."):
                reply = llm.answer(prompt, hist, backend=backend, self_review=self_review if backend else False)
            msg = {"role": "assistant", "content": reply.text, "reply": reply, "id": uuid.uuid4().hex, "question": prompt}
            ss.messages.append(msg)
            render_reply(msg, is_last=True, stream=True)


# ============================================================================================ PAGE: EXECUTIVE DASHBOARD
PERIODS = {"Last quarter": "last_quarter", "Last 12 months": "last_12_months", "2026": "2026", "2025": "2025", "All time": "all"}


def gauge(score: float, label: str) -> go.Figure:
    color = "#1baf7a" if score >= 70 else "#eda100" if score >= 50 else "#d6453a"
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=round(score), number=dict(suffix="/100", font=dict(size=46, color="#0b0b0b")),
        title=dict(text=f"Business health: <b>{label}</b>", font=dict(size=15, color=INK_2)),
        gauge=dict(axis=dict(range=[0, 100], tickcolor=MUTED), bar=dict(color=color, thickness=0.28), borderwidth=0,
                   steps=[dict(range=[0, 50], color="#fbe3e0"), dict(range=[50, 70], color="#fff1cf"), dict(range=[70, 100], color="#d7f1e6")])))
    fig.update_layout(height=270, margin=dict(l=24, r=24, t=60, b=10), paper_bgcolor=SURFACE)
    return fig


def components_chart(comp: dict) -> go.Figure:
    names, vals = list(comp)[::-1], list(comp.values())[::-1]
    fig = go.Figure(go.Bar(x=vals, y=names, orientation="h", marker=dict(color=["#1baf7a" if v >= 70 else "#eda100" if v >= 50 else "#d6453a" for v in vals], cornerradius=4),
                           text=[f"{v:.0f}" for v in vals], textposition="outside", cliponaxis=False))
    fig.update_xaxes(range=[0, 112], showgrid=False, visible=False)
    T.style(fig, "What is driving the score", 270, legend=False)
    return fig


def page_dashboard():
    hero("Executive dashboard", "The whole business on one screen: health, money, sales, operations and early warnings.", f"Data: {data.coverage_text()}")
    label = st.segmented_control("Period", list(PERIODS), default="Last quarter", key="dash_period", label_visibility="collapsed") or "Last quarter"
    period = PERIODS[label]
    hs = PRO.health_score(period)
    k, kp = hs["kpis"], hs["prev"]

    def delta(cur, prev, kind):
        return T._delta(cur, prev, kind) if kp else None
    st.caption(f"Showing {hs['period']}" + (" - compared with the previous period of equal length." if kp else "."))
    g1, g2 = st.columns([2, 3])
    with g1:
        chart(gauge(hs["score"], hs["label"]), "dash-gauge")
    with g2:
        chart(components_chart(hs["components"]), "dash-comp")
    kpi_row([("Cash profit", T.money(k["profit"]), delta(k["profit"], kp and kp["profit"], "money")),
             ("Cash revenue", T.money(k["revenue"]), delta(k["revenue"], kp and kp["revenue"], "money")),
             ("Cash margin", T.pct(k["margin"]), delta(k["margin"], kp and kp["margin"], "pp")),
             ("Win rate", T.pct(k["win_rate"]), delta(k["win_rate"], kp and kp["win_rate"], "pp")),
             ("On-time delivery", T.pct(k["on_time"]), delta(k["on_time"], kp and kp["on_time"], "pp")),
             ("Avg lead time", f"{k['avg_lead']:.1f} days" if k["avg_lead"] == k["avg_lead"] else "n/a", delta(k["avg_lead"], kp and kp["avg_lead"], "days"))])
    st.write("")

    t_money, t_sales, t_ops, t_warn = st.tabs(["Money", "Sales", "Operations", "Early warnings"])
    with t_money:
        bridge = tool("profit_bridge", period=period)
        a, b = st.columns(2)
        with a:
            chart(bridge.chart, "dash-bridge")
        with b:
            chart(tool("financial_trend", period="all", granularity="quarter").chart, "dash-fin")
        callouts(bridge.insights[:2])
        with st.expander("Profit bridge table"):
            show_table(bridge.table, "dash-bridge-t", "profit_bridge")
    with t_sales:
        fun = tool("quote_funnel", period=period)
        fc = tool("forecast_outlook", metric="quotes", horizon_months=3)
        a, b = st.columns(2)
        with a:
            chart(fun.chart, "dash-fun")
        with b:
            chart(fc.chart, "dash-fc")
        callouts(fun.insights[:1] + fc.insights[:2])
    with t_ops:
        rad = tool("late_risk_radar", top_n=8)
        a, b = st.columns(2)
        with a:
            chart(rad.chart, "dash-rad")
        with b:
            chart(rad.extra_charts[0][1], "dash-load")
        callouts(rad.insights[:2], rad.actions[:2])
        with st.expander("Bottleneck analysis for the same period"):
            bn = tool("production_bottleneck", period=period if period != "last_12_months" else "2026")
            chart(bn.chart, "dash-bn")
            for f in bn.facts[:4]:
                st.markdown(esc(f"- {f}"))
    with t_warn:
        an = tool("anomaly_scan")
        chart(an.chart, "dash-an")
        callouts(an.insights, an.actions)
        show_table(an.table, "dash-an-t", "anomalies")

    st.divider()
    d1, d2, d3 = st.columns([2, 1, 1])
    with d1:
        st.markdown("**Take it with you**")
        st.caption("A single self-contained page with the health score, insights, actions and every chart. Open it in a browser or print it to PDF.")
    with d2:
        st.download_button("Download executive report", report.build_html(period).encode(), file_name="mfc_executive_briefing.html", mime="text/html",
                           icon=":material/download:", width="stretch")
    with d3:
        st.button("Ask the agent about this", on_click=queue, args=(f"Give me a full executive briefing for {label.lower()}.",), icon=":material/forum:", width="stretch")


# ============================================================================================ PAGE: PREDICTIVE LAB
def page_predict():
    hero("Predictive lab", "Machine-learning risk scores and back-tested forecasts - with the accuracy shown, not hidden.", f"Data: {data.coverage_text()}")
    t1, t2, t3, t4 = st.tabs(["Delivery-risk radar", "Forecast studio", "Pricing lab", "Model report card"])

    with t1:
        n = st.slider("Orders to show", 5, 17, 10, key="pl_topn")
        r = tool("late_risk_radar", top_n=n)
        lm = P.late_model()
        op = ds.orders[ds.orders["in_progress"]]
        risk = lm.predict(op)
        kpi_row([("Orders in progress", f"{len(op)}", None), ("Expected to finish late", f"{risk.sum():.1f}", None),
                 ("High-risk orders", f"{int((risk >= 2 * lm.base_rate).sum())}", None),
                 ("Expected penalty exposure", T.money(float((risk * op['quotePrice'] * 0.05).sum())), None),
                 ("Model accuracy (AUC)", f"{lm.auc:.2f}", None)])
        st.write("")
        a, b = st.columns(2)
        with a:
            chart(r.chart, "pl-rad")
        with b:
            chart(r.extra_charts[0][1], "pl-load")
        callouts(r.insights, r.actions)
        with st.expander("Order-level detail and what drives the risk", expanded=False):
            show_table(r.table, "pl-rad-t", "late_risk")
            for j, (cap, tdf) in enumerate(r.extra_tables):
                st.caption(cap)
                show_table(tdf, f"pl-rad-x{j}", "risk_drivers")

    with t2:
        c1, c2 = st.columns([2, 1])
        with c1:
            metric = st.segmented_control("Metric", ["revenue", "profit", "costs", "quotes", "accepted", "win_rate", "on_time"], default="revenue",
                                          format_func=lambda m: PRO.METRIC_LABELS[m][0], key="pl_metric") or "revenue"
        with c2:
            hz = st.slider("Months ahead", 1, 6, 3, key="pl_hz")
        r = tool("forecast_outlook", metric=metric, horizon_months=hz)
        chart(r.chart, "pl-fc")
        a, b = st.columns([3, 2])
        with a:
            callouts(r.insights, r.actions)
        with b:
            st.markdown("<div class='sect'>Forecast table</div>", unsafe_allow_html=True)
            show_table(r.table, "pl-fc-t", f"forecast_{metric}")
        with st.expander("How accurate has each method been? (rolling back-test)"):
            for cap, tdf in r.extra_tables:
                st.caption(cap)
                show_table(tdf, "pl-fc-bt", "backtest")
            st.caption(r.confidence)

    with t3:
        r = tool("price_strategy_analysis")
        a, b = st.columns(2)
        with a:
            chart(r.chart, "pl-price")
        with b:
            chart(r.extra_charts[0][1], "pl-win")
        callouts(r.insights, r.actions)
        with st.expander("Profit at each price change, by customer reaction"):
            show_table(r.table, "pl-price-t", "price_scenarios")

    with t4:
        lm, wm, el = P.late_model(), P.win_model(), P.price_elasticity()
        fc_rows = []
        for m_ in ("revenue", "quotes", "win_rate", "on_time"):
            f = P.forecast(m_, 3)
            fc_rows.append((PRO.METRIC_LABELS[m_][0], f.method, f.backtest.iloc[0]["Error %"]))
        st.markdown("Every model here is small and explainable, trained only on the anonymised simulator data. Below is how good each one really is.")
        cards = [
            ("Late-delivery risk model", "Strong", f"AUC {lm.auc:.2f} (5-fold cross-validated) on {lm.n} delivered orders. 0.50 = coin flip, 1.00 = perfect.",
             "Workshop load per maker is the dominant driver, then days allowed. Safe to use for prioritising orders."),
            ("Quote-win model", "No signal", f"AUC {wm.auc:.2f} on {wm.n:,} quotes - no better than a coin flip.",
             "Price, product, complexity and timber do not explain which quotes are accepted, so I do not pretend to predict individual quotes."),
            ("Price sensitivity", "Uncertain", f"Elasticity {el['estimate']:+.2f} (80% interval {el['lo']:+.2f} to {el['hi']:+.2f}); the data cannot separate it from zero.",
             "Price scenarios are therefore shown under three reaction assumptions rather than one."),
            ("Forecasts", "Planning range", "; ".join(f"{a_}: typical error {pct_:.0%} ({m_.lower()})" for a_, m_, pct_ in fc_rows),
             "For each metric the method that would have forecast best in rolling back-tests is chosen automatically. Only 24 months of history exist."),
        ]
        for name, verdict, evidence, meaning in cards:
            cls = {"Strong": "action", "No signal": "caution", "Uncertain": "caution", "Planning range": "insight"}[verdict]
            st.markdown(f"<div class='{cls}'><b>{h(name)} - {h(verdict)}</b><br>{h(evidence)}<br><span style='color:#52514e'>{h(meaning)}</span></div>", unsafe_allow_html=True)


# ============================================================================================ PAGE: WHAT-IF
PRESETS = {
    "Current business": (0, 0, 0, 0, "Data estimate"),
    "Premium pricing: +8% prices": (8, 0, 0, 0, "Moderate (-1.0)"),
    "Growth push: +30% quotes": (0, 30, 0, 0, "Data estimate"),
    "Growth with 2 extra makers": (0, 30, 2, 0, "Data estimate"),
    "Timber shock: +15% timber cost": (0, 0, 0, 15, "Data estimate"),
    "Lean year: -20% demand": (0, -20, 0, 0, "Data estimate"),
}
ELASTICITY = {"Data estimate": None, "Moderate (-1.0)": -1.0, "High (-2.0)": -2.0}


def _apply_preset():
    p_ = PRESETS[ss.wi_preset]
    ss.wi_price, ss.wi_vol, ss.wi_mk, ss.wi_tim, ss.wi_el = p_


def page_whatif():
    hero("What-if simulator", "Move the levers and see profit, delivery performance and risk respond. Built on last year's real unit economics.",
         f"Baseline: last 12 complete months ({P.baseline()['start']:%b %Y} - {P.baseline()['end']:%b %Y})")
    for key, default in (("wi_price", 0), ("wi_vol", 0), ("wi_mk", 0), ("wi_tim", 0), ("wi_el", "Data estimate"), ("wi_preset", "Current business")):
        ss.setdefault(key, default)
    t1, t2 = st.tabs(["Scenario builder", "Capacity and hiring planner"])
    with t1:
        left, right = st.columns([1, 2.2], gap="large")
        with left:
            st.selectbox("Start from a preset", list(PRESETS), key="wi_preset", on_change=_apply_preset)
            st.slider("Change prices", -20, 30, key="wi_price", format="%d%%")
            st.slider("Change quote volume (demand)", -40, 60, key="wi_vol", format="%d%%")
            st.slider("Add or remove makers", -2, 5, key="wi_mk")
            st.slider("Change timber cost", -20, 40, key="wi_tim", format="%d%%")
            st.radio("How price-sensitive are customers?", list(ELASTICITY), key="wi_el", horizontal=False,
                     help="The data shows almost no link between price and win rate, but cannot prove it. Use the stressed options to test the downside.")
        r = tool("scenario_simulator", price_change_pct=ss.wi_price, quote_volume_change_pct=ss.wi_vol, extra_makers=ss.wi_mk,
                 timber_cost_change_pct=ss.wi_tim, **({} if ELASTICITY[ss.wi_el] is None else {"price_elasticity": ELASTICITY[ss.wi_el]}))
        base = P.sim_baseline()
        scn = P.simulate(PRO._sc(ss.wi_price, ss.wi_vol, ss.wi_mk, ss.wi_tim, ELASTICITY[ss.wi_el]))
        with right:
            kpi_row([("Monthly profit", T.money(scn["profit"]), f"{'+' if scn['profit'] >= base['profit'] else '-'}${abs(scn['profit'] - base['profit']):,.0f}"),
                     ("On-time delivery", T.pct(scn["on_time"], 0), f"{(scn['on_time'] - base['on_time']) * 100:+.1f} pp"),
                     ("Win rate", T.pct(scn["win_rate"], 1), f"{(scn['win_rate'] - base['win_rate']) * 100:+.1f} pp"),
                     ("Orders per month", f"{scn['orders']:.1f}", f"{scn['orders'] - base['orders']:+.1f}")])
            st.write("")
            chart(r.chart, "wi-chart")
        callouts(r.insights, r.actions, [r.confidence] if r.confidence else [])
        a, b = st.columns(2)
        with a:
            st.markdown("<div class='sect'>Current vs scenario (per month)</div>", unsafe_allow_html=True)
            show_table(r.table, "wi-t", "scenario")
        with b:
            for cap, tdf in r.extra_tables:
                st.markdown(f"<div class='sect'>{h(cap)}</div>", unsafe_allow_html=True)
                show_table(tdf, "wi-x", "stress_test")
    with t2:
        r = tool("capacity_planner")
        a, b = st.columns(2)
        with a:
            chart(r.chart, "cp-profit")
        with b:
            chart(r.extra_charts[0][1], "cp-ont")
        callouts(r.insights, r.actions, r.notes)
        with st.expander("Grid as a table"):
            show_table(r.table, "cp-t", "capacity_grid")


# ============================================================================================ PAGE: DATA & TRUST
def page_trust():
    hero("Data and trust", "What the agent knows, what it will never show, and how every number is produced.", f"Data: {data.coverage_text()}")
    t1, t2, t3 = st.tabs(["How it works", "Privacy", "Definitions"])
    with t1:
        st.markdown("""
1. **Guardrail check** - requests for restricted detail or prompt tricks are refused before anything runs.
2. **Understand the question** - the AI picks from vetted analysis tools (it never writes code and never sees raw data).
3. **Run the analysis** - fixed pandas / statistics code returns facts, tables and charts, including the predictive models.
4. **Write the answer** - the AI explains only what the results contain, with recommended actions.
5. **Self-review** - a second AI pass checks for leaks, wrong numbers and over-claimed causes.
6. **Output filter** - anything that looks like an ID, email or key is redacted.
""")
        hs = PRO.health_score("last_quarter")
        st.markdown("##### Business health score (0-100)")
        st.caption("Equal-weight average of five components, each scaled between a 'poor' and a 'good' reference value.")
        st.dataframe({"Component": list(hs["components"]),
                      "Scale (0 -> 100)": ["Cash margin -10% -> +20%", "On-time rate 70% -> 100%", "Win rate 25% -> 55%", "Avg late-risk of open orders 40% -> 10%", "Revenue vs prior period -20% -> +20%"],
                      "Latest": [f"{v:.0f}" for v in hs["components"].values()]}, hide_index=True, width="stretch")
    with t2:
        st.markdown("**Never loaded, shown or used by any model:**")
        for v in RESTRICTED_FIELDS.values():
            st.markdown(f"- {v}")
        st.markdown("Models use only aggregate shop-floor features (orders in production, makers available, days allowed, hours, product). "
                    "Order numbers appear in the risk radar because they identify work in progress, not people.")
    with t3:
        for term, meaning in T.DEFINITIONS.items():
            st.markdown(f"**{term.title()}** - {meaning}")
        st.markdown("##### Data files")
        for f_, d_ in T.FILES.items():
            st.markdown(f"**{f_}** - {d_}")


# ============================================================================================ router
if page == "Ask the Agent":
    page_chat()
elif page == "Executive Dashboard":
    page_dashboard()
elif page == "Predictive Lab":
    page_predict()
elif page == "What-If Simulator":
    page_whatif()
else:
    page_trust()
