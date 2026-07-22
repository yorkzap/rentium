# Rentium — Setup & Redeploy Reference (the one file)

Everything needed to run/redeploy Rentium. Deep dives live in `GO_LIVE.md`,
`USING_RENTIUM.md`, `RAMA_GUIDE.md`, `RAMA_EVOLUTION_PLAN.md`. **This is the
single "what has to be set up" checklist.**

---

## 0. Architecture — what runs where

| Piece | Runs on | Domain |
|---|---|---|
| Backend (Django + Celery + Postgres + Redis) | **Docker** on your machine, `docker-compose.local.yml` | exposed at **api.rentium.ca** via a **Cloudflare Tunnel** (`rentium-api`) |
| Frontend (Next.js) | **Vercel** | **rentium.ca**, **www**, and **`*.rentium.ca`** (landlord showcases) |
| Email | SendGrid | — |
| Telegram | Telegram Bot API + a webhook back to api.rentium.ca | — |

So: **backend config = the Docker env file**; **frontend config = Vercel env
vars**; **redeploy frontend = push `main` (Vercel auto-builds)**.

---

## 1. Restart / redeploy — the exact commands

**Backend after a CODE change** (git pull, or edits): the code is mounted, but
long-running processes hold it in memory. The **web** (django) dev-server
auto-reloads; the **Celery worker does NOT** — so Telegram RAMA and any
background task keep running the OLD code until you restart it. After any backend
change, restart the workers (management commands reload each run, so they're
fine):
```bash
docker compose -f docker-compose.local.yml restart celeryworker celerybeat django
```
Symptom of stale workers: RAMA over Telegram ignores a new tool or repeats old
behaviour (e.g. "that feature isn't available yet") even though the code is
updated. Restarting the worker fixes it.

**Backend after an ENV change** (`.envs/.local/.django`): `restart` does NOT
reload env; plain `up -d` often won't recreate on an env-content change. Always:
```bash
docker compose -f docker-compose.local.yml up -d --force-recreate django celeryworker celerybeat
```
Verify a var actually reached the server (not just your shell):
```bash
docker compose -f docker-compose.local.yml exec django \
  python -c "from django.conf import settings; print(bool(settings.TELEGRAM_BOT_TOKEN))"
```
`True` = loaded. **`export` in a shell does not count** — that was the Telegram bug.

**Frontend:** `git push` on `main` → Vercel builds automatically. Or Vercel →
Deployments → Redeploy.

**Migrations after a pull:**
```bash
docker compose -f docker-compose.local.yml exec django python manage.py migrate
```

---

## 2. Backend env vars — `.envs/.local/.django`

One `KEY=value` per line, no `export`, no quotes. After editing → force-recreate (§1).

```
# Core
DJANGO_SECRET_KEY=<long random>
DJANGO_ALLOWED_HOSTS=api.rentium.ca,localhost,127.0.0.1
FRONTEND_URL=https://rentium.ca
PUBLIC_SITE_URL=https://rentium.ca

# Email (SendGrid)
SENDGRID_API_KEY=<key>

# Maps (address autocomplete / geocoding)
GEOAPIFY_KEY=<key>

# RAMA platform fallback keys (landlords can also BYOK in Settings)
XAI_API_KEY=<optional>
ANTHROPIC_API_KEY=<optional>
OPENAI_API_KEY=<optional>
GEMINI_API_KEY=<optional>
MISTRAL_API_KEY=<optional>

# Telegram (see §4)
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_BOT_USERNAME=Rentium_CA_Bot
TELEGRAM_WEBHOOK_SECRET=<long random>

# WhatsApp — only if/when you enable it (leave unset = safe no-op)
# WHATSAPP_TOKEN=... WHATSAPP_PHONE_NUMBER_ID=... WHATSAPP_APP_SECRET=...
```

---

## 3. DNS (Cloudflare) + Vercel domains — the subdomain fix

### Cloudflare DNS (you already have these)
- `api.rentium.ca` → Tunnel `rentium-api` (Proxied) ✓
- `rentium.ca` / `www` → Vercel ✓
- `*.rentium.ca` → CNAME to `rentium.ca`, **Proxied** ✓
- SPF + DKIM TXT ✓

### Vercel — add the wildcard domain (this is the missing step)
1. Vercel → your **rentium-frontend** project → **Settings → Domains**.
2. Click **Add**, type **`*.rentium.ca`**, Add. Vercel provisions a certificate
   for it (may take a minute). *(If it asks you to verify/point DNS, your
   `*.rentium.ca` CNAME already satisfies it.)*
3. Vercel → **Settings → Environment Variables** → add
   **`NEXT_PUBLIC_ROOT_DOMAIN`** = `rentium.ca` (Production). Save.
4. **Redeploy** (Deployments → ⋯ → Redeploy) so the new env var takes effect.

Now `https://raj.rentium.ca` loads that landlord's showcase. (Without the Vercel
wildcard domain, DNS resolves but Vercel returns "domain not configured".)

---

## 4. Telegram — full sequence

1. Telegram → **@BotFather** → `/newbot` → name + username `Rentium_CA_Bot` →
   copy the token.
2. Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`
   to `.envs/.local/.django`.
3. **Force-recreate** (§1) and **verify** the server sees the token (§1) — this
   is the step that was missing; a shell `export` doesn't reach gunicorn.
4. Register the webhook (host only; the path is appended automatically):
   ```bash
   docker compose -f docker-compose.local.yml exec django \
     python manage.py set_telegram_webhook --url https://api.rentium.ca
   ```
   Inspect: `... set_telegram_webhook --info`.
5. In the app: Settings → Channels → **Link Telegram** → send `/link <code>` to
   **@Rentium_CA_Bot**. It verifies; RAMA replies.

If `/link` is silent: the server didn't have the token/secret → repeat step 3.

---

## 5. Celery — is it running?

Yes if `docker compose -f docker-compose.local.yml ps` shows **celeryworker** and
**celerybeat** as `Up` (yours are). They drive rent-charge generation, lease
expiry, SLA checks, event replay, RAMA briefings. If either is down:
```bash
docker compose -f docker-compose.local.yml up -d celeryworker celerybeat
```

---

## 6. First run (after deploy)

1. **Wipe test data** (keeps your account):
   ```bash
   docker compose -f docker-compose.local.yml exec django \
     python manage.py wipe_landlord_data --email rajgurshersingh@gmail.com --confirm
   ```
2. **Enable RAMA:** Settings → Account & RAMA → toggle on, pick provider/model,
   paste your API key. (Optional: turn on "smarter model for the General" and
   pick any provider — needs the frontend redeployed to appear.)
3. Enter real listings/leases, or **Financial → Import** for historical data.

---

## 7. Optional / later

- **HSTS:** a security header, not required. Ignore unless you want it; then bump
  `SECURE_HSTS_SECONDS` to `31536000` in `config/settings/production.py`.
- **Registration:** `ACCOUNT_ALLOW_REGISTRATION` defaults on (anyone can sign
  up). Set off for invite-only launch.
- **WhatsApp:** unset by default (safe). Set `WHATSAPP_APP_SECRET` before enabling.

---

## 8. Troubleshooting quick table

| Symptom | Cause | Fix |
|---|---|---|
| `TELEGRAM_BOT_TOKEN is not set` after editing env file | container not recreated | `up -d --force-recreate` (§1), not `restart` |
| `/link` does nothing | server process lacks token/secret | §1 verify prints `True`, then re-register (§4) |
| `raj.rentium.ca` → not found | Vercel wildcard domain missing | add `*.rentium.ca` in Vercel Domains (§3) |
| New UI (e.g. decision-layer picker) not showing | Vercel on old commit | push `main` / redeploy |
| Webhook `set` returns 404 | wrong path | just pass `--url https://api.rentium.ca` (path auto-added) |
