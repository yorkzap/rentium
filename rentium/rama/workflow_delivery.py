"""Durable invitation outbox; domain records commit before external delivery."""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

_execution = ContextVar("rama_workflow_execution", default=None)


@contextmanager
def execution_context(task, step_id):
    token = _execution.set((task, step_id))
    try:
        yield
    finally:
        _execution.reset(token)


def queue_invitation(lease_tenant, landlord):
    from .models import RamaTask
    from .tasks import deliver_workflow_invitation

    current = _execution.get()
    parent, step_id = current if current else (None, str(uuid.uuid4()))
    key = f"invite:{parent.pk if parent else step_id}:{step_id}:{lease_tenant.pk}"
    task, created = RamaTask.objects.get_or_create(
        landlord=landlord,
        idempotency_key=key,
        defaults={
            "conversation_id": parent.conversation_id if parent else uuid.uuid4(),
            "parent": parent,
            "workflow_key": "onboarding_delivery",
            "capability_key": "deliver_tenant_invitation",
            "stage": "delivery",
            "input": {
                "lease_tenant_id": str(lease_tenant.pk),
                "summary": f"Deliver invitation to {lease_tenant.invited_email}",
            },
        },
    )
    if created:
        task.transition_to(RamaTask.Status.EXECUTING)
        task.transition_to(RamaTask.Status.WAITING)
        transaction.on_commit(
            lambda: deliver_workflow_invitation.apply_async(
                args=[str(task.pk)],
                retry=False,
                ignore_result=True,
            ),
            robust=True,
        )
    return task


def deliver_invitation(task_id):
    """Claim once. Ambiguous/crashed sends need review, never blind replay."""
    from rentium.leases.models import LeaseInviteEvent
    from rentium.leases.models import LeaseTenant
    from rentium.leases.services import record_invite_event
    from rentium.showcase.emails import send_tenant_invite

    from .models import RamaTask

    with transaction.atomic():
        task = RamaTask.objects.select_for_update().get(
            pk=task_id,
            capability_key="deliver_tenant_invitation",
        )
        if task.status != RamaTask.Status.WAITING:
            return
        task.transition_to(RamaTask.Status.READY)
        task.transition_to(RamaTask.Status.EXECUTING)
        slot = LeaseTenant.objects.get(
            pk=task.input["lease_tenant_id"],
            lease__landlord=task.landlord,
        )
        if slot.declined:
            task.transition_to(
                RamaTask.Status.FAILED,
                error="Invitation was cancelled before delivery.",
            )
            return
    try:
        sent = bool(send_tenant_invite(slot))
    except Exception:  # noqa: BLE001 — uncertain delivery must never be replayed
        task.transition_to(
            RamaTask.Status.FAILED,
            error=(
                "Delivery could not be confirmed. Check the invitation before "
                "requesting another send."
            ),
        )
        return
    with transaction.atomic():
        task = RamaTask.objects.select_for_update().get(pk=task_id)
        if sent:
            slot.invite_sent_at = timezone.now()
            slot.save(update_fields=["invite_sent_at", "updated_at"])
            record_invite_event(
                slot,
                LeaseInviteEvent.Kind.SENT,
                actor=task.landlord.user,
                metadata={
                    "email_sent": True,
                    "source": "rama_workflow",
                    "delivery_task": str(task.pk),
                },
            )
            task.transition_to(
                RamaTask.Status.VERIFIED,
                outcome={
                    "kind": "COMPLETED",
                    "message": "Invitation delivery recorded.",
                },
            )
        else:
            task.transition_to(
                RamaTask.Status.FAILED,
                error=(
                    "The mail service did not confirm delivery. The saved lease and "
                    "invitation remain available."
                ),
            )


@transaction.atomic
def refresh_waiting(task):
    """Observe external progress without authorizing new business actions."""
    from rentium.leases.models import Lease
    from rentium.leases.models import MoveOutRequest

    from .models import RamaTask

    task = RamaTask.objects.select_for_update().get(pk=task.pk, landlord=task.landlord)
    if (
        task.status != RamaTask.Status.WAITING
        or task.workflow_key == "onboarding_delivery"
    ):
        return
    waiting = False
    failed = False
    for row in task.progress:
        result = row.get("result") or {}
        if result.get("delivery_task_id"):
            delivery = RamaTask.objects.filter(
                pk=result["delivery_task_id"],
                landlord=task.landlord,
            ).first()
            if delivery is None:
                failed = True
                continue
            failed |= delivery.status == RamaTask.Status.FAILED
            failed |= (
                delivery.status == RamaTask.Status.EXECUTING
                and delivery.updated_at < timezone.now() - timedelta(minutes=5)
            )
            waiting |= delivery.status != RamaTask.Status.VERIFIED
            # Sending an invitation is not a signed, active tenancy.
            lease = Lease.objects.filter(
                landlord=task.landlord,
                lease_number=row.get("arguments", {}).get("lease_number"),
            ).first()
            failed |= lease is None or lease.status == Lease.LeaseStatus.TERMINATED
            waiting |= lease is None or lease.status != Lease.LeaseStatus.ACTIVE
        if result.get("moveout_id"):
            moveout = MoveOutRequest.objects.filter(
                pk=result["moveout_id"],
                lease__landlord=task.landlord,
            ).first()
            if moveout is None:
                failed = True
                continue
            failed |= moveout.status in {"DECLINED", "CANCELLED"}
            waiting |= moveout.status != "ACCEPTED"
            lease = Lease.objects.filter(
                pk=result["lease"]["id"],
                landlord=task.landlord,
            ).first()
            failed |= lease is None or lease.status == Lease.LeaseStatus.TERMINATED
            waiting |= lease is None or lease.status != Lease.LeaseStatus.ACTIVE
    if failed:
        task.transition_to(
            RamaTask.Status.PARTIAL,
            error="An external step needs attention; completed records were preserved.",
        )
    elif not waiting:
        task.transition_to(
            RamaTask.Status.VERIFIED,
            outcome={
                "kind": "COMPLETED",
                "message": "The remaining workflow conditions are verified.",
            },
        )

    task.stage = "review" if failed else "waiting" if waiting else "completed"
    task.save(update_fields=["stage", "updated_at"])
