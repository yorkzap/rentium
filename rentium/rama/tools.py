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


def data_catalogue(landlord) -> dict:
    """What RAMA can query with the generic `read` tool: the entities and their
    filterable/readable fields. Call this when a landlord asks a specific/composed
    question (e.g. 'active leases with parking and rent over 800') to see what
    fields exist, then use `read`."""
    from .manifest import entity_catalogue
    return {"entities": entity_catalogue()}


def read(landlord, entity: str = "", filters: str = "", fields: str = "",
         limit: str = "20") -> dict:
    """Query ANY catalogued entity directly — use for specific/composed questions
    a fixed list_* tool doesn't answer. entity = lease, property, lease_tenant,
    work_order, inquiry, appointment, ledger_entry, inspection, inventory,
    conversation, or property_group (call data_catalogue to see fields). filters =
    comma list of 'field OP value', OP in = > < >= <= ~ (contains) != , e.g.
    'status=active, total_rent>800, parking_included=true'. fields = optional
    comma list to return (default: all). Always scoped to your portfolio;
    read-only. Example: entity='work_order', filters='status=open, priority=urgent'."""
    from .domain_read import read as _fn
    return _fn(landlord, entity=entity, filters=filters, fields=fields, limit=limit)


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
            | Q(tenant__user__name__icontains=name)
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
            }
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
                }
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

    from rentium.ledger.models import CHARGE_TYPES, LedgerEntry

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
            .order_by("due_date", "created_at")
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
        rows.append(
            {
                "description": charge.description,
                "type": charge.entry_type,
                "amount": str(charge.amount),
                "due_date": charge.due_date.isoformat(),
                "paid": str(charge.settled_amount),
                "outstanding": str(outstanding),
                "status": status,
                "overdue": bool(outstanding > 0 and charge.due_date < today),
            }
        )
    return {"lease_id": str(lease_id), "month": start.strftime("%Y-%m"), "charges": rows}


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
    document_id="Business document UUID shown in the attachment note. Pass this OR upload_id.",
    upload_id="Staged photo UUID when the attachment is photographed mail, receipt, "
    "invoice, notice, or other paperwork. Pass this OR document_id.",
    scope_query="Physical holding name or exact legal street address, e.g. "
    "'950 McKenzie Ave'. This is NOT a listing query.",
    issuer="Optional sender/issuer, e.g. Scotiabank.",
    document_date="Optional document/received date in YYYY-MM-DD.",
    confirm="Leave empty to preview; pass yes only after landlord approval.",
)
def catalog_business_document(
    landlord,
    scope_query: str,
    document_id: str = "",
    upload_id: str = "",
    issuer: str = "",
    document_date: str = "",
    confirm: str = "",
) -> dict:
    """Store an uploaded business record against a PHYSICAL PROPERTY/HOLDING.
    Use when the landlord says an address overall, whole property, house, or
    building—not a rental listing. If no holding exists but listings share the
    exact legal address, this proposes creating it and linking those child
    rooms/units. Distinct apartment addresses stay separate. Never force a
    child listing choice."""
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
        "scope_query": scope_query,
        "actor": getattr(landlord, "user", None),
        "issuer": issuer,
        "document_date": parsed_date,
        "confirm": str(confirm).strip().lower() in {"yes", "y", "true", "1"},
    }
    if upload_id.strip():
        return catalog_staged_photo_as_document(
            upload_id=upload_id.strip(),
            **common,
        )
    if not document_id.strip():
        return {"error": "Pass document_id or upload_id."}
    return catalog_document_scope(
        document_id=document_id.strip(),
        **common,
    )


def business_document_location(landlord, document_id: str) -> dict:
    """Return the exact durable location of a RAMA business document: canonical
    storage key, local container filesystem path when one exists, production
    object-storage URI, Documents-page URL, and authenticated download path.
    Use for 'where is it?', 'what directory?', 'show the path', or manual access.
    Never say there is no directory/location without calling this tool."""
    from .document_services import document_location

    return document_location(landlord, document_id)


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
    confirm: str = "",
) -> dict:
    """Create a maintenance work order. ALWAYS call once WITHOUT confirm to
    preview, show the landlord, then call again with confirm=yes to save.

    property_query = listing name (e.g. 'Room E'). priority HIGH/MEDIUM/LOW/EMERGENCY.
    category PLUMBING/ELECTRICAL/…/OTHER."""
    from .domain_actions import create_work_order as _fn

    return _fn(
        landlord,
        property_query=property_query,
        title=title,
        description=description,
        priority=priority,
        category=category,
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
    landlord, inquiry_id: str = "", name_query: str = "", confirm: str = ""
) -> dict:
    """Mark a listing inquiry as REPLIED. Preview first; confirm=yes to save."""
    from .domain_actions import mark_inquiry_replied as _fn

    return _fn(
        landlord, inquiry_id=inquiry_id, name_query=name_query, confirm=confirm
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
    landlord, conversation_id: str = "", confirm: str = ""
) -> dict:
    """Mark unread tenant messages as read. Empty conversation_id = all threads.
    Preview first; confirm=yes to apply."""
    from .domain_actions import mark_messages_read as _fn

    return _fn(landlord, conversation_id=conversation_id, confirm=confirm)


def schedule_viewing(
    landlord,
    property_query: str,
    when: str,
    contact_name: str = "",
    contact_email: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Schedule a property SHOWING/viewing for prospective tenants only.
    when like '2026-08-05 14:00'.
    Do NOT use for move-in condition inspections — use create_condition_inspection.
    Preview first; confirm=yes to create."""
    from .domain_actions import schedule_viewing as _fn

    return _fn(
        landlord,
        property_query=property_query,
        when=when,
        contact_name=contact_name,
        contact_email=contact_email,
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
    always-on in-app dashboard and email. Use to answer "how will you reach
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
        landlord, property_query=property_query, lease_number=lease_number
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
        landlord, property_query=property_query, items=items, confirm=confirm
    )


def create_expense(
    landlord,
    amount: str,
    description: str,
    property_query: str = "",
    effective_date: str = "",
    confirm: str = "",
) -> dict:
    """Record a landlord expense. amount e.g. '75.00'. Preview first; confirm=yes."""
    from .domain_actions import create_expense as _fn

    return _fn(
        landlord,
        amount=amount,
        description=description,
        property_query=property_query,
        effective_date=effective_date,
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
    landlord, property_query: str = "", lease_number: str = ""
) -> dict:
    """Who is on a lease: pending invites, signed tenants, rent shares, primary.
    Use for 'who should sign?', 'any invitations sent?', rent split questions."""
    from .domain_actions import list_lease_roster as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number
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
    landlord, request: str, detail: str = "", learn_now: str = ""
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
    upload_id="Staged photo id(s) from the chat note(s) '[The landlord attached a "
    "photo, upload_id=…]'. One id, a comma-separated list for MULTIPLE photos, "
    "'all', or blank = every photo they just attached.",
    set_primary="yes to make the FIRST photo the listing's MAIN photo (rest go to "
    "the gallery); omit for all gallery.",
    pick="If the name matches two listings: oldest|newest|1|2.",
    confirm="Leave empty to preview; 'yes' to attach.",
)
def attach_photo_to_listing(
    landlord,
    property_query: str,
    upload_id: str = "",
    set_primary: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Attach a photo the landlord uploaded IN THE CHAT to a listing. When the
    conversation contains '[The landlord attached a photo, upload_id=X]', use
    this with that upload_id (or omit it to use their latest attachment).
    set_primary=yes makes it the main photo. Preview; confirm=yes."""
    from .domain_crud import attach_photo_to_listing as _fn
    return _fn(
        landlord, property_query=property_query, upload_id=upload_id,
        set_primary=set_primary, pick=pick, confirm=confirm,
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
    cleaning_fee: str = "0",
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
        cleaning_fee=cleaning_fee,
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
    """Atomically create/reuse one physical house, its property groups, rooms,
    private areas, and exact room-to-common-area associations. Use for a
    hierarchical instruction such as "add a house with a basement group and a
    three-room main floor; master has a private bathroom; the other two share
    one." Missing city/province or landlord-sharing classification returns one
    focused clarification. One complete preview, then confirm=yes."""
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
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Update (edit) an existing listing's fields — this includes RENAMING it:
    to rename a listing, call update_property with name=<the new name>. Also
    sets status/description/address/city/province/asking_rent/visibility and
    structured category/layout fields. For "full suite/unit", set
    property_category=COMPLETE_UNIT (and unit_type when known); do not rewrite
    the description. Questions such as "how many rooms?" are read-only and must
    use list_properties, not this tool.
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
        is_publicly_visible=is_publicly_visible, pick=pick, confirm=confirm,
    )


@_params(
    property_query="Which listing to delete: its exact name or id.",
    pick="If the name/id matches MORE THAN ONE listing, choose which to delete: "
    "oldest|first|1 (the older one) | newest|last|2 (the newer one). Use this "
    "for 'delete the old one' / 'the duplicate I just made'.",
    confirm="Leave empty to PREVIEW; pass 'yes' to delete.",
)
def delete_property(
    landlord, property_query: str, pick: str = "", confirm: str = ""
) -> dict:
    """Delete a listing. Blocked if ANY lease still references it (PROTECT).
    On duplicate names pass pick=oldest|newest|1|2 (or property_query=<id>) to
    pick which one — e.g. 'delete the old one' → pick=oldest. Preview;
    confirm=yes."""
    from .domain_crud import delete_property as _fn
    return _fn(landlord, property_query=property_query, pick=pick, confirm=confirm)


def create_property_group(
    landlord, name: str, description: str = "", confirm: str = ""
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
    cleaning_fee: str = "0",
    is_month_to_month: str = "0",
    pets_allowed: str = "0",
    smoking_allowed: str = "0",
    special_terms: str = "",
    etransfer_email: str = "",
    bills_included: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Create a DRAFT lease (UI New Lease). Type auto: room→Standard Roommate,
    BC complete unit→RTB-1. Fixed-term needs end_date.
    property_query can be listing id or name; pick=first|with_group|… if duplicates.
    Defaults for protection: no smoking/pets; pet_deposit & cleaning_fee 0 unless set.
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
        pet_deposit=pet_deposit, cleaning_fee=cleaning_fee,
        is_month_to_month=is_month_to_month, pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        etransfer_email=etransfer_email, bills_included=bills_included,
        pick=pick, confirm=confirm,
    )


def update_lease(
    landlord,
    property_query: str = "",
    lease_number: str = "",
    total_rent: str = "",
    security_deposit: str = "",
    start_date: str = "",
    end_date: str = "",
    pets_allowed: str = "",
    smoking_allowed: str = "",
    special_terms: str = "",
    house_rules: str = "",
    shared_with: str = "",
    bills: str = "",
    etransfer_email: str = "",
    is_month_to_month: str = "",
    confirm: str = "",
) -> dict:
    """Update draft/pending lease fields. Blocked if ACTIVE/locked (LeaseNotLocked).
    special_terms = extra clauses/terms; house_rules = roommate house rules (both
    are how you 'customise the lease' / add clauses). shared_with = who else uses
    the shared common areas on a ROOMMATE lease — comma list of landlord /
    roommates / relatives. bills = utilities on the lease, a comma list like
    'water included, hydro tenant pays, internet included' (utilities: hydro/
    electricity, water, gas, heat, internet, cable, garbage, sewer; each is either
    'included' in rent or 'tenant pays'); merges onto existing bills. Changing
    total_rent rebalances unsigned shares. Preview; confirm=yes."""
    from .domain_crud import update_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number,
        total_rent=total_rent, security_deposit=security_deposit,
        start_date=start_date, end_date=end_date, pets_allowed=pets_allowed,
        smoking_allowed=smoking_allowed, special_terms=special_terms,
        house_rules=house_rules, shared_with=shared_with, bills=bills,
        etransfer_email=etransfer_email, is_month_to_month=is_month_to_month,
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
    landlord, property_query: str = "", lease_number: str = "", confirm: str = ""
) -> dict:
    """Delete ONLY a DRAFT lease. Pending/active → terminate_lease. Preview; confirm=yes."""
    from .domain_crud import delete_draft_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm
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
    landlord, property_query: str = "", lease_number: str = "", confirm: str = ""
) -> dict:
    """Record landlord signature. Rent must be fully allocated across tenants first.
    May activate lease if a tenant already signed. Preview; confirm=yes."""
    from .domain_crud import landlord_sign_lease as _fn
    return _fn(
        landlord, property_query=property_query, lease_number=lease_number, confirm=confirm
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
    landlord, property_query: str, item_name: str, confirm: str = ""
) -> dict:
    """Delete private inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_inventory_item as _fn
    return _fn(
        landlord, property_query=property_query, item_name=item_name, confirm=confirm
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
    landlord, group_name: str, item_name: str, confirm: str = ""
) -> dict:
    """Delete shared inventory item. Preview; confirm=yes."""
    from .domain_crud import delete_shared_inventory_item as _fn
    return _fn(
        landlord, group_name=group_name, item_name=item_name, confirm=confirm
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
    confirm: str = "",
) -> dict:
    """Build a multi-step PLAN over a set of listings — ALWAYS use this (never a
    hand-rolled tool sequence) for bulk or multi-step asks like 'delete all X'.
    operation: delete_listings | terminate_and_delete (end leases first, then
    delete — use when landlord says delete listings that still have leases) |
    update_status (needs new_status).
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
        new_status=new_status, confirm=confirm,
    )


def plan_move_tenant(
    landlord,
    tenant: str,
    from_property: str,
    to_property: str,
    start_date: str = "",
    total_rent: str = "",
    confirm: str = "",
) -> dict:
    """Build a PLAN that moves a tenant to another room: end the current lease
    (own confirmation), then new lease + signing invite + move-in inspection on
    the target room. tenant: name or email. total_rent defaults to the old
    lease's rent; start_date YYYY-MM-DD (default today). Show the plan, then
    STOP — the system confirms and executes."""
    from .playbooks import plan_move_tenant as _fn
    return _fn(
        landlord, tenant=tenant, from_property=from_property,
        to_property=to_property, start_date=start_date, total_rent=total_rent,
        confirm=confirm,
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
    from .constitution import amend, parse_rule_changes
    from .domain_crud import _confirmed, _preview
    from .models import RamaConstitutionSection

    key_s = (key or "").strip().lower()
    if not key_s:
        return {"error": "key is required (e.g. balances, vendors)."}
    changes, err = parse_rule_changes(rule_changes)
    if err:
        return {"error": err}

    preview = {
        "section": key_s,
        "title": title or "(unchanged)",
        "new_body_md": (new_body_md or "(unchanged)")[:1500],
        "rule_changes": changes or "(none)",
        "note": "Creates a new version; the old version stays in history.",
    }
    if not _confirmed(confirm):
        return _preview(
            "amend_constitution", preview, "Amends the landlord's Constitution."
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
