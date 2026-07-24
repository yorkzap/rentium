# Local PostgreSQL recovery

This runbook recovers the local named PostgreSQL volume without recreating or
discarding it. Use it when PostgreSQL refuses to start because
`postmaster.pid` is corrupt (for example, the file contains only NUL bytes).
Do not use these steps for a normal, readable PID file: that usually means a
server may still own the data directory.

## Safety boundary

The data volume is `rentium_rentium_local_postgres_data` in the default local
Compose project. Resolve the actual name with `docker volume ls` if the Compose
project name differs.

1. Stop PostgreSQL and prove no running container owns the volume:

   ```bash
   docker compose -f docker-compose.local.yml stop postgres
   docker inspect --format '{{json .State}}' rentium_local_postgres
   docker ps \
     --filter volume=rentium_rentium_local_postgres_data \
     --format '{{.ID}} {{.Names}} {{.Status}}'
   ```

   The inspected state must report `Running: false`, and the filtered `docker
   ps` command must print nothing.

2. Take a filesystem backup while the database is stopped:

   ```bash
   docker run --rm \
     -v rentium_rentium_local_postgres_data:/data:ro \
     -v rentium_rentium_local_postgres_data_backups:/backups \
     alpine sh -c \
     'tar -czf /backups/postgres-volume-pre-recovery.tar.gz -C /data . &&
      gzip -t /backups/postgres-volume-pre-recovery.tar.gz'
   ```

   For repeated incidents, add an ISO timestamp to the archive name rather
   than overwriting an earlier backup.

3. Inspect `postmaster.pid` read-only. Confirm it exists, has an implausible
   size, and contains no non-NUL data:

   ```bash
   docker run --rm \
     -v rentium_rentium_local_postgres_data:/data:ro \
     alpine sh -c \
     'wc -c /data/postmaster.pid;
      od -An -t x1 /data/postmaster.pid | head'
   ```

4. Only after steps 1–3, remove that single corrupt file:

   ```bash
   docker run --rm \
     -v rentium_rentium_local_postgres_data:/data \
     alpine sh -c \
     'test -f /data/postmaster.pid &&
      test "$(tr -d "\000" < /data/postmaster.pid | wc -c)" -eq 0 &&
      rm -f /data/postmaster.pid'
   ```

   This command deliberately refuses to remove a PID file containing any
   non-NUL byte. Never remove the data directory, WAL, or the named volume.

## Recovery and verification

Recreate only the PostgreSQL container. Recreating the container clears stale
socket state in `/var/run/postgresql`; the named data volume remains attached.

```bash
docker compose -f docker-compose.local.yml up -d --force-recreate postgres
docker compose -f docker-compose.local.yml logs -f postgres
docker compose -f docker-compose.local.yml exec -T postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Wait until the log reports that the database system is ready to accept
connections. Crash-recovery redo and a checkpoint before readiness are normal.

Run a read/write transaction that rolls itself back:

```bash
docker compose -f docker-compose.local.yml exec -T postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
   -c "BEGIN; CREATE TEMP TABLE rentium_recovery_check(id integer);
       INSERT INTO rentium_recovery_check VALUES (1);
       SELECT count(*) FROM rentium_recovery_check; ROLLBACK;"'
```

Then start the complete stack:

```bash
docker compose -f docker-compose.local.yml up -d
docker compose -f docker-compose.local.yml ps -a
docker compose -f docker-compose.local.yml logs migrate django celeryworker celerybeat
```

The local Compose file gates the one-shot `migrate` service on PostgreSQL
health, then gates Django and Celery on successful migration completion.
PostgreSQL and Redis have health checks, and long-running local services use
`restart: unless-stopped`. Every Django-derived image uses `pg_isready`; raw
TCP probes are not used because they generate misleading `incomplete startup
packet` log entries.

Verify all migrations, including `ledger.0007_ledgerentry_holding`, are applied:

```bash
docker compose -f docker-compose.local.yml run --rm --no-deps django \
  python manage.py showmigrations ledger properties rama
```

Finally check the local API (`http://127.0.0.1:8000/`), the authenticated RAMA
endpoints, Celery worker registration of
`rentium.comms.tasks.handle_telegram_message`, Telegram webhook protection, and
the Cloudflare tunnel before treating recovery as complete.
