"""
Celery tasks for the RAMA hierarchy — currently just the Sergeant → FSA
analysis step. See rama/handlers.py for what triggers it and rama/
sergeants.py for what a "finding" contains.
"""

from __future__ import annotations

import json
import logging
import uuid

from django.conf import settings

from config.celery_app import app

logger = logging.getLogger(__name__)

# See comms/tasks.py: any task that runs a model turn must grant it more than
# the project-wide 60s soft limit, or the turn's own graceful stop is dead code.
_TURN_LIMITS = {
    "soft_time_limit": settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT,
    "time_limit": settings.RAMA_TURN_TASK_TIME_LIMIT,
}
# One whole turn per landlord, off the interactive path.
_BATCH_TURN_LIMITS = {
    "soft_time_limit": settings.RAMA_TURN_BATCH_SOFT_TIME_LIMIT,
    "time_limit": settings.RAMA_TURN_BATCH_TIME_LIMIT,
}

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
    "rama.sentinel.mortgage_renewal": (
        "Mortgage renewal coming up",
        "The mortgage on {holding} renews in {days_to_renewal} days at "
        "{term_end} (currently {rate_percent}%). Say what the landlord should "
        "do BEFORE that date, and what it would cost to do nothing.",
    ),
    "rama.sentinel.valuation_stale": (
        "Property value is out of date",
        "{holding} was last valued at {amount} on {last_valued} ({basis}). "
        "Equity and return figures rest on it. Say what a fresh figure would "
        "change and the cheapest way to get one.",
    ),
    "rama.sentinel.spend_drift": (
        "Spending has crept up",
        "{category} cost {this_year} over the last year against {last_year} "
        "the year before — up {increase}. Say what is most likely behind it "
        "and what to check first.",
    )
}


@app.task(bind=True, autoretry_for=(OSError,), retry_backoff=True, max_retries=2)
def process_rama_document(self, document_id: str) -> None:
    """Build the PDF/A copy, OCR it, and propose filing/accounting metadata."""
    from .document_services import process_document

    process_document(document_id)


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


@app.task(bind=True, max_retries=2, **_TURN_LIMITS)
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


@app.task(**_BATCH_TURN_LIMITS)
def run_weekly_deliberation() -> dict:
    """One Treasurer analysis per landlord per week.

    Deliberately not per-holding and not daily. A background agent that
    produces something every morning trains people to stop reading it, and the
    topics rotate so a quiet week on energy still surfaces something on
    financing or revenue.
    """
    from datetime import date

    from django.db.models import Count

    from rentium.properties.models import PropertyHolding
    from rentium.users.models import LandlordProfile

    from . import deliberation
    from .interventions import TOPIC_ROTATION
    from .models import RamaDeliberation, RamaPreferences

    week = date.today().isocalendar()
    topic = TOPIC_ROTATION[week.week % len(TOPIC_ROTATION)]
    started = 0

    for landlord in LandlordProfile.objects.all():
        prefs = RamaPreferences.objects.filter(landlord=landlord).first()
        if prefs is None or not prefs.enabled:
            continue

        # The holding with the most spend is where the money is; analysing the
        # quietest one weekly would be busywork.
        holding = (
            PropertyHolding.objects.filter(landlord=landlord)
            .annotate(n=Count("ledger_entries"))
            .order_by("-n")
            .first()
        )
        dedupe = f"delib:{landlord.pk}:{topic}:{holding.pk if holding else 'all'}:{week.year}-{week.week}"
        if RamaDeliberation.objects.filter(dedupe_key=dedupe).exists():
            continue

        try:
            # Stamped at creation, not afterwards: a run that takes minutes
            # must not leave a window where a second beat starts a duplicate.
            deliberation.run(
                landlord, topic=topic, holding=holding, trigger="beat",
                dedupe_key=dedupe,
            )
            started += 1
        except Exception:  # noqa: BLE001 — one landlord must not sink the run
            logger.exception("Weekly deliberation failed for %s", landlord.pk)
    return {"deliberations": started, "topic": topic}
