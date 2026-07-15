"""
publish() + handler registry.

Business code:
    from rentium.events.registry import publish
    publish("maintenance.created", {"work_order_id": str(wo.id), ...},
            property_id=wo.property_id)

Handlers (see handlers.py):
    @on("maintenance.created")
    def notify_landlord(event): ...

Event type naming convention: "<domain>.<past_tense_verb>", e.g.
    lease.activated, lease.terminated
    ledger.charge_posted, ledger.payment_posted, ledger.entry_voided,
    ledger.charge_due_soon
    maintenance.created, maintenance.status_changed, maintenance.sla_breached
"""

import logging
from collections import defaultdict

from django.db import transaction

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, list] = defaultdict(list)


def on(event_type: str):
    """Decorator: register a handler for an event type ('*' = all events)."""

    def decorator(func):
        _HANDLERS[event_type].append(func)
        return func

    return decorator


def handlers_for(event_type: str):
    return list(_HANDLERS.get(event_type, [])) + list(_HANDLERS.get("*", []))


def publish(event_type: str, payload: dict | None = None, *, property_id=None, lease_id=None):
    """
    Append the event (same transaction as the caller's writes) and queue
    dispatch after commit. Safe to call anywhere; failure to *dispatch*
    never breaks the business operation.
    """
    from .models import DomainEvent

    event = DomainEvent.objects.create(
        event_type=event_type,
        payload=payload or {},
        property_id=property_id,
        lease_id=lease_id,
    )

    def _enqueue():
        try:
            from .tasks import process_domain_event

            process_domain_event.delay(str(event.id))
        except Exception:  # broker down, eager mode misconfig, etc.
            logger.exception("Could not enqueue dispatch for event %s", event.id)

    transaction.on_commit(_enqueue)
    return event
