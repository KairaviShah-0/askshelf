# AskShelf - Retail Sales Intelligence Assistant

A web product that lets sales and operations managers ask questions about their business in plain English and get answers, charts, forecasts,
early-warning alerts and recommended actions. A marketing site (`/`) and the product app (`/app`) are served by one Python server
(Starlette + a vanilla-JS front end); the analytics engine (pandas / numpy models, vetted tools) and Google Gemini sit behind a JSON API.

## Run it locally

```
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .streamlit/secrets.toml.example .env     # then paste your Gemini key (optional - without it the app runs in built-in mode)
python server.py                      # http://localhost:8000   (set PORT to change)
pytest -q
```

Key lookup order: environment variable, `.env`, `.streamlit/secrets.toml`. Optional: `GEMINI_MODELS`, `MAX_QUESTIONS_PER_SESSION` (default 30).

## Deploy

Any host that runs Python works (Render, Railway, Fly.io, a VM). Build: `pip install -r requirements.txt`. Start: `python server.py` (the `Procfile` is included).
Set `GEMINI_API_KEY` as a secret environment variable and serve behind HTTPS. Audit and feedback logs are written to `audit/` and `feedback/`; mount a
persistent disk if you need them to survive restarts.

## Data Lab (bring your own data)

* **Upload:** `.xlsx`, `.xls`, `.csv`, `.tsv`, 25 MB each. Names are cleaned, numbers and dates detected, and columns that look like personal data (by name or by value pattern: emails, phones, names, pay, cards) are **removed on upload**. No database server: data lives in an in-memory SQLite database per session (it is lost when the server restarts).
* **Questions:** the AI plans the analysis and writes one SELECT (or asks for a forecast). Every query is validated as a single read-only statement, run with an authoriser, a row cap and a time limit, and shown to the user. One automatic self-repair attempt; rule-based fallback with no AI key.
* **Forecasting:** Prophet (monthly by default, weekly/daily for short histories), back-tested, with an 80% range; grouped forecasts for the top five groups.
* **Reports:** title page, table of contents, lists of figures and tables, executive summary, introduction, data and methodology, numbered analysis sections (figure, table, interpretation, key finding), forecast, conclusions and recommendations, limitations, references, and appendices (AI usage log, query log, data dictionary). PDF is typeset with ReportLab; Word and Excel versions are generated from the same model. AI-written narrative is checked against the query results; any paragraph that cites a figure the data does not contain is replaced by rule-written text and the replacement is declared in Appendix A.
* **Access:** Owner and Finance Lead roles only (free-form analysis bypasses the built-in per-role restrictions). Actions are written to the audit log.
* Try it with `sample_data/` (two yearly wholesale order files, with an email column that gets removed).

## Who it is for

Furniture wholesalers and distributors managing purchase orders, suppliers, stock availability, delivery schedules and penalties. The demo runs on a simulated
furniture business (orders, stock log, delivery dates, late penalties); the same engine can be pointed at a real order and stock export.

## Pricing model (illustrative, on the landing page)

Customers pay for three things they can count: **people**, **data sources** and **answers per month**. Starter $149 (3 people, 1 source, 1,000 answers), Growth $499
(10 / 3 / 5,000), Scale $1,299 (30 / 10 / 20,000), Enterprise custom; extra answers $20 per 1,000; annual billing saves 20%; 14-day free trial. An interactive
calculator picks the cheapest fitting plan and compares it with answering the same questions by hand (assumptions are visible and adjustable). Unit economics:
an answer costs about $0.004 in model fees (`agent/config.py`), so the overage rate leaves a wide margin.

## Product areas

| Page | What it does |
|---|---|
| Ask | Chat with the data: routed to 1 of 20 vetted analyses, answer + KPIs + charts + tables + steps, thumbs, report/teach a term, follow-ups |
| Dashboard | Health score, KPIs vs previous period, profit bridge, quote funnel, late-risk radar, anomaly map, executive report download |
| Alerts | Six live rules (delivery risk, penalty exposure, demand dip, unusual months, low stock, win-rate slide), filtered by role, daily digest download |
| Deliveries | Every open order on a timeline to its due date coloured by late risk, due this week/next, overdue orders, penalty exposure |
| Stock | Days of cover per material, stock on hand vs open-order demand (shortfalls), reorder cadence |
| Forecasts | Late-risk radar, back-tested forecast studio, pricing lab, model report card |
| Scenario planner | Live what-if sliders with stress tests, capacity and hiring planner |
| Data Lab | Upload your own Excel/CSV files (several are fused if columns match), preview and profile them, ask questions (AI-written read-only SQL or rule-based fallback), Prophet forecasts (incl. per group), export results to Excel, and generate a formal report (PDF, Word, Excel) |
| Governance | Role matrix, hash-chained audit trail, privacy rules |
| Knowledge base | Definitions and data sources |

Roles (switch top-right): Owner / General Manager, Sales Manager, Operations Supervisor, Finance Lead. Access is enforced on the API and inside the agent pipeline.

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

## Data

The six CSVs in `data/` are the simulator export (`customers_data.csv` is intentionally not included - it only maps customers to orders).
Key definitions and verified facts: 50% deposit at acceptance and 50% on completion; a late order loses 10% of the final payment (checked against all 63 late orders in the ledger); wages are a fixed daily cost. "Last quarter" means the last complete quarter in the data (Q4 2026).

## Known limitations

* Feedback is stored in a CSV on the app's temporary disk, so it resets when the app restarts.
* Product margins after wages/overheads rely on an allocation assumption (labour hours), stated in each answer.
* The demand outlook is a simple seasonal-naive method on two years of data - directional only.
* The data has no reason-for-loss, machine-downtime or supplier-delay fields, so some "why" questions can only be answered as associations.
