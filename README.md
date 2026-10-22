# AskShelf - Retail Sales Intelligence Assistant

A web product for retail and wholesale teams. Upload an order or sales export (Excel or CSV), confirm what each column means, and AskShelf builds a dashboard,
alerts, delivery and penalty analysis, stock cover, forecasts, a scenario planner and formal reports from **your** data. Questions can be asked in plain English;
every answer shows the read-only query behind it.

A marketing site (`/`) and the product app (`/app`) are served by one Python server (Starlette + a vanilla-JS front end). Nothing in the product is tied to a
particular company: the only bundled data is a synthetic retail sample in `sample_data/` (behind the "Try the sample" button).

## Run it locally

```
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .streamlit/secrets.toml.example .env     # then paste your Gemini key (optional - without it the app runs in built-in mode)
python server.py                      # http://localhost:8000   (set PORT to change)
pytest -q
```

Key lookup order: environment variable, `.env`, `.streamlit/secrets.toml`. Optional settings: `GEMINI_MODELS`, `MAX_QUESTIONS_PER_SESSION` (default 30),
`DAILY_AI_LIMIT` (shared AI answers per day, default 400; afterwards the app keeps working in built-in mode).

## How it works

1. **Upload** `.xlsx`, `.xls`, `.csv` or `.tsv` files (25 MB each). Files with the same columns are fused into one table. Columns that look like personal data
   (emails, phone numbers, names, pay, card numbers - by column name *and* by value pattern) are removed on upload.
2. **Map columns.** The date, amount, quantity, product, category, supplier, store, cost, due date, delivered date, penalty, stock and reorder level are detected
   automatically; you confirm or change them. The Data page shows which pages each mapping unlocks.
3. **Use the pages.** Each page declares the columns it needs and says so clearly when they are missing.

| Page | What it does (all computed from the uploaded data) |
|---|---|
| Data | Upload, column mapping, preview, profile (types, missing values, duplicates), privacy notice |
| Ask | Plain-English questions: the AI writes one read-only SQL query (validated, row- and time-limited, shown to you) or requests a forecast; rule-based fallback with no AI key |
| Dashboard | KPIs vs the previous period (revenue, orders, units, order value, margin, on-time rate, penalties), trend, top products, category/store/supplier mix |
| Alerts | Rules for sales drops, unusual months, declining products, margin squeeze, overdue orders, supplier lateness, delivery risk and stock-outs; filtered by role; daily digest |
| Deliveries | On-time rate, lateness by supplier/store, a late-risk model **trained on your own delivered orders** (cross-validated AUC shown; refuses with too little history) |
| Penalties | Penalty cost by month/supplier, exposure on open orders, an expedite plan with estimated saving; uses a mapped penalty column or an assumed rate |
| Stock | Days of cover, below-reorder and overstock status, suggested order quantities, weekly sales at risk |
| Forecasts | Prophet forecasts (revenue, units, orders; by category/product/supplier/store) with an 80% range and back-test error |
| Scenario planner | Price, volume and cost changes with a user-chosen price-sensitivity assumption and a price x volume heatmap |
| Reports | A formal report as PDF, Word and Excel: cover, contents, lists of figures and tables, numbered sections, recommendations, references, appendices |
| Governance | Roles, access matrix, hash-chained audit trail, privacy rules |

## Roles and privacy

Four roles (Owner / General Manager, Sales Manager, Operations Manager, Finance Lead) decide which pages open, who may load or reset data, and who may export.
Access is enforced on every API call. Uploaded data lives in server memory for the browser session only (never written to disk) and is lost on restart.
The AI never sees raw rows: it receives the schema, a few example category values and aggregated results. Every question is PII-masked and written to the audit log.
AI-written report narrative is checked against the query results; a paragraph that cites a figure the data does not contain is replaced by rule-written text and the
replacement is declared in the report's AI-usage appendix.

## Deploy

Any host that runs Python or Docker works. A `Dockerfile`, `render.yaml` and `Procfile` are included. On Render: New > Web Service (Docker) > pick the repo, set
`GEMINI_API_KEY` as a secret environment variable. Prophet report generation needs roughly 1-2 GB of RAM; the free 512 MB plan can run out of memory when generating reports.
Serve behind HTTPS.

## Known limitations

* Data is per session and in memory; restarting the server clears it.
* Models are small and explainable (logistic regression, Prophet). The late-risk model needs at least 150 delivered orders with 20+ late and 20+ on time, and reports its
  own accuracy, which may be modest. Forecasts are shown with ranges and a back-test error.
* Scenario results depend on a price-sensitivity assumption the user chooses; sales data alone cannot prove how customers react to price.
* Currency is not assumed: amounts are shown as plain numbers in whatever unit the uploaded file uses.
