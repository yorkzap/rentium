"""Shared landlord mutual-agreement boundary used by REST and RAMA."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


def _invalid(message):
    raise ValidationError(message)


@transaction.atomic
def propose_mutual_moveout(  # noqa: PLR0913
    *,
    landlord,
    lease,
    requested_end,
    reason="",
    rent_handling="NONE",
    actor=None,
):
    from .form_services import ensure_mutual_agreement_form
    from .models import Lease
    from .models import MoveOutRequest
    from .tenancy_rules import rules_for_lease
    from .tenancy_rules import rules_payload

    lease = Lease.objects.select_for_update().get(pk=lease.pk, landlord=landlord)
    if lease.status not in {
        Lease.LeaseStatus.ACTIVE,
        Lease.LeaseStatus.PENDING_SIGNATURES,
    }:
        _invalid("Only a live tenancy can enter a move-out agreement.")
    if requested_end < timezone.localdate():
        _invalid("The end date cannot be in the past.")
    if rent_handling not in MoveOutRequest.RentHandling.values:
        _invalid("Invalid rent handling.")
    if lease.moveout_requests.filter(status=MoveOutRequest.Status.PENDING).exists():
        _invalid(
            "There is already a pending move-out request on this lease.",
        )
    rules = rules_for_lease(lease)
    request = MoveOutRequest.objects.create(
        lease=lease,
        initiated_by=MoveOutRequest.InitiatedBy.LANDLORD,
        kind=MoveOutRequest.Kind.MUTUAL_AGREEMENT,
        requested_end_date=requested_end,
        reason=reason,
        rent_handling=rent_handling,
        form_type=rules.mutual_agreement_form,
        rules_snapshot=rules_payload(lease),
    )
    request.sign(as_landlord=True)
    request.save()
    ensure_mutual_agreement_form(request, actor=actor)
    request._publish("lease.moveout_requested")  # noqa: SLF001 — existing domain event hook
    return request
