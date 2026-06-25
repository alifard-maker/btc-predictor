FROM python:3.12-slim

WORKDIR /app

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

RUN mkdir -p /data/candles /data/models /data/logs

EXPOSE 8080

# Railway injects $PORT at runtime — must use shell form to expand it
CMD ["sh", "-c", "exec uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
