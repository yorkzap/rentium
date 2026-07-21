# Rentium — Go-Live Checklist

Steps to take Rentium from the test environment into real production use. Items
are grouped: **must-do before launch**, **recommended**, and **operational**.

---

## 1. Wipe the test data (keep your account)

You've been testing with fake leases/tenants/ledger entries. Clear them WITHOUT
losing your login, showcase page, channel links, or RAMA settings:

```bash
# Dry run first — prints exactly what will be deleted, changes nothing:
python manage.py wipe_landlord_data --email you@example.com

# When the counts look right, actually delete:
python manage.py wipe_landlord_data --email you@example.com --confirm
```

- Preserves: your `User` + `LandlordProfile`, `Showcase` (+ slug), Telegram/
  WhatsApp `ChannelAccount`s, `RamaPreferences` (provider/model/BYOK key).
- Deletes: leases, properties, ledger, payments, inspections, work orders,
  occupancy, inquiries, conversations, appointments, agenda, RAMA memory, and the
  test tenant logins (a tenant shared with another landlord is left alone).
- Flags: `--keep-tenants` (leave tenant logins), `--include-events` (also clear
  DomainEvent/Notification history). Runs in one transaction — a PROTECT surprise
  rolls the whole thing back.

Not DEBUG-gated, so it runs in production. It's the one destructive command that's
safe to point at a single landlord.

---

## 2. Required environment variables (must-do)

Production reads these from the environment (`config/settings/production.py`,
`base.py`). Missing ones either break the app or silently no-op a feature.

| Env var | Why it matters if unset |
|---|---|
| `DJANGO_SECRET_KEY` | Prod refuses to start without it (no default). |
| `DJANGO_ALLOWED_HOSTS` | Must include `api.rentium.ca` (and the apex) or every request 400s with `DisallowedHost`. |
| `FRONTEND_URL` | Invite / password-reset / "continue the conversation" links point at `localhost` otherwise. |
| `PUBLIC_SITE_URL` | Used to build the `<slug>.rentium.ca` subdomain URLs and email links. |
| `SENDGRID_API_KEY` | No key → no outbound email (invites, inquiries, viewing confirmations). |
| `GEOAPIFY_KEY` | Address autocomplete + geocoding silently return empty. |
| RAMA provider key (`XAI_API_KEY` by default, or the provider you set) | RAMA can't answer/act. |
| `TELEGRAM_WEBHOOK_SECRET` | The Telegram webhook's ONLY auth — unset means broken/unauthenticated. |
| `WHATSAPP_APP_SECRET` | **Before enabling WhatsApp:** the inbound webhook skips HMAC signature verification when this is blank, so a spoofed POST could drive a landlord's RAMA turn. Set it whenever you wire WhatsApp (it's a no-op seam until then). |
| `CORS_ALLOWED_ORIGINS` | Optional override; the default is now safe (localhost only in DEBUG). |

After setting hosts, run `python manage.py check --deploy` and resolve findings.

---

## 3. DNS & email deliverability (must-do)

### Landlord vanity subdomains (`raj.rentium.ca`) — the `ERR_NAME_NOT_RESOLVED` fix

The middleware already rewrites `<slug>.rentium.ca` → the showcase, but the
subdomain has to actually resolve in DNS first. **No Cloudflare Worker is
needed — just a wildcard DNS record:**

1. Cloudflare → your `rentium.ca` zone → DNS → Add record:
   - Type `CNAME`, Name `*`, Target = the same host your apex/`www` points at
     (your frontend/Vercel host), Proxy status **Proxied** (orange cloud).
   - (If the frontend is on an IP, use an `A` record with Name `*` instead.)
2. TLS: Cloudflare Universal SSL covers the first label (`*.rentium.ca`) — for
   Vercel/Netlify, also add `*.rentium.ca` as a domain in the project so it
   issues a cert and accepts the Host.
3. Set `NEXT_PUBLIC_ROOT_DOMAIN=rentium.ca` on the frontend so the editor shows
   `<slug>.rentium.ca` and it matches the backend `subdomain_url`.

After this, `raj.rentium.ca` resolves and serves the showcase.

### Email

- **SPF + DKIM** DNS records for `rentium.ca` (per your SendGrid domain auth), or
  invites and notifications land in spam.

## 3a. Connecting Telegram (the "no such bot" fix)

The Channels screen shows `Message @the Rentium bot: /link ABC123`, but
`@the Rentium bot` is a **placeholder** — it means no bot is configured yet.
There is no Rentium bot on Telegram until you create one:

1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a username
   (e.g. `RentiumOpsBot`). BotFather gives you a **token**.
2. Set env on the backend: `TELEGRAM_BOT_TOKEN=<token>`,
   `TELEGRAM_BOT_USERNAME=RentiumOpsBot` (no `@`), and a random
   `TELEGRAM_WEBHOOK_SECRET`.
3. Register the webhook: `python manage.py set_telegram_webhook`.
4. Now the Channels screen shows your real bot username; `/link <code>` to it
   verifies the channel and RAMA replies.

---

## 4. Recommended hardening

- **HSTS:** `config/settings/production.py` sets `SECURE_HSTS_SECONDS = 60` with a
  TODO. Once you've confirmed HTTPS everywhere works, raise it (e.g. `31536000`)
  and keep `SECURE_HSTS_INCLUDE_SUBDOMAINS`.
- **Registration:** `ACCOUNT_ALLOW_REGISTRATION` defaults `True` — anyone can
  self-register a landlord account. Decide whether launch should be open or
  invite-only, and set it accordingly.

---

## 5. Operational — background jobs must be running

`CELERY_BEAT_SCHEDULE` (`base.py`) drives rent-charge generation, lease expiry,
SLA-breach checks, event replay (the dropped-notification safety net), geocoding,
and RAMA briefings. These only fire if BOTH are deployed and running:

- a Celery **worker**, and
- Celery **beat**.

If beat isn't running, rent charges silently stop being generated and leases
never expire. Verify both processes are up after deploy.

---

## Quick post-deploy smoke test

1. Load `https://<slug>.rentium.ca/` → your showcase; a listing card → apex listing.
2. Submit a public inquiry → it appears in your inbox and as a lead thread.
3. Ask RAMA to set up a room without a rent → it should ASK for the rent.
4. Link Telegram from Settings → Channels → send `/link <code>` → RAMA replies.
5. `python manage.py check --deploy` is clean.
