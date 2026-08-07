"""
Business logic that spans more than a single model method, or that needs to
be callable identically from more than one place (an API view today, tests,
and eventually agent tooling). Model methods stay on the model when they're
genuinely about that one model's own state (Lease.check_and_activate(), for
example); anything that's closer to "a calculation" than "a state change"
lives here instead.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction


@transaction.atomic
def create_lease_record(*, landlord, values: dict):
    """Create one lease through the application boundary used by API and RAMA.

    Callers may prepare different input shapes, but ownership enforcement,
    inherited utilities, model validation, legal shared-space derivation, and
    post-save domain signals happen exactly once here.
    """
    from rentium.leases.models import Lease

    data = dict(values)
    data.pop("landlord", None)
    property_obj = data.get("property")
    group_obj = data.get("group")
    if not ((property_obj is None) ^ (group_obj is None)):
        raise ValidationError("Lease must link to either one property or one group.")
    if property_obj is not None and property_obj.landlord_id != landlord.pk:
        raise ValidationError({"property": "That property is outside this portfolio."})
    if group_obj is not None and group_obj.landlord_id != landlord.pk:
        raise ValidationError({"group": "That group is outside this portfolio."})
    if (
        not data.get("bills_included")
        and property_obj is not None
        and getattr(property_obj, "default_bills_included", None)
    ):
        data["bills_included"] = property_obj.default_bills_included

    lease = Lease(landlord=landlord, **data)
    lease.full_clean()
    lease.save()

    if "ROOMMATE" in (lease.lease_type or "") and not lease.common_space_shared_with:
        from rentium.leases.tenancy_rules import landlord_shares_common_areas

        if landlord_shares_common_areas(lease):
            lease.common_space_shared_with = ["LANDLORD"]
            lease.save(update_fields=["common_space_shared_with", "updated_at"])
    return lease


# Changing one of these is changing the deal, not fixing a typo — so it is what
# triggers an amendment record against anyone who had already signed.
MATERIAL_LEASE_FIELDS = frozenset(
    {
        "total_rent",
        "security_deposit",
        "pet_deposit",
        "cleaning_deposit",
        "start_date",
        "end_date",
        "move_in_date",
        "move_out_date",
        "is_month_to_month",
        "rent_due_day",
        "lease_type",
        "pets_allowed",
        "smoking_allowed",
        "parking_included",
        "parking_extra_charge",
        "bills_included",
        "custom_tenant_notice_months",
    }
)


# What a LIVE tenancy can still have changed.
#
# A signed lease was frozen completely, which is right for the deal and wrong
# for everything around it: a landlord who agreed a new quiet-hours rule, or
# whose service address changed, had no route but Django admin. Real tenancies
# get amended by mutual agreement all the time, and BC expects the current
# terms to be recorded.
#
# So this list is defined by what does NOT have machinery behind it. Deliberately
# absent: every field in MATERIAL_LEASE_FIELDS. Rent and deposits have ledger
# charges already posted against them, and the dates drive statutory clocks —
# notice periods, the deposit-return deadline. Changing those on a live tenancy
# needs the ledger to move too, which is what terminate/renew and the rent
# adjustment tools are for, not a text box.
AMENDABLE_WHEN_ACTIVE = frozenset(
    {
        "special_terms",
        "house_rules",
        "pets_terms",
        "smoking_terms",
        "parking_description",
        "services_and_facilities",
        "occupants",
        "bills_summary",
        "common_space_shared_with",
        "common_space_clause_text",
        "landlord_service_address",
        "landlord_service_email",
        "landlord_daytime_phone",
        "landlord_other_phone",
        "landlord_fax",
        "etransfer_email",
        "co_hosts",
    }
)


def amendable_fields_for(lease) -> frozenset | None:
    """Which fields this lease will accept, or None for "all of them".

    None       — not executed yet; the landlord still owns the document.
    a set      — live tenancy; wording may be amended, the deal may not.
    empty set  — expired, terminated or superseded. History is not editable:
                 those records are what a dispute is argued from.
    """
    from rentium.leases.models import Lease

    if not lease.is_locked():
        return None
    if lease.status == Lease.LeaseStatus.ACTIVE:
        return AMENDABLE_WHEN_ACTIVE
    return frozenset()


@transaction.atomic
def update_lease_record(*, landlord, lease, values: dict, actor=None) -> dict:
    """Edit one lease through the single boundary the API and RAMA both use.

    Before execution the landlord owns the document and everything is editable.
    Once the lease is ACTIVE the deal is fixed but its WORDING is not — see
    `amendable_fields_for`. Past ACTIVE nothing is editable at all.

    Re-checked here rather than trusted from the permission class, so no caller
    can reach a locked lease by picking a different door.

    Before then a lease can already carry signatures: the landlord's, and any
    tenant who signed early. Editing is still allowed — the landlord owns the
    document — but every material change writes an immutable TERMS_AMENDED
    event against each person who had already signed. Nothing is sent to them;
    the record exists so the landlord can see, and later prove, who agreed to
    what and when.

    Returns {"lease", "changed", "amended_signers"} so callers can tell the
    landlord what their edit actually did.
    """
    from rentium.leases.models import Lease
    from rentium.leases.models import LeaseInviteEvent

    if lease.landlord_id != landlord.pk:
        raise ValidationError("That lease is outside this portfolio.")

    data = dict(values)
    allowed = amendable_fields_for(lease)
    if allowed is not None:
        refused = sorted(set(data) - allowed)
        if not allowed:
            raise ValidationError(
                f"Lease {lease.lease_number} is "
                f"{lease.get_status_display().lower()} and can no longer be "
                f"edited — that record is what a dispute is argued from."
            )
        if refused:
            raise ValidationError(
                f"Lease {lease.lease_number} is active, so its wording can be "
                f"amended but the deal itself cannot: {', '.join(refused)} "
                f"{'have' if len(refused) > 1 else 'has'} charges or notice "
                f"periods already running against "
                f"{'them' if len(refused) > 1 else 'it'}. Use a rent "
                f"adjustment, or terminate and re-issue, so the ledger moves "
                f"with the change."
            )

    # Never settable through an edit: identity, ownership, and the two fields
    # that are the signed document's own tamper evidence.
    for protected in (
        "landlord",
        "lease_number",
        "status",
        "signed_document",
        "signed_document_sha256",
        "landlord_signed",
        "landlord_signed_date",
    ):
        data.pop(protected, None)

    before = {}
    changed = {}
    for field, new in data.items():
        old = getattr(lease, field, None)
        if old == new:
            continue
        before[field] = old
        changed[field] = new
        setattr(lease, field, new)
    if not changed:
        return {"lease": lease, "changed": {}, "amended_signers": []}

    lease.full_clean(
        exclude=[f.name for f in Lease._meta.fields if f.name not in changed]
    )
    lease.save()

    material = sorted(set(changed) & MATERIAL_LEASE_FIELDS)
    amended_signers = []
    if material:
        signed_slots = lease.lease_tenants.filter(has_signed=True, declined=False)
        for slot in signed_slots:
            LeaseInviteEvent.objects.create(
                lease_tenant=slot,
                kind=LeaseInviteEvent.Kind.TERMS_AMENDED,
                actor=actor if getattr(actor, "pk", None) else None,
                metadata={
                    "fields": material,
                    "before": {f: _jsonable(before[f]) for f in material},
                    "after": {f: _jsonable(changed[f]) for f in material},
                    "signed_on": _jsonable(slot.signed_date),
                },
            )
            amended_signers.append(slot.display_name)

    return {"lease": lease, "changed": changed, "amended_signers": amended_signers}


def _jsonable(value):
    """Decimals, dates and models don't survive JSONField as-is."""
    from datetime import date as _date
    from datetime import datetime as _datetime

    if value is None or isinstance(value, (bool, int, str, list, dict)):
        return value
    if isinstance(value, (_date, _datetime)):
        return value.isoformat()
    return str(value)


def record_invite_event(
    lease_tenant,
    kind: str,
    *,
    actor=None,
    metadata=None,
    debounce_seconds: int = 0,
):
    """Append an invite/view event. Optional debounce avoids spam on reloads."""
    from datetime import timedelta

    from django.utils import timezone

    from rentium.leases.models import LeaseInviteEvent

    if debounce_seconds > 0:
        since = timezone.now() - timedelta(seconds=debounce_seconds)
        recent = (
            lease_tenant.invite_events.filter(kind=kind, created_at__gte=since)
            .order_by("-created_at")
            .first()
        )
        if recent is not None:
            return recent
    return LeaseInviteEvent.objects.create(
        lease_tenant=lease_tenant,
        kind=kind,
        actor=actor,
        metadata=metadata or {},
    )


# Events that mean the tenant (or invitee) actually looked at the agreement.
_SEEN_KINDS = frozenset(
    {
        "LINK_OPENED",
        "LEASE_VIEWED",
    }
)


def invite_lifecycle(lease_tenant) -> dict:
    """Facts RAMA/UI may state without conflating opened, linked, and signed.

    last_seen_at is the most recent invite-link open or authenticated
    agreement/PDF view. It is evidence of access, not proof of reading.
    """
    from rentium.leases.models import LeaseInviteEvent

    events = list(lease_tenant.invite_events.order_by("created_at"))
    latest: dict[str, object] = {}
    for event in events:
        latest[event.kind] = event.created_at
    seen_events = [e for e in events if e.kind in _SEEN_KINDS]
    first_seen = seen_events[0].created_at if seen_events else None
    last_seen_event = seen_events[-1] if seen_events else None
    last_seen = last_seen_event.created_at if last_seen_event else None
    opened_at = latest.get(LeaseInviteEvent.Kind.LINK_OPENED)
    linked_at = (
        latest.get(LeaseInviteEvent.Kind.ACCOUNT_LINKED)
        or lease_tenant.invite_accepted_at
    )
    signed_at = latest.get(LeaseInviteEvent.Kind.SIGNED) or lease_tenant.signed_date
    last_source = None
    if last_seen_event is not None:
        last_source = (
            "invite_link"
            if last_seen_event.kind == LeaseInviteEvent.Kind.LINK_OPENED
            else "agreement"
        )
        meta = last_seen_event.metadata or {}
        if meta.get("via") == "pdf":
            last_source = "pdf"
        elif meta.get("via") == "document":
            last_source = "agreement"
    return {
        "invite_sent": bool(lease_tenant.invite_sent_at),
        "invite_sent_at": (
            lease_tenant.invite_sent_at.isoformat()
            if lease_tenant.invite_sent_at
            else None
        ),
        # Opening the token-gated preview proves the URL was opened. It does not
        # prove the named person read or understood the agreement.
        "invite_link_opened": bool(opened_at),
        "invite_link_opened_at": opened_at.isoformat() if opened_at else None,
        # Aggregate "has the tenant seen the lease?" (invite link and/or agreement).
        "has_seen_lease": bool(last_seen),
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
        "seen_count": len(seen_events),
        "last_seen_source": last_source,
        "account_linked": bool(lease_tenant.tenant_id),
        "account_linked_at": linked_at.isoformat() if linked_at else None,
        "signed": bool(lease_tenant.has_signed),
        "signed_at": signed_at.isoformat() if signed_at else None,
        "declined": bool(lease_tenant.declined),
        "evidence_note": (
            "last_seen_at is the latest invite-link open or authenticated "
            "agreement/PDF view. LINK_OPENED / LEASE_VIEWED prove access to the "
            "lease page; they are not proof that the recipient read or "
            "understood every clause."
        ),
    }


def record_lease_view_for_user(lease, user, *, via: str = "document") -> int:
    """Record that an authenticated tenant party viewed this lease.

    Returns how many tenant slots were updated (0 if viewer is not a tenant).
    """
    from django.db.models import Q

    from rentium.leases.models import LeaseInviteEvent

    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    qs = lease.lease_tenants.filter(declined=False)
    email = (getattr(user, "email", None) or "").strip()
    match = Q()
    if hasattr(user, "tenant_profile"):
        match |= Q(tenant=user.tenant_profile)
    if email:
        match |= Q(invited_email__iexact=email)
    if not match:
        return 0
    slots = list(qs.filter(match).distinct())
    for lt in slots:
        record_invite_event(
            lt,
            LeaseInviteEvent.Kind.LEASE_VIEWED,
            actor=user,
            metadata={"via": via},
            debounce_seconds=120,
        )
    return len(slots)


def co_landlord_grants_for_lease(lease):
    """LandlordTeamMember grants that make someone a co-landlord on THIS lease:
    a grant on the lease's property, or on its group. Portfolio-wide grants are
    NOT included — an office manager with blanket access isn't automatically a
    named signing party on every agreement."""
    from django.db.models import Q

    from rentium.users.models import LandlordTeamMember

    scope = Q()
    if lease.property_id:
        scope |= Q(scope_property_id=lease.property_id)
        if lease.property and lease.property.group_id:
            scope |= Q(scope_group_id=lease.property.group_id)
    if lease.group_id:
        scope |= Q(scope_group_id=lease.group_id)
    if not scope:
        return LandlordTeamMember.objects.none()
    return LandlordTeamMember.objects.filter(
        Q(owner=lease.landlord) & scope
    ).select_related("member")


def sync_lease_landlord_signatories(lease):
    """Ensure every property/group co-landlord of `lease` is a signing party on
    it. Idempotent — adds missing LeaseLandlordSignatory rows, never removes.
    Called when a lease is created (and can be re-run safely)."""
    from rentium.leases.models import LeaseLandlordSignatory

    existing_emails = {
        (s.email or "").lower()
        for s in lease.landlord_signatories.all()
    }
    existing_members = {
        s.member_id for s in lease.landlord_signatories.all() if s.member_id
    }
    created = []
    for g in co_landlord_grants_for_lease(lease):
        email = (g.invited_email or (g.member.email if g.member_id else "")).lower()
        if email and email in existing_emails:
            continue
        if g.member_id and g.member_id in existing_members:
            continue
        created.append(
            LeaseLandlordSignatory.objects.create(
                lease=lease,
                member=g.member if g.member_id else None,
                name=g.invited_name or (g.member.name if g.member_id else ""),
                email=email,
            )
        )
    return created


def grant_co_landlord(owner, *, name, email, scope_property=None, lease=None):
    """Create (or reuse) a co-landlord grant and, if a lease is given, make them a
    co-signer on it — the ONE place both RAMA and the dashboard invite flows call,
    so the DB write, account-linking and invite email stay identical. Returns
    (member, created, emailed)."""
    from django.utils import timezone

    from rentium.users.models import LandlordTeamMember, User

    email = (email or "").strip().lower()
    prop = scope_property or (lease.property if lease is not None else None)
    existing_user = User.objects.filter(email__iexact=email).first()

    member, created = LandlordTeamMember.objects.get_or_create(
        owner=owner,
        invited_email=email,
        scope_property=prop,
        scope_group=None,
        defaults={"invited_name": (name or "")[:150]},
    )
    if existing_user is not None and member.member_id is None:
        member.member = existing_user
        member.accepted_at = timezone.now()  # immediate access on next login
        member.save(update_fields=["member", "accepted_at"])

    if lease is not None:
        sync_lease_landlord_signatories(lease)

    emailed = False
    try:
        from rentium.showcase.emails import send_co_landlord_invite

        emailed = send_co_landlord_invite(member)
    except Exception:  # email must never block the grant
        emailed = False
    return member, created, emailed


def compute_rent_split(rows, total_rent):
    """
    The single source of truth for the "equal split with manual override
    cascading" rule described to tenants/landlords as: edit one person's
    rent and the others automatically absorb the difference, so the total
    always adds up.

    This used to be implemented independently in two places in the
    frontend (CreateLeaseForm.tsx's step 3, and LeaseDetail.tsx's tenant
    roster editor) with no shared backend equivalent — meaning an API
    caller (including a future agent) had no way to compute a valid split
    without reimplementing this algorithm itself, and the two frontend
    copies could silently drift out of sync with each other. Now there's
    exactly one implementation, and both the API (via LeaseViewSet's
    `preview-split` action) and the frontend (which just calls that
    endpoint) go through it.

    Args:
        rows: list of dicts, each with:
            - id: str | None (existing LeaseTenant id, or None for a new,
              not-yet-created row)
            - rent_amount: Decimal | None (the row's current amount; None
              means "not manually set, please compute it")
            - touched: bool (True if a human explicitly typed an amount for
              this row — it should be treated as fixed, not recomputed)
            - has_signed: bool (True if this LeaseTenant has already
              signed — always treated as fixed, regardless of `touched`,
              since a signed tenant's rent_amount is locked at the model
              level and recomputing it here would just be immediately
              rejected on save anyway)
        total_rent: Decimal, the lease's total_rent to split across `rows`

    Returns:
        A new list of dicts in the same shape as `rows`, with `rent_amount`
        filled in as a Decimal (quantized to cents) for every row —
        unchanged for touched/signed rows, freshly computed for the rest so
        the full set sums to `total_rent`.

    A row that's both untouched AND unsigned is "editable" — those are the
    ones that get recomputed. If there are zero editable rows (everyone is
    either touched or signed), nothing changes; if there's nothing left to
    allocate after the fixed rows, editable rows get $0.00 rather than a
    negative number.
    """
    if not rows:
        return []

    total_rent = Decimal(total_rent or "0.00")

    fixed_rows = [r for r in rows if r["touched"] or r["has_signed"]]
    editable_rows = [r for r in rows if not r["touched"] and not r["has_signed"]]

    fixed_sum = sum(
        (Decimal(r["rent_amount"]) if r["rent_amount"] is not None else Decimal("0.00"))
        for r in fixed_rows
    )
    remaining = max(total_rent - fixed_sum, Decimal("0.00"))

    per_editable = (
        (remaining / Decimal(len(editable_rows))).quantize(Decimal("0.01"))
        if editable_rows
        else Decimal("0.00")
    )

    result = []
    for row in rows:
        if row["touched"] or row["has_signed"]:
            amount = (
                Decimal(row["rent_amount"]).quantize(Decimal("0.01"))
                if row["rent_amount"] is not None
                else Decimal("0.00")
            )
        else:
            amount = per_editable
        result.append({**row, "rent_amount": amount})

    return result
