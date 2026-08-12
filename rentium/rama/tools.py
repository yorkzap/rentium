"""
RAMA's tool surface: typed, side-effect-free read functions over the same
service layer the dashboard uses. The model's job is to pick a function and
fill its arguments; every number, date, and record comes from here.

Contract for every function:
- First parameter is `landlord`, injected by the registry from the
  authenticated session — never from model output. Cross-landlord
  retrieval is therefore impossible at this layer.
- Read-only. No writes, no domain events, no emails.
- Returns a JSON-serializable dict. Errors come back as {"error": ...}
  so the model can explain them instead of the request crashing.
- The docstring IS the tool description the model sees — write it for
  the model.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


def _params(**docs: str):
    """Attach per-parameter descriptions to a tool function.

    The registry's schema builder reads ``fn.param_docs`` and emits a
    ``description`` alongside each argument's ``type``. Use this on the tools
    where a weak model needs to be told what an argument DOES (not just its
    name) — most importantly that a value SETS/renames rather than looks up.
    """

    def deco(fn):
        fn.param_docs = docs
        return fn

    return deco


def _month_bounds(month: str):
    """Parse 'YYYY-MM' (empty = current month) into [start, end)."""
    from .union import _month_bounds as bounds

    if not month:
        return bounds(date.today())
    try:
        year, mon = month.split("-")
        return bounds(date(int(year), int(mon), 1))
    except (ValueError, TypeError):
        raise ValueError(f"month must look like 2026-07, got {month!r}")


def portfolio_snapshot(landlord) -> dict:
    """Dashboard ground truth + full portfolio. ALWAYS call this (or re-call it)
    for totals — never invent numbers from memory.

    dashboard_truth has the exact figures the UI shows: total_listings, occupied
    today, expected/collected this month, deposits_held, expenses, next_charge.
    layout.groups vs layout.standalone_units explains shared rooms vs garden suite.
    rented_listings has lease_number, agreement_type, vacant_today.
    upcoming_appointments has viewings with date/weekday/time_display.

    TIME-AWARE: vacant_today vs leased_future; listing Available ≠ vacant of lease.
    MONEY: copy expense_lines descriptions/amounts exactly (never invent Hydro)."""
    from .union import state_of_the_union

    return state_of_the_union(landlord)


def list_properties(landlord) -> dict:
    """Physical holdings and their rental listings, with type, layout and occupancy.

    Use counts.physical_holdings for houses/buildings/addresses and
    counts.total_listings for rentable rooms/units. Never call each listing a
    separate physical property. A complete unit also includes recorded bedroom,
    bathroom and internal-area counts; unknown facts remain null."""
    from .union import property_inventory

    return property_inventory(landlord)


def occupancy_as_of(landlord, on_date: str = "") -> dict:
    """Occupancy of every listing on one calendar day (YYYY-MM-DD). Empty = today.
    Use for 'next month', 'in August', 'on 2026-08-01'. When user says next month
    and today is July, pass on_date=YYYY-08-01 (August), not July."""
    from .union import occupancy_as_of as _fn

    return _fn(landlord, on_date=(on_date or "").strip())


def list_leases(landlord, include_ended: str = "") -> dict:
    """List leases for this landlord (active and pending signatures by default).
    Each row has occupancy_phase, vacant_today, start/end dates, tenants, and a
    plain-language summary. Pass include_ended as '1' or 'true' to also include
    expired/terminated/renewed leases."""
    from .union import list_leases as _list

    flag = str(include_ended or "").strip().lower() in ("1", "true", "yes", "y")
    return _list(landlord, include_ended=flag)


@_params(
    entity="Optional. Leave empty for the index of all entities (their relations "
           "and what each can total/group by). Pass one entity name, e.g. "
           "'lease', to get that entity's full field list.",
)
def data_catalogue(landlord, entity: str = "") -> dict:
    """What `read` can query. Call with no argument for the index; call again with
    one entity name for its fields.

    The index shows each entity's RELATIONS — that is how you see two entities can
    be queried together. `ledger_entry` relates to `lease`, so a question about
    August move-ins is answered from the ledger in ONE call with
    filters='lease__start_date=2026-08-01..2026-08-31', group_by='lease' — never
    by reading the ledger once per lease."""
    from .manifest import entity_catalogue
    return {"entities": entity_catalogue(entity)}


def search_capabilities(landlord, query: str = "") -> dict:
    """Find the RAMA operations relevant to a request in business language.
    Call this when the right operation is not in the initially offered tool
    set. Returns capability keys, descriptions, risk, and confirmation policy;
    it never changes data."""
    from .capabilities import search_capability_catalogue

    return {
        "query": query,
        "capabilities": search_capability_catalogue(query, limit=12),
    }


@_params(
    entity="What to query: lease, property, lease_tenant, work_order, inquiry, "
           "appointment, ledger_entry, inspection, inventory, conversation, "
           "property_group, business_document, lease_form. "
           "Call data_catalogue to see each one's fields.",
    filters="Comma list of 'field OP value'. OP is one of = > < >= <= ~ (contains) "
            "!= . Also 'field is empty' / 'field is set', and ranges with "
            "'field=2026-08-01..2026-08-31'. You may filter on a RELATED "
            "entity's field with 'rel__field', e.g. "
            "'lease__start_date=2026-08-01..2026-08-31'. "
            "Example: 'status=active, total_rent>800, parking_included=true'.",
    fields="Optional comma list of fields to return. Default: all of them.",
    limit="Max rows to return, 1-100 (default 20). Does NOT limit totals.",
    aggregate="Comma list of 'count' or 'FUNC:field' with FUNC in "
              "sum/avg/min/max/count. Example: 'count, sum:amount'. "
              "Computed over EVERY matching row, not just the returned page.",
    group_by="Up to two keys: a groupable field, a RELATION name (groups by its "
             "human label — 'lease' gives lease numbers, not ids), or "
             "month:<date field> / year:<date field>. "
             "Example: 'charge_state', 'lease', or 'month:due_date'.",
    order_by="One field, '-' for descending. May also name an aggregate you "
             "asked for, e.g. '-sum_amount'.",
    month="'YYYY-MM', or 'this' / 'last'. Narrows to that month on the entity's "
          "date field. Prefer this over writing two date filters by hand.",
    year="'YYYY'. Same idea as month, for a whole year.",
    between="'YYYY-MM-DD..YYYY-MM-DD' inclusive, on the entity's date field.",
)
def read(landlord, entity: str = "", filters: str = "", fields: str = "",
         limit: str = "20", aggregate: str = "", group_by: str = "",
         order_by: str = "", month: str = "", year: str = "",
         between: str = "") -> dict:
    """Query or TOTAL any catalogued entity — the general way to answer a
    specific question, including "how many / how much / which are still owed".
    Prefer this over guessing; it is always available and always scoped to your
    own portfolio, read-only.

    COUNTING AND TOTALLING: pass `aggregate` and/or `group_by` and you get
    `totals` (and `groups`) computed across EVERY matching row — not just the
    page `limit` returns. Use it instead of listing rows and counting them.

    ACROSS ENTITIES: filter and group on a related entity's fields with
    `rel__field`. Call data_catalogue to see each entity's relations. Do this
    instead of looping — one query per row will exhaust the turn before it
    answers.

    "EVERYONE" / "ALL" / "ANYONE" QUESTIONS NEED TWO READS. A group_by can only
    show rows that EXIST, so grouping deposits by lease silently omits every
    lease that has no deposit charge at all — and those are usually the answer.
    Read the parent set first, then the child totals, and name the gap:
      1. read(entity='lease', filters='start_date=2026-08-01..2026-08-31')
      2. read(entity='ledger_entry', filters='entry_type=DEPOSIT_CHARGE,
         lease__start_date=2026-08-01..2026-08-31', group_by='lease', …)
      3. Any lease in (1) missing from (2) has nothing recorded — say so
         explicitly rather than leaving it out of the list.

    Deposits from everyone moving in during August, per lease:
      entity='ledger_entry',
      filters='entry_type=DEPOSIT_CHARGE, lease__start_date=2026-08-01..2026-08-31',
      group_by='lease', aggregate='count, sum:amount, sum:settled_amount, sum:outstanding'
    Rents received vs still owed in August:
      entity='ledger_entry', filters='entry_type=RENT_CHARGE', month='2026-08',
      group_by='charge_state', aggregate='count, sum:amount, sum:outstanding'
    Arrears, worst first:
      entity='ledger_entry', filters='charge_state=OVERDUE',
      order_by='due_date', aggregate='count, sum:outstanding'
    Spending by category this year:
      entity='ledger_entry', filters='entry_type=EXPENSE', year='2026',
      group_by='category', aggregate='sum:amount'
    Open urgent work:
      entity='work_order', filters='status=open, priority=urgent'

    MONEY, READ THIS BEFORE QUOTING A FIGURE:
    - Whether a charge is paid is `charge_state`
      (PAID / PARTIALLY_PAID / OVERDUE / DUE / SCHEDULED), NOT `paid_on`.
      `paid_on` is a bank-clearing date that only expenses ever carry, so a
      rent charge's is always empty — grouping rents by it reports every rent
      as unpaid.
    - "Collected this month" is NOT sum:amount over charges due this month.
      Money received in August against a July charge counts as August. Use
      `month_money` for collected/expected/net; this tool totals CHARGES.
    - Deposits are refundable liabilities, not income. Filter
      `entry_type=RENT_CHARGE`, or group by entry_type, rather than summing
      every charge together.
    - "Has a deposit been received?" is answered from the LEDGER —
      `entry_type=DEPOSIT_CHARGE` with `charge_state` / `settled_amount`. The
      lease also carries `security_deposit_received_date` and friends, but
      those are the lease paperwork's record, not the money, and they are
      known to disagree with the ledger. Never quote one as a payment.
    - A FEE_CHARGE with `is_damage_claim=true` is contested damage recovery,
      not expected income — exclude it with `is_damage_claim!=true` when
      reporting what you expect to collect.
    - Voided/reversed entries are already excluded; the reply says so.
    """
    from .domain_read import read as _fn
    return _fn(
        landlord, entity=entity, filters=filters, fields=fields, limit=limit,
        aggregate=aggregate, group_by=group_by, order_by=order_by,
        month=month, year=year, between=between,
    )


def link(landlord, entity: str = "", query: str = "") -> dict:
    """A canonical clickable Rentium LINK to a dashboard collection or one thing.
    Fixed collections: dashboard, properties, property_groups, documents, leases,
    finances, maintenance, settings (query is omitted for these). Use
    whenever the landlord asks to SEE / OPEN / DOWNLOAD / 'send me' / 'give me a
    link to' a lease, property, or property group. entity = 'lease' | 'property' |
    'property_group' for one record; query identifies it (lease number, property
    name/address, group name). Never refuse with 'I can't give a link' — use this
    and paste the returned URL. If several entities match, it returns choices."""
    from .domain_read import link as _fn
    return _fn(landlord, entity=entity, query=query)


def update(landlord, entity: str = "", query: str = "", changes: str = "",
           confirm: str = "") -> dict:
    """Edit fields on ONE catalogued thing — use for a field the bespoke update_*
    tools don't cover (e.g. a lease's parking_included, rent_due_day, landlord
    notice phone/email; a property's neighbourhood, availability, visibility).
    entity = 'lease' or 'property'; query identifies it (lease number / property
    name); changes = comma list 'field=value, …' (see data_catalogue for editable
    fields). Locked/active leases are refused. Previews first; confirm=yes to
    apply. Example: entity='lease', query='RMT…', changes='parking_included=true,
    rent_due_day=1'. For property type corrections, property_category=COMPLETE_UNIT
    is canonical; listing_type/property_type/category aliases are also accepted
    and safely routed through update_property."""
    from .domain_write import update as _fn
    return _fn(landlord, entity=entity, query=query, changes=changes, confirm=confirm)


def deliver_lease_pdf(landlord, lease_number: str = "",
                      property_query: str = "") -> dict:
    """Send the lease's signed PDF as an ACTUAL FILE attachment (use on a
    messaging channel like Telegram when the landlord asks to download/get the
    PDF — do NOT paste an /api/ URL). Identify by lease_number or property_query."""
    from .domain_actions import deliver_lease_pdf as _fn
    return _fn(landlord, lease_number=lease_number, property_query=property_query)


def deliver_property_photos(landlord, property_query: str = "") -> dict:
    """Send a property's photos as ACTUAL images (use on Telegram when the
    landlord asks to see/show a specific listing's photos). Identify by name."""
    from .domain_actions import deliver_property_photos as _fn
    return _fn(landlord, property_query=property_query)


def open_lease(landlord, property_query: str = "", lease_number: str = "") -> dict:
    """A clickable in-app LINK to a lease. Use whenever the landlord asks to SEE /
    OPEN / DOWNLOAD / 'send me' a lease or its PDF — give them the returned link
    (opening it lets them click Download PDF). Identify the lease by lease_number
    or property_query (e.g. Room C)."""
    from .domain_actions import open_lease as _fn
    return _fn(landlord, property_query=property_query, lease_number=lease_number)


def open_property(landlord, property_query: str = "") -> dict:
    """A clickable in-app LINK to a property's full listing (details + photos).
    Use whenever the landlord asks to SEE / OPEN / 'show me' / 'send me' a property
    or its photos/metadata — give them the returned link. Identify by name/address
    (e.g. Room C)."""
    from .domain_actions import open_property as _fn
    return _fn(landlord, property_query=property_query)


def public_property_link(landlord, property_query: str = "") -> dict:
    """Return the canonical logged-out public URL for a listing and whether it
    is currently live. Use specifically when the landlord says public link,
    rental link, applicant link, or the URL a prospect should open."""
    from .domain_actions import public_property_link as _fn

    return _fn(landlord, property_query=property_query)


def list_appointments(landlord, day: str = "", days_ahead: str = "60") -> dict:
    """List viewings, showings, and contractor appointments. Optional day as
    YYYY-MM-DD (e.g. 2026-07-30) filters that calendar day; otherwise returns
    upcoming appointments for days_ahead days (default 60).

    Use for "any viewings?", "what's on Thursday?", "showings on July 30".
    Each row has property, status (SCHEDULED/REQUESTED/…), date, weekday,
    time_local, and kind (VIEWING, etc.). Never invent appointments — if the
    list is empty for that day, say none are in the system."""
    from .union import list_appointments as _list

    try:
        ahead = int(str(days_ahead or "60").strip() or "60")
    except ValueError:
        ahead = 60
    return _list(landlord, day=(day or "").strip(), days_ahead=ahead)


def attention_items(landlord) -> dict:
    """Everything that currently needs the landlord's attention, most urgent
    first: missing or undelivered condition inspections, stalled lease
    signatures, expiring fixed terms, overdue rent, and new or SLA-breached
    maintenance requests."""
    from rentium.attention.service import compute_attention

    return {"items": [item.as_dict() for item in compute_attention(landlord)]}


def resolve_person(landlord, name: str) -> dict:
    """Find tenants in this landlord's portfolio by partial name or email.
    Returns candidates with their lease context. If more than one candidate
    matches, ask the user which one they mean — never guess between
    people."""
    from django.db.models import Q

    from rentium.leases.models import LeaseTenant

    hits = (
        LeaseTenant.objects.filter(lease__landlord=landlord)
        .filter(
            Q(invited_name__icontains=name)
            | Q(invited_email__icontains=name)
            | Q(tenant__user__name__icontains=name),
        )
        .select_related("tenant__user", "lease", "lease__property", "lease__group")
        .order_by("-lease__start_date")[:10]
    )
    candidates = []
    for lt in hits:
        place = (
            lt.lease.property.name
            if lt.lease.property
            else (lt.lease.group.name if lt.lease.group else "")
        )
        email = lt.invited_email or (
            lt.tenant.user.email if lt.tenant_id else ""
        )
        candidates.append(
            {
                "name": lt.display_name,
                "email": email,
                "lease_id": str(lt.lease_id),
                "lease_status": lt.lease.status,
                "property": place,
                "is_primary_tenant": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
            },
        )
    return {"query": name, "candidates": candidates}


def lease_state(landlord, lease_id: str) -> dict:
    """Full state of one lease: lease_number, agreement_type (Standard Roommate
    Agreement / RTB-1), term_shape, dates, rent, deposits, tenants, bills_included,
    etransfer_email, occupancy. Get lease_id from list_properties / list_leases /
    portfolio_snapshot. 'Lease type?' → agreement_type. 'Lease number?' → lease_number."""
    from django.core.exceptions import ValidationError

    from rentium.leases.models import Lease

    from .union import _serialize_lease_brief

    try:
        lease = (
            Lease.objects.select_related("property", "group")
            .prefetch_related("lease_tenants__tenant__user")
            .get(pk=lease_id, landlord=landlord)
        )
    except (Lease.DoesNotExist, ValidationError, ValueError):
        return {"error": f"No lease {lease_id!r} in this portfolio."}

    today = date.today()
    brief = _serialize_lease_brief(lease, today)
    place = brief["property"]
    listing_status = lease.property.status if lease.property_id else None
    bills = lease.bills_included or {}
    bills_summary = []
    if isinstance(bills, dict):
        for key, details in bills.items():
            if not isinstance(details, dict):
                continue
            bills_summary.append(
                {
                    "bill": key,
                    "included_in_rent": bool(details.get("included")),
                    "provider": details.get("provider") or "",
                },
            )
    try:
        etransfer = lease.get_effective_etransfer_email() or ""
    except Exception:  # noqa: BLE001
        etransfer = lease.etransfer_email or ""
    return {
        **brief,
        "property": place,
        "listing_status": listing_status,
        "security_deposit": str(lease.security_deposit),
        "pet_deposit": str(lease.pet_deposit),
        "bills_included": bills,
        "bills_summary": bills_summary,
        "etransfer_email": etransfer,
        "tenants": [
            {
                "name": lt.display_name,
                "is_primary": lt.is_primary_tenant,
                "has_signed": lt.has_signed,
                "declined": lt.declined,
            }
            for lt in lease.lease_tenants.all()
        ],
        "note": (
            "lease_number is the human id (e.g. RMT…). agreement_type is the form. "
            "term_shape is fixed_term vs month_to_month. bills_summary answers "
            "'bills included?'. etransfer_email answers landlord e-transfer destination."
        ),
    }


def charge_status(landlord, lease_id: str, month: str = "") -> dict:
    """Charges on one lease for one month (format 2026-07; empty = current
    month) and how much of each has been paid. Answers "did X pay rent this
    month?" — call resolve_person first to get the lease_id."""
    from django.core.exceptions import ValidationError

    from rentium.ledger.models import CHARGE_TYPES
    from rentium.ledger.models import LedgerEntry

    try:
        start, end = _month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        charges = list(
            LedgerEntry.objects.with_settlement()
            .filter(
                landlord=landlord,
                lease_id=lease_id,
                entry_type__in=CHARGE_TYPES,
                reversed_by__isnull=True,
                due_date__gte=start,
                due_date__lt=end,
            )
            .order_by("due_date", "created_at"),
        )
    except (ValidationError, ValueError):
        return {"error": f"lease_id {lease_id!r} is not a valid id."}

    today = date.today()
    rows = []
    for charge in charges:
        outstanding = charge.outstanding
        if outstanding <= 0:
            status = "paid"
        elif charge.settled_amount > 0:
            status = "partially_paid"
        else:
            status = "unpaid"
        due_now = (
            outstanding
            if outstanding > 0 and charge.due_date and charge.due_date <= today
            else Decimal("0.00")
        )
        rows.append(
            {
                "description": charge.description,
                "type": charge.entry_type,
                "amount": str(charge.amount),
                "due_date": charge.due_date.isoformat(),
                "paid": str(charge.settled_amount),
                # Balance on this line, whatever the due date.
                "outstanding": str(outstanding),
                "balance_on_charge": str(outstanding),
                # Money actually owed today. Carried here so answering "what do
                # they owe now?" never requires switching to charge_schedule,
                # where the same question used to be answered by a key that
                # meant something else.
                "due_now": str(due_now),
                "status": status,
                "overdue": bool(outstanding > 0 and charge.due_date < today),
            },
        )
    return {
        "lease_id": str(lease_id),
        "month": start.strftime("%Y-%m"),
        "charges": rows,
        "note": (
            "outstanding / balance_on_charge is the unpaid balance on the line. "
            "due_now is only what is owed as of today. A future scheduled charge "
            "has a balance but due_now=0.00 — it is not owed yet."
        ),
    }


def month_money(landlord, month: str = "") -> dict:
    """Money for one month (format YYYY-MM e.g. 2026-07; empty = current month
    from as_of / today): expected vs collected income, expense total, expense
    line items, amount not yet taken from bank, net, deposits. Always use the
    year returned in 'month' / 'label' — never invent 2025 when as_of is 2026."""
    from .union import month_money as compute

    try:
        start, end = _month_bounds(month)
    except ValueError as exc:
        return {"error": str(exc)}
    return compute(landlord, start, end)


def list_expenses(
    landlord,
    month: str = "",
    day: str = "",
    property_query: str = "",
    amount: str = "",
) -> dict:
    """List expense ledger lines. Optional month (YYYY-MM), day (YYYY-MM-DD for
    'today'), property_query (e.g. 'Room E'), amount (e.g. '600').

    Use for "expenses today", "expenses for room E", "$600 expense", "utilities
    this month". If day has zero lines, still read this_month_expenses — bills
    often use period end dates, not today. bank_status NOT_YET_TAKEN = not left bank."""
    from .union import list_expenses as _list

    return _list(
        landlord,
        month=(month or "").strip(),
        day=(day or "").strip(),
        property_query=(property_query or "").strip(),
        amount=(amount or "").strip(),
    )


def deposits_summary(landlord) -> dict:
    """Total security/pet deposits currently held as a refundable liability
    (payments received on deposit charges, minus deposits returned)."""
    from rentium.ledger import services

    return {"deposits_held": str(services.deposits_held(landlord))}


def next_charge(landlord) -> dict:
    """The earliest not-yet-settled charge with a future due date across the
    portfolio, or null if the billing calendar is empty."""
    from rentium.ledger import services

    return {"next_charge": services.next_upcoming_charge(landlord)}


def open_work_orders(landlord) -> dict:
    """Open maintenance work orders only (not completed/cancelled). Prefer
    list_work_orders for full detail (category, contractor, SLA breach, costs)."""
    from .domain_reads import list_work_orders

    return list_work_orders(landlord, include_closed=False)


def list_work_orders(
    landlord,
    include_closed: str = "",
    property_query: str = "",
    status: str = "",
    priority: str = "",
) -> dict:
    """Strong work-order list for UI-style and natural questions.

    include_closed: '1' to include COMPLETED/CANCELLED. property_query filters
    by property name or title (e.g. 'Room E', 'plumbing'). status/priority
    optional (NEW, IN_PROGRESS, HIGH, EMERGENCY…).

    Returns title, property, area, status, priority, category, origin, SLA,
    contractor, cost, is_rta_emergency. Empty list = no work orders — say so."""
    from .domain_reads import list_work_orders as _fn

    return _fn(
        landlord,
        include_closed=str(include_closed or "").strip().lower()
        in ("1", "true", "yes", "y"),
        property_query=(property_query or "").strip(),
        status=(status or "").strip(),
        priority=(priority or "").strip(),
    )


def list_inquiries(
    landlord,
    status: str = "",
    property_query: str = "",
    include_archived: str = "",
) -> dict:
    """Public listing interest inquiries (name, email, phone, message, move-in
    target, status NEW/REPLIED). Not the same as signed lease applications.

    Use for 'any inquiries?', 'who messaged about Garden Suite?', 'new leads'.
    If empty, say none in the system."""
    from .domain_reads import list_inquiries as _fn

    return _fn(
        landlord,
        status=(status or "").strip(),
        property_query=(property_query or "").strip(),
        include_archived=str(include_archived or "").strip().lower()
        in ("1", "true", "yes"),
    )


def list_conversations(landlord, tenant_query: str = "") -> dict:
    """Message threads between this landlord and tenants. Each has subject,
    tenant name/email, property, unread count, last message preview.

    Use for 'any messages?', 'threads with someone'. Then list_messages for
    full bodies."""
    from .domain_reads import list_conversations as _fn

    return _fn(landlord, tenant_query=(tenant_query or "").strip())


def list_messages(
    landlord,
    conversation_id: str = "",
    tenant_query: str = "",
) -> dict:
    """Messages in one conversation_id, or recent messages if no id.
    Bodies truncated; sender is landlord or tenant. Read-only."""
    from .domain_reads import list_messages as _fn

    return _fn(
        landlord,
        conversation_id=(conversation_id or "").strip(),
        tenant_query=(tenant_query or "").strip(),
    )


def list_inspections(
    landlord,
    property_query: str = "",
    status: str = "",
    include_items: str = "1",
) -> dict:
    """Condition inspections (RTB-27 style): status, dates, signatures, plus
    checklist_by_section (per-room line items with move-in condition codes).

    property_query e.g. 'Room E'. include_items default on. Use for
    'move-in checklist', 'inspection items Bedroom', attention beyond flags."""
    from .domain_reads import list_inspections as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        status=(status or "").strip(),
        include_items=str(include_items or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def list_move_events(
    landlord,
    days_ahead: str = "120",
    days_past: str = "30",
    property_query: str = "",
) -> dict:
    """Move-ins (lease start dates) and move-outs (lease end + move-out requests).

    Use for 'when is move-in for Room E?', 'any move-outs coming?',
    'who's moving in August?'. Room E lease start 2026-08-01 is a move-in."""
    from .domain_reads import list_move_events as _fn

    try:
        ahead = int(str(days_ahead or "120").strip() or "120")
    except ValueError:
        ahead = 120
    try:
        past = int(str(days_past or "30").strip() or "30")
    except ValueError:
        past = 30
    return _fn(
        landlord,
        days_ahead=ahead,
        days_past=past,
        property_query=(property_query or "").strip(),
    )


def list_inventory(landlord, property_query: str = "", include_shared: str = "1") -> dict:
    """Furniture and inventory: private items per room/unit + shared items for
    household groups (kitchen etc.). name, qty, condition, location, property/group.

    Use for 'what's in Room E?', 'inventory for Side Unit', 'any furniture listed?'."""
    from .domain_reads import list_inventory as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        include_shared=str(include_shared or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def charge_schedule(
    landlord,
    property_query: str = "",
    month: str = "",
    include_paid: str = "1",
) -> dict:
    """Charge schedule for a property or whole portfolio: rent, deposits, fees
    with due_date, amount, paid, outstanding, status (scheduled/unpaid/paid).

    Empty month = this month through ~6 months ahead. month=YYYY-MM narrows.
    property_query e.g. 'Room E'. Scheduled future rent is NOT portfolio
    outstanding until due."""
    from .domain_reads import charge_schedule as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        month=(month or "").strip(),
        include_paid=str(include_paid or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def list_tenants(
    landlord,
    query: str = "",
    include_past: str = "1",
) -> dict:
    """Tenants as people with full lease history across properties.

    Each person: name, email, leases[] with property, dates, status, signed.
    Use for 'who is on Room E?', 'tenant history for someone@…', 'list tenants'."""
    from .domain_reads import list_tenants as _fn

    return _fn(
        landlord,
        query=(query or "").strip(),
        include_past=str(include_past or "1").strip().lower()
        not in ("0", "false", "no"),
    )


def tenant_history(landlord, query: str) -> dict:
    """Deep history for one tenant by name or email (all leases over time).
    If multiple people match, list them — do not merge different people."""
    from .domain_reads import tenant_history as _fn

    return _fn(landlord, query=(query or "").strip())


def list_documents(
    landlord,
    property_query: str = "",
    lease_id: str = "",
) -> dict:
    """Documents/PDFs metadata: lease attachments (title, file name) and whether
    lease agreement PDFs exist. Does NOT return file contents.

    Use for 'any documents on Room E?', 'PDFs for lease RMT…', 'uploaded files'."""
    from .domain_reads import list_documents as _fn

    return _fn(
        landlord,
        property_query=(property_query or "").strip(),
        lease_id=(lease_id or "").strip(),
    )


@_params(
    document_id="Business document UUID if already prepared/OCR'd. Pass this OR "
    "attachment_id/upload_id.",
    attachment_id="Exact file UUID from a RAMA attachment batch. Preferred for "
    "new chat uploads; pass this OR document_id/upload_id.",
    upload_id="Staged photo UUID when the attachment is photographed mail, receipt, "
    "invoice, notice, or other paperwork. Pass this OR document_id.",
    scope_query="Physical holding street address e.g. '950 McKenzie Ave'. "
    "OPTIONAL on the first call — omit to OCR/hash first and detect duplicates. "
    "Only required to file a NEW unscoped document.",
    issuer="Optional sender/issuer, e.g. Scotiabank.",
    document_date="Optional document/received date in YYYY-MM-DD.",
    confirm="Leave empty to preview; pass yes only after landlord approval.",
)
def catalog_business_document(
    landlord,
    scope_query: str = "",
    document_id: str = "",
    attachment_id: str = "",
    upload_id: str = "",
    issuer: str = "",
    document_date: str = "",
    confirm: str = "",
) -> dict:
    """File a chat PDF/photo as a business document at a PHYSICAL HOLDING.

    Correct order (ALWAYS):
    1) Call with attachment_id or upload_id and NO scope_query first — this
       hashes the file, OCRs it, and returns either already_done (duplicate
       of a document already in the inbox) or intelligence + needs_input for
       the address.
    2) Only if needs_input: ask the address, then call again with
       document_id + scope_query (preview, then confirm=yes).
    3) For expenses: file_business_document after catalog (paid/unpaid).

    NEVER ask for the address before step 1. NEVER re-catalog a duplicate.
    Amounts come from intelligence — never invent figures."""
    from .document_services import catalog_batch_attachment_as_document
    from .document_services import catalog_document_scope
    from .document_services import catalog_staged_photo_as_document

    parsed_date = None
    if document_date.strip():
        try:
            parsed_date = date.fromisoformat(document_date.strip())
        except ValueError:
            return {"error": "document_date must be YYYY-MM-DD."}
    common = {
        "landlord": landlord,
        "scope_query": (scope_query or "").strip(),
        "actor": getattr(landlord, "user", None),
        "issuer": issuer,
        "document_date": parsed_date,
        "confirm": str(confirm).strip().lower() in {"yes", "y", "true", "1"},
    }
    aid = (attachment_id or "").strip()
    uid = (upload_id or "").strip()
    did = (document_id or "").strip()

    # Telegram photos are staged as RamaUpload (upload_id=…). Weak models (and
    # confused tool arg binding) often put that UUID in attachment_id. Resolve
    # by what actually exists for this landlord — never invent "file gone".
    if aid or uid or did:
        from .models import RamaAttachment
        from .models import RamaDocument
        from .models import RamaUpload

        candidates = [x for x in (aid, uid, did) if x]
        resolved_aid = resolved_uid = resolved_did = ""
        for cand in candidates:
            if (
                not resolved_aid
                and RamaAttachment.objects.filter(
                    pk=cand, batch__landlord=landlord
                ).exists()
            ):
                resolved_aid = cand
            elif (
                not resolved_uid
                and RamaUpload.objects.filter(pk=cand, landlord=landlord).exists()
            ):
                resolved_uid = cand
            elif (
                not resolved_did
                and RamaDocument.objects.filter(pk=cand, landlord=landlord).exists()
            ):
                resolved_did = cand
        # Prefer the most specific handle the caller meant.
        if aid and resolved_aid == aid:
            pass
        elif uid and resolved_uid == uid:
            resolved_aid = resolved_aid  # keep
        aid, uid, did = resolved_aid, resolved_uid, resolved_did

    if aid:
        return catalog_batch_attachment_as_document(
            attachment_id=aid,
            **common,
        )
    if uid:
        return catalog_staged_photo_as_document(
            upload_id=uid,
            **common,
        )
    if did:
        # document_id path (preferred after prepare): status or scope+confirm.
        if not (scope_query or "").strip():
            from .document_services import business_document_status as status_fn

            return status_fn(landlord, document_id=did)
        return catalog_document_scope(
            document_id=did,
            **common,
        )
    # Bare address after prepare: pick the newest unscoped document for this
    # landlord so the model can say "yes" / "950 McKenzie" without re-passing ids.
    if (scope_query or "").strip():
        from .models import RamaDocument

        pending = (
            RamaDocument.objects.filter(landlord=landlord, holding__isnull=True)
            .exclude(status=RamaDocument.Status.FILED)
            .order_by("-created_at")
            .first()
        )
        if pending is None:
            return {
                "error": (
                    "No pending unscoped business document to attach that address to. "
                    "Pass attachment_id/upload_id first so the file is OCR'd."
                ),
            }
        return catalog_document_scope(
            document_id=str(pending.pk),
            **common,
        )
    return {
        "error": (
            "Pass attachment_id, upload_id, or document_id. "
            "For a new chat file, pass attachment_id alone first (no address) "
            "to OCR and check duplicates."
        ),
    }


def business_document_location(landlord, document_id: str) -> dict:
    """Return the exact durable location of a RAMA business document: canonical
    storage key, local container filesystem path when one exists, production
    object-storage URI, Documents-page URL, and authenticated download path.
    Use for 'where is it?', 'what directory?', 'show the path', or manual access.
    Never say there is no directory/location without calling this tool."""
    from .document_services import document_location

    return document_location(landlord, document_id)


def business_document_status(landlord, document_id: str) -> dict:
    """Read OCR results for a catalogued business document: kind, title, amount,
    payment_state, next steps. ALWAYS call this (or use catalog's intelligence
    payload) before inventing an expense amount. Never guess totals."""
    from .document_services import business_document_status as _fn

    return _fn(landlord, document_id=document_id)


@_params(
    query="Free-text search: vendor, address, invoice words, OCR content, e.g. "
    "'window screens McKenzie' or 'property tax 2026'.",
    holding_query="Optional physical address or holding name to narrow the shelf.",
    kind="Optional kind: EXPENSE, MAINTENANCE, TAX, INSURANCE, MORTGAGE, NOTICE, "
    "LEASE, BANK_STATEMENT, OTHER.",
    year="Optional calendar year of document_date, e.g. '2026'.",
    tag="Optional tag slug, e.g. 'tax-2026' or 'insurance'.",
    status="Optional status: QUEUED, PROCESSING, NEEDS_REVIEW, READY, FILED, FAILED.",
    payment_state="Optional: PAID, UNPAID, UNKNOWN, NOT_APPLICABLE.",
    has_expense="yes/no — only docs linked (or not) to a ledger expense.",
    limit="Max rows to return (default 20, max 50).",
)
def search_business_documents(
    landlord,
    query: str = "",
    holding_query: str = "",
    kind: str = "",
    year: str = "",
    tag: str = "",
    status: str = "",
    payment_state: str = "",
    has_expense: str = "",
    limit: str = "20",
) -> dict:
    """Search the landlord's business document library (OCR text, title, issuer,
    tags, holding, year, kind). Use for 'find the window screens invoice',
    'any tax notices for McKenzie 2026?', 'unpaid invoices', 'what's unfiled?'.
    Returns titles, amounts, links — never invents amounts or files. Read-only."""
    from .document_services import search_business_documents_for_chat as _fn

    try:
        lim = int(limit or "20")
    except ValueError:
        lim = 20
    return _fn(
        landlord,
        query=(query or "").strip(),
        holding_query=(holding_query or "").strip(),
        kind=(kind or "").strip(),
        year=(year or "").strip(),
        tag=(tag or "").strip(),
        status=(status or "").strip(),
        payment_state=(payment_state or "").strip(),
        has_expense=(has_expense or "").strip(),
        limit=lim,
    )


@_params(
    document_query="Vendor, current title, OCR words, filename, or reference used "
    "to find the document, e.g. 'PNR Screens LTD'. Combined with amount when both "
    "are supplied.",
    amount="Exact document amount used to distinguish similar receipts, e.g. "
    "'39.36'. Optional when document_id identifies the exact record.",
    document_date="Optional exact YYYY-MM-DD date to distinguish documents with "
    "the same vendor and amount.",
    holding_query="Optional physical property name or street address to narrow "
    "otherwise similar documents.",
    new_title="The new human-facing document title. This never renames or rewrites "
    "the preserved original file.",
    document_id="Exact document UUID. Preferred after a search or clarification; "
    "when supplied it is the authoritative target.",
    expected_title="Confirmation safety value returned in resolved_arguments. Pass "
    "it back unchanged; do not invent it.",
    confirm="Leave empty to preview; pass yes only after landlord approval.",
)
def rename_business_document(
    landlord,
    new_title: str,
    document_query: str = "",
    amount: str = "",
    document_id: str = "",
    document_date: str = "",
    holding_query: str = "",
    expected_title: str = "",
    confirm: str = "",
) -> dict:
    """Rename one existing receipt, invoice, notice, or business document.

    Resolves by vendor/title/OCR text plus an exact amount, so requests such as
    "two PNR Screens receipts; rename the $39.36 one" select the right record.
    If zero or several records match, asks for a date/property/document ID and
    changes nothing. Preview first; confirm=yes. Only the display title changes:
    original file, archival PDF, hash, filing status, and ledger stay untouched.
    """
    from .document_services import rename_business_document_for_chat as _fn

    return _fn(
        landlord,
        new_title=new_title,
        document_query=document_query,
        amount=amount,
        document_id=document_id,
        document_date=document_date,
        holding_query=holding_query,
        expected_title=expected_title,
        confirm=confirm,
    )


@_params(
    document_id="UUID of the catalogued business document.",
    payment_state="PAID if the money has left the bank; UNPAID if still owing. "
    "Required when OCR payment_state is UNKNOWN.",
    amount="Override only if OCR amount is wrong; default uses OCR amount. "
    "NEVER invent a figure — use intelligence.amount from catalog/status.",
    confirm="Leave empty to preview; yes to file document + post expense.",
)
def file_business_document(
    landlord,
    document_id: str,
    payment_state: str = "",
    amount: str = "",
    title: str = "",
    expense_category: str = "",
    issuer: str = "",
    document_date: str = "",
    duplicate_resolution: str = "",
    confirm: str = "",
) -> dict:
    """File a catalogued invoice/receipt and post the ledger expense.

    Use after catalog_business_document (or business_document_status) when the
    document is expense-like. PAID sets paid_on (already cleared the bank).
    UNPAID leaves paid_on empty. NEVER void because something was paid — void
    means reverse a wrong entry. Preview; confirm=yes."""
    from .document_services import file_business_document_for_chat as _fn

    return _fn(
        landlord,
        document_id=document_id,
        payment_state=payment_state,
        amount=amount,
        title=title,
        expense_category=expense_category,
        issuer=issuer,
        document_date=document_date,
        duplicate_resolution=duplicate_resolution,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Write actions (require confirm=yes)
# ---------------------------------------------------------------------------


def create_work_order(
    landlord,
    property_query: str,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    category: str = "OTHER",
    area: str = "",
    confirm: str = "",
) -> dict:
    """Create a maintenance work order. ALWAYS call once WITHOUT confirm to
    preview, show the landlord, then call again with confirm=yes to save.

    property_query = the listing name ('Room C') OR the unit when the fault is
    in SHARED space ('McKenzie Basement'). If the landlord says the space is
    shared, or names several rooms that are in one unit, target the UNIT — do
    not pick one of the rooms. area = the space inside it ('shared washroom',
    'kitchen'); everyone renting that unit then sees the job.
    priority HIGH/MEDIUM/LOW/EMERGENCY. category PLUMBING/ELECTRICAL/…/OTHER."""
    from .domain_actions import create_work_order as _fn

    return _fn(
        landlord,
        property_query=property_query,
        title=title,
        description=description,
        priority=priority,
        category=category,
        area=area,
        confirm=confirm,
    )


def transition_work_order(
    landlord,
    new_status: str,
    work_order_id: str = "",
    title_query: str = "",
    confirm: str = "",
) -> dict:
    """Change work order status (NEW→SCHEDULED→IN_PROGRESS→COMPLETED/CANCELLED).
    Preview first; confirm=yes to apply. Pass work_order_id or title_query."""
    from .domain_actions import transition_work_order as _fn

    return _fn(
        landlord,
        work_order_id=work_order_id,
        title_query=title_query,
        new_status=new_status,
        confirm=confirm,
    )


def mark_inquiry_replied(
    landlord, inquiry_id: str = "", name_query: str = "", confirm: str = "",
) -> dict:
    """Mark a listing inquiry as REPLIED. Preview first; confirm=yes to save."""
    from .domain_actions import mark_inquiry_replied as _fn

    return _fn(
        landlord, inquiry_id=inquiry_id, name_query=name_query, confirm=confirm,
    )


def send_tenant_message(
    landlord,
    body: str,
    tenant_query: str = "",
    conversation_id: str = "",
    property_query: str = "",
    subject: str = "",
    confirm: str = "",
) -> dict:
    """Send a message to a tenant. Preview first; confirm=yes to send.
    Pass conversation_id or tenant_query (name/email of linked tenant)."""
    from .domain_actions import send_tenant_message as _fn

    return _fn(
        landlord,
        body=body,
        tenant_query=tenant_query,
        conversation_id=conversation_id,
        property_query=property_query,
        subject=subject,
        confirm=confirm,
    )


def mark_messages_read(
    landlord, conversation_id: str = "", confirm: str = "",
) -> dict:
    """Mark unread tenant messages as read. Empty conversation_id = all threads.
    Preview first; confirm=yes to apply."""
    from .domain_actions import mark_messages_read as _fn

    return _fn(landlord, conversation_id=conversation_id, confirm=confirm)


@_params(
    contact="Prospect name or email, e.g. 'Ishupreet' or 'ishu@gmail.com'.",
    property_query="Optional listing name to narrow, e.g. 'Garden Suite'.",
    appointment_ref="Optional appointment id/prefix from list_appointments.",
)
def viewing_invite_status(
    landlord,
    contact: str = "",
    property_query: str = "",
    appointment_ref: str = "",
) -> dict:
    """Has the prospect opened their viewing invite/status link?

    Use for 'have they seen the link?', 'did they open the invite?', 'viewing
    link opened?'. Tracks status-page loads (not email open pixels). Read-only."""
    from .domain_actions import viewing_invite_status as _fn

    return _fn(
        landlord,
        contact=contact,
        property_query=property_query,
        appointment_ref=appointment_ref,
    )


def schedule_viewing(
    landlord,
    property_query: str,
    when: str,
    contact_name: str = "",
    contact_email: str = "",
    notes: str = "",
    duration_minutes: str = "30",
    confirm: str = "",
) -> dict:
    """Schedule a property SHOWING/viewing for prospective tenants only.
    when like '2026-08-05 14:00' or relative dates resolved by the router.
    duration_minutes default 30 (e.g. 15:00-15:30 → 30).
    Always pass contact_email when the landlord gave one — that queues the
    prospect invite. Do NOT use for move-in inspections.
    Preview first; confirm=yes to create."""
    from .domain_actions import schedule_viewing as _fn

    return _fn(
        landlord,
        property_query=property_query,
        when=when,
        contact_name=contact_name,
        contact_email=contact_email,
        notes=notes,
        duration_minutes=duration_minutes,
        confirm=confirm,
    )


def reschedule_viewing(
    landlord,
    when: str,
    appointment_ref: str = "",
    property_query: str = "",
    contact: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Move an EXISTING viewing to a new date/time (not a new booking).
    when like '2026-08-04 14:00'. Prefer appointment_ref from list_appointments
    (uuid or 8-char ref). Or property_query + contact email/name
    (e.g. property_query='Room D' contact='Hitakshiverma01@gmail.com').
    Emails the prospect the new time; keeps their status link. Preview first;
    confirm=yes to apply."""
    from .domain_actions import reschedule_viewing as _fn

    return _fn(
        landlord,
        when=when,
        appointment_ref=appointment_ref,
        property_query=property_query,
        contact=contact,
        notes=notes,
        confirm=confirm,
    )


def list_viewing_requests(landlord, scope: str = "pending") -> dict:
    """List viewing REQUESTS and their negotiation state — who asked, when,
    whether it's in or out of your preferred hours, the current tenant's
    consent, and whose reply it's waiting on. scope=pending (default) shows
    what needs action; scope=all includes scheduled/cancelled. Use before
    respond_to_viewing_request to get each request's ref."""
    from .domain_actions import list_viewing_requests as _fn

    return _fn(landlord, scope=(scope or "pending").strip())


def respond_to_viewing_request(
    landlord,
    request_ref: str,
    action: str,
    when: str = "",
    confirm: str = "",
) -> dict:
    """Confirm, counter (propose a new time), or decline a pending viewing
    request. action = confirm | counter | decline. For counter, pass when like
    '2026-08-05 14:00'. Get request_ref from list_viewing_requests. Preview
    first; confirm=yes to run. The result's `notified` says exactly who was
    told and how."""
    from .domain_actions import respond_to_viewing_request as _fn

    return _fn(
        landlord,
        request_ref=request_ref,
        action=action,
        when=when,
        confirm=confirm,
    )


def get_viewing_availability(landlord, property_query: str = "") -> dict:
    """Show the landlord's preferred viewing hours (their weekly default, or a
    specific property's override when property_query is given)."""
    from .domain_actions import get_viewing_availability as _fn

    return _fn(landlord, property_query=property_query)


@_params(
    weekday="Day name (e.g. Tuesday) for RECURRING weekly hours. Omit if using "
    "specific_date.",
    specific_date="YYYY-MM-DD for a ONE-OFF window on a single date (e.g. 'only "
    "July 25, 2–4pm'). Overrides the weekly hours for that date only.",
    start="Start time 'HH:MM' 24-hour.",
    end="End time 'HH:MM' 24-hour.",
    property_query="Optional listing name/id for a per-property override.",
    confirm="Leave empty to preview; 'yes' to save.",
)
def set_viewing_availability(
    landlord,
    weekday: str = "",
    start: str = "",
    end: str = "",
    property_query: str = "",
    specific_date: str = "",
    confirm: str = "",
) -> dict:
    """Add a preferred viewing window — RECURRING (pass weekday) or a ONE-OFF for
    a single date (pass specific_date=YYYY-MM-DD instead of weekday, e.g. 'only
    available July 25, 2–4pm'). start/end = 'HH:MM' 24-hour. property_query sets a
    per-property override. Preview; confirm=yes."""
    from .domain_actions import set_viewing_availability as _fn

    return _fn(
        landlord,
        weekday=weekday,
        start=start,
        end=end,
        property_query=property_query,
        specific_date=specific_date,
        confirm=confirm,
    )


def get_notification_channels(landlord) -> dict:
    """How this landlord is reachable outside the app: which external channels
    (Telegram now, WhatsApp later) they've linked and verified, plus the
    always-on in-app dashboard and email. ALSO reports the daily morning
    briefing — whether it is switched on and where it goes — so use this to
    answer "why am I not getting morning updates?". Rentium DOES send a
    scheduled daily briefing; never say it doesn't. Use to answer "how will you reach
    me?", "am I on Telegram?", or "how were people notified?" grounding."""
    from .domain_actions import get_notification_channels as _fn

    return _fn(landlord)


def create_condition_inspection(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """Create a real RTB-style condition inspection (lease → Condition Inspections).
    Requires at least one tenant on the lease. NOT a calendar viewing.
    Preview first; confirm=yes."""
    from .domain_actions import create_condition_inspection as _fn

    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        tenant_email=tenant_email,
        confirm=confirm,
    )


def lease_pdf_info(
    landlord,
    property_query: str = "",
    lease_number: str = "",
) -> dict:
    """How to download a lease PDF. Always available via /api/leases/<id>/pdf/
    even when document_file is empty — never say 'no PDF' for an existing lease."""
    from .domain_actions import lease_pdf_info as _fn

    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
    )


def bulk_add_inventory(
    landlord,
    property_query: str,
    items: str,
    confirm: str = "",
) -> dict:
    """Add multiple private inventory items at once.
    items e.g. 'Single bed, Mattress, Desk'. Preview; confirm=yes."""
    from .domain_actions import bulk_add_inventory as _fn

    return _fn(
        landlord, property_query=property_query, items=items, confirm=confirm,
    )


@_params(
    amount="The cost, e.g. '75.00' or '18.41'.",
    description="What the money was spent on, e.g. 'Draino for 950 McKenzie'.",
    property_query="The LISTING this belongs to, or the UNIT when the cost is "
    "for shared space inside one ('McKenzie Basement') — never pick one of the "
    "rooms that share it. Leave blank for a whole-property or portfolio cost.",
    holding_name="The whole physical property, when the cost belongs to the "
    "building rather than to anything inside it (roof, property tax, mulch, "
    "Draino for the house) — e.g. '950 McKenzie Ave'. Use this INSTEAD of "
    "property_query when they name the street/house.",
    paid_on="If already paid: 'paid', 'today', or YYYY-MM-DD. Leave blank if "
    "not yet taken from the bank.",
    category="Optional: SUPPLIES, MAINTENANCE, UTILITIES, OTHER, …",
    vendor="Optional payee/vendor, e.g. Chris Klatt's Second Hand Appliances.",
)
def create_expense(
    landlord,
    amount: str,
    description: str,
    property_query: str = "",
    holding_name: str = "",
    effective_date: str = "",
    paid_on: str = "",
    category: str = "",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Record a landlord expense with NO receipt photo. Use when they say they
    bought/spent/paid something and did NOT attach a receipt — do NOT call
    catalog_business_document. Four scopes: LISTING (property_query), unit
    shared space, WHOLE property (holding_name=address), or portfolio-wide.
    Pass paid_on when they said it is already paid. A receipt can be attached
    to the posted expense later. Preview first; confirm=yes."""
    from .domain_actions import create_expense as _fn

    return _fn(
        landlord,
        amount=amount,
        description=description,
        property_query=property_query,
        holding_name=holding_name,
        effective_date=effective_date,
        paid_on=paid_on,
        category=category,
        vendor=vendor,
        confirm=confirm,
    )


@_params(
    amount="How much ARRIVED, e.g. '100.00'. A partial payment is normal — "
    "record what was actually received, never the balance still owing. LEAVE "
    "BLANK when the landlord did not state a figure ('her deposits were "
    "received'): the charges already say how much, and this tool reads it from "
    "them. Never ask the landlord for an amount their own books hold.",
    charge_query="Words from the charge this settles, e.g. 'deposit' or "
    "'August rent'. Money is always recorded against a charge. Pass the "
    "landlord's whole phrase — a tenant's name in it narrows the search to "
    "that person's charges.",
    property_query="The listing, to narrow it down when several charges match.",
    payment_method="etransfer | cash | cheque. Take it from what the landlord said; leave BLANK if they did not say and the tool will ask. Never guess.",
    payment_date="The day the money ARRIVED, YYYY-MM-DD. Leave blank if they did not say — it then dates from today, which is when they are telling you.",
    reference_number="Optional bank/e-transfer/cheque reference from the UI or landlord.",
    notes="Optional factual payment note; never invent one.",
    tenant_query="Optional exact payer name/email, especially for a joint household charge.",
)
def record_payment(
    landlord,
    amount: str = "",
    charge_query: str = "",
    property_query: str = "",
    payment_method: str = "",
    payment_date: str = "",
    reference_number: str = "",
    notes: str = "",
    tenant_query: str = "",
    confirm: str = "",
) -> dict:
    """Record money RECEIVED against a charge — rent arrived, deposit paid,
    damage claim settled. Handles PARTIAL payments: $100 against a $425 deposit
    leaves $325 owing, which the preview shows before you confirm. `amount` is
    OPTIONAL: told only that a tenant's deposits were received, this finds their
    open charges and settles the full outstanding balance, so the landlord is
    never asked for a figure their books already hold. This is the ONLY way
    money-in reaches the ledger. Preview first; confirm=yes."""
    from .domain_actions import record_payment as _fn

    return _fn(
        landlord,
        amount=amount,
        charge_query=charge_query,
        property_query=property_query,
        payment_method=payment_method,
        payment_date=payment_date,
        reference_number=reference_number,
        notes=notes,
        tenant_query=tenant_query,
        confirm=confirm,
    )


@_params(
    expense_query="Words from the expense's description, e.g. 'hot water knob'.",
    amount="Its amount, to narrow the match when the wording is ambiguous.",
    property_query="The LISTING it should be booked against instead. Leave blank "
    "when the cost belongs to the whole property.",
    holding_name="The whole physical property it should be booked against — use "
    "this when a repair serves space shared by several rooms (a shower, a roof, "
    "the yard), so the cost sits on the address rather than on one tenant's room.",
    reason="Why it is moving. This goes on the permanent audit trail.",
)
def reallocate_expense(
    landlord,
    expense_query: str = "",
    amount: str = "",
    property_query: str = "",
    holding_name: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Move an already-recorded expense to the scope it belongs to. Use this —
    never post a second expense — when a cost was booked against the wrong
    place: a shared-space repair charged to one room, a building cost charged
    to a listing. The original is voided and the new entry is linked to it, so
    the ledger keeps ONE live line and the correction stays auditable.
    Preview first; confirm=yes."""
    from .domain_actions import reallocate_expense as _fn

    return _fn(
        landlord,
        expense_query=expense_query,
        amount=amount,
        property_query=property_query,
        holding_name=holding_name,
        reason=reason,
        confirm=confirm,
    )


def invite_tenant_to_lease(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    rent_amount: str = "",
    is_primary: str = "auto",
    phone: str = "",
    mode: str = "add",
    replace_email: str = "",
    replace_name: str = "",
    confirm: str = "",
) -> dict:
    """Invite someone to sign a lease. Rent auto-splits (do NOT invent $1000 each).
    mode=replace + replace_name/email removes that pending invite first.
    Prefer replace_lease_invite for 'suspend X invite Y'. Preview; confirm=yes."""
    from .domain_actions import invite_tenant_to_lease as _fn

    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        rent_amount=rent_amount,
        is_primary=is_primary,
        phone=phone,
        mode=mode,
        replace_email=replace_email,
        replace_name=replace_name,
        confirm=confirm,
    )


def resend_lease_invite(
    landlord,
    email: str = "",
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Resend an existing pending lease signing invite. Preview first; confirm=yes."""
    from .domain_actions import resend_lease_invite as _fn

    return _fn(
        landlord,
        email=email,
        property_query=property_query,
        lease_number=lease_number,
        confirm=confirm,
    )


def cancel_lease_invite(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    email: str = "",
    name: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Suspend/remove a pending (unsigned) lease invite. Preview first; confirm=yes.
    Use when landlord says cancel/suspend/remove invite for X. Does NOT resend."""
    from .domain_actions import cancel_lease_invite as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        reason=reason or "Landlord cancelled invite",
        confirm=confirm,
    )


def replace_lease_invite(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    remove_email: str = "",
    remove_name: str = "",
    rent_amount: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """Replace one pending invite with another person in ONE step.
    remove_name/remove_email = who to suspend; email/name = who to invite instead.
    Rent auto-fills (sole tenant → full lease total_rent). Preview; confirm=yes.
    Use: 'suspend Jabi, invite Movy instead'."""
    from .domain_actions import replace_lease_invite as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        remove_email=remove_email,
        remove_name=remove_name,
        email=email,
        name=name,
        rent_amount=rent_amount,
        phone=phone,
        confirm=confirm,
    )


def list_lease_roster(
    landlord, property_query: str = "", lease_number: str = "",
) -> dict:
    """Who is on a lease: pending invites, signed tenants, rent shares, primary,
    and when each person last saw the lease (invite link open or agreement/PDF
    view — seen_summary / invite_lifecycle.last_seen_at). Use for 'who should
    sign?', 'has X opened/seen the lease?', 'when did they view it?', rent split."""
    from .domain_actions import list_lease_roster as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
    )


def add_roommate_to_lease(
    landlord,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """Add a roommate/co-tenant without removing anyone. ALWAYS use this when
    landlord says add another tenant/roommate. Rebalances rent equally
    (e.g. $1000 total → $500 each). Preview; confirm=yes. Never replaces."""
    from .domain_actions import add_roommate_to_lease as _fn
    return _fn(
        landlord,
        email=email,
        name=name,
        property_query=property_query,
        lease_number=lease_number,
        phone=phone,
        confirm=confirm,
    )


def rebalance_lease_rents(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Equal-split total_rent across unsigned tenants on a lease. Preview; confirm=yes."""
    from .domain_actions import rebalance_lease_rents as _fn
    return _fn(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# CRUD: properties, leases, maintenance update, inventory
# ---------------------------------------------------------------------------


def create_property(
    landlord,
    name: str,
    address: str,
    city: str,
    property_category: str = "ROOM",
    province: str = "BC",
    status: str = "AVAILABLE",
    unit_type: str = "",
    room_type: str = "PRIVATE",
    bedrooms: str = "",
    bathrooms: str = "",
    description: str = "",
    group_name: str = "",
    asking_rent: str = "",
    inventory_items: str = "",
    postal_code: str = "",
    neighbourhood: str = "",
    max_occupancy: str = "",
    square_footage: str = "",
    available_from: str = "",
    building_amenities: str = "",
    default_bills_included: str = "",
    is_publicly_visible: str = "",
    furnishing_status: str = "",
    furnishing_details: str = "",
    allow_duplicate_name: str = "0",
    confirm: str = "",
) -> dict:
    """Create a property listing (same rules as Properties UI).
    CLASSIFY IT RIGHT: a self-contained home is a COMPLETE_UNIT — pass
    property_category=COMPLETE_UNIT and unit_type = one of: garden suite,
    basement, main floor, apartment, other. A 'garden suite', 'basement suite',
    'laneway', 'in-law/secondary suite', 'apartment', 'whole unit' → COMPLETE_UNIT
    (NOT a room). Only a single bedroom rented inside a shared home is
    property_category=ROOM (room_type: private/shared). Passing a unit_type makes
    it a COMPLETE_UNIT automatically. Optional group_name for rooms only.
    inventory_items: if landlord names furniture (e.g. 'Single bed, Mattress'), pass it
    so 'What's in it' is not empty.
    Refuses exact-name duplicates unless allow_duplicate_name=yes.
    For room+lease+tenant in one ask, prefer setup_room_tenancy instead.
    Preview first; confirm=yes."""
    from .domain_crud import create_property as _fn
    return _fn(
        landlord, name=name, address=address, city=city,
        property_category=property_category, province=province, status=status,
        unit_type=unit_type, room_type=room_type, bedrooms=bedrooms,
        bathrooms=bathrooms, description=description, group_name=group_name,
        asking_rent=asking_rent, inventory_items=inventory_items,
        postal_code=postal_code, neighbourhood=neighbourhood,
        max_occupancy=max_occupancy, square_footage=square_footage,
        available_from=available_from, building_amenities=building_amenities,
        default_bills_included=default_bills_included,
        is_publicly_visible=is_publicly_visible,
        furnishing_status=furnishing_status,
        furnishing_details=furnishing_details,
        allow_duplicate_name=allow_duplicate_name, confirm=confirm,
    )


@_params(
    property_query="The listing to duplicate: its exact name or id.",
    new_name="Name for the copy. Default is the SAME name (it's a deliberate "
    "duplicate) — pass a new name only if the landlord wants one.",
    copy_images="yes/no — copy the source's photos (default yes).",
    copy_inventory="yes/no — copy the source's inventory (default yes).",
    group_name="Optional: put the copy in this property group (defaults to the "
    "source's group).",
    pick="If the name matches two listings: oldest|newest|1|2.",
    confirm="Leave empty to preview; 'yes' to create the copy.",
)
def duplicate_listing(
    landlord,
    property_query: str,
    new_name: str = "",
    copy_images: str = "1",
    copy_inventory: str = "1",
    group_name: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Duplicate an existing listing — a REAL copy WITH its photos and inventory.
    Use this whenever the landlord says 'duplicate/copy/clone this listing';
    never hand-roll it with create_property (that makes an EMPTY listing with no
    photos or inventory, which is not what they mean). Copies address, category,
    type, beds, description, asking rent, status, primary + gallery photos, and
    private inventory. Leases are never copied. Preview; confirm=yes."""
    from .domain_crud import duplicate_listing as _fn
    return _fn(
        landlord, property_query=property_query, new_name=new_name,
        copy_images=copy_images, copy_inventory=copy_inventory,
        group_name=group_name, pick=pick, confirm=confirm,
    )


@_params(
    request="What the landlord wanted, in their own words (one sentence).",
    detail="Optional: why it was blocked / what tool or field was missing.",
    learn_now="yes if the landlord said 'learn now' or otherwise asked us to "
    "build this — prioritises it.",
)
def log_capability_gap(
    landlord, request: str, detail: str = "", learn_now: str = "",
) -> dict:
    """Log something you genuinely CANNOT do yet as a structured gap for the team
    to build — instead of just saying 'I can't'. Call this whenever you hit a
    real capability limit, and especially when the landlord says 'learn now'.
    Known property operations, room/group creation, room lists, and dashboard
    links are rejected as false gaps and return the tool to retry. Then tell the
    landlord a genuine gap was noted (and prioritised if learn_now). This never
    writes code; it records the need for a human to build safely."""
    from .domain_actions import log_capability_gap as _fn
    return _fn(landlord, request=request, detail=detail, learn_now=learn_now)


def list_capability_gaps(landlord, status: str = "", limit: str = "20") -> dict:
    """List the capability gaps RAMA has logged for this landlord — what it
    couldn't do yet and what's been flagged to build. status: NEW|REVIEWED|
    BUILT|DISMISSED (blank = all)."""
    from .domain_actions import list_capability_gaps as _fn
    return _fn(landlord, status=status, limit=limit)


@_params(
    property_query="The listing to add the photo to: its exact name or id.",
    attachment_batch_id="Required for new chat uploads: the exact batch id shown "
    "in '[RAMA attachment batch …]'. Only files in this batch may be used.",
    attachment_ids="Optional comma-separated attachment ids from that batch. Omit "
    "to use every image in the explicitly named batch.",
    upload_id="Legacy only: explicit staged photo id(s). Blank and 'all' are refused.",
    set_primary="yes to make the FIRST photo the listing's MAIN photo (rest go to "
    "the gallery); omit for all gallery.",
    pick="If the name matches two listings: oldest|newest|1|2.",
    confirm="Leave empty to preview; 'yes' to attach.",
)
def attach_photo_to_listing(
    landlord,
    property_query: str,
    attachment_batch_id: str = "",
    attachment_ids: str = "",
    upload_id: str = "",
    set_primary: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Attach property photos from one explicit chat attachment batch to a
    listing. Never uses older or unrelated pending files. Gallery is the safe
    default; set_primary=yes makes only the first selected image the main photo.
    Preview; confirm=yes."""
    from .domain_crud import attach_photo_to_listing as _fn
    return _fn(
        landlord, property_query=property_query,
        attachment_batch_id=attachment_batch_id,
        attachment_ids=attachment_ids, upload_id=upload_id,
        set_primary=set_primary, pick=pick, confirm=confirm,
    )


def list_listing_media(
    landlord,
    property_query: str,
    pick: str = "",
) -> dict:
    """List every image currently on one listing with a stable handle such as
    primary or gallery:42. Use before removing an incorrect or old photo."""
    from .domain_crud import list_listing_media as _fn

    return _fn(landlord, property_query=property_query, pick=pick)


@_params(
    property_query="The listing whose image should be removed.",
    media_handle="Exact handle from list_listing_media: primary or gallery:<id>.",
    pick="If the name matches two listings: oldest|newest|1|2.",
    confirm="Leave empty to preview; 'yes' to remove the exact image.",
)
def remove_photo_from_listing(
    landlord,
    property_query: str,
    media_handle: str,
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Remove one exact image from a listing. Call list_listing_media first when
    the handle is not already known. Preview; confirm=yes."""
    from .domain_crud import remove_photo_from_listing as _fn

    return _fn(
        landlord,
        property_query=property_query,
        media_handle=media_handle,
        pick=pick,
        confirm=confirm,
    )


@_params(
    property_query="The listing whose selected images should be removed.",
    media_handles_json=(
        "JSON list containing only exact handles from list_listing_media, "
        'e.g. ["primary", "gallery:42"].'
    ),
    pick="If the name matches two listings: oldest|newest|1|2.",
    confirm="Leave empty to preview; 'yes' removes the exact selected set.",
)
def remove_photos_from_listing(
    landlord,
    property_query: str,
    media_handles_json: str,
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Remove an exact selection of one or more listing images as one
    operation. Always call list_listing_media first so the landlord can choose
    by numbered thumbnail. Preview once; one confirmation removes that set."""
    from .domain_crud import remove_photos_from_listing as _fn

    return _fn(
        landlord,
        property_query=property_query,
        media_handles_json=media_handles_json,
        pick=pick,
        confirm=confirm,
    )


def setup_room_tenancy(
    landlord,
    room_name: str,
    address: str,
    city: str,
    group_name: str = "",
    province: str = "bc",
    asking_rent: str = "",
    inventory_items: str = "",
    start_date: str = "",
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_deposit: str = "0",
    special_terms: str = "",
    tenant_name: str = "",
    tenant_email: str = "",
    smoking_allowed: str = "0",
    pets_allowed: str = "0",
    create_inspection: str = "1",
    use_existing_if_name_matches: str = "1",
    confirm: str = "",
) -> dict:
    """ONE-SHOT workflow: create/reuse room + inventory + DRAFT lease + invite +
    condition inspection. Use when landlord asks for a full room setup with tenant.
    Prefer over many separate tools. Preview; confirm=yes runs all steps."""
    from .domain_crud import setup_room_tenancy as _fn

    return _fn(
        landlord,
        room_name=room_name,
        address=address,
        city=city,
        group_name=group_name,
        province=province,
        asking_rent=asking_rent,
        inventory_items=inventory_items,
        start_date=start_date,
        end_date=end_date,
        total_rent=total_rent,
        security_deposit=security_deposit,
        pet_deposit=pet_deposit,
        cleaning_deposit=cleaning_deposit,
        special_terms=special_terms,
        tenant_name=tenant_name,
        tenant_email=tenant_email,
        smoking_allowed=smoking_allowed,
        pets_allowed=pets_allowed,
        create_inspection=create_inspection,
        use_existing_if_name_matches=use_existing_if_name_matches,
        confirm=confirm,
    )


@_params(
    holding_name=(
        "Name for the physical house/holding, normally its canonical street address."
    ),
    address="Canonical street address for the house.",
    city="City for every room in the layout. Never guess; leave blank to clarify.",
    province=(
        "Canadian province name or two-letter code for every room. Never guess; "
        "leave blank to clarify."
    ),
    layout_json=(
        'JSON object {"groups":[{"name":"...","rooms":[{"name":"...",'
        '"private_areas":["BATHROOM"]}],"shared_areas":[{"area_type":'
        '"BATHROOM","rooms":["Room 2","Room 3"]}]}]}. Empty groups are allowed. '
        "Use private_areas for an ensuite and explicit room lists for subset-shared "
        "areas."
    ),
    shared_with_landlord=(
        "yes/no: whether the landlord or immediate relatives use any shared areas. "
        "Leave blank to ask the required legal-classification question."
    ),
    confirm="Leave empty to preview; pass yes only after the landlord confirms.",
)
def create_house_layout(  # noqa: PLR0913 - explicit public tool fields
    landlord,
    holding_name: str,
    address: str,
    layout_json: str,
    city: str = "",
    province: str = "",
    shared_with_landlord: str = "",
    confirm: str = "",
) -> dict:
    """LEGACY — use create_property_structure instead for anything new. This
    tool makes EVERY described bedroom its own rentable room listing, which is
    only right when the landlord genuinely lets bedrooms separately to
    different people. For a floor let as one home it produces one listing per
    bedroom and no way to say they are a single place. Kept only for existing
    room-by-room houses whose shared-area associations are already expressed
    this way. Atomically creates/reuses one house, its property groups, rooms,
    private areas, and room-to-common-area associations. One preview, then
    confirm=yes."""
    from .house_layout import create_house_layout as _fn

    return _fn(
        landlord,
        holding_name=holding_name,
        address=address,
        city=city,
        province=province,
        layout_json=layout_json,
        shared_with_landlord=shared_with_landlord,
        confirm=confirm,
    )


@_params(
    name="The new room/listing name (for example, Mackenzie B).",
    group_name="Existing property group whose agreed address and holding are inherited.",
    inventory_items="Private room inventory as a comma-separated list or JSON list.",
    shared_areas="Group common areas to associate, e.g. bathroom, kitchen, living room.",
    shared_with_landlord=(
        "yes/no: whether the landlord or immediate relatives also use NEW common "
        "areas. Leave blank only when no new classification is being created or changed."
    ),
    holding_name=(
        "For an EMPTY group only: existing physical holding name. Pass this when "
        "the landlord identified the house/building."
    ),
    address=(
        "For an EMPTY group only: exact street address used to resolve its holding."
    ),
    city="For an EMPTY group only: city, when the holding does not record it.",
    province=(
        "For an EMPTY group only: two-letter province code, when no sibling "
        "listing can supply it."
    ),
    confirm="Leave empty to preview; pass 'yes' to run the whole atomic operation.",
)
def create_group_room(
    landlord,
    name: str,
    group_name: str,
    inventory_items: str = "",
    shared_areas: str = "",
    shared_with_landlord: str = "",
    holding_name: str = "",
    address: str = "",
    city: str = "",
    province: str = "",
    confirm: str = "",
) -> dict:
    """Create one ROOM inside an existing property group as a single atomic
    operation. Derives address, city, province, postal code, country, and holding
    from consistent group members. For an empty group, holding_name or exact
    address safely bootstraps the first room; missing/conflicting data is asked
    for rather than guessed. Creates private inventory and associates all
    requested group common areas in the same transaction. New shared areas
    require an explicit landlord-sharing yes/no. Exact/near duplicate names are
    surfaced. Preview first; confirm=yes."""
    from .domain_crud import create_group_room as _fn

    return _fn(
        landlord,
        name=name,
        group_name=group_name,
        inventory_items=inventory_items,
        shared_areas=shared_areas,
        shared_with_landlord=shared_with_landlord,
        holding_name=holding_name,
        address=address,
        city=city,
        province=province,
        confirm=confirm,
    )


@_params(
    property_query="Which listing to change: its exact name or id (this is the "
    "LOOKUP key — it is never modified).",
    name="RENAME the listing to this new name. This is how you rename a listing "
    "— pass the new name here. Works on any listing, draft or leased (the name "
    "is just the listing's label; the lease document only ever says 'the Room').",
    status="New status: AVAILABLE | OCCUPIED | MAINTENANCE | NOT_AVAILABLE.",
    description="New public description text.",
    address="New street address.",
    city="New city.",
    province="New province code (e.g. BC).",
    asking_rent="New asking rent, e.g. '850.00'.",
    property_category="Structured classification: ROOM or COMPLETE_UNIT. Use this "
    "for 'make it a full suite/unit'; NEVER alter description as a substitute.",
    unit_type="COMPLETE_UNIT listings only: new unit_type.",
    room_type="ROOM listings only: new room_type.",
    bedrooms="COMPLETE_UNIT only: bedroom count, or blank to clear.",
    bathrooms="COMPLETE_UNIT only: bathroom count, or blank to clear.",
    max_occupancy="COMPLETE_UNIT only: maximum occupants, or blank to clear.",
    square_footage="COMPLETE_UNIT only: square footage, or blank to clear.",
    is_publicly_visible="yes/no — whether the listing shows publicly.",
    pick="If property_query matches MORE THAN ONE listing, choose which: "
    "oldest|first|1 (the older/earlier one) | newest|last|2 (the newer one). "
    "Use this for 'the old one' / 'the new one' / 'the first/second one'.",
    confirm="Leave empty to PREVIEW; pass 'yes' to apply.",
)
def update_property(
    landlord,
    property_query: str,
    name: str = "",
    status: str = "",
    description: str = "",
    address: str = "",
    city: str = "",
    province: str = "",
    asking_rent: str = "",
    property_category: str = "",
    unit_type: str = "",
    room_type: str = "",
    bedrooms: str = "",
    bathrooms: str = "",
    max_occupancy: str = "",
    square_footage: str = "",
    is_publicly_visible: str = "",
    furnishing_status: str = "",
    furnishing_details: str = "",
    postal_code: str = "",
    neighbourhood: str = "",
    available_from: str = "",
    building_amenities: str = "",
    default_bills_included: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Update (edit) an existing listing's fields — this includes RENAMING it:
    to rename a listing, call update_property with name=<the new name>. Also
    sets status/description/address/city/province/asking_rent/visibility,
    furnishing_status (furnished|semi_furnished|unfurnished) and optional
    furnishing_details, and structured category/layout fields. For "full suite/
    unit", set property_category=COMPLETE_UNIT (and unit_type when known); do not
    rewrite the description. Questions such as "how many rooms?" are read-only
    and must use list_properties, not this tool.
    Renaming works even if the listing has a signed lease (the name is just a
    label; nothing about the tenancy changes). If the name/id matches two
    listings (duplicates), add pick=oldest|newest|1|2. Preview first, then
    confirm=yes."""
    from .domain_crud import update_property as _fn
    return _fn(
        landlord, property_query=property_query, name=name, status=status,
        description=description, address=address, city=city, province=province,
        asking_rent=asking_rent, property_category=property_category,
        unit_type=unit_type, room_type=room_type, bedrooms=bedrooms,
        bathrooms=bathrooms, max_occupancy=max_occupancy,
        square_footage=square_footage,
        is_publicly_visible=is_publicly_visible,
        furnishing_status=furnishing_status,
        furnishing_details=furnishing_details,
        postal_code=postal_code,
        neighbourhood=neighbourhood,
        available_from=available_from,
        building_amenities=building_amenities,
        default_bills_included=default_bills_included,
        pick=pick, confirm=confirm,
    )


@_params(
    property_query="Which listing to delete: its exact name or id.",
    pick="If the name/id matches MORE THAN ONE listing, choose which to delete: "
    "oldest|first|1 (the older one) | newest|last|2 (the newer one). Use this "
    "for 'delete the old one' / 'the duplicate I just made'.",
    confirm="Leave empty to PREVIEW; pass 'yes' to delete.",
)
def delete_property(
    landlord, property_query: str, pick: str = "", confirm: str = "",
) -> dict:
    """Delete a listing. Blocked if ANY lease still references it (PROTECT).
    On duplicate names pass pick=oldest|newest|1|2 (or property_query=<id>) to
    pick which one — e.g. 'delete the old one' → pick=oldest. Preview;
    confirm=yes."""
    from .domain_crud import delete_property as _fn
    return _fn(landlord, property_query=property_query, pick=pick, confirm=confirm)


def create_property_group(
    landlord, name: str, description: str = "", confirm: str = "",
) -> dict:
    """Create a property group for shared rooms (e.g. McKenzie Side Unit). Preview; confirm=yes."""
    from .domain_crud import create_property_group as _fn
    return _fn(landlord, name=name, description=description, confirm=confirm)


def assign_property_to_group(
    landlord,
    property_query: str,
    group_name: str = "",
    clear: str = "",
    confirm: str = "",
) -> dict:
    """Put a ROOM into a group, or clear=yes to remove from group. Complete units blocked.
    Preview; confirm=yes."""
    from .domain_crud import assign_property_to_group as _fn
    return _fn(
        landlord, property_query=property_query, group_name=group_name,
        clear=clear, confirm=confirm,
    )


def create_holding(
    landlord, name: str, kind: str = "HOUSE", address: str = "", city: str = "",
    confirm: str = "",
) -> dict:
    """Create a holding — the physical/financial container for one address:
    one bank account, any mix of rooms AND complete units (e.g. a garden
    suite + basement suite + upstairs rooms, all one house). kind:
    HOUSE|BUILDING|OTHER. This is what bank-balance policy attaches to —
    unlike property groups (rooms only, layout). Preview; confirm=yes."""
    from .domain_crud import create_holding as _fn
    return _fn(landlord, name=name, kind=kind, address=address, city=city, confirm=confirm)


def assign_property_to_holding(
    landlord, property_query: str, holding_name: str = "", clear: str = "",
    confirm: str = "",
) -> dict:
    """Put ANY listing (room or complete unit) into a holding, or clear=yes to
    remove it. Preview; confirm=yes."""
    from .domain_crud import assign_property_to_holding as _fn
    return _fn(
        landlord, property_query=property_query, holding_name=holding_name,
        clear=clear, confirm=confirm,
    )


def list_holdings(landlord) -> dict:
    """List holdings (houses/buildings) and their listings. Use to answer
    'which listings share a bank account/address' and before setting a
    min-balance policy."""
    from .domain_crud import list_holdings as _fn
    return _fn(landlord)


def update_bank_balance(
    landlord, holding_name: str = "", label: str = "Operating", balance: str = "",
    as_of: str = "", confirm: str = "",
) -> dict:
    """Record the landlord's reported bank balance for one holding
    (holding_name) or the whole portfolio (blank). balance e.g. '5230.00';
    as_of YYYY-MM-DD (default today). ALWAYS preview and get the landlord's
    explicit confirmation — never write a balance without it."""
    from .finance import update_bank_balance as _fn
    return _fn(
        landlord, holding_name=holding_name, label=label, balance=balance,
        as_of=as_of, confirm=confirm,
    )


def list_bank_balances(landlord) -> dict:
    """Landlord-reported bank balances per holding, with staleness and
    estimated ledger drift since last reported. Use for balance/cash
    questions and before any min-balance analysis."""
    from .finance import list_bank_balances as _fn
    return _fn(landlord)


def list_import_batches(landlord) -> dict:
    """Uploaded historical data batches (bank statements, prior-year
    spreadsheets) and whether they are still DRAFT. DRAFT rows are NOT in the
    ledger. You cannot commit a batch — only the landlord can."""
    from .finance import list_import_batches as _fn
    return _fn(landlord)


@_params(
    batch_id="Batch id from list_import_batches. Blank uses the newest DRAFT batch.",
    limit="Max rows to return (default 200).",
)
def read_staged_entries(landlord, batch_id: str = "", limit: str = "") -> dict:
    """Rows of an uploaded batch, as PROVISIONAL history. Use for prior-year
    context. Never add these to a ledger total — say they are provisional."""
    from .finance import read_staged_entries as _fn
    return _fn(landlord, batch_id=batch_id, limit=limit)


@_params(
    total_rent="Monthly rent for the listing. Rent is ESSENTIAL — if the "
    "landlord hasn't said it and the listing has no asking rent, LEAVE THIS "
    "BLANK and the tool will ask them; do not guess. Pass '0' only if the "
    "landlord explicitly wants a free/zero-rent arrangement.",
)
def create_lease(
    landlord,
    property_query: str,
    start_date: str,
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "0",
    cleaning_deposit: str = "0",
    is_month_to_month: str = "0",
    pets_allowed: str = "0",
    smoking_allowed: str = "0",
    special_terms: str = "",
    house_rules: str = "",
    shared_with: str = "",
    move_in_date: str = "",
    co_hosts: str = "",
    landlord_service_address: str = "",
    landlord_daytime_phone: str = "",
    landlord_other_phone: str = "",
    landlord_fax: str = "",
    landlord_service_email: str = "",
    custom_tenant_notice_months: str = "",
    fixed_term_end_reason: str = "",
    fixed_term_end_regulation_section: str = "",
    etransfer_email: str = "",
    bills_included: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Create a DRAFT lease (UI New Lease). Type auto: room→Standard Roommate,
    BC complete unit→RTB-1. Fixed-term needs end_date.
    property_query can be listing id or name; pick=first|with_group|… if duplicates.
    Defaults for protection: no smoking/pets; pet and cleaning deposits are 0 unless set.
    RENT is essential: if the landlord didn't give it (and the listing has no
    asking rent), leave total_rent blank — the tool will ASK rather than make a
    $0 lease. Pass '0' only for a genuinely free room.
    security_deposit: omit for half monthly rent; pass '0' only if landlord wants zero.
    Then invite tenants; use create_condition_inspection (not schedule_viewing) for move-in.
    Preview; confirm=yes."""
    from .domain_crud import create_lease as _fn
    return _fn(
        landlord, property_query=property_query, start_date=start_date,
        end_date=end_date, total_rent=total_rent, security_deposit=security_deposit,
        pet_deposit=pet_deposit, cleaning_deposit=cleaning_deposit,
        is_month_to_month=is_month_to_month, pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        house_rules=house_rules, shared_with=shared_with,
        move_in_date=move_in_date, co_hosts=co_hosts,
        landlord_service_address=landlord_service_address,
        landlord_daytime_phone=landlord_daytime_phone,
        landlord_other_phone=landlord_other_phone,
        landlord_fax=landlord_fax,
        landlord_service_email=landlord_service_email,
        custom_tenant_notice_months=custom_tenant_notice_months,
        fixed_term_end_reason=fixed_term_end_reason,
        fixed_term_end_regulation_section=fixed_term_end_regulation_section,
        etransfer_email=etransfer_email, bills_included=bills_included,
        pick=pick, confirm=confirm,
    )


def update_lease(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    pet_deposit: str = "",
    cleaning_deposit: str = "",
    start_date: str = "",
    end_date: str = "",
    move_in_date: str = "",
    move_out_date: str = "",
    pets_allowed: str = "",
    smoking_allowed: str = "",
    special_terms: str = "",
    house_rules: str = "",
    shared_with: str = "",
    bills: str = "",
    etransfer_email: str = "",
    co_hosts: str = "",
    landlord_service_address: str = "",
    landlord_daytime_phone: str = "",
    landlord_other_phone: str = "",
    landlord_fax: str = "",
    landlord_service_email: str = "",
    custom_tenant_notice_months: str = "",
    fixed_term_end_reason: str = "",
    fixed_term_end_regulation_section: str = "",
    is_month_to_month: str = "",
    rent_due_day: str = "",
    parking_included: str = "",
    parking_description: str = "",
    parking_extra_charge: str = "",
    pets_terms: str = "",
    smoking_terms: str = "",
    services_and_facilities: str = "",
    occupants: str = "",
    confirm: str = "",
) -> dict:
    """Update DRAFT or PENDING lease fields (including start_date and end_date).
    Only ACTIVE/EXPIRED/TERMINATED/RENEWED are locked — PENDING signature leases
    ARE editable (same as the UI), even if some tenants already signed; the
    result names anyone whose signed terms this amends. For furnished/
    semi-furnished changes use adjust_lease (inventory-driven). special_terms /
    house_rules / shared_with / bills as documented. Preview; confirm=yes."""
    from .domain_crud import update_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
        total_rent=total_rent, security_deposit=security_deposit,
        pet_deposit=pet_deposit, cleaning_deposit=cleaning_deposit,
        start_date=start_date, end_date=end_date,
        move_in_date=move_in_date, move_out_date=move_out_date,
        pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        house_rules=house_rules, shared_with=shared_with, bills=bills,
        etransfer_email=etransfer_email, co_hosts=co_hosts,
        landlord_service_address=landlord_service_address,
        landlord_daytime_phone=landlord_daytime_phone,
        landlord_other_phone=landlord_other_phone,
        landlord_fax=landlord_fax,
        landlord_service_email=landlord_service_email,
        custom_tenant_notice_months=custom_tenant_notice_months,
        rent_due_day=rent_due_day,
        parking_included=parking_included,
        parking_description=parking_description,
        parking_extra_charge=parking_extra_charge,
        pets_terms=pets_terms,
        smoking_terms=smoking_terms,
        services_and_facilities=services_and_facilities,
        occupants=occupants,
        fixed_term_end_reason=fixed_term_end_reason,
        fixed_term_end_regulation_section=fixed_term_end_regulation_section,
        is_month_to_month=is_month_to_month,
        confirm=confirm,
    )


def adjust_lease(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    start_date: str = "",
    end_date: str = "",
    furnishing: str = "",
    inventory_items: str = "",
    special_terms: str = "",
    confirm: str = "",
) -> dict:
    """One-step edit for an unlocked lease: change start/end dates and/or
    furnishing. Prefer this over inventing a new lease. furnishing =
    furnished | semi_furnished | unfurnished (sets listing inventory so the
    PDF shows furnished correctly — a bed makes a room furnished). Optional
    inventory_items e.g. 'Queen bed, Mattress, Desk'. lease_number preferred
    (e.g. RMT415536-0617). Preview; confirm=yes."""
    from .domain_crud import adjust_lease as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        start_date=start_date,
        end_date=end_date,
        furnishing=furnishing,
        inventory_items=inventory_items,
        special_terms=special_terms,
        confirm=confirm,
    )


@_params(
    lease_number="Existing lease to renew (preferred), e.g. RMT415536-0617.",
    start_date="New term start YYYY-MM-DD (default: day after old end_date).",
    end_date="New fixed-term end YYYY-MM-DD (required unless month-to-month).",
    total_rent="Monthly rent for the new term (default: same as old lease).",
    copy_tenants="yes (default) copies roster as unsigned invites; no skips.",
)
def renew_lease(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    start_date: str = "",
    end_date: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    is_month_to_month: str = "",
    copy_tenants: str = "1",
    confirm: str = "",
) -> dict:
    """Renew an ACTIVE/EXPIRED lease: old→RENEWED, new DRAFT linked as
    previous_lease (same as UI Renew). Prefer this over terminate+create.
    Preview; confirm=yes."""
    from .domain_composites import renew_lease as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        start_date=start_date,
        end_date=end_date,
        total_rent=total_rent,
        security_deposit=security_deposit,
        is_month_to_month=is_month_to_month,
        copy_tenants=copy_tenants,
        confirm=confirm,
    )


@_params(
    requested_end_date="Tenancy end date YYYY-MM-DD when opening a move-out.",
    kind="LANDLORD_NOTICE (auto-applies if notice period met) or MUTUAL_AGREEMENT.",
    rent_handling="NONE | VOID_FINAL | PRORATE_FINAL for mutual agreements.",
    deposit_settlement="PENDING | RETURNED | AGREED | RTB when settling deposit.",
    deposit_return_method=(
        "Required for RETURNED when deposits are held: e-transfer, cash, or cheque."
    ),
)
def settle_moveout(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    requested_end_date: str = "",
    kind: str = "MUTUAL_AGREEMENT",
    reason: str = "",
    rent_handling: str = "NONE",
    moveout_id: str = "",
    action: str = "",
    forwarding_address: str = "",
    forwarding_address_received_on: str = "",
    deposit_settlement: str = "",
    deposit_return_method: str = "",
    deposit_return_date: str = "",
    tenant_agreement_signed_on: str = "",
    rtb_file_number: str = "",
    confirm: str = "",
) -> dict:
    """End a tenancy (landlord notice or mutual agreement) and/or record
    deposit settlement with evidence — same as UI move-out flow.
    action=accept|decline|cancel on a pending move-out. Preview; confirm=yes."""
    from .domain_composites import settle_moveout as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        requested_end_date=requested_end_date,
        kind=kind,
        reason=reason,
        rent_handling=rent_handling,
        moveout_id=moveout_id,
        action=action,
        forwarding_address=forwarding_address,
        forwarding_address_received_on=forwarding_address_received_on,
        deposit_settlement=deposit_settlement,
        deposit_return_method=deposit_return_method,
        deposit_return_date=deposit_return_date,
        tenant_agreement_signed_on=tenant_agreement_signed_on,
        rtb_file_number=rtb_file_number,
        confirm=confirm,
    )


@_params(
    fill_move_in_good="yes (default) fills empty move-in condition codes as GOOD.",
    landlord_signature_name="Typed name to sign the move-in pass as landlord.",
    start_move_out="yes to open the move-out pass (only after move-in signed).",
)
def complete_inspection_package(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    fill_move_in_good: str = "1",
    landlord_signature_name: str = "",
    start_move_out: str = "0",
    move_out_date: str = "",
    confirm: str = "",
) -> dict:
    """Create or complete a condition inspection package: ensure report
    exists, fill empty move-in codes GOOD, optional landlord sign, optional
    start move-out. Never forges tenant signatures. Preview; confirm=yes."""
    from .domain_composites import complete_inspection_package as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        tenant_email=tenant_email,
        fill_move_in_good=fill_move_in_good,
        landlord_signature_name=landlord_signature_name,
        start_move_out=start_move_out,
        move_out_date=move_out_date,
        confirm=confirm,
    )


@_params(
    adjustment_type="DISCOUNT | INCREASE | PRORATION | OTHER.",
    amount="Dollars for FLAT_AMOUNT, or percent for PERCENTAGE.",
    target_lease_total=(
        "Optional exact household rent total for the effective period. The "
        "backend derives and allocates the required discount/increase; do not "
        "calculate the difference yourself."
    ),
    calculation_method="FLAT_AMOUNT (default) | PERCENTAGE | EXACT_NIGHTLY.",
    effective_date="When the adjustment starts (YYYY-MM-DD, default today).",
)
def apply_rent_adjustment(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    adjustment_type: str = "DISCOUNT",
    amount: str = "",
    target_lease_total: str = "",
    expected_current_total: str = "",
    calculation_method: str = "FLAT_AMOUNT",
    reason: str = "",
    effective_date: str = "",
    end_date: str = "",
    is_recurring: str = "0",
    confirm: str = "",
) -> dict:
    """Record a rent discount/increase on a lease tenant and reconcile open
    rent charges (same as UI rent-adjustments). Preview; confirm=yes."""
    from .domain_composites import apply_rent_adjustment as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        tenant_email=tenant_email,
        adjustment_type=adjustment_type,
        amount=amount,
        target_lease_total=target_lease_total,
        expected_current_total=expected_current_total,
        calculation_method=calculation_method,
        reason=reason,
        effective_date=effective_date,
        end_date=end_date,
        is_recurring=is_recurring,
        confirm=confirm,
    )


@_params(
    total_amount="Full bill amount the utility company charged.",
    period_start="Billing period start YYYY-MM-DD.",
    period_end="Billing period end YYYY-MM-DD.",
    bill_key="Optional key from lease bills_included (e.g. electricity).",
    record_landlord_expense="yes to also book the full bill as landlord expense.",
)
def record_utility_bill(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    total_amount: str = "",
    period_start: str = "",
    period_end: str = "",
    description: str = "Utility bill",
    bill_key: str = "",
    due_date: str = "",
    record_landlord_expense: str = "0",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Post a utility bill to a lease: tenant share per bills_included, optional
    landlord expense for the full amount (POST /api/ledger/utility-bills/).
    Preview; confirm=yes."""
    from .domain_composites import record_utility_bill as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        total_amount=total_amount,
        period_start=period_start,
        period_end=period_end,
        description=description,
        bill_key=bill_key,
        due_date=due_date,
        record_landlord_expense=record_landlord_expense,
        vendor=vendor,
        confirm=confirm,
    )


@_params(
    inquiry_id="Inquiry UUID from list_inquiries (preferred).",
    name_query="Prospect name if you don't have the id.",
    when="Viewing datetime YYYY-MM-DD HH:MM (America/Vancouver).",
)
def convert_inquiry_to_viewing(
    landlord,
    inquiry_id: str = "",
    name_query: str = "",
    when: str = "",
    confirm: str = "",
) -> dict:
    """Turn a listing inquiry into a scheduled viewing, carrying name/email/
    phone and marking the inquiry replied (UI to_appointment). Preview;
    confirm=yes."""
    from .domain_composites import convert_inquiry_to_viewing as _fn

    return _fn(
        landlord,
        inquiry_id=inquiry_id,
        name_query=name_query,
        when=when,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------
# API gap-close tools (ledger, inspection, viewing, collection)
# ---------------------------------------------------------------------------


@_params(
    entry_id="Exact ledger entry UUID when known.",
    description_query="Words from the expense description, e.g. 'window screens'.",
    amount="Optional amount to narrow the match, e.g. '125' or '125.00'.",
    reason="Why it is voided — stored on the audit trail.",
    void_all="yes to void EVERY open match (use when two duplicate wrong posts).",
    confirm="Leave empty to preview; yes to void.",
)
def void_ledger_entry(
    landlord,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    reason: str = "",
    void_all: str = "",
    confirm: str = "",
) -> dict:
    """Void an expense/ledger entry via REVERSAL (never deletes). Use when the
    landlord says void/cancel/undo a wrong expense — NEVER create_expense with
    description 'void…'. reason required. Pass void_all=yes for duplicates.
    Preview; confirm=yes."""
    from .domain_gap_tools import void_ledger_entry as _fn

    return _fn(
        landlord,
        entry_id=entry_id,
        description_query=description_query,
        amount=amount,
        reason=reason,
        void_all=void_all,
        confirm=confirm,
    )


def mark_ledger_paid(
    landlord,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    paid_on: str = "",
    unmark: str = "0",
    confirm: str = "",
) -> dict:
    """Mark expense bank-cleared (paid_on) or unmark=yes. Use when landlord says
    'mark the Draino expense paid' or 'why not yet taken'. Preview; confirm=yes."""
    from .domain_gap_tools import mark_ledger_paid as _fn

    return _fn(
        landlord,
        entry_id=entry_id,
        description_query=description_query,
        amount=amount,
        paid_on=paid_on,
        unmark=unmark,
        confirm=confirm,
    )


def correct_ledger_entry(
    landlord,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    description: str = "",
    category: str = "",
    vendor: str = "",
    due_date: str = "",
    effective_date: str = "",
    reference_number: str = "",
    reason: str = "Correction",
    confirm: str = "",
) -> dict:
    """Correct a posted entry by void+repost. Preview; confirm=yes."""
    from .domain_gap_tools import correct_ledger_entry as _fn

    return _fn(
        landlord,
        entry_id=entry_id,
        description_query=description_query,
        amount=amount,
        description=description,
        category=category,
        vendor=vendor,
        due_date=due_date,
        effective_date=effective_date,
        reference_number=reference_number,
        reason=reason,
        confirm=confirm,
    )


def post_ledger_credit(
    landlord,
    entry_id: str = "",
    description_query: str = "",
    amount: str = "",
    reason: str = "Credit",
    confirm: str = "",
) -> dict:
    """Post goodwill/discount credit against a charge. Preview; confirm=yes."""
    from .domain_gap_tools import post_ledger_credit as _fn

    return _fn(
        landlord,
        entry_id=entry_id,
        description_query=description_query,
        amount=amount,
        reason=reason,
        confirm=confirm,
    )


def post_one_off_charge(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    amount: str = "",
    due_date: str = "",
    description: str = "Charge",
    entry_type: str = "OTHER_CHARGE",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """One-off charge (damage, late fee) on a lease. Preview; confirm=yes."""
    from .domain_gap_tools import post_one_off_charge as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        amount=amount,
        due_date=due_date,
        description=description,
        entry_type=entry_type,
        tenant_email=tenant_email,
        confirm=confirm,
    )


def update_inspection_items(
    landlord,
    inspection_id: str = "",
    lease_number: str = "",
    items_json: str = "",
    fill_empty_move_in_good: str = "0",
    confirm: str = "",
) -> dict:
    """Bulk-update condition inspection item codes. Preview; confirm=yes."""
    from .domain_gap_tools import update_inspection_items as _fn

    return _fn(
        landlord,
        inspection_id=inspection_id,
        lease_number=lease_number,
        items_json=items_json,
        fill_empty_move_in_good=fill_empty_move_in_good,
        confirm=confirm,
    )


def approve_inspection_suggestion(
    landlord, item_id: str = "", inspection_id: str = "", confirm: str = "",
) -> dict:
    """Approve inspection damage suggestion → work order. Preview; confirm=yes."""
    from .domain_gap_tools import approve_inspection_suggestion as _fn

    return _fn(
        landlord,
        item_id=item_id,
        inspection_id=inspection_id,
        confirm=confirm,
    )


def dismiss_inspection_suggestion(
    landlord, item_id: str = "", confirm: str = "",
) -> dict:
    """Dismiss inspection maintenance suggestion. Preview; confirm=yes."""
    from .domain_gap_tools import dismiss_inspection_suggestion as _fn

    return _fn(landlord, item_id=item_id, confirm=confirm)


def mark_inspection_delivered(
    landlord,
    inspection_id: str = "",
    lease_number: str = "",
    inspection_pass: str = "MOVE_IN",
    confirm: str = "",
) -> dict:
    """Stamp that the tenant received their inspection report copy.
    Preview; confirm=yes."""
    from .domain_gap_tools import mark_inspection_delivered as _fn

    return _fn(
        landlord,
        inspection_id=inspection_id,
        lease_number=lease_number,
        inspection_pass=inspection_pass,
        confirm=confirm,
    )


@_params(
    appointment_id="Exact appointment UUID when known.",
    request_ref="Short ref from list_viewing_requests / list_appointments.",
    property_query="Listing name when unique, e.g. 'Garden Suite'.",
    contact="Prospect name or email, e.g. 'Ishupreet' or 'ishu@gmail.com'.",
    reason="Optional why cancelled (audit/email).",
    confirm="Leave empty to preview; yes to cancel.",
)
def cancel_viewing(
    landlord,
    appointment_id: str = "",
    request_ref: str = "",
    property_query: str = "",
    contact: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Cancel a pending or scheduled viewing (emails prospect when possible).
    Prefer contact= name/email. Preview; confirm=yes."""
    from .domain_gap_tools import cancel_viewing as _fn

    return _fn(
        landlord,
        appointment_id=appointment_id,
        request_ref=request_ref,
        property_query=property_query,
        contact=contact,
        reason=reason,
        confirm=confirm,
    )


@_params(
    person_query="Tenant name or email, e.g. 'Siya' or 'siya@gmail.com'.",
)
def tenant_lease_status(landlord, person_query: str = "") -> dict:
    """Has this person signed or opened their lease invite?

    Use for 'has Siya signed?', 'has she seen the lease?', 'when did they open
    the invite?'. Portfolio-wide search. Read-only."""
    from .domain_actions import tenant_lease_status as _fn

    return _fn(landlord, person_query=person_query)


def mark_cleaning_deposit_paid(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """Mark a refundable cleaning deposit paid for a lease tenant. Preview; confirm=yes."""
    from .domain_gap_tools import mark_cleaning_deposit_paid as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        tenant_email=tenant_email,
        confirm=confirm,
    )


@_params(
    deposit=(
        "Which deposit this comes out of: security, pet, or cleaning. They are "
        "held and returned separately — never guess, ask."
    ),
    basis=(
        "labour (your own time) | supplies | cleaner | garbage | other. "
        "Take it from what the landlord said."
    ),
    hours="For basis=labour: hours spent, e.g. '3'. Needs hourly_rate too.",
    hourly_rate="For basis=labour: rate per hour, e.g. '35'.",
    amount="For every other basis: what it cost, e.g. '80.00'.",
    note="What was cleaned, hauled or repaired. A bare amount loses a hearing.",
    tenant_email="Roommate leases have one inspection each — whose is this?",
)
def record_deposit_deduction(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    tenant_email: str = "",
    deposit: str = "cleaning",
    basis: str = "",
    hours: str = "",
    hourly_rate: str = "",
    amount: str = "",
    note: str = "",
    confirm: str = "",
) -> dict:
    """Record ONE costed line of what the landlord wants to keep from a deposit
    — own labour (hours x rate), supplies, professional cleaners, garbage
    removal. Attaches to the move-out inspection as evidence. Recording it
    KEEPS NOTHING: deposit money can only be kept with the tenant's written
    agreement or an RTB order. Preview first; confirm=yes."""
    from .domain_gap_tools import record_deposit_deduction as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        tenant_email=tenant_email,
        deposit=deposit,
        basis=basis,
        hours=hours,
        hourly_rate=hourly_rate,
        amount=amount,
        note=note,
        confirm=confirm,
    )


@_params(
    payment_method=(
        "etransfer | cash | cheque. How the money is going back. Leave BLANK "
        "if the landlord did not say and the tool will ask. Never guess."
    ),
    return_date="Date the money went back, YYYY-MM-DD. Defaults to today.",
)
def return_deposits(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    payment_method: str = "",
    return_date: str = "",
    confirm: str = "",
) -> dict:
    """Return the deposits held on a lease at the end of a tenancy. Each
    deposit (security, pet, cleaning) goes back as its OWN transfer, never as
    one lump. Deductions the tenant already agreed to in writing are held back
    automatically; nothing else is. Preview first; confirm=yes."""
    from .domain_gap_tools import return_deposits as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        payment_method=payment_method,
        return_date=return_date,
        confirm=confirm,
    )


def list_payment_reminders(
    landlord, lease_number: str = "", pending_only: str = "1",
) -> dict:
    """List payment reminders (pending by default)."""
    from .domain_gap_tools import list_payment_reminders as _fn

    return _fn(landlord, lease_number=lease_number, pending_only=pending_only)


def create_payment_reminder(
    landlord,
    lease_number: str = "",
    property_query: str = "",
    reminder_date: str = "",
    message: str = "",
    send_method: str = "EMAIL",
    confirm: str = "",
) -> dict:
    """Schedule a payment reminder on an open legacy Payment. Preview; confirm=yes."""
    from .domain_gap_tools import create_payment_reminder as _fn

    return _fn(
        landlord,
        lease_number=lease_number,
        property_query=property_query,
        reminder_date=reminder_date,
        message=message,
        send_method=send_method,
        confirm=confirm,
    )


def mark_payment_reminder_sent(
    landlord, reminder_id: str = "", confirm: str = "",
) -> dict:
    """Mark a payment reminder as sent. Preview; confirm=yes."""
    from .domain_gap_tools import mark_payment_reminder_sent as _fn

    return _fn(landlord, reminder_id=reminder_id, confirm=confirm)


def update_inquiry(
    landlord,
    inquiry_id: str = "",
    name_query: str = "",
    status: str = "",
    landlord_notes: str = "",
    confirm: str = "",
) -> dict:
    """Update inquiry status (e.g. ARCHIVED) or landlord notes. Preview; confirm=yes."""
    from .domain_gap_tools import update_inquiry as _fn

    return _fn(
        landlord,
        inquiry_id=inquiry_id,
        name_query=name_query,
        status=status,
        landlord_notes=landlord_notes,
        confirm=confirm,
    )


def commit_import_batch(
    landlord, batch_id: str = "", confirm: str = "",
) -> dict:
    """Commit a DRAFT ledger import batch into live ledger rows. Preview; confirm=yes."""
    from .domain_gap_tools import commit_import_batch as _fn

    return _fn(landlord, batch_id=batch_id, confirm=confirm)


def discard_import_batch(
    landlord, batch_id: str = "", confirm: str = "",
) -> dict:
    """Discard a DRAFT import batch without posting. Preview; confirm=yes."""
    from .domain_gap_tools import discard_import_batch as _fn

    return _fn(landlord, batch_id=batch_id, confirm=confirm)


def list_notifications(
    landlord, unread_only: str = "1", limit: str = "30",
) -> dict:
    """List in-app notifications for this landlord."""
    from .domain_gap_tools import list_notifications as _fn

    return _fn(landlord, unread_only=unread_only, limit=limit)


def mark_notifications_read(
    landlord,
    notification_id: str = "",
    all_unread: str = "0",
    confirm: str = "",
) -> dict:
    """Mark one notification or all unread as read. Preview; confirm=yes."""
    from .domain_gap_tools import mark_notifications_read as _fn

    return _fn(
        landlord,
        notification_id=notification_id,
        all_unread=all_unread,
        confirm=confirm,
    )


@_params(
    name="The co-host / co-landlord's name (required).",
    email="Their email (optional, for the agreement + notice).",
    phone="Their phone (optional).",
    property_query="The listing whose lease to edit (name or id), OR use lease_number.",
    lease_number="The lease to edit (alternative to property_query).",
    remove="yes to REMOVE this co-host instead of adding.",
    confirm="Leave empty to preview; 'yes' to apply.",
)
def add_co_host_to_lease(
    landlord,
    name: str,
    email: str = "",
    phone: str = "",
    property_query: str = "",
    lease_number: str = "",
    remove: str = "",
    confirm: str = "",
) -> dict:
    """Add (or remove) a CO-HOST / CO-LANDLORD on a lease — a second landlord
    party (partner, co-owner, manager) recorded on the agreement and reachable
    for notice. Use for 'add a co-host/co-landlord to this lease'. This records
    them on the document; it does NOT create an app login for them. Preview;
    confirm=yes."""
    from .domain_actions import add_co_host_to_lease as _fn
    return _fn(
        landlord, name=name, email=email, phone=phone,
        property_query=property_query, lease_number=lease_number,
        remove=remove, confirm=confirm,
    )


@_params(
    name="The co-landlord's name.",
    email="Their email — the account that gets access (required).",
    remove="yes to REVOKE their access instead of granting it.",
    confirm="Leave empty to preview; 'yes' to apply.",
)
def add_co_landlord(
    landlord,
    name: str = "",
    email: str = "",
    property_query: str = "",
    lease_number: str = "",
    remove: str = "",
    confirm: str = "",
) -> dict:
    """Invite a co-landlord who signs in, manages properties/leases, AND co-signs
    leases. Use for 'add a co-landlord / give someone access to manage my
    properties / add another landlord to this property or lease'. SCOPE it:
    property_query = tie them to ONE property + its group (every FUTURE lease
    there names them as a co-signing landlord); lease_number = ALSO co-sign that
    existing lease and grant its property; NEITHER = whole-portfolio access. The
    lease only activates once every landlord (incl. co-signers) and a tenant sign.
    Invites by email; links immediately if they already have an account.
    Preview; confirm=yes."""
    from .domain_actions import add_co_landlord as _fn
    return _fn(
        landlord, name=name, email=email, property_query=property_query,
        lease_number=lease_number, remove=remove, confirm=confirm,
    )


def list_co_landlords(landlord) -> dict:
    """The co-landlords / property managers with access to this portfolio (active
    or invited). Answers 'who can manage my properties?'."""
    from .domain_actions import list_co_landlords as _fn
    return _fn(landlord)


def delete_draft_lease(
    landlord, property_query: str = "", lease_number: str = "", confirm: str = "",
) -> dict:
    """Delete ONLY a DRAFT lease. Pending/active → terminate_lease. Preview; confirm=yes."""
    from .domain_crud import delete_draft_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm,
    )


def terminate_lease(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    termination_date: str = "",
    move_out_date: str = "",
    confirm: str = "",
) -> dict:
    """Terminate pending/active lease (not draft). Voids open charges, closes occupancy.
    Same as UI Terminate. Preview; confirm=yes."""
    from .domain_crud import terminate_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
        termination_date=termination_date, move_out_date=move_out_date, confirm=confirm,
    )


def landlord_sign_lease(
    landlord, property_query: str = "", lease_number: str = "", confirm: str = "",
) -> dict:
    """Record landlord signature. Rent must be fully allocated across tenants first.
    May activate lease if a tenant already signed. Preview; confirm=yes."""
    from .domain_crud import landlord_sign_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm,
    )


def update_work_order(
    landlord,
    work_order_id: str = "",
    title_query: str = "",
    title: str = "",
    description: str = "",
    priority: str = "",
    category: str = "",
    contractor_name: str = "",
    contractor_phone: str = "",
    scheduled_date: str = "",
    confirm: str = "",
) -> dict:
    """Update work order fields (not status). Status → transition_work_order or
    complete_work_order. Never delete WOs — cancel via transition. Preview; confirm=yes."""
    from .domain_crud import update_work_order as _fn
    return _fn(
        landlord, work_order_id=work_order_id, title_query=title_query, title=title,
        description=description, priority=priority, category=category,
        contractor_name=contractor_name, contractor_phone=contractor_phone,
        scheduled_date=scheduled_date, confirm=confirm,
    )


def complete_work_order(
    landlord,
    work_order_id: str = "",
    title_query: str = "",
    cost: str = "",
    post_expense: str = "0",
    vendor: str = "",
    confirm: str = "",
) -> dict:
    """Complete a work order (FSM). Optional cost; post_expense=yes books MAINTENANCE
    ledger entry (same as UI complete). Preview; confirm=yes."""
    from .domain_crud import complete_work_order as _fn
    return _fn(
        landlord, work_order_id=work_order_id, title_query=title_query, cost=cost,
        post_expense=post_expense, vendor=vendor, confirm=confirm,
    )


def add_work_order_comment(
    landlord,
    body: str,
    work_order_id: str = "",
    title_query: str = "",
    confirm: str = "",
) -> dict:
    """Add a comment on a work order. Preview; confirm=yes."""
    from .domain_crud import add_work_order_comment as _fn
    return _fn(
        landlord, body=body, work_order_id=work_order_id, title_query=title_query,
        confirm=confirm,
    )


def create_inventory_item(
    landlord,
    property_query: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Add private inventory/furniture to a listing. Preview; confirm=yes."""
    from .domain_crud import create_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, name=name, quantity=quantity,
        condition=condition, location=location, description=description, confirm=confirm,
    )


def update_inventory_item(
    landlord,
    property_query: str,
    item_name: str,
    name: str = "",
    quantity: str = "",
    condition: str = "",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Update a private inventory item by name match. Preview; confirm=yes."""
    from .domain_crud import update_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, item_name=item_name, name=name,
        quantity=quantity, condition=condition, location=location,
        description=description, confirm=confirm,
    )


def delete_inventory_item(
    landlord, property_query: str, item_name: str, confirm: str = "",
) -> dict:
    """Delete private inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, item_name=item_name, confirm=confirm,
    )


def create_shared_inventory_item(
    landlord,
    group_name: str,
    name: str,
    quantity: str = "1",
    condition: str = "GOOD",
    location: str = "",
    description: str = "",
    confirm: str = "",
) -> dict:
    """Add shared inventory to a property group (e.g. kitchen). Preview; confirm=yes."""
    from .domain_crud import create_shared_inventory_item as _fn
    return _fn(
        landlord, group_name=group_name, name=name, quantity=quantity,
        condition=condition, location=location, description=description, confirm=confirm,
    )


def delete_shared_inventory_item(
    landlord, group_name: str, item_name: str, confirm: str = "",
) -> dict:
    """Delete shared inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_shared_inventory_item as _fn
    return _fn(
        landlord, group_name=group_name, item_name=item_name, confirm=confirm,
    )


def crud_capabilities(landlord) -> dict:
    """List what CRUD the agent can do and UI restrictions (read-only help).
    Call when unsure which write tool to use or what is forbidden."""
    from .domain_crud import crud_capabilities as _fn
    return _fn(landlord)


# ---------------------------------------------------------------------------
# Finders + plans: deterministic set-scoping and multi-step chains
# ---------------------------------------------------------------------------


def find_listings(
    landlord,
    has_images: str = "",
    vacant_today: str = "",
    has_lease: str = "",
    listing_status: str = "",
    group: str = "",
    name_contains: str = "",
    exclude: str = "",
    include_parked: str = "",
) -> dict:
    """Find listings matching filters — USE THIS whenever the landlord scopes a
    request over a set ('all/every listing that/without …'). Never enumerate or
    filter listings yourself; this returns the COMPLETE set, grounded
    (image_count, lease_count, work orders, vacancy). Filters ('' = any):
    has_images yes/no, vacant_today yes/no, has_lease yes/no (any lease incl.
    drafts — blocks deletion), listing_status, group, name_contains,
    exclude='name or id, …' (kept out, echoed back)."""
    from .domain_reads import find_listings as _fn
    return _fn(
        landlord, has_images=has_images, vacant_today=vacant_today,
        has_lease=has_lease, listing_status=listing_status, group=group,
        name_contains=name_contains, exclude=exclude,
        include_parked=include_parked,
    )


def find_leases(
    landlord,
    status: str = "",
    property_query: str = "",
    ending_before: str = "",
    include_ended: str = "",
) -> dict:
    """Find leases matching filters — use for set questions over leases
    ('all draft leases', 'leases ending before …'). Returns the COMPLETE set.
    Filters: status DRAFT|PENDING_SIGNATURES|SIGNED|ACTIVE|TERMINATED|EXPIRED|
    RENEWED, property_query, ending_before YYYY-MM-DD, include_ended yes."""
    from .domain_reads import find_leases as _fn
    return _fn(
        landlord, status=status, property_query=property_query,
        ending_before=ending_before, include_ended=include_ended,
    )


@_params(
    operation="delete_listings | terminate_and_delete | update_status.",
    include="Operate on ONLY these named listings, comma-separated (do not "
    "combine with filters). An id here targets exactly one listing.",
    pick="If an include name matches two listings (duplicates), which one: "
    "oldest|newest|1|2. 'the old one'→oldest, 'the new one'→newest.",
    exclude="Names the landlord wants to KEEP ('except X' → exclude='X').",
    has_images="Filter: yes|no.",
    vacant_today="Filter: yes|no.",
    has_lease="Filter: yes|no.",
    listing_status="Filter by status, e.g. AVAILABLE.",
    group="Filter to one property group.",
    name_contains="Filter to listings whose name contains this text.",
    new_status="Required for update_status: the target status.",
    confirm="Leave empty — the system handles confirmation of the returned plan.",
)
def plan_operation(
    landlord,
    operation: str,
    include: str = "",
    pick: str = "",
    exclude: str = "",
    has_images: str = "",
    vacant_today: str = "",
    has_lease: str = "",
    listing_status: str = "",
    group: str = "",
    name_contains: str = "",
    new_status: str = "",
    new_mode: str = "",
    visible: str = "",
    holding: str = "",
    confirm: str = "",
) -> dict:
    """Build a multi-step PLAN over a set of listings — ALWAYS use this (never a
    hand-rolled tool sequence) for bulk or multi-step asks like 'delete all X'.
    operation over LISTINGS: delete_listings | terminate_and_delete (end leases
    first, then delete — use when landlord says delete listings that still have
    leases) | retire_listings (take off the market, keep everything) |
    update_status (needs new_status) | set_visibility (needs visible=yes|no).
    operation over UNITS: switch_rental_mode (needs new_mode=WHOLE_UNIT|BY_ROOM;
    scope with include='Main Floor, Basement' and holding='950 McKenzie Ave').
    Scoping: filters (has_images/vacant_today/has_lease/listing_status/group/
    name_contains) select the set; exclude='name' = the items the landlord
    wants to KEEP ('except X / keep X' → exclude='X', NOTHING else changes);
    include='name, name' = operate on ONLY those named listings (do not
    combine with filters).
    EXAMPLE: 'delete all listings that have no images except Garden Suite' →
    operation=delete_listings, has_images=no, exclude='Garden Suite'.
    DUPLICATES: if an include name matches two listings the result comes back
    with needs_disambiguation (NOT blocked) — ask which, then re-call with
    pick=oldest|newest ('the old one'→oldest). Returns the full plan + blocked
    items with reasons; the SYSTEM confirms and executes it — show the whole
    plan, ask question_for_user if present, then STOP."""
    from .playbooks import plan_operation as _fn
    return _fn(
        landlord, operation=operation, include=include, pick=pick, exclude=exclude,
        has_images=has_images, vacant_today=vacant_today, has_lease=has_lease,
        listing_status=listing_status, group=group, name_contains=name_contains,
        new_status=new_status, new_mode=new_mode, visible=visible,
        holding=holding, confirm=confirm,
    )


def plan_move_tenant(
    landlord,
    tenant: str,
    from_property: str,
    to_property: str,
    start_date: str = "",
    total_rent: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Build a PLAN that moves a tenant to another room: end the current lease
    (own confirmation), then new lease + signing invite + move-in inspection on
    the target room. tenant: name or email. total_rent defaults to the old
    lease's rent; start_date YYYY-MM-DD (default today). If a room name matches
    more than one listing, re-call with pick=oldest|newest. Show the plan, then
    STOP — the system confirms and executes."""
    from .playbooks import plan_move_tenant as _fn
    return _fn(
        landlord, tenant=tenant, from_property=from_property,
        to_property=to_property, start_date=start_date, total_rent=total_rent,
        pick=pick, confirm=confirm,
    )


# ---------------------------------------------------------------------------
# Constitution: the landlord's written policy (read + guarded amendment)
# ---------------------------------------------------------------------------


def read_constitution(landlord) -> dict:
    """Read the landlord's Constitution: policy sections (markdown) and the
    structured rules sentinels enforce (MIN_BALANCE, GRACE_PERIOD, LATE_FEE,
    VENDOR_PREFERENCE, AUTO_RECORD_PAYMENT). Treat it as authoritative."""
    from .constitution import section_payload
    return section_payload(landlord)


def amend_constitution(
    landlord,
    key: str,
    title: str = "",
    new_body_md: str = "",
    rule_changes: str = "",
    confirm: str = "",
) -> dict:
    """Amend one Constitution section (creates a NEW version — append-only;
    never edits in place). key: balances|vendors|tenant-policies|workflows or
    a new slug. new_body_md: full replacement markdown ('' keeps current).
    rule_changes: JSON list, e.g.
    [{"action":"add","rule_type":"MIN_BALANCE","params":{"property_id":null,
    "amount":"5000.00"}}, {"action":"remove","rule_id":3}].
    ALWAYS preview first (no confirm) and show the landlord exactly what will
    change; confirm=yes applies. Never amend silently."""
    from .constitution import amend
    from .constitution import parse_rule_changes
    from .constitution import unlawful_deposit_language
    from .domain_crud import _confirmed
    from .domain_crud import _preview
    from .models import RamaConstitutionSection

    key_s = (key or "").strip().lower()
    if not key_s:
        return {"error": "key is required (e.g. balances, vendors)."}
    changes, err = parse_rule_changes(rule_changes)
    if err:
        return {"error": err}

    # Whatever goes in here, RAMA reads back as policy and acts on later.
    unlawful = unlawful_deposit_language(f"{title}\n{new_body_md}")
    if unlawful:
        return {
            "error": unlawful,
            "suggested_wording": (
                "Tenant-caused damage is charged to the tenant's ledger as a "
                "claim they owe. It is not taken from their deposit unless "
                "they agree in writing, or the landlord applies to the RTB "
                "within 15 days of the tenancy ending."
            ),
            "relay_instruction": (
                "Do NOT amend the Constitution with the original wording. "
                "Explain why to the landlord and offer suggested_wording."
            ),
        }

    preview = {
        "section": key_s,
        "title": title or "(unchanged)",
        "new_body_md": (new_body_md or "(unchanged)")[:1500],
        "rule_changes": changes or "(none)",
        "note": "Creates a new version; the old version stays in history.",
    }
    if not _confirmed(confirm):
        return _preview(
            "amend_constitution", preview, "Amends the landlord's Constitution.",
        )
    return amend(
        landlord,
        key=key_s,
        title=title,
        body_md=new_body_md,
        rule_changes=changes,
        origin=RamaConstitutionSection.Origin.GENERAL_PROPOSAL,
    )


def list_vendors(landlord) -> dict:
    """Preferred vendors/contractors from the Constitution's
    VENDOR_PREFERENCE rules (trade, name, phone, priority). Use when picking
    who to contact for maintenance."""
    from .constitution import active_rules

    vendors = [
        {"id": r.pk, **(r.params or {})}
        for r in active_rules(landlord, "VENDOR_PREFERENCE")
    ]
    vendors.sort(key=lambda v: v.get("priority", 99))
    return {
        "vendors": vendors,
        "count": len(vendors),
        "instruction": (
            "These are the landlord's preferred vendors, in priority order. "
            "If empty, ask the landlord who they use."
        ),
    }


def create_property_structure(  # noqa: PLR0913 - explicit public tool fields
    landlord,
    holding_name: str,
    address: str,
    units_json: str,
    city: str = "",
    province: str = "",
    confirm: str = "",
) -> dict:
    """Record a building as UNITS (floors/suites) with their internal layout.
    PREFER THIS over create_house_layout. Bedrooms described inside a unit are
    internal layout, NOT rentable listings — set rental_mode=BY_ROOM only when
    the landlord lets the bedrooms separately to different people. units_json:
    [{"name":"Main Floor","rental_mode":"WHOLE_UNIT","spaces":[{"name":"Master
    Bedroom","type":"BEDROOM"},{"name":"Ensuite","type":"BATHROOM","serves":
    ["Master Bedroom"]},{"name":"Kitchen"}]}]. Leave rental_mode out when the
    landlord hasn't said — the tool asks once instead of guessing. One preview,
    then confirm=yes."""
    from .unit_structure import create_property_structure as _fn

    return _fn(
        landlord,
        holding_name=holding_name,
        address=address,
        city=city,
        province=province,
        units_json=units_json,
        confirm=confirm,
    )


def update_unit_layout(
    landlord,
    unit_name: str,
    spaces_json: str = "",
    layout_complete: str = "",
    missing: str = "",
    confirm: str = "",
) -> dict:
    """Record what is INSIDE one unit — bedrooms, bathrooms, kitchen, living
    room. Never creates a listing: describing a bedroom does not put it on the
    market. spaces_json: [{"name":"Master Bedroom","type":"BEDROOM"},
    {"name":"Second Bathroom","type":"BATHROOM","serves":["Bedroom 2",
    "Bedroom 3"]}]. Use `missing` to say what is still unknown instead of
    guessing it. One preview, then confirm=yes."""
    from .unit_structure import update_unit_layout as _fn

    return _fn(
        landlord,
        unit_name=unit_name,
        spaces_json=spaces_json,
        layout_complete=layout_complete,
        missing=missing,
        confirm=confirm,
    )


def set_unit_rental_mode(
    landlord,
    unit_name: str,
    rental_mode: str,
    holding: str = "",
    confirm: str = "",
) -> dict:
    """Switch a unit between being let as ONE home (WHOLE_UNIT) and let room by
    room (BY_ROOM). Nothing is deleted — the other mode's listings are parked
    and return if you switch back. Refused while any draft, pending or active
    lease exists anywhere in the unit. Prefer unit_name=<uuid>; when using a
    free-text name that exists on more than one house, pass holding=<street
    or house name> (e.g. holding='950 McKenzie Ave'). One preview, then
    confirm=yes."""
    from .unit_structure import set_unit_rental_mode as _fn

    return _fn(
        landlord,
        unit_name=unit_name,
        rental_mode=rental_mode,
        holding=holding,
        confirm=confirm,
    )


def configure_unit_room_offerings(
    landlord,
    unit_name: str,
    room_names_json: str,
    group_name: str = "",
    shared_areas_json: str = "",
    holding: str = "",
    confirm: str = "",
) -> dict:
    """Convert an existing suite/floor into named room-by-room rentals in ONE
    atomic operation. Use this whenever the landlord wants to add rooms into a
    whole suite, turn a complete unit into a property group, or rent bedrooms
    separately. room_names_json is a JSON list of the landlord's exact room
    labels, e.g. ["Bonus room J", "Room K"] — never invent sequential letters
    like L/M. shared_areas_json records kitchen/washroom/patio etc in the same
    call. Prefer unit_name=<uuid>, or pass holding=<street> when names collide.
    Parks the complete-unit listing (never deletes it). Refuses while draft/
    pending/active leases exist. One complete preview, then confirm=yes."""
    from .unit_structure import configure_unit_room_offerings as _fn

    return _fn(
        landlord,
        unit_name=unit_name,
        room_names_json=room_names_json,
        group_name=group_name,
        shared_areas_json=shared_areas_json,
        holding=holding,
        confirm=confirm,
    )


def triage_capability_gap(
    landlord,
    gap_query: str,
    status: str = "",
    prioritise: str = "",
    confirm: str = "",
) -> dict:
    """Move a logged capability gap through the backlog: status=REVIEWED |
    BUILT | DISMISSED, prioritise=yes to flag it as wanted next. Records a
    human decision only — it never builds or runs anything. gap_query is the
    gap id or distinctive words from the request. One preview, then
    confirm=yes."""
    from .domain_actions import triage_capability_gap as _fn

    return _fn(
        landlord,
        gap_query=gap_query,
        status=status,
        prioritise=prioritise,
        confirm=confirm,
    )


def attribute_work_order(
    landlord,
    work_order_id: str = "",
    title_query: str = "",
    tenant: str = "",
    chargeable: str = "",
    confirm: str = "",
) -> dict:
    """Record WHO caused a repair and whether they are being charged for it —
    e.g. "the shower knob was broken by the tenant in Room C". Use as soon as
    the landlord says a tenant caused the damage, because at move-out nobody
    remembers, and the evidence has to exist before the deposit clock starts.
    chargeable=yes raises a claim the tenant owes once the job is completed
    with a cost. It NEVER deducts from a deposit — say so if asked. One
    preview, then confirm=yes."""
    from .domain_crud import attribute_work_order as _fn

    return _fn(
        landlord,
        work_order_id=work_order_id,
        title_query=title_query,
        tenant=tenant,
        chargeable=chargeable,
        confirm=confirm,
    )


def deposit_position(landlord, lease_number: str = "") -> dict:
    """What is held as a deposit on a lease, what is claimed against it, and
    the 15-day deadline. Answers "how much of their deposit can I keep?" —
    the answer is NEVER "just deduct it": BC law allows keeping deposit money
    only with the tenant's WRITTEN agreement or an RTB application within 15
    days of the tenancy ending, and getting it wrong means the claim is lost
    AND double the deposit becomes payable. Relay lawful_routes and the
    deadline whenever a landlord talks about keeping a deposit."""
    from .domain_crud import deposit_position as _fn

    return _fn(landlord, lease_number=lease_number)


def tenant_statement(landlord, tenant: str, lease_number: str = "") -> dict:
    """Everything one tenant owes and has paid: rent, utilities, damage claims
    and the deposit held, in one place. Answers "what does X owe?" without
    adding up three screens. Joint (household) charges are included and
    flagged — on a roommate lease each tenant is liable for the WHOLE
    household charge, not a share of it. Deposit money is reported separately
    and is never netted off what is owed."""
    from .domain_crud import tenant_statement as _fn

    return _fn(landlord, tenant=tenant, lease_number=lease_number)


def list_memories(landlord, query: str = "") -> dict:
    """What the landlord has told you to remember across conversations
    (preferences and standing instructions, NOT portfolio data). Answers
    'what do you know about how I work?' / 'what have I told you?'."""
    from .memory import payload

    return payload(landlord, query)


@_params(
    subject="Short label for what this is about, e.g. 'viewings' or 'bookkeeper'. "
    "Reusing a subject REPLACES the old preference on it.",
    fact="The standing preference, in one sentence, in the landlord's terms.",
    applies_to="Optional listing/property name when it is only true for one of them. "
    "Leave empty for portfolio-wide preferences.",
)
def remember(
    landlord,
    subject: str,
    fact: str,
    applies_to: str = "",
    confirm: str = "",
) -> dict:
    """Store a durable PREFERENCE or standing instruction the landlord stated,
    so it survives into future conversations ("invoices go to my bookkeeper
    Dana", "never viewings on Sundays", "call the basement suite the Garden").
    NEVER use for portfolio facts — rents, balances, dates, counts, occupancy
    and lease terms are read live every turn and must not be copied here.
    Reusing a subject replaces what you knew about it."""
    from .domain_memory import remember as _fn

    return _fn(
        landlord,
        subject=subject,
        fact=fact,
        applies_to=applies_to,
        confirm=confirm,
    )


def forget(landlord, subject: str, confirm: str = "") -> dict:
    """Drop a preference you were told to remember. subject is its label or
    distinctive words from it."""
    from .domain_memory import forget as _fn

    return _fn(landlord, subject=subject, confirm=confirm)


@_params(
    holding_name="The physical property, e.g. '950 McKenzie Ave'.",
    purchase_price="What was paid for it.",
    purchase_date="YYYY-MM-DD.",
    year_built="Year of construction — decides which improvements even apply.",
    heating_type="e.g. 'gas furnace', 'baseboard electric', 'heat pump'.",
    capital_improvements="Total spent on improvements since purchase. Raises "
    "the adjusted cost base and reduces the eventual capital gain.",
)
def record_holding_financials(
    landlord,
    holding_name: str,
    purchase_price: str = "",
    purchase_date: str = "",
    year_built: str = "",
    heating_type: str = "",
    capital_improvements: str = "",
    confirm: str = "",
) -> dict:
    """Record what a property cost to acquire. Used for return, equity and
    capital-gain estimates. Preview; confirm=yes."""
    from .domain_financials import record_holding_financials as _fn

    return _fn(
        landlord,
        holding_name=holding_name,
        purchase_price=purchase_price,
        purchase_date=purchase_date,
        year_built=year_built,
        heating_type=heating_type,
        capital_improvements=capital_improvements,
        confirm=confirm,
    )


@_params(
    holding_name="The physical property.",
    amount="What it is worth.",
    as_of="YYYY-MM-DD the figure was true on. Defaults to today.",
    basis="Where the number came from: BC_ASSESSMENT, REALTOR_CMA, APPRAISAL, "
    "LANDLORD_ESTIMATE or AUTOMATED. The basis changes how much weight it "
    "carries, so do not guess it.",
)
def record_valuation(
    landlord,
    holding_name: str,
    amount: str,
    as_of: str = "",
    basis: str = "LANDLORD_ESTIMATE",
    confirm: str = "",
) -> dict:
    """Record what a property is worth on a date. ADDS to the history rather
    than replacing it — the series is what makes equity trend and return
    computable. Preview; confirm=yes."""
    from .domain_financials import record_valuation as _fn

    return _fn(
        landlord,
        holding_name=holding_name,
        amount=amount,
        as_of=as_of,
        basis=basis,
        confirm=confirm,
    )


@_params(
    holding_name="The physical property.",
    current_principal="Balance still owing.",
    balance_as_of="YYYY-MM-DD that balance was true on. Required with a "
    "balance — without it there is no way to tell how stale the figure is.",
    rate_percent="Annual rate as a percentage, e.g. '4.5'.",
    payment_amount="The regular payment.",
    payment_frequency="MONTHLY, BIWEEKLY, ACCELERATED_BIWEEKLY, WEEKLY or "
    "SEMI_MONTHLY.",
    term_end="YYYY-MM-DD the term ends — the renewal date.",
    lender="Who holds the mortgage.",
)
def record_mortgage(
    landlord,
    holding_name: str,
    current_principal: str = "",
    balance_as_of: str = "",
    rate_percent: str = "",
    payment_amount: str = "",
    payment_frequency: str = "MONTHLY",
    term_end: str = "",
    lender: str = "",
    confirm: str = "",
) -> dict:
    """Record the mortgage on a property. Any existing one is kept as history
    rather than edited, so past rates stay answerable at renewal.
    Preview; confirm=yes."""
    from .domain_financials import record_mortgage as _fn

    return _fn(
        landlord,
        holding_name=holding_name,
        current_principal=current_principal,
        balance_as_of=balance_as_of,
        rate_percent=rate_percent,
        payment_amount=payment_amount,
        payment_frequency=payment_frequency,
        term_end=term_end,
        lender=lender,
        confirm=confirm,
    )


@_params(
    subject="Short label for what this fact is about, e.g. 'upstairs rent "
    "2024'. Reusing a subject REPLACES what was recorded under it.",
    fact="The fact in one sentence, in the landlord's own terms.",
    amount="The figure, e.g. '2000' or '$2,000/mo'.",
    period="ONE_TIME, MONTHLY or ANNUAL.",
    direction="INCOME (money in), EXPENSE (money out), or NEUTRAL (a rate, a "
    "value, a count).",
    holding_name="Which physical property this applies to. Leave blank for "
    "the whole portfolio.",
    effective_from="YYYY-MM-DD the figure starts applying. Required for a "
    "per-month or per-year amount.",
    effective_to="YYYY-MM-DD it stops applying.",
)
def record_treasurer_fact(
    landlord,
    subject: str,
    fact: str,
    amount: str = "",
    period: str = "",
    direction: str = "",
    holding_name: str = "",
    effective_from: str = "",
    effective_to: str = "",
    confirm: str = "",
) -> dict:
    """Record a financial fact the books do not have, so future analysis takes
    it into account — e.g. "we took $2,000/mo rent from another tenant for a
    year that isn\'t in the ledger". It is checked against the ledger first: if
    the books already record most of it, it is kept on file but left OUT of
    totals so it can\'t be double-counted. Preview; confirm=yes."""
    from .domain_treasurer import record_treasurer_fact as _fn

    return _fn(
        landlord,
        subject=subject,
        fact=fact,
        amount=amount,
        period=period,
        direction=direction,
        holding_name=holding_name,
        effective_from=effective_from,
        effective_to=effective_to,
        confirm=confirm,
    )
