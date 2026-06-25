FROM python:3.12-slim

WORKDIR /app

# LightGBM / XGBoost runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

ENV PYTHONPATH=/app
ENV DATA_DIR=/data
ENV ENABLE_SCHEDULER=true
ENV PORT=8000

RUN mkdir -p /data/candles /data/models /data/logs

EXPOSE 8000

CMD uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
