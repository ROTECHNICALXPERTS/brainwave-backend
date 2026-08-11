#!/usr/bin/env bash
# Starts the orchestrator + all 8 x402-gated microservices. Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [ ! -f ".env" ]; then
  echo "No .env found. Copying .env.example -> .env"
  cp .env.example .env
fi

PIDS=()
cleanup() {
  echo ""
  echo "Stopping all services..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting search service (provider: search)..."
python services/search/main.py &
PIDS+=($!)

echo "Starting search-b service (reverse-auction provider)..."
python "services/search-b/main.py" &
PIDS+=($!)

echo "Starting search-c service (reverse-auction provider)..."
python "services/search-c/main.py" &
PIDS+=($!)

echo "Starting fact-check service..."
python "services/fact-check/main.py" &
PIDS+=($!)

echo "Starting enrichment service..."
python services/enrichment/main.py &
PIDS+=($!)

echo "Starting summarizer service..."
python services/summarizer/main.py &
PIDS+=($!)

echo "Starting report-gen service (premium)..."
python "services/report-gen/main.py" &
PIDS+=($!)

echo "Starting report-gen-cheap service..."
python "services/report-gen-cheap/main.py" &
PIDS+=($!)

sleep 1.5

echo "Starting orchestrator..."
python -m uvicorn orchestrator.main:app --host 0.0.0.0 --port "${PORT_ORCHESTRATOR:-4000}" &
PIDS+=($!)

echo ""
echo "All services started. Orchestrator: http://localhost:${PORT_ORCHESTRATOR:-4000}"
echo "Run 'python scripts/test_e2e.py' in another terminal to try it end to end."
echo "Press Ctrl-C to stop everything."

wait
