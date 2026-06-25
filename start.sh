#!/bin/sh
set -e
PORT="${PORT:-8080}"
echo "=== BTC Predictor starting on 0.0.0.0:${PORT} ==="
echo "DATA_DIR=${DATA_DIR:-/data}"
echo "DATABASE_URL=${DATABASE_URL:+set}"
echo "EXCHANGE=${EXCHANGE:-kraken}"
exec uvicorn src.api.main:app --host 0.0.0.0 --port "$PORT" --log-level info
