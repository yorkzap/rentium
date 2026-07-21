#!/usr/bin/env bash
#
# Collect a diagnostics bundle to hand to an AI (or a human). It gathers
# container status, which env vars are present (booleans only — NEVER the
# secret values), recent RAMA errors, and filtered app/worker logs.
#
# Usage (from the repo root):
#   bash scripts/diagnostics.sh                 # print to screen
#   bash scripts/diagnostics.sh > diag.txt      # save, then paste diag.txt to the AI
#
# Then tell the AI: "here's the diagnostics bundle + I did X and saw Y — fix it."

set -uo pipefail
COMPOSE="docker compose -f docker-compose.local.yml"
DENOISE='urllib3|RequestsDependencyWarning|staticfiles|No directory at|WatchFiles'

section() { printf '\n===== %s =====\n' "$1"; }

section "CONTAINERS"
$COMPOSE ps 2>/dev/null

section "ENV PRESENT? (booleans only — no secret values)"
$COMPOSE exec -T django /entrypoint python manage.py shell -c "
from django.conf import settings as s
keys = ['SECRET_KEY','ALLOWED_HOSTS','FRONTEND_URL','PUBLIC_SITE_URL',
        'SENDGRID_API_KEY','GEOAPIFY_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_BOT_USERNAME',
        'TELEGRAM_WEBHOOK_SECRET']
for k in keys:
    print(' ', k + ':', bool(getattr(s, k, '')))
" 2>/dev/null | grep -vE "$DENOISE"

section "RECENT RAMA ERRORS (last 10)"
$COMPOSE exec -T django /entrypoint python manage.py shell -c "
from rentium.rama.models import RamaAudit
rows = RamaAudit.objects.filter(kind='ERROR').order_by('-created_at')[:10]
print('  total:', RamaAudit.objects.filter(kind='ERROR').count())
for a in rows:
    print(' ', a.created_at.strftime('%m-%d %H:%M'), a.model, '|', str(a.content)[:200])
" 2>/dev/null | grep -vE "$DENOISE"

section "DJANGO LOG (errors/warnings, last 40)"
$COMPOSE logs --tail=400 django 2>&1 | grep -iE "error|exception|traceback|not found|failed" \
  | grep -viE "$DENOISE" | tail -40

section "CELERY LOG (errors, last 30)"
$COMPOSE logs --tail=300 celeryworker celerybeat 2>&1 | grep -iE "error|exception|traceback|failed" \
  | grep -viE "$DENOISE" | tail -30

section "END"
echo "Paste everything above to the AI, plus: what you did and what you saw."
