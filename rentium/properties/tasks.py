from celery import shared_task
from django.utils import timezone


@shared_task
def activate_due_rental_configurations():
    """Only approved configurations; departure/conflict checks run under lock."""
    from .configuration_services import activate_configuration
    from .rental_configuration import RentalConfiguration

    counts = {"ACTIVE": 0, "BLOCKED": 0}
    for configuration in RentalConfiguration.objects.filter(
        status__in=["SCHEDULED", "BLOCKED"],
        effective_from__lte=timezone.localdate(),
    ).order_by("effective_from", "approved_at"):
        result = activate_configuration(configuration)
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
