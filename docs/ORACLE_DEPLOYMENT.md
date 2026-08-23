# Single-VPS deployment

Rentium keeps the Next.js frontend on Vercel and runs the stateful backend
services on one Linux VPS:

- Django/Gunicorn
- PostgreSQL 16 with pgvector (full-text + vector retrieval in one database)
- Redis
- one Celery worker and Celery beat
- nginx as the origin proxy
- the existing `rentium-api` Cloudflare Tunnel

The deployment is described by `docker-compose.oracle.yml`. It deliberately
does not expose PostgreSQL, Redis, or Django on a public interface. nginx is
bound to `127.0.0.1:8000`, and Cloudflare reaches it over the private Compose
network.

Despite the historical filename, the stack has no Oracle API dependency. Its
images support ARM64 and x86-64, so it can move to another Docker-capable Linux
VPS without application changes. Keep the Compose project name unchanged when
moving so the persistent volume names remain predictable.

## Persistent data

The Compose project owns four persistent volumes: PostgreSQL data, PostgreSQL
backups, Redis data, and Django media. Never remove these volumes during a
routine deployment. A complete backup includes both PostgreSQL and the media
volume.

Only property and showcase images are public under `/media/`. Lease files,
signatures, receipts, ledger imports, maintenance evidence, and RAMA documents
remain behind authenticated Django download endpoints.

## First deployment

1. Clone the backend into `/opt/rentium/app`.
2. Run `scripts/bootstrap_oracle_env.sh` once. It creates fresh Django and
   PostgreSQL secrets and an empty, optional `.envs/.oracle/.providers` file;
   it does not copy development credentials. Put only explicitly approved
   production provider keys in the provider file.
3. Put the Cloudflare tunnel credential at
   `/etc/cloudflared/credentials.json` and create
   `/etc/cloudflared/config.yml` from
   `compose/oracle/cloudflared.yml.example`. Own the directory and both files
   as `root:65532`, set the directory to mode `750`, and set the files to mode
   `640`; `65532` is the unprivileged user in the pinned cloudflared image.
4. Run `docker compose -f docker-compose.oracle.yml build`.
5. Run `docker compose -f docker-compose.oracle.yml up -d`.
6. Verify `http://127.0.0.1:8000/healthz`, then verify
   `https://api.rentium.ca/` externally.

The pinned pgvector/PostgreSQL image is multi-architecture (including the
Oracle VM's ARM64 shape). The migration service enables the `vector` extension
and must complete successfully before Django or Celery can
start. External providers such as SendGrid, Geoapify, Telegram, Anthropic, and
OpenAI remain disabled until their keys are added to
`.envs/.oracle/.providers`.

## Provider credentials

Put production-only provider credentials in `.envs/.oracle/.providers`, which
is optional, mode `600`, and ignored by Git. Never paste tokens into tickets,
chat, shell history, or Compose YAML.

After changing the provider file, recreate the application processes:

```console
sudo docker compose -f docker-compose.oracle.yml up -d \
  --force-recreate migrate django celeryworker celerybeat
```

### Telegram

Required variables are:

```dotenv
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_BOT_USERNAME=<username without @>
TELEGRAM_WEBHOOK_SECRET=<random A-Z/a-z/0-9/_/- value>
```

After recreating the application processes, register and inspect the webhook:

```console
sudo docker compose -f docker-compose.oracle.yml exec -T django \
  /entrypoint python manage.py set_telegram_webhook \
  --url https://api.rentium.ca

sudo docker compose -f docker-compose.oracle.yml exec -T django \
  /entrypoint python manage.py set_telegram_webhook --info
```

If a bot token is ever displayed or copied through an untrusted channel,
revoke it in BotFather, replace it in `.providers`, recreate the application
processes, and register the webhook again.

## Moving to another VPS

1. Install Docker Engine and the Compose plugin on the destination.
2. Copy this repository plus `.envs/.oracle/` and `/etc/cloudflared/` through
   SSH or another encrypted channel.
3. Copy a PostgreSQL custom-format dump and a tar archive of the media volume.
4. Build the two local images, start PostgreSQL and Redis, restore the dump and
   media, then run the full Compose stack.
5. Confirm `/healthz`, authentication, media access, Celery, and the Telegram
   webhook before retiring the old VPS.

The public hostname remains behind Cloudflare Tunnel, so a move does not
require changing frontend builds or exposing ports 80/443 on the VPS.

## Updates

Deploy one complete Git revision. Do not copy only the files mentioned by the
latest commit: a Python module can import an API introduced by an earlier
commit, and database-image changes (such as pgvector) must ship with the Django
code that uses them.

A host without a Git checkout can still receive a coherent release:

```console
git archive --format=tar.gz --output=rentium-release.tar.gz HEAD
scp rentium-release.tar.gz user@vps:/tmp/
ssh user@vps
sudo tar -xzf /tmp/rentium-release.tar.gz -C /opt/rentium/app
cd /opt/rentium/app
sudo docker compose -f docker-compose.oracle.yml build
sudo docker compose -f docker-compose.oracle.yml up -d
```

Always build the full Compose project, including PostgreSQL—not only Django and
Celery. Before considering the release complete, verify the migration gate,
paired RAMA imports, container health, and the public endpoint:

```console
sudo docker compose -f docker-compose.oracle.yml exec -T django \
  /entrypoint python manage.py migrate --check
sudo docker compose -f docker-compose.oracle.yml exec -T django \
  /entrypoint python manage.py shell -c \
  'from rentium.rama.orchestrator import classify_core_read; from rentium.rama.service import run_turn; print("RAMA imports OK")'
sudo docker compose -f docker-compose.oracle.yml ps
curl -fsS https://api.rentium.ca/healthz
```

Keep a code backup of the prior release before extraction. Never include
`.envs/.oracle/` in a release archive, and never use `down -v`; the `-v` flag
deletes production data.
