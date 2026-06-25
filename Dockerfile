FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml pyproject.toml start.sh ./
COPY src ./src
COPY scripts ./scripts

ENV PYTHONPATH=/app
ENV DATA_DIR=/data
ENV ENABLE_SCHEDULER=true

RUN mkdir -p /data/candles /data/models /data/logs && chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
