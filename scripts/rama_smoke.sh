#!/usr/bin/env bash
# RAMA gold-checklist smoke runner.
#
# Usage (from host, API on :8000):
#   bash scripts/rama_smoke.sh
#
# Via docker:
#   docker compose -f docker-compose.local.yml exec -T django bash /app/scripts/rama_smoke.sh
#
# Env:
#   RAMA_BASE_URL   default http://127.0.0.1:8000
#   RAMA_EMAIL      default rajgurshersingh@gmail.com
#   RAMA_TOKEN      if set, skip token lookup
#   RAMA_FRESH=1    new conversation per question (default 1)
#   RAMA_SLEEP=0.3  pause between questions (seconds)

set -euo pipefail

BASE_URL="${RAMA_BASE_URL:-http://127.0.0.1:8000}"
EMAIL="${RAMA_EMAIL:-rajgurshersingh@gmail.com}"
FRESH="${RAMA_FRESH:-1}"
SLEEP_S="${RAMA_SLEEP:-0.3}"

# Prefer python for UUID + JSON; curl for HTTP.
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "python3 required" >&2
  exit 1
fi
PY="$(command -v python3 || command -v python)"

get_token() {
  if [[ -n "${RAMA_TOKEN:-}" ]]; then
    echo "$RAMA_TOKEN"
    return
  fi
  # Inside Django container: mint/read DRF token for the landlord user.
  if [[ -f /app/manage.py ]]; then
    export DATABASE_URL="${DATABASE_URL:-postgres://debug:debug@postgres:5432/rentium}"
    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.local}"
    "$PY" - <<PY
import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ["DJANGO_SETTINGS_MODULE"])
django.setup()
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
u = get_user_model().objects.filter(email="${EMAIL}").first()
if not u:
    raise SystemExit("No user with email ${EMAIL}")
t, _ = Token.objects.get_or_create(user=u)
print(t.key)
PY
    return
  fi
  echo "Set RAMA_TOKEN or run inside the django container." >&2
  exit 1
}

TOKEN="$(get_token)"
CONV="$("$PY" -c 'import uuid; print(uuid.uuid4())')"

QUESTIONS=(
  "How many properties / listings?"
  "Are D and E the same unit as Garden Suite?"
  "Occupied today?"
  "Next month? (occupancy as of first of next calendar month)"
  "Is Room E vacant today?"
  "Lease type + number for McKenzie Room E?"
  "Viewings on 2026-07-30?"
  "Expenses this month? List description and amount for each line."
  "Expected rent this month, collected this month, and deposits held?"
  "Bills included and e-transfer email on Room E lease?"
  "So Room E has no lease?"
  "Listing says Available — is Room E free with no commitment?"
  "Is Room E rented in July 2026?"
  "Is Room E rented in August 2026?"
  "What is McKenzie Side Unit?"
  "How many complete units vs rooms?"
  "Who is on Room E lease, rent amount, and term dates?"
  "Move-out date for Room E?"
  "Fixed-term or month-to-month for Room E?"
  "Any draft leases?"
  "Security deposit on Room E lease?"
  "Next charge?"
  "Expenses for Room D this month?"
  "Has the 600 dollars in expenses left the bank?"
  "Expenses today?"
  "Viewings on Garden Suite?"
  "Anything needing my attention?"
  "Open work orders?"
  "What kind of lease would Garden Suite use if I create one?"
  "What kind of lease would a new room use?"
)

echo "========================================"
echo "RAMA smoke  base=$BASE_URL  email=$EMAIL"
echo "fresh_chat_per_q=$FRESH  conversation=$CONV"
echo "========================================"
echo

i=0
for q in "${QUESTIONS[@]}"; do
  i=$((i + 1))
  if [[ "$FRESH" == "1" ]]; then
    CONV="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
  fi

  echo "---------- Q$i ----------"
  echo "Q: $q"

  # Build JSON body without jq dependency.
  BODY="$("$PY" -c "
import json, sys
print(json.dumps({
  'message': sys.argv[1],
  'conversation_id': sys.argv[2],
}))
" "$q" "$CONV")"

  RESP="$(curl -sS -X POST "${BASE_URL}/api/rama/chat/" \
    -H "Authorization: Token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$BODY" || true)"

  "$PY" -c "
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    print('RAW:', raw[:2000])
    sys.exit(0)
if 'detail' in data and 'reply' not in data and 'message' not in data and 'text' not in data:
    print('ERROR:', data.get('detail') or data)
else:
    # API shape: { reply / message / text / answer }
    text = data.get('reply') or data.get('message') or data.get('text') or data.get('answer') or data
    if isinstance(text, dict):
        text = text.get('text') or text.get('reply') or json.dumps(text, indent=2)
    print('A:', text)
    tools = data.get('tools_used') or data.get('tools') or []
    if tools:
        print('tools:', ', '.join(tools) if isinstance(tools, list) else tools)
" <<<"$RESP"

  echo
  sleep "$SLEEP_S"
done

echo "========================================"
echo "Done. $i questions."
echo "Tip: RAMA_FRESH=0 reuses one conversation (multi-turn)."
echo "========================================"
