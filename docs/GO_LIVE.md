# Rentium — Go-Live Checklist

Take Rentium from testing into real use. Your setup today: the **backend runs in
Docker** and is exposed at **api.rentium.ca via a Cloudflare Tunnel**; the
**frontend is on Vercel** (rentium.ca / www). So "production env vars" means the
Docker env file, and "redeploy the frontend" means push → Vercel builds.

---

## 1. Wipe the test data (keep your account)

Clear fake listings/leases/ledger WITHOUT losing your login/showcase/channels:

```bash
# dry run — prints what it will delete, changes nothing:
docker compose -f docker-compose.local.yml exec django \
  python manage.py wipe_landlord_data --email you@example.com

# do it:
docker compose -f docker-compose.local.yml exec django \
  python manage.py wipe_landlord_data --email you@example.com --confirm
```

Deletes listings, leases, ledger (incl. reversals/settlements), payments,
inspections, work orders, occupancy, inquiries, conversations, appointments,
agenda, RAMA memory, and test tenant logins. Keeps your `User` + landlord
profile, showcase + slug, channel links, RAMA settings. One transaction. Flags:
`--keep-tenants`, `--include-events`. Not DEBUG-gated.

---

## 2. Where env vars go (the thing that bit you)

Exporting vars in an interactive shell (`export FOO=bar`) is **temporary** — it
only affects that shell, not the running gunicorn/Django process, and it's gone
on restart. Put them in the env file and **restart the containers**:

- Local Docker: edit **`.envs/.local/.django`** (one `KEY=value` per line, no
  `export`, no quotes needed).
- Then reload so every process (web + workers) picks them up:

```bash
docker compose -f docker-compose.local.yml up -d   # recreates with new env
# or: docker compose -f docker-compose.local.yml restart django celeryworker celerybeat
```

Verify a var is actually loaded by the server (not just your shell):

```bash
docker compose -f docker-compose.local.yml exec django \
  python -c "from django.conf import settings; print(bool(settings.TELEGRAM_BOT_TOKEN))"
```

### Required keys

| Env var | Why |
|---|---|
| `DJANGO_SECRET_KEY` | Required in production. |
| `DJANGO_ALLOWED_HOSTS` | Must include `api.rentium.ca`. |
| `FRONTEND_URL`, `PUBLIC_SITE_URL` | Emailed links (invites, chat, reset) use these — else they point at localhost. |
| `SENDGRID_API_KEY` | Outbound email. |
| `GEOAPIFY_KEY` | Address autocomplete + geocoding. |
| RAMA provider key(s) | Landlords BYOK in Settings; platform keys are the fallback. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` / `TELEGRAM_WEBHOOK_SECRET` | Telegram (see §4). |

---

## 3. The subdomain `raj.rentium.ca` (Cloudflare + Vercel)

DNS is only half of it. You've already added the wildcard DNS record
(`*.rentium.ca` CNAME, proxied) — good. The missing piece is **Vercel must be
told to accept the wildcard host**:

1. **Vercel → your project → Settings → Domains → Add** `*.rentium.ca`. Vercel
   issues the cert and starts accepting `<anything>.rentium.ca`. Without this,
   the subdomain resolves but Vercel returns a 404/misdirected-request.
2. Set **`NEXT_PUBLIC_ROOT_DOMAIN=rentium.ca`** in Vercel → Environment
   Variables, then redeploy, so the middleware and the settings screen use the
   right root domain.
3. Keep the apex/`www` records as they are (Vercel DNS).

After that, `raj.rentium.ca` loads the showcase.

### Email deliverability
Your DKIM (`cf2024-1._domainkey`) and SPF TXT records are already present — good.
Make sure SendGrid domain auth matches them.

---

## 4. Telegram — the exact fix for "the bot does nothing"

Two separate things must be true, and the second is what bit you:

**A) A real bot exists and the webhook is registered.**
1. Telegram → **@BotFather** → `/newbot` → name + username (yours:
   `Rentium_CA_Bot`) → copy the **token**.
2. Put in `.envs/.local/.django`:
   ```
   TELEGRAM_BOT_TOKEN=8700021820:AA...        # from BotFather
   TELEGRAM_BOT_USERNAME=Rentium_CA_Bot        # no @
   TELEGRAM_WEBHOOK_SECRET=<any long random string>
   ```
3. **Restart the containers** (§2) so the web process has them.
4. Register the webhook (the path is added for you now — just give the host):
   ```bash
   docker compose -f docker-compose.local.yml exec django \
     python manage.py set_telegram_webhook --url https://api.rentium.ca
   ```
   Check it: `... set_telegram_webhook --info`.

**B) The SERVER process (not your shell) has the token + secret.**
This is why your `/link 4904F1` did nothing: you'd `export`ed the vars in one
interactive shell, but the gunicorn process answering `api.rentium.ca` didn't
have them — so it couldn't verify Telegram's secret header or call the bot back.
Fixing §2 (env file + restart) fixes this. Confirm with the verify command in §2.

Then in the app: Settings → Channels → **Link Telegram** → message
`/link <code>` to **@Rentium_CA_Bot** → it verifies and RAMA replies.

---

## 5. Background jobs (Celery) — how to verify

Rent charges, lease expiry, SLA checks, event replay, and RAMA briefings run on
Celery. Two processes must be up: a **worker** and **beat**. Check:

```bash
docker compose -f docker-compose.local.yml ps
# expect celeryworker AND celerybeat = "Up"
```

If either is missing/exited: `docker compose -f docker-compose.local.yml up -d
celeryworker celerybeat`. If beat isn't running, rent charges silently stop
generating.

---

## 6. HSTS (optional — skip unless you care)

HSTS is a browser security header that says "only ever use HTTPS for this
domain." It's **hardening, not required to function** — everything works without
touching it. If you want it later: in `config/settings/production.py` raise
`SECURE_HSTS_SECONDS` from `60` to `31536000` (1 year) once you're sure HTTPS
works everywhere. That's the whole task. Cloudflare already serves HTTPS, so
you can safely ignore this for now.

---

## 7. Deploy the latest frontend (for the layered-AI picker etc.)

Your Vercel deploy is on an older commit. New UI — the **decision-layer model
picker**, the RAMA setup prompt, the clearer Telegram state — only appears after
the frontend redeploys. **Push `main`** (or Vercel → Deployments → Redeploy the
latest commit). Vercel auto-builds on push.

---

## Post-deploy smoke test
1. `docker compose ... exec django python -c "from django.conf import settings; print(bool(settings.TELEGRAM_BOT_TOKEN))"` → `True`.
2. `raj.rentium.ca` → your showcase.
3. Settings → Account & RAMA shows the "smarter model for the General" toggle.
4. `/link <code>` to your bot → RAMA replies.
5. `docker compose ... ps` → celeryworker + celerybeat up.
