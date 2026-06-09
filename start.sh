#!/bin/bash
set -e

MODEL=${OLLAMA_MODEL:-qwen2.5:14b}
PORT=${PORT:-8000}
# Один воркер + asyncio = оптимально для SQLite.
# Для PostgreSQL: увеличьте до $(nproc) воркеров.
WORKERS=1

echo "Starting Ollama..."
ollama serve &

echo "Waiting for Ollama..."
until curl -s http://localhost:11434/api/tags > /dev/null 2>&1; do sleep 1; done

echo "Checking model: $MODEL"
ollama pull "$MODEL"

echo "Starting FastAPI (workers=$WORKERS, port=$PORT)..."
exec uvicorn main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --loop uvloop \
  --log-level info \
  --access-log
