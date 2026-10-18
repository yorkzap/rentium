"""Explicit, auditable corrections to operational scope and legal facts."""

import hashlib
import json

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction

from .space_services import lease_unit, record_space_scope
from .tenancy_state import LeaseSpaceScope, TenancyAssessment, TenancyPresence
from .tenancy_summary import tenancy_summary

SALT = "rentium.tenancy-review.v1"


def _revision(lease):
    value = [
        str(lease.updated_at),
        str(lease.presence_events.values_list("pk", flat=True).first()),
        str(lease.scope_versions.values_list("pk", flat=True).first()),
        str(lease.tenancy_assessments.values_list("pk", flat=True).first()),
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def preview_tenancy_review(
    lease, *, scope=None, assessment=None, presence=None, reason=""
):
    unit = lease_unit(lease)
    if not reason.strip() or not (scope or assessment or presence):
        raise ValidationError("Describe the correction and the facts you verified.")
    payload = {
        "lease_id": str(lease.pk),
        "revision": _revision(lease),
        "reason": reason,
        "scope": scope,
        "assessment": assessment,
        "presence": presence,
    }
    if scope:
        if unit is None:
            raise ValidationError(
                "Map this offering to its physical unit before reviewing scope."
            )
        if not isinstance(scope.get("whole_unit"), bool):
            raise ValidationError("Choose whole unit or selected rooms explicitly.")
        obj = LeaseSpaceScope(
            lease=lease,
            unit=unit,
            whole_unit=scope["whole_unit"],
            room_ids=scope.get("room_ids", []),
            reason=reason,
            snapshot={"unit": unit.name},
        )
        obj.full_clean()
        from .space_services import validate_space_commitment

        validate_space_commitment(
            lease,
            proposed_scope={
                "unit_id": str(unit.pk),
                "whole_unit": obj.whole_unit,
                "room_ids": obj.room_ids,
                "coverage": "COMPLETE",
            },
        )
    if assessment:
        from .tenancy_rules import _jurisdiction

        obj = TenancyAssessment(
            lease=lease,
            jurisdiction=_jurisdiction(lease),
            decision=assessment["decision"],
            facts=assessment.get("facts", {}),
            reason=reason,
        )
        obj.full_clean()
    if presence:
        TenancyPresence(
            lease=lease,
            state=presence["state"],
            occurred_on=presence["occurred_on"],
            reason=reason,
            receipt_key="preview",
        ).full_clean(exclude=["receipt_key"])
    return {
        "before": tenancy_summary(lease),
        "after": payload,
        "note": "This appends reviewed operational facts. It does not amend the signed agreement, send notices, or change money.",
        "preview_token": signing.dumps(payload, salt=SALT),
    }


@transaction.atomic
def confirm_tenancy_review(lease, *, preview_token, user):
    from rentium.properties.models import PropertyUnit
    from .models import Lease

    unit = lease_unit(lease)
    if unit:
        PropertyUnit.objects.select_for_update().get(pk=unit.pk)
    lease = Lease.objects.select_for_update().get(pk=lease.pk)
    try:
        data = signing.loads(preview_token, salt=SALT, max_age=1800)
    except signing.BadSignature as exc:
        raise ValidationError("Review a fresh tenancy correction preview.") from exc
    if data["lease_id"] != str(lease.pk):
        raise ValidationError("This preview belongs to another tenancy.")
    key = hashlib.sha256(preview_token.encode()).hexdigest()
    if (
        lease.presence_events.filter(receipt_key=key).exists()
        or lease.tenancy_assessments.filter(facts__review_receipt=key).exists()
        or lease.scope_versions.filter(snapshot__review_receipt=key).exists()
    ):
        return tenancy_summary(lease)
    if data["revision"] != _revision(lease):
        raise ValidationError("This tenancy changed. Review a fresh preview.")
    preview_tenancy_review(
        lease,
        scope=data["scope"],
        assessment=data["assessment"],
        presence=data.get("presence"),
        reason=data["reason"],
    )
    if data["scope"]:
        record_space_scope(
            lease,
            unit=unit,
            user=user,
            reason=data["reason"],
            receipt=key,
            **data["scope"],
        )
        # Store the receipt on creation only; a no-op correction reuses the
        # existing version and is already idempotent by scope comparison.
    if data["assessment"]:
        from .tenancy_rules import _jurisdiction

        TenancyAssessment.objects.create(
            lease=lease,
            jurisdiction=_jurisdiction(lease),
            decision=data["assessment"]["decision"],
            facts={**data["assessment"].get("facts", {}), "review_receipt": key},
            reason=data["reason"],
            recorded_by=user,
        )
    if data.get("presence"):
        TenancyPresence.objects.create(
            lease=lease,
            state=data["presence"]["state"],
            occurred_on=data["presence"]["occurred_on"],
            reason=data["reason"],
            recorded_by=user,
            receipt_key=key,
        )
    return tenancy_summary(lease)
