# Modern Furniture Co. - AI Analytics Agent (proof of concept)

A chat app that lets a manager ask questions about the Advanced Furniture Manufacturing Simulator data in plain English
and get a written answer, key numbers and charts. Built with Streamlit + Google Gemini.

## How it works

```
Question -> 1 Guardrail check -> 2 LLM routing -> 3 Vetted analysis tool(s) -> 4 LLM drafts answer -> 5 LLM critic reviews -> 6 Output filter -> Answer + chart
                (rules)          (Gemini function     (fixed pandas code,        (only from tool        (leakage, wrong       (regex redaction)
                                  calling)             no LLM-written code)       results)               numbers, over-claims)
```

* **The LLM never writes code and never sees raw data.** It picks one of 10 vetted tools (`agent/tools.py`); each returns facts, a table and a chart. Every number is traceable ("How this answer was made" panel).
* **Domain-knowledge layer** (`agent/knowledge.py`): business context, glossary and data dictionary injected into every prompt. Users can flag misunderstood terms via the feedback form.
* **Privacy by design** (`agent/data.py`, `agent/guardrails.py`): customer IDs, enquiry text, employee IDs and individual pay are dropped/aggregated when the data is loaded; restricted requests get a "contact your manager" reply; output is scanned and redacted; the critic step checks for leakage too.
* **Built-in mode:** with no API key (or if Gemini is down/over quota) a keyword router + template answer keeps the app working.
* **Feedback module:** thumbs up/down, issue reports and "teach the agent a term" -> `feedback/feedback_log.csv`, summarised in the sidebar.

| Tool | Answers questions like |
|---|---|
| `business_summary` | How are we doing last quarter / in 2026? |
| `production_bottleneck` | What was our biggest production bottleneck? |
| `late_delivery_analysis` | Which products are late most often? What do penalties cost? |
| `resignation_impact` | What is the impact of resignations on on-time delivery? |
| `workforce_overview` | How is the team made up? How high is turnover? |
| `product_profitability` | Which product type is the most profitable? |
| `financial_trend` | How has profit changed month by month? |
| `sales_pricing_analysis` | Why do we lose quotes? Does price matter? |
| `demand_forecast` | What does demand look like for the next 3 months? |
| `inventory_analysis` | Which timber runs low most often? What do we spend? |
| `executive_briefing` | Give me a full review / board summary |
| `late_risk_radar` | **Predictive:** which open orders are likely to be late? |
| `forecast_outlook` | **Predictive:** back-tested forecasts with 80% ranges (revenue, profit, quotes, win rate...) |
| `scenario_simulator` | **What-if:** raise prices 10%? demand +30%? hire 2 makers? |
| `capacity_planner` | Do we need to hire? Profit and on-time by demand and team size |
| `price_strategy_analysis` | Should we raise prices? What drives winning quotes? |
| `profit_bridge` | Why did profit change versus the previous period? |
| `anomaly_scan` | Anything unusual or worrying? |
| `quote_funnel` | Where do we lose value between quote and delivery? |
| `data_dictionary` | What does 'production days' mean? What data do you use? |

## Pages

Ask the Agent (chat, streamed answers, charts, tables, CSV downloads) | Executive Dashboard (health score, KPIs, downloadable HTML report) |
Predictive Lab (delivery-risk radar, forecast studio, pricing lab, model report card) | What-If Simulator | Data & Trust.
Deep links: `?page=exec`, `?page=pred`, `?page=what`.

Predictive models live in `agent/predict.py` (numpy only): a cross-validated logistic late-delivery model (AUC about 0.96), an honest quote-win
model (AUC about 0.5 - no signal, and the app says so), a rolling-back-test forecaster that picks its own method, and a unit-economics simulator.

## Run it in VS Code

1. Install Python 3.11+ and VS Code (with the Python extension). Open this folder in VS Code.
2. Terminal -> New Terminal, then:
   ```
   python -m venv .venv
   .venv\Scripts\activate          # Windows      (macOS/Linux: source .venv/bin/activate)
   pip install -r requirements-dev.txt
   ```
3. Add your key: copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and paste your Gemini API key (get one free at aistudio.google.com -> Get API key).
4. `streamlit run app.py` - opens http://localhost:8501. Without a key it runs in built-in mode.
5. Tests: `pytest -q` (about 100 tests, no key needed).

## Put it online (Streamlit Community Cloud)

1. Create a GitHub repo (private is fine) and push this folder (`.streamlit/secrets.toml` is git-ignored, so your key is not uploaded).
2. share.streamlit.io -> sign in with GitHub -> **Create app** -> pick the repo, branch `main`, main file `app.py`. Under *Advanced settings* choose Python 3.12 and paste your secrets:
   ```
   GEMINI_API_KEY = "your-key"
   ```
3. Deploy. Set the app to **public** (Share -> "Public") so assessors can open it without logging in. Open the link yourself shortly before submitting: free apps sleep when idle and wake in ~30 seconds.

Optional secrets: `GEMINI_MODELS` (comma-separated, tried in order; default `gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` - check aistudio.google.com for the models your key can use), `MAX_QUESTIONS_PER_SESSION` (default 30).

## Data

The six CSVs in `data/` are the simulator export (`customers_data.csv` is intentionally not included - it only maps customers to orders).
Key definitions and verified facts: 50% deposit at acceptance and 50% on completion; a late order loses 10% of the final payment (checked against all 63 late orders in the ledger); wages are a fixed daily cost. "Last quarter" means the last complete quarter in the data (Q4 2026).

## Known limitations

* Feedback is stored in a CSV on the app's temporary disk, so it resets when the app restarts.
* Product margins after wages/overheads rely on an allocation assumption (labour hours), stated in each answer.
* The demand outlook is a simple seasonal-naive method on two years of data - directional only.
* The data has no reason-for-loss, machine-downtime or supplier-delay fields, so some "why" questions can only be answered as associations.
