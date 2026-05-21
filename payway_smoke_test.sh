#!/usr/bin/env bash
set -u
set -o pipefail

echo "== PayWay smoke test =="

# 0) Quick env sanity
echo "[env]"
grep -E "^(DEBUG|APP_URL|ABA_PAYWAY_API_URL|ABA_PAYWAY_CHECKOUT_PATH|ABA_PAYWAY_WEBHOOK_SECRET)=" .env || true
echo

# 1) Auth sanity
echo "[auth/me]"
curl -s -b cookies.txt http://localhost:8000/api/auth/me
echo
echo

# 2) Create booking (Monday UTC)
BOOKING_JSON=$(curl -s -L -b cookies.txt -X POST http://localhost:8000/api/bookings/ \
  -H "Content-Type: application/json" \
  -d '{
    "service_id":"26c80532-9ce8-439e-8b72-92636be272d4",
    "staff_id":"7ce66218-4343-4207-9301-3ffe6681210d",
    "customer_id":"ignored-for-customer-role",
    "start_time_utc":"2026-03-09T10:00:00Z",
    "booking_source":"web",
    "customer_timezone":"UTC"
  }')

echo "[booking]"
echo "$BOOKING_JSON"
BOOKING_ID=$(python -c 'import sys,json; print(json.loads(sys.stdin.read()).get("id",""))' <<< "$BOOKING_JSON")
echo "BOOKING_ID=$BOOKING_ID"
if [ -z "${BOOKING_ID}" ]; then
  echo "Booking failed. Check logs:"
  docker compose logs backend --tail=120
  exit 1
fi
echo

# 3) Create payment intent
PAYMENT_JSON=$(curl -s -b cookies.txt -X POST http://localhost:8000/api/payments/create-intent \
  -H "Content-Type: application/json" \
  -d "{
    \"booking_id\":\"$BOOKING_ID\",
    \"amount\":\"0.20\",
    \"currency\":\"USD\",
    \"provider\":\"aba_payway\"
  }")

echo "[payment intent]"
echo "$PAYMENT_JSON"

PAYMENT_ID=$(python -c 'import sys,json; print(json.loads(sys.stdin.read()).get("payment_id",""))' <<< "$PAYMENT_JSON")
PAYMENT_URL=$(python -c 'import sys,json; print(json.loads(sys.stdin.read()).get("payment_url",""))' <<< "$PAYMENT_JSON")

echo "PAYMENT_ID=$PAYMENT_ID"
echo "PAYMENT_URL=$PAYMENT_URL"
if [ -z "${PAYMENT_ID}" ]; then
  echo "Create intent failed. Check logs:"
  docker compose logs backend --tail=120
  exit 1
fi
echo

echo "Open PAYMENT_URL in browser and complete sandbox payment, then press Enter..."
read -r _

# 4) Check webhook effect
echo "[db verify]"
docker compose exec -T db psql -U booking_user -d booking_database -c "SELECT id, status, payment_status FROM bookings WHERE id='${BOOKING_ID}';"
docker compose exec -T db psql -U booking_user -d booking_database -c "SELECT id, booking_id, provider, status, provider_reference, created_at FROM payments WHERE id='${PAYMENT_ID}';"

echo
echo "[recent backend logs]"
docker compose logs backend --tail=80
