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


def record_invite_event(lease_tenant, kind: str, *, actor=None, metadata=None):
    from rentium.leases.models import LeaseInviteEvent

    return LeaseInviteEvent.objects.create(
        lease_tenant=lease_tenant,
        kind=kind,
        actor=actor,
        metadata=metadata or {},
    )


def invite_lifecycle(lease_tenant) -> dict:
    """Facts RAMA/UI may state without conflating opened, linked, and signed."""
    from rentium.leases.models import LeaseInviteEvent

    events = list(lease_tenant.invite_events.order_by("created_at"))
    latest: dict[str, object] = {}
    for event in events:
        latest[event.kind] = event.created_at
    opened_at = latest.get(LeaseInviteEvent.Kind.LINK_OPENED)
    linked_at = (
        latest.get(LeaseInviteEvent.Kind.ACCOUNT_LINKED)
        or lease_tenant.invite_accepted_at
    )
    signed_at = latest.get(LeaseInviteEvent.Kind.SIGNED) or lease_tenant.signed_date
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
        "account_linked": bool(lease_tenant.tenant_id),
        "account_linked_at": linked_at.isoformat() if linked_at else None,
        "signed": bool(lease_tenant.has_signed),
        "signed_at": signed_at.isoformat() if signed_at else None,
        "declined": bool(lease_tenant.declined),
        "evidence_note": (
            "LINK_OPENED means the token-gated invite URL was opened; it is not "
            "proof that the recipient read the agreement."
        ),
    }


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
