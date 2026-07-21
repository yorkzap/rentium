# Using Rentium — quickstart

A short "from zero to running" guide. Deep dives: `GO_LIVE.md` (deploy/DNS/bot),
`RAMA_GUIDE.md` (Constitution + model layers), `RAMA_EVOLUTION_PLAN.md`.

---

## 1. Wipe the test data (start fresh)

You've been testing with fake listings/leases/tenants. Clear them **without
losing your login, showcase, channels, or RAMA settings**:

```bash
# from the backend (the Django container):
# DRY RUN first — prints exactly what will be deleted, changes nothing:
python manage.py wipe_landlord_data --email you@example.com

# looks right? actually delete:
python manage.py wipe_landlord_data --email you@example.com --confirm
```

In Docker locally that's:

```bash
docker compose -f docker-compose.local.yml exec django \
  python manage.py wipe_landlord_data --email you@example.com --confirm
```

- **Deletes:** listings, leases, ledger, payments, inspections, work orders,
  occupancy, inquiries, conversations, appointments, agenda, RAMA memory, and the
  test tenant logins.
- **Keeps:** your `User` + landlord profile, your showcase page + slug, your
  Telegram/WhatsApp channel links, your RAMA settings.
- Flags: `--keep-tenants` (leave tenant logins), `--include-events` (also wipe
  notification/event history). One transaction — a PROTECT surprise rolls it all
  back, nothing half-deleted.

Now you're a clean slate to enter real data (or import it — Financial → Import).

---

## 2. Turn on RAMA

Settings → **Account & RAMA**:
1. Toggle **Enable RAMA**.
2. Pick a **Provider** + **Model** and paste that provider's **API key** (BYOK).
   Small/cheap models are fine — RAMA is built for them.
3. Save. The **Ask RAMA** button appears on your dashboard. (If you open it
   before it's configured, it now tells you to finish setup instead of hiding.)

**Optional — a smarter decision layer:** tick *"Use a smarter model for the
General"* and choose any provider/model (Claude, Grok, OpenAI, Gemini, Mistral)
+ its key. The General routes/plans with the smarter model; your main model still
does the actual work. Off = the General uses your main model.

---

## 3. What you can do in RAMA chat

- **Create / duplicate / rename listings.** "Duplicate McKenzie Room E" now
  copies its **photos and inventory** too.
- **Attach photos:** click the **paperclip**, pick photos, say "add these to
  Room E as the main photo." (The image stays on your account only.)
- **Set up tenancies:** "set up Room F, $850/mo, from Sept 1, invite
  jo@example.com." If you leave out something essential (like rent), RAMA
  **asks** instead of guessing. Inviting an email that already has an account
  **links** it automatically.
- **Ask about money, leases, deposits, maintenance, viewings.**
- **"Learn now":** if RAMA can't do something, it logs the gap; say *"learn now"*
  to prioritise it. Review the backlog in Django admin → Rama capability gaps.

RAMA always **previews risky actions and asks before running them.**

---

## 4. Notifications & your public page (operational, one-time)

- **Telegram:** the in-app "Link Telegram" only works once a bot exists. Create
  one with **@BotFather**, set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` /
  `TELEGRAM_WEBHOOK_SECRET`, run `python manage.py set_telegram_webhook`. Until
  then the screen now says "Telegram isn't set up on this server yet" instead of
  pointing at a bot that doesn't exist. (Full steps: `GO_LIVE.md` §3a.)
- **Your `slug.rentium.ca` page:** add a **wildcard DNS record** `*.rentium.ca`
  (proxied) and set `NEXT_PUBLIC_ROOT_DOMAIN=rentium.ca`. No Cloudflare Worker
  needed. (Full steps: `GO_LIVE.md` §3.)
