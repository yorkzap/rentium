"""
Event handlers. This is the ONLY place delivery channels live.
Adding mobile push later = add a handler here; zero core-code changes.
The AI co-pilot later = another handler (or a reader of DomainEvent).
"""

import logging

from .registry import on

logger = logging.getLogger(__name__)


@on("maintenance.created")
def notify_new_work_order(event):
    """Email the landlord when a tenant reports an issue.
    V1: console/SMTP email via Django; swap in templates as needed."""
    _send_email_stub("New maintenance report", event)


@on("maintenance.sla_breached")
def notify_sla_breach(event):
    _send_email_stub("Maintenance SLA breached", event)


@on("ledger.payment_posted")
def notify_payment_received(event):
    _send_email_stub("Payment recorded", event)


@on("ledger.charge_due_soon")
def notify_rent_due_soon(event):
    _send_email_stub("Rent due soon", event)


def _send_email_stub(subject, event):
    # Wire recipients from event.payload once email templates are designed.
    # Kept as a logged no-op so the pipeline is testable end-to-end today.
    logger.info("[notify] %s :: %s :: %s", subject, event.event_type, event.payload)
