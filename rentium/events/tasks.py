"""Celery dispatcher: fans a DomainEvent out to its registered handlers."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_domain_event(self, event_id: str):
    from .models import DomainEvent
    from .registry import handlers_for

    try:
        event = DomainEvent.objects.get(pk=event_id)
    except DomainEvent.DoesNotExist:
        return

    errors = []
    for handler in handlers_for(event.event_type):
        try:
            handler(event)
        except Exception as exc:  # one bad handler never blocks the others
            logger.exception("Handler %s failed for %s", handler.__name__, event.event_type)
            errors.append(f"{handler.__name__}: {exc}")

    event.mark_processed(error="; ".join(errors))
    if errors:
        raise self.retry(exc=Exception("; ".join(errors)))


@shared_task
def replay_unprocessed_events():
    """Safety net (beat, e.g. every 10 min): re-dispatch events that were
    published but never processed (worker was down, broker hiccup)."""
    from django.utils import timezone
    from datetime import timedelta

    from .models import DomainEvent

    cutoff = timezone.now() - timedelta(minutes=5)
    stale = DomainEvent.objects.filter(processed_at__isnull=True, created_at__lt=cutoff)
    for event in stale[:200]:
        process_domain_event.delay(str(event.id))
    return stale.count()
