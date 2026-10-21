#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
VUS="${VUS:-10000}"
ADMIT_RATE="${ADMIT_RATE:-1000}"
PAYMENT_WEBHOOK_SECRET="${PAYMENT_WEBHOOK_SECRET:-dev-payment-secret-change-me}"
LOADTEST_HARDWARE_DETAIL="${LOADTEST_HARDWARE_DETAIL:-k6 GOMAXPROCS=2; local services run in Docker Desktop}"
export PAYMENT_WEBHOOK_SECRET LOADTEST_HARDWARE_DETAIL

EVENT=$(curl -fsS -X POST "$BASE_URL/admin/events" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Seatlock flash sale load test"}')
EVENT_ID=$(printf '%s' "$EVENT" | jq -r '.id')
SHOW=$(curl -fsS -X POST "$BASE_URL/admin/shows" \
  -H 'Content-Type: application/json' \
  -d "{\"event_id\":$EVENT_ID,\"starts_at\":\"2030-01-01T00:00:00Z\",\"seat_count\":500,\"seat_price_minor\":2500,\"queue_enabled\":true,\"queue_admit_per_second\":$ADMIT_RATE}")
SHOW_ID=$(printf '%s' "$SHOW" | jq -r '.id')
SUMMARY="loadtest/k6-summary-$SHOW_ID.json"
RUN_LOG="loadtest/k6-run-$SHOW_ID.log"

uv run python -m app.workers.admitter >"/tmp/seatlock-admitter-$SHOW_ID.log" 2>&1 &
ADMITTER_PID=$!
cleanup() {
  kill "$ADMITTER_PID" 2>/dev/null || true
  wait "$ADMITTER_PID" 2>/dev/null || true
}
trap cleanup EXIT

RUN_COMMAND="GOMAXPROCS=2 k6 run --summary-export=$SUMMARY --env BASE_URL=$BASE_URL --env SHOW_ID=$SHOW_ID --env VUS=$VUS --env PAYMENT_WEBHOOK_SECRET=<configured> loadtest/flash_sale.js"
GOMAXPROCS=2 k6 run \
  --summary-export="$SUMMARY" \
  --env "BASE_URL=$BASE_URL" \
  --env "SHOW_ID=$SHOW_ID" \
  --env "VUS=$VUS" \
  --env "PAYMENT_WEBHOOK_SECRET=$PAYMENT_WEBHOOK_SECRET" \
  loadtest/flash_sale.js 2>&1 | tee "$RUN_LOG"
uv run python loadtest/extract_events.py "$RUN_LOG" \
  --show-id "$SHOW_ID" \
  --database-url "${DATABASE_URL:-postgresql://seatlock:seatlock@localhost:5432/seatlock}" \
  --output loadtest/events.json
uv run python loadtest/verify.py \
  --show-id "$SHOW_ID" \
  --k6-summary "$SUMMARY" \
  --command "$RUN_COMMAND"
printf 'Load test show: %s\n' "$SHOW_ID"
