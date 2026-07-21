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
)


def _dispatch(event):
    from .tasks import analyze_finding

    analyze_finding.delay(str(event.id))


for _event_type in SENTINEL_EVENT_TYPES:
    on(_event_type)(_dispatch)
