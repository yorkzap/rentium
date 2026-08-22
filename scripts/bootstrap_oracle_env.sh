#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

cd "$(dirname "$0")/.."

env_dir=".envs/.oracle"
django_env="${env_dir}/.django"
postgres_env="${env_dir}/.postgres"
providers_env="${env_dir}/.providers"

if [[ -e "${django_env}" || -e "${postgres_env}" ]]; then
  echo "Oracle environment files already exist; leaving them unchanged." >&2
  exit 0
fi

umask 077
mkdir -p "${env_dir}"

django_secret="$(openssl rand -hex 48)"
admin_path="$(openssl rand -hex 24)/"
postgres_password="$(openssl rand -hex 32)"

printf '%s\n' \
  'POSTGRES_HOST=postgres' \
  'POSTGRES_PORT=5432' \
  'POSTGRES_DB=rentium' \
  'POSTGRES_USER=rentium' \
  "POSTGRES_PASSWORD=${postgres_password}" \
  >"${postgres_env}"

printf '%s\n' \
  'DJANGO_SETTINGS_MODULE=config.settings.production' \
  "DJANGO_SECRET_KEY=${django_secret}" \
  "DJANGO_ADMIN_URL=${admin_path}" \
  'DJANGO_ACCOUNT_ALLOW_REGISTRATION=True' \
  'DJANGO_SERVER_EMAIL=Rentium <noreply@rentium.ca>' \
  'SENDGRID_API_KEY=' \
  'SENDGRID_GENERATE_MESSAGE_ID=True' \
  'SENDGRID_MERGE_FIELD_FORMAT=None' \
  'SENTRY_DSN=' \
  'GEOAPIFY_KEY=' \
  'ANTHROPIC_API_KEY=' \
  'OPENAI_API_KEY=' \
  'TELEGRAM_BOT_TOKEN=' \
  'TELEGRAM_BOT_USERNAME=' \
  'TELEGRAM_WEBHOOK_SECRET=' \
  >"${django_env}"

chmod 600 "${django_env}" "${postgres_env}"
touch "${providers_env}"
chmod 600 "${providers_env}"
echo "Created fresh Oracle environment files. Add approved provider keys to ${providers_env}."
