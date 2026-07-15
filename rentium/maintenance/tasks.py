"""Maintenance Celery tasks (schedule in beat — see INTEGRATION.md)."""

from celery import shared_task
from django.utils import timezone


@shared_task
def flag_sla_breaches():
    """
    Hourly: any NEW work order past its SLA deadline that hasn't been
    flagged yet gets a maintenance.sla_breached event (the notification
    handler takes it from there). Flag is set so the landlord is pinged once.
    """
    from rentium.events.registry import publish

    from .models import WorkOrder

    breached = WorkOrder.objects.filter(
        status=WorkOrder.Status.NEW,
        first_actioned_at__isnull=True,
        sla_due_at__lt=timezone.now(),
        sla_breach_notified=False,
    ).select_related("property")

    count = 0
    for wo in breached:
        publish(
            "maintenance.sla_breached",
            {
                "work_order_id": str(wo.id),
                "title": wo.title,
                "priority": wo.priority,
                "rta_emergency": wo.is_rta_emergency,
                "hours_overdue": round((timezone.now() - wo.sla_due_at).total_seconds() / 3600, 1),
            },
            property_id=wo.property_id,
            lease_id=wo.lease_id,
        )
        wo.sla_breach_notified = True
        wo.save(update_fields=["sla_breach_notified", "updated_at"])
        count += 1
    return count
