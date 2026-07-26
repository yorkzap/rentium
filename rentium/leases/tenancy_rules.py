"""
Tenancy rules registry — the BACKEND twin of the frontend's leaseFormats.ts.

leaseFormats.ts decides how a lease *looks*; this module decides how a
tenancy legally *behaves*: how much notice each side must give to end it,
whether the provincial tenancy act applies at all, and which mutual-
agreement form (e.g. BC's RTB-8) short-circuits the notice period.

Everything here is pure computation over a Lease — no writes. The move-out
workflow (rentium/leases/moveout.py + its API) consumes these rules and is
the only thing that mutates state. Keeping the rules pure and centralized
means the future AI controller (and any new UI) can ask ONE endpoint
"what are the rules for this lease?" and get an authoritative answer.

Extending to a new jurisdiction or agreement type = adding one entry in
_resolve_rules(). Nothing else changes.

BC specifics encoded here:
- Tenant notice: one clear month. A notice given during month M ends the
  tenancy on the last day of month M+1 (rent is due on the 1st in this
  system, so "received on or before the day before rent is due" ≡ "given
  during the previous month"). We use end-of-month arithmetic throughout
  so tenancies always end on clean rent-period boundaries.
- Landlord notice for landlord/purchaser use (e.g. moving in): three
  months (RTA as amended 2024).
- Mutual Agreement to End a Tenancy: form RTB-8. Ends the tenancy on any
  agreed date once BOTH parties sign; neither is obliged to sign.
- EXEMPTION (RTA s.4(c)): the Act does NOT apply where the tenant shares
  bathroom or kitchen facilities with the OWNER of the accommodation.
  For those arrangements the notice period is whatever the lease itself
  says (Lease.custom_tenant_notice_months, default 1), the landlord has
  no statutory minimum, and we still offer RTB-8 as the belt-and-braces
  written record ("for being sure").
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta


# ---------------------------------------------------------------- helpers
def add_months(d: date, months: int) -> date:
    """First day of the month `months` after d's month."""
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def last_day_of_month(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def end_of_month_after(d: date, months: int) -> date:
    """Last day of the month `months` after d's month — the standard
    'clear months of notice' end date (notice in July, 1 month -> Aug 31)."""
    return last_day_of_month(add_months(d, months))


# ------------------------------------------------------------------ rules
@dataclass(frozen=True)
class TenancyRules:
    """The resolved rulebook for one lease. Serialized verbatim into API
    responses and into MoveOutRequest.rules_snapshot for auditability."""

    code: str                      # e.g. "BC_RTA", "BC_EXEMPT_SHARED_WITH_LANDLORD", "GENERIC"
    jurisdiction: str              # "BC" | "SK" | "GENERIC"
    rta_applies: bool              # provincial tenancy act governs this tenancy
    tenant_notice_months: int      # clear months a tenant must give
    landlord_notice_months: int | None  # statutory minimum for landlord-use notice; None = no statutory minimum
    mutual_agreement_form: str     # "RTB-8" for BC, "MUTUAL" generic label elsewhere
    summary: str                   # one-paragraph human/AI-readable explanation
    covers_whole_unit: bool = False  # whole self-contained unit vs one room in a shared one
    landlord_shares: bool = False    # the s.4(c) test that drove `rta_applies`

    def as_dict(self) -> dict:
        return asdict(self)


def lease_covers_whole_unit(lease) -> bool:
    """Does this lease cover a whole self-contained unit, or one room in a
    shared one?

    This is the distinction that decides the legal regime, and it is NOT the
    same as the unit's rental_mode. rental_mode says how the landlord is
    OFFERING the space right now; this says what a particular tenancy actually
    got. A floor let room-by-room where ONE party ends up holding every room is
    a whole-unit tenancy in law, whatever the offering looked like.

    Three ways to cover a whole unit:
      - the lease is on a COMPLETE_UNIT listing;
      - it is a group lease whose tenants between them hold every room;
      - it is a group lease with no per-room assignment at all, which means
        the whole group.
    """
    from rentium.properties.models import Property

    if lease.property_id and lease.property.property_category == (
        Property.PropertyCategory.COMPLETE_UNIT
    ):
        return True

    if lease.group_id:
        room_ids = set(lease.group.grouped_properties.values_list("id", flat=True))
        if not room_ids:
            return False
        assigned = set(
            lease.lease_tenants.filter(room__isnull=False).values_list(
                "room_id", flat=True
            )
        )
        # No room assignment on a group lease = the whole group.
        return not assigned or room_ids <= assigned

    return False


def landlord_shares_common_areas(lease) -> bool:
    """
    Does the landlord (or their relatives) share kitchen/bath/common areas
    with this tenancy? Two sources of truth, either one suffices:

    1. The lease's own signed clause: common_space_shared_with containing
       LANDLORD or LANDLORD_RELATIVES. Authoritative — it is a signed
       statement about this specific tenancy, so it is checked first and is
       never overridden by inferred area flags.
    2. Area-level flags: any PropertyArea with shared_with_landlord=True
       that belongs to (or is shared by) this lease's property/rooms, or to
       the UNIT they sit in. This is what the property-group pages edit, and
       what lease creation derives the clause from.

    The unit lookup matters: once a floor's layout lives on its PropertyUnit
    rather than on per-room listings, a landlord-shared kitchen recorded
    against the unit would otherwise be invisible here — and an invisible
    landlord-sharing flag means an RTA exemption silently stops being applied.
    """
    csw = set(lease.common_space_shared_with or [])
    if csw & {"LANDLORD", "LANDLORD_RELATIVES"}:
        return True

    from django.db.models import Q

    from rentium.properties.models import PropertyArea

    if lease.property_id:
        prop_ids = [lease.property_id]
    elif lease.group_id:
        prop_ids = list(lease.group.grouped_properties.values_list("id", flat=True))
    else:
        return False
    if not prop_ids:
        return False

    scope = Q(property_id__in=prop_ids) | Q(shared_by__id__in=prop_ids)

    # Areas recorded against the unit(s) and room-group(s) these listings
    # belong to. Derived from the listings themselves, not from lease.group_id:
    # a lease on ONE room of a shared floor has no group_id, yet the shared
    # kitchen it uses is recorded against that room's group.
    from rentium.properties.models import Property

    parents = Property.objects.filter(id__in=prop_ids).values_list(
        "unit_id", "group_id"
    )
    unit_ids = {u for u, _g in parents if u}
    group_ids = {g for _u, g in parents if g}
    if lease.group_id:
        group_ids.add(lease.group_id)
    if unit_ids:
        scope |= Q(unit_id__in=unit_ids)
    if group_ids:
        scope |= Q(group_id__in=group_ids)

    return (
        PropertyArea.objects.filter(shared_with_landlord=True).filter(scope).exists()
    )


def _jurisdiction(lease) -> str:
    """Which province's rulebook governs.

    The lease type names it when it can (BC_RESIDENTIAL, SK_RESIDENTIAL). It
    often can't: every NEW room lease uses the one GENERIC_ROOMMATE agreement
    regardless of province, so a BC room tenancy would otherwise fall through
    to GENERIC and be offered a generic mutual-agreement form instead of BC's
    RTB-8. Fall back to where the property actually is — the same thing
    inspection_services.resolve_template() does.
    """
    t = (lease.lease_type or "").upper()
    if t.startswith("BC"):
        return "BC"
    if t.startswith("SK"):
        return "SK"

    prop = lease.property if lease.property_id else None
    if prop is None and lease.group_id:
        prop = lease.group.grouped_properties.first()
    province = ((prop.province if prop else "") or "").strip().lower()
    if province in ("bc", "british columbia"):
        return "BC"
    if province in ("sk", "saskatchewan"):
        return "SK"
    return "GENERIC"


def _resolve_rules(lease) -> TenancyRules:
    """The registry. One branch per (jurisdiction, exemption) combination —
    add new provinces/agreement types here and only here."""
    juris = _jurisdiction(lease)
    shared = landlord_shares_common_areas(lease)
    whole_unit = lease_covers_whole_unit(lease)
    custom_months = max(int(getattr(lease, "custom_tenant_notice_months", 1) or 1), 1)

    if shared:
        # RTA s.4(c) exemption (and its equivalents): owner shares
        # kitchen/bath -> the Act doesn't govern; the lease's own terms do.
        return TenancyRules(
            code=f"{juris}_EXEMPT_SHARED_WITH_LANDLORD",
            jurisdiction=juris,
            rta_applies=False,
            tenant_notice_months=custom_months,
            landlord_notice_months=None,  # no statutory minimum
            mutual_agreement_form="RTB-8" if juris == "BC" else "MUTUAL",
            covers_whole_unit=whole_unit,
            landlord_shares=shared,
            summary=(
                "The landlord (or their relatives) shares kitchen/bathroom or "
                "common areas with this tenancy, so the provincial tenancy act "
                "does not apply. The notice period is set by the lease itself: "
                f"{custom_months} month(s) for the tenant; the landlord has no "
                "statutory minimum. A signed mutual agreement "
                "(RTB-8 in BC) is still recorded when ending early, as the "
                "clean written record for both sides."
            ),
        )

    if juris == "BC":
        return TenancyRules(
            code="BC_RTA",
            jurisdiction="BC",
            rta_applies=True,
            tenant_notice_months=1,
            landlord_notice_months=3,
            mutual_agreement_form="RTB-8",
            covers_whole_unit=whole_unit,
            landlord_shares=shared,
            summary=(
                "BC Residential Tenancy Act applies. The tenant must give one "
                "clear month's written notice (given this month, the tenancy "
                "ends on the last day of next month) — valid notice is accepted "
                "automatically. The landlord must give three clear months for "
                "landlord/purchaser use. To end sooner, both parties may sign a "
                "Mutual Agreement to End a Tenancy (form RTB-8); neither is "
                "obliged to sign, and until it is accepted the tenant still "
                "owes rent through the full notice period."
            ),
        )

    # SK + generic default to the same shape as BC for now — adjust the
    # numbers per the Saskatchewan Residential Tenancies Act when you're
    # ready; the workflow doesn't change.
    return TenancyRules(
        code=f"{juris}_STANDARD",
        jurisdiction=juris,
        rta_applies=True,
        tenant_notice_months=1,
        landlord_notice_months=3,
        mutual_agreement_form="MUTUAL",
        covers_whole_unit=whole_unit,
        landlord_shares=shared,
        summary=(
            "Standard terms: one clear month's notice from the tenant "
            "(accepted automatically when valid), three from the landlord for "
            "landlord use, or a signed mutual agreement to end on any date "
            "both parties accept."
        ),
    )


def rules_for_lease(lease) -> TenancyRules:
    return _resolve_rules(lease)


# ------------------------------------------------------ date computations
def earliest_tenant_end_date(lease, notice_date: date | None = None) -> date:
    """Earliest lawful/contractual end date for a TENANT notice given on
    notice_date (default today). End-of-month arithmetic keeps tenancy ends
    on rent-period boundaries in both RTA and exempt modes."""
    notice_date = notice_date or date.today()
    rules = rules_for_lease(lease)
    return end_of_month_after(notice_date, rules.tenant_notice_months)


def earliest_landlord_end_date(lease, notice_date: date | None = None) -> date:
    """Earliest end date for a LANDLORD-use notice. None-minimum (exempt)
    arrangements may end as soon as the landlord chooses — the notice date
    itself is the floor."""
    notice_date = notice_date or date.today()
    rules = rules_for_lease(lease)
    if rules.landlord_notice_months is None:
        return notice_date
    return end_of_month_after(notice_date, rules.landlord_notice_months)


def rules_payload(lease) -> dict:
    """Everything a UI (or the AI controller) needs to present the move-out
    options for this lease, in one call."""
    today = date.today()
    rules = rules_for_lease(lease)
    return {
        **rules.as_dict(),
        "today": today.isoformat(),
        "earliest_tenant_end_date": earliest_tenant_end_date(lease, today).isoformat(),
        "earliest_landlord_end_date": earliest_landlord_end_date(lease, today).isoformat(),
        "landlord_shares_common_areas": not rules.rta_applies
        and "EXEMPT" in rules.code,
    }
