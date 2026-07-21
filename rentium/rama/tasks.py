"""
Celery tasks for the RAMA hierarchy — currently just the Sergeant → FSA
analysis step. See rama/handlers.py for what triggers it and rama/
sergeants.py for what a "finding" contains.
"""

from __future__ import annotations

import json
import logging
import uuid

from config.celery_app import app

logger = logging.getLogger(__name__)

# Per-kind title + a short instruction on what the FSA should produce —
# kept here (not in the model) so it's easy to extend per finding type.
_KIND_BRIEF = {
    "rama.sentinel.min_balance": (
        "Minimum balance",
        "The reported balance for {holding_name} is {balance} against a "
        "{min_amount} minimum (as of {as_of}, stage={stage}). Explain what "
        "this means and recommend a concrete next step.",
    ),
    "rama.sentinel.deposit_deadline": (
        "Deposit return deadline",
        "Lease {lease_number} ({property}) has a ${outstanding_deposit} "
        "deposit outstanding with a return deadline of {deadline} "
        "(stage={stage}). Explain the statutory risk and what to do now.",
    ),
    "rama.sentinel.late_pattern": (
        "Tenant payment pattern",
        "Lease {lease_number} ({property}, tenant {tenant_name}) has been "
        "late {late_count} times in the trailing window (median "
        "{median_days_late} days late); a late fee has "
        "{late_fee_note}. Summarize the pattern and suggest how to handle it.",
    ),
    "rama.sentinel.expense_anomaly": (
        "Expense anomaly",
        "{property}'s {category} expenses this month ({this_month}) are "
        "well above the trailing average ({trailing_mean}). Explain the "
        "likely cause and recommend whether to investigate or act.",
    ),
    "rama.sentinel.surplus": (
        "Cash surplus",
        "{holding_name} shows an estimated surplus of {surplus} after "
        "upcoming committed expenses ({committed_30d}) and a safety buffer "
        "({buffer}), balance {balance} as of {as_of}. Suggest what to do "
        "with it.",
    ),
}


def _fact_pack(event_type: str, payload: dict) -> str:
    title, template = _KIND_BRIEF.get(
        event_type, (event_type, "Facts: {facts}")
    )
    fields = dict(payload)
    fields.setdefault("late_fee_note", "")
    if event_type == "rama.sentinel.late_pattern":
        fields["late_fee_note"] = (
            "been charged before" if payload.get("late_fee_ever_charged") else "never been charged"
        )
    try:
        instruction = template.format(**fields, facts=json.dumps(payload, default=str))
    except (KeyError, IndexError):
        instruction = f"Facts: {json.dumps(payload, default=str)}"
    return (
        f"## FACTS ({title})\n"
        + json.dumps(payload, default=str)
        + "\n\n## TASK\n"
        + instruction
    )


@app.task
def run_sergeants() -> dict:
    """Beat entry point: run every Sergeant. Idempotent — see sergeants.py."""
    from . import sergeants

    return sergeants.run_all()


@app.task(bind=True, max_retries=2)
def analyze_finding(self, event_id: str) -> None:
    from django.core.exceptions import ValidationError

    from rentium.events.models import DomainEvent
    from rentium.events.registry import publish
    from rentium.users.models import LandlordProfile

    from .models import RamaInsight
    from .service import run_turn

    try:
        event = DomainEvent.objects.filter(pk=event_id).first()
    except (ValueError, ValidationError):
        event = None
    if event is None:
        return
    payload = event.payload or {}
    landlord_id = payload.get("landlord_id")
    landlord = LandlordProfile.objects.filter(pk=landlord_id).first()
    if landlord is None:
        logger.warning("analyze_finding: no landlord for event %s", event_id)
        return

    fact_pack = _fact_pack(event.event_type, payload)
    result = run_turn(
        landlord,
        "Analyze this finding.",
        uuid.uuid4(),  # each analysis is its own short-lived conversation
        role="fsa",
        channel="system",
        depth=1,
        extra_system=fact_pack,
    )
    analysis = (
        result.reply
        if result.error is None
        else f"(FSA unavailable: {result.error.get('detail', 'error')})"
    )

    title, _ = _KIND_BRIEF.get(event.event_type, (event.event_type, ""))
    severity = payload.get("severity", "INFO")
    insight = RamaInsight.objects.create(
        landlord=landlord,
        kind=event.event_type,
        severity=severity if severity in dict(RamaInsight.Severity.choices) else "INFO",
        facts=payload,
        analysis=analysis,
        source_event_id=event.id,
    )

    publish(
        "rama.insight.created",
        {
            "landlord_id": str(landlord.pk),
            "insight_id": str(insight.pk),
            "title": title,
            "analysis": analysis,
            "severity": insight.severity,
        },
        property_id=event.property_id,
        lease_id=event.lease_id,
    )
