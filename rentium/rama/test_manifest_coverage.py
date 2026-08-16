"""A new model must be triaged, not silently invisible to RAMA.

13 of 87 models were exposed — 14%. That number was nobody's decision; it is
what you get when the manifest is a hand-written list and the schema keeps
growing past it. The cost showed up as a confident wrong answer: asked about a
discount, RAMA could not see leases.RentAdjustment, so it searched the ledger
and reported "No — I don't see any discounts recorded" over a $1,600 discount.

A missing FIELD is self-correcting: `read` refuses and names the fields that do
exist, and the model tries again. A missing ENTITY is not. It looks exactly like
an empty table from every angle the model can see.

So the default for a model nobody has considered is a failing test, not silence.
Add it to MANIFEST, or add it to WAIVED with a reason. Both are decisions; only
one of them used to happen by accident.

`scope_path` is deliberately NOT derived here. Walking foreign keys to find a
LandlordProfile finds several paths and the shortest is often wrong —
RentAdjustment's is `created_by`, which records who typed the row rather than
whose portfolio it belongs to, and Payment's is
`rent_adjustment__created_by`. Scope is the boundary between two landlords'
data; it is declared by a human every time.
"""

from __future__ import annotations

from collections import deque

import pytest
from django.apps import apps

from rentium.rama.manifest import MANIFEST
from rentium.users.models import LandlordProfile

# Apps whose models are landlord-facing domain data. RAMA's own bookkeeping
# (audits, messages, plans, receipts) is machinery, not something a landlord
# asks questions about, and `events` rows are the plumbing under the questions.
DOMAIN_APPS = (
    "leases",
    "properties",
    "ledger",
    "maintenance",
    "appointments",
    "showcase",
    "messaging",
    "agenda",
    "users",
)

# Models a human has looked at and decided RAMA does not need to read directly.
# A reason is required. "Reachable through its parent" means the data is not
# lost — it is read via the parent entity's fields or relations.
WAIVED: dict[str, str] = {
    # --- rows that exist to hold another row's detail -----------------------
    "leases.InspectionItem": "one line of an inspection; read via `inspection`",
    "leases.InspectionKeyRow": "key handover rows inside an inspection",
    "leases.AreaConditionState": "per-area condition inside an inspection",
    "leases.LeaseFormPlacement": "field coordinates on a form template",
    "leases.LeaseFormSigner": "signer slot inside a lease form",
    "leases.LeaseFormSignature": "captured signature inside a lease form",
    "leases.LeaseFormEvent": "audit trail of a lease form",
    "leases.LeaseInviteEvent": "audit trail of a tenant invite",
    "leases.LeaseLandlordSignatory": "who signs for the landlord on a lease",
    "leases.LeaseDocument": "generated PDF of a lease; offered via `link`",
    "properties.PropertyImage": "listing photos; not a question about data",
    "maintenance.WorkOrderImage": "photos on a work order",
    "maintenance.WorkOrderComment": "comment thread on a work order",
    "ledger.LedgerAttachment": "file attached to a ledger entry",
    "showcase.ShowcaseSlugHistory": "old public URLs of a showcase",
    "users.LandlordTeamMember": "team access control, not portfolio data",
    # --- import staging: not the books until it is posted -------------------
    "ledger.ImportBatch": "in-flight bank import; the ledger is the record",
    "ledger.StagedLedgerEntry": "unposted import row; not money yet",
    # --- superseded ---------------------------------------------------------
    "leases.Payment": (
        "superseded by the ledger, which is the append-only source of truth "
        "for money; table is empty on live data"
    ),
    "leases.PaymentReminder": "hangs off leases.Payment, itself superseded",
    # --- declared but not yet needed ----------------------------------------
    # These are real landlord data and are the shortlist for the next pass.
    # They are waived rather than rushed: each needs its scope_path chosen and
    # its fields reviewed, and a wrong scope_path leaks across landlords.
    "properties.SharedInventoryItem": "shared-space inventory — NEXT",
    "leases.MoveOutRequest": "move-out workflow — NEXT, empty today",
    "leases.DepositDeduction": "deposit deductions — NEXT, empty today",
    "leases.LeaseFormTemplate": "form templates — configuration, not portfolio",
    "ledger.HoldingFinancials": "holding-level finances — NEXT, empty today",
    "ledger.HoldingValuation": "holding valuations — NEXT, empty today",
    "ledger.HoldingMortgage": "holding mortgages — NEXT, empty today",
    "ledger.PropertyBankBalance": "bank balances — NEXT, empty today",
    "showcase.Showcase": "public listing pages — NEXT",
    "appointments.AppointmentProposal": "proposed slots inside an appointment",
    "appointments.AvailabilityWindow": "bookable windows — NEXT",
    "messaging.Message": "tenant messages — read via `conversation`",
    "agenda.AgendaEvent": "calendar events — NEXT, empty today",
}


def _reaches_a_landlord(model, max_depth=3) -> bool:
    """Whether any forward FK/O2O chain from this model lands on a
    LandlordProfile. If none does, the model cannot be scoped and so cannot be
    exposed at all — that is a structural exclusion, not a decision."""
    queue = deque([(model, 0)])
    seen = {model}
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for field in current._meta.get_fields():
            if not (field.many_to_one or field.one_to_one):
                continue
            if not hasattr(field, "attname"):
                continue
            target = field.related_model
            if target is None:
                continue
            if target is LandlordProfile:
                return True
            if target not in seen:
                seen.add(target)
                queue.append((target, depth + 1))
    return False


def _scopeable_models() -> set[str]:
    found = set()
    for label in DOMAIN_APPS:
        try:
            config = apps.get_app_config(label)
        except LookupError:  # pragma: no cover - app removed
            continue
        for model in config.get_models():
            if model._meta.proxy or model._meta.auto_created:
                continue
            if _reaches_a_landlord(model):
                found.add(f"{label}.{model.__name__}")
    return found


def _declared() -> set[str]:
    return {spec.model for spec in MANIFEST.values()}


def test_every_scopeable_model_is_declared_or_waived():
    undecided = sorted(_scopeable_models() - _declared() - set(WAIVED))
    assert not undecided, (
        "These models hold landlord data that RAMA cannot see, and nobody has "
        "said whether that is intended:\n  "
        + "\n  ".join(undecided)
        + "\n\nAdd an EntitySpec to rama/manifest.py (choosing scope_path by "
        "hand — see this module's docstring), or add the model to WAIVED here "
        "with the reason. An entity RAMA cannot see does not produce an error; "
        "it produces a confident 'no'."
    )


def test_waivers_are_not_stale():
    """A waiver for a model that is now declared, renamed or deleted is
    misleading — it reads as a considered decision about something that no
    longer exists."""
    declared = _declared()
    scopeable = _scopeable_models()
    stale = sorted(
        name
        for name in WAIVED
        if name in declared or name not in scopeable
    )
    assert not stale, (
        f"WAIVED entries that no longer apply (now declared, or no longer a "
        f"scopeable model): {stale}"
    )


def test_every_waiver_gives_a_reason():
    empty = sorted(name for name, why in WAIVED.items() if not why.strip())
    assert not empty, f"waived with no reason: {empty}"


def test_the_discount_table_is_no_longer_invisible():
    """The specific regression. Pinned because it is the cheapest possible
    check on the thing that produced a wrong answer to a landlord."""
    assert "leases.RentAdjustment" in _declared()
    assert "leases.RentAdjustment" not in WAIVED


@pytest.mark.parametrize("key,spec", sorted(MANIFEST.items()))
def test_scope_path_is_declared_and_resolves(key, spec):
    """Every entity is scoped, and the path it names actually exists.

    A typo here does not fail loudly — Django raises only when the query runs,
    which is at a landlord's keyboard.
    """
    assert spec.scope_path, f"{key} has no scope_path"
    model = apps.get_model(*spec.model.split("."))
    # EVERY disjunct, not just the first. An entity with several parents
    # (PropertyArea hangs off a unit, a group or a property) ORs them together,
    # and one unchecked path is a path that reaches another landlord's rows.
    for path in (spec.scope_path, *spec.alt_scope_paths):
        current = model
        for part in path.split("__"):
            field = current._meta.get_field(part)
            current = field.related_model
        assert current is LandlordProfile, (
            f"{key} scope path {path!r} ends at "
            f"{current._meta.label if current else None}, not LandlordProfile"
        )


@pytest.mark.parametrize("key,spec", sorted(MANIFEST.items()))
def test_nothing_scopes_itself_through_a_to_many(key, spec):
    """A to-many hop in a scope path fans the row out and multiplies aggregates.

    Forward FK/O2O only, for the same reason relation traversal is forward-only.
    """
    model = apps.get_model(*spec.model.split("."))
    for path in (spec.scope_path, *spec.alt_scope_paths):
        current = model
        for part in path.split("__"):
            field = current._meta.get_field(part)
            assert field.many_to_one or field.one_to_one, (
                f"{key} scope path {path!r} traverses {part!r}, which is to-many"
            )
            current = field.related_model
