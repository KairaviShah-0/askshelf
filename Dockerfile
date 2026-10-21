FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Warm the Prophet/Stan model at build time so the first report is not slow
RUN python -c "from prophet import Prophet; import pandas as pd, numpy as np; d=pd.DataFrame({'ds':pd.date_range('2024-01-31',periods=12,freq='ME'),'y':np.arange(12.)}); Prophet(yearly_seasonality=False,weekly_seasonality=False,daily_seasonality=False).fit(d)" || true
EXPOSE 8000
CMD ["python", "server.py"]
