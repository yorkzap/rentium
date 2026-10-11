"""
Sergeant → FSA bridge. A Sergeant's finding is a DomainEvent; this file's
only job is to hand it to the analyze_finding Celery task, which runs a
bounded FSA turn over the deterministic facts and creates the RamaInsight
the landlord actually sees. Registered explicitly per event type (the
registry has no wildcard-prefix matching — only exact type or "*").
"""

from __future__ import annotations

from rentium.events.registry import on

SENTINEL_EVENT_TYPES = (
    "rama.sentinel.min_balance",
    "rama.sentinel.deposit_deadline",
    "rama.sentinel.late_pattern",
    "rama.sentinel.expense_anomaly",
    "rama.sentinel.surplus",
    "rama.sentinel.mortgage_renewal",
    "rama.sentinel.valuation_stale",
    "rama.sentinel.spend_drift",
)


def _dispatch(event):
    from .tasks import analyze_finding

    analyze_finding.delay(str(event.id))


for _event_type in SENTINEL_EVENT_TYPES:
    on(_event_type)(_dispatch)


@on("*")
def refresh_daily_workflow_findings(event):
    """Domain events only trigger read/prepare work, never new business writes."""
    if not event.event_type.startswith(("lease.", "ledger.", "property.")):
        return
    from .models import RamaPreferences
    from .tasks import prepare_daily_workflows

    owner_id = (event.payload or {}).get("landlord_id")
    if not owner_id and event.lease_id:
        from rentium.leases.models import Lease

        owner_id = (
            Lease.objects.filter(pk=event.lease_id)
            .values_list("landlord_id", flat=True)
            .first()
        )
    if not owner_id and event.property_id:
        from rentium.properties.models import Property

        owner_id = (
            Property.objects.filter(pk=event.property_id)
            .values_list("landlord_id", flat=True)
            .first()
        )
    if (
        owner_id
        and RamaPreferences.objects.filter(
            landlord_id=owner_id, workflows_enabled=True
        ).exists()
    ):
        prepare_daily_workflows.delay(str(owner_id))
