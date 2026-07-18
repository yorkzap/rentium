#!/usr/bin/env python
"""RAMA gold-checklist smoke runner (no curl required).

Docker:
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_smoke.py

Host (API on :8000, need a token):
  RAMA_TOKEN=... python scripts/rama_smoke.py

Env:
  RAMA_BASE_URL   default http://127.0.0.1:8000
  RAMA_EMAIL      default rajgurshersingh@gmail.com
  RAMA_TOKEN      optional; else mint DRF token inside Django
  RAMA_FRESH=1    new conversation per question (default)
  RAMA_SLEEP=0.4  seconds between calls
  RAMA_LIMIT=0    if >0, only first N questions
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request

BASE_URL = os.environ.get("RAMA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL = os.environ.get("RAMA_EMAIL", "rajgurshersingh@gmail.com")
FRESH = os.environ.get("RAMA_FRESH", "1") == "1"
SLEEP_S = float(os.environ.get("RAMA_SLEEP", "0.4"))
LIMIT = int(os.environ.get("RAMA_LIMIT", "0") or "0")
# 1-based start index (resume after rate limit), e.g. RAMA_START=65
START = max(1, int(os.environ.get("RAMA_START", "1") or "1"))
MAX_RETRIES = int(os.environ.get("RAMA_RETRIES", "4") or "4")
RETRY_SLEEP = float(os.environ.get("RAMA_RETRY_SLEEP", "45") or "45")
# RAMA_SECTION=domain → only new-domain questions; all = full list
SECTION = (os.environ.get("RAMA_SECTION", "all") or "all").strip().lower()

# Full regression list. Use RAMA_LIMIT / RAMA_SECTION to slice.
QUESTIONS = [
    # --- A inventory & layout ---
    "How many properties / listings do I have?",
    "List every listing by name.",
    "How many complete units vs rooms?",
    "What is McKenzie Side Unit?",
    "Which listings are in McKenzie Side Unit?",
    "Are McKenzie Room D and Room E in the same household unit?",
    "Are D and E the same unit as Garden Suite?",
    "Does Garden Suite share kitchen or bathrooms with Room D and E?",
    "What street address do my listings use?",
    "What type of property is Garden Suite?",
    "What type are Room D and Room E?",
    "Is Garden Suite a condo?",
    # --- B occupancy today / time ---
    "Occupied today? Give occupied count over total.",
    "Which listings are vacant today?",
    "Which listings are occupied today?",
    "Is Room E vacant today?",
    "Is Room D vacant today?",
    "Is Garden Suite vacant today?",
    "Is Room E rented this month (July 2026)?",
    "Is Room E rented next month (August 2026)?",
    "Occupancy as of 2026-08-01 for each listing.",
    "Occupancy as of 2026-09-15 for Room E.",
    "When does Room E become occupied under its lease?",
    "When does the Room E tenant move out?",
    # --- C pushback / consistency ---
    "So Room E has no lease?",
    "Listing says Available — is Room E free with no commitment?",
    "Is Available the same as vacant with no lease?",
    "Summarize Room E in one sentence: vacant today? leased? from when to when?",
    # --- D leases ---
    "How many active leases do I have?",
    "List all leases with status, property, and lease number.",
    "Lease type / agreement name for McKenzie Room E?",
    "Lease number for McKenzie Room E?",
    "Who is on the Room E lease, rent amount, and term dates?",
    "Fixed-term or month-to-month for Room E?",
    "Security deposit and pet deposit on Room E lease?",
    "Any draft leases? Which property?",
    "Is the Room D draft lease considered rented?",
    "Bills included on the Room E lease?",
    "e-transfer email for Room E rent payments?",
    "What kind of lease would Garden Suite use if I create one?",
    "What kind of lease would a new room use?",
    # --- E money ---
    "Expected rent this month, collected this month, and deposits held?",
    "Deposits collected this month vs deposits held — both numbers.",
    "Next charge? Date, amount, property.",
    "Expenses this month? List description and amount for each line.",
    "Total expenses this month and how much has not left the bank?",
    "What are the July expenses for? Property and vendors.",
    "Expenses for Room E this month?",
    "Expenses for Room D this month?",
    "Expenses for Garden Suite this month?",
    "Has the $600 in expenses left the bank?",
    "Expenses today (calendar day)?",
    "Is there a $3125 property tax expense this month?",
    "Is expected rent this month $4850?",
    "Are deposits held $5000?",
    # --- F calendar / viewings ---
    "Any viewings coming up?",
    "Viewings on 2026-07-30?",
    "How many viewings on Thursday July 30 and for which property?",
    "Viewings on Garden Suite?",
    "Viewings on Room D?",
    "Any contractor visits scheduled?",
    # --- G attention / ops ---
    "Anything needing my attention?",
    "Open work orders?",
    "Is there a move-in condition inspection needed for Room E?",
    "Any overdue rent?",
    "Outstanding charges total?",
    # --- H comparisons / multi-hop ---
    "Compare Room D and Room E occupancy and lease status.",
    "Which listing will generate rent first and when?",
    "If a tenant asks for Room E move-in date, what do I tell them?",
    "Give me a landlord standup: listings, occupancy today, money this month, next charge, attention.",
    # --- I work orders (strong) ---
    "Any open work orders? List title, property, priority if any.",
    "Any emergency or high-priority maintenance?",
    "Work orders for McKenzie Room E?",
    # --- J inquiries ---
    "Any inquiries or listing interest messages?",
    "Any new (unreplied) inquiries?",
    "Inquiries about Garden Suite?",
    # --- K messages / threads ---
    "Any message threads with tenants?",
    "Any unread messages?",
    "List recent conversations.",
    # --- L inspections ---
    "Any condition inspections on file?",
    "Move-in inspection status for Room E?",
    "Any move-out inspections in progress?",
    # --- M move-in / move-out ---
    "Any upcoming move-ins?",
    "When is the next move-in and for which property?",
    "Any move-out requests or lease end dates coming up?",
    "Who is moving into Room E and when?",
    # --- N inventory / furniture ---
    "Any furniture or inventory recorded?",
    "What inventory is in McKenzie Room E?",
    "Any shared inventory for McKenzie Side Unit?",
    "List inventory items with condition if available.",
    # --- O charge schedule per property ---
    "Charge schedule for McKenzie Room E — upcoming rent and deposits.",
    "List all charges for Room E with due dates and amounts.",
    "Is the August 1 rent for Room E outstanding or scheduled?",
    "Charge schedule for Garden Suite?",
    # --- P tenants first-class ---
    "List my tenants with which properties they are on.",
    "Who is renting McKenzie Room E? Include email if known.",
    "Tenant history for someone (or test@test.com) — all leases.",
    "How many current tenants do I have?",
    # --- Q documents / PDFs ---
    "Any documents or PDFs uploaded on leases?",
    "Documents for McKenzie Room E / lease RMT905081-BD24?",
    "Does Room E have a signed agreement PDF on file?",
    # --- R polish: checklist / unread / docs with seed data ---
    "Show the move-in inspection checklist for Room E by section if any.",
    "Any inspection line items that need attention?",
    "Any unread messages from tenants? Who and preview.",
    "List documents and PDFs on the Room E lease including file names.",
    "Any open HIGH priority work orders? Title and property.",
    "Any new listing inquiries? Name and property.",
    # --- S write-actions (preview only — do NOT confirm=yes in smoke) ---
    "Preview creating a work order on Garden Suite titled RAMA smoke test leak — do not confirm.",
    "What would happen if I marked the demo inquiry as replied? Preview only.",
    # --- T full CRUD capabilities (preview only unless told otherwise) ---
    "What CRUD can you do for properties, leases, maintenance, and inventory? List tools and restrictions.",
    "Preview creating a room listing named Smoke Test Room at 1 Smoke St Victoria BC — do not confirm.",
    "Preview updating Garden Suite status to MAINTENANCE — do not confirm.",
    "Can you delete Garden Suite? What would block it?",
    "Preview creating a draft lease on Garden Suite starting 2026-10-01 ending 2026-12-31 rent 1200 deposit 600 — do not confirm.",
    "Can you edit an ACTIVE lease's rent fields? Why or why not?",
    "Can you delete an ACTIVE lease? What tool if any?",
    "Preview completing a work order if any is open — do not confirm.",
    "Preview adding a private inventory item 'Smoke Desk' to Garden Suite qty 1 — do not confirm.",
    "Preview adding shared inventory 'Smoke Kettle' to McKenzie Side Unit group — do not confirm.",
    # --- U smart room setup (preview / read only) ---
    "If I create a room and say it has a single bed and mattress, which tool field puts furniture into What's in it?",
    "When creating a lease at $800/mo if I omit security_deposit, what deposit do you use by default?",
    "If I said deposit $400 earlier then later said pet and cleaning deposits are 0, what is security deposit?",
    "For move-in condition inspection on possession day, do you use schedule_viewing or create_condition_inspection?",
    "Can I download a PDF for a signed lease even if document_file is empty? How?",
    "Preview bulk_add_inventory for McKenzie Room F items 'Single bed, Mattress' — do not confirm unless inventory empty and I ask.",
    "List inventory for McKenzie Room F if it exists.",
    "Does Room F lease have a downloadable PDF? Call lease_pdf_info if needed.",
]


# Domain-only subset (new tools) — RAMA_SECTION=domain
_DOMAIN_START = "Any open work orders? List title, property, priority if any."
DOMAIN_QUESTIONS = QUESTIONS[QUESTIONS.index(_DOMAIN_START) :]

# CRUD-only subset — RAMA_SECTION=crud
_CRUD_START = "What CRUD can you do for properties, leases, maintenance, and inventory? List tools and restrictions."
CRUD_QUESTIONS = QUESTIONS[QUESTIONS.index(_CRUD_START) :]


def get_token() -> str:
    if os.environ.get("RAMA_TOKEN"):
        return os.environ["RAMA_TOKEN"].strip()

    # Inside Django container / project with manage.py
    app_root = "/app" if os.path.isdir("/app") else os.path.dirname(os.path.dirname(__file__))
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    os.chdir(app_root)
    os.environ.setdefault(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgres://debug:debug@postgres:5432/rentium"),
    )
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings.local"),
    )
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from rest_framework.authtoken.models import Token

    user = get_user_model().objects.filter(email=EMAIL).first()
    if not user:
        raise SystemExit(f"No user with email {EMAIL}")
    token, _ = Token.objects.get_or_create(user=user)
    return token.key


def chat(token: str, message: str, conversation_id: str) -> dict:
    body = json.dumps(
        {"message": message, "conversation_id": conversation_id}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/rama/chat/",
        data=body,
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return {"detail": json.loads(raw), "status": exc.code}
        except Exception:
            return {"detail": raw, "status": exc.code}


def _is_rate_limited(data: dict) -> bool:
    if data.get("status") == 429:
        return True
    detail = data.get("detail")
    text = json.dumps(detail) if not isinstance(detail, str) else detail
    return "rate limit" in text.lower() or "429" in text or "quota" in text.lower()


def main() -> int:
    token = get_token()
    if SECTION in ("domain", "new", "i"):
        base_list = DOMAIN_QUESTIONS
    elif SECTION in ("crud", "write", "t"):
        base_list = CRUD_QUESTIONS
    else:
        base_list = QUESTIONS
    questions = base_list[:LIMIT] if LIMIT > 0 else list(base_list)
    # RAMA_START is 1-based index into the selected list
    start_idx = START - 1
    if start_idx > 0:
        questions = questions[start_idx:]
    conv = str(uuid.uuid4())
    total_label = len(base_list[:LIMIT] if LIMIT > 0 else base_list)

    print("=" * 60)
    print(f"RAMA smoke  base={BASE_URL}  email={EMAIL}")
    print(
        f"fresh_per_q={FRESH}  section={SECTION}  questions={len(questions)}  "
        f"start=Q{START}  retries={MAX_RETRIES}"
    )
    print("=" * 60)
    print()

    for offset, q in enumerate(questions):
        i = START + offset  # original question number
        if FRESH:
            conv = str(uuid.uuid4())
        print(f"---------- Q{i}/{total_label if START == 1 else max(total_label, i)} ----------")
        print(f"Q: {q}")
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            data = chat(token, q, conv)
            if "reply" in data:
                break
            if _is_rate_limited(data) and attempt < MAX_RETRIES:
                wait = RETRY_SLEEP * attempt
                print(
                    f"(rate limited — sleep {wait:.0f}s, "
                    f"retry {attempt}/{MAX_RETRIES})"
                )
                time.sleep(wait)
                continue
            break
        if data and "reply" in data:
            print(f"A: {data['reply']}")
            tools = data.get("tools_used") or []
            if tools:
                print(f"tools: {', '.join(tools)}")
            print(f"model: {data.get('provider')}/{data.get('model')}")
        else:
            print(f"ERROR: {json.dumps(data, indent=2)[:1500]}")
            if data and _is_rate_limited(data):
                print(
                    f"\nStopped on rate limit at Q{i}. Resume with:\n"
                    f"  RAMA_START={i} RAMA_SLEEP=2 python scripts/rama_smoke.py\n"
                )
                return 2
        print()
        time.sleep(SLEEP_S)

    print("=" * 60)
    print("Done.")
    print("Tips:")
    print("  RAMA_FRESH=0     multi-turn single conversation")
    print("  RAMA_LIMIT=7     only first 7 questions")
    print("  RAMA_START=65    resume from question 65")
    print("  RAMA_SLEEP=2     slower (rate limits)")
    print("  RAMA_RETRY_SLEEP=60  wait between 429 retries")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
