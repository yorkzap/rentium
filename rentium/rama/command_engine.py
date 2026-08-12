"""Task and receipt primitives shared by deterministic and model-routed turns."""

from __future__ import annotations

import hashlib
import json

from django.db import transaction

from .models import RamaActionReceipt
from .models import RamaTask
from .outcomes import CommandOutcome
from .outcomes import OutcomeKind


def _receipt_references(effects: dict) -> tuple[list[dict], list[dict]]:
    """Extract useful identifiers/links without making every adapter repeat it."""
    entity_refs: list[dict] = []
    links: list[dict] = []
    seen_refs: set[tuple[str, str]] = set()
    seen_links: set[str] = set()

    def visit(value, path: str = "result") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if child not in (None, "") and (
                    key == "id"
                    or key.endswith("_id")
                    or key in {"lease_number", "media_handle"}
                ):
                    pair = (child_path, str(child))
                    if pair not in seen_refs:
                        seen_refs.add(pair)
                        entity_refs.append({"field": child_path, "value": str(child)})
                if child not in (None, "") and (
                    key in {"link", "url", "invite_url"} or key.endswith("_url")
                ):
                    url = str(child)
                    if url not in seen_links:
                        seen_links.add(url)
                        links.append({"field": child_path, "url": url})
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(effects or {})
    return entity_refs, links


def idempotency_key(task: RamaTask, capability_key: str, inputs: dict) -> str:
    canonical = json.dumps(
        inputs or {},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{task.id}:{capability_key}:{digest}"


def create_task(
    *,
    landlord,
    conversation_id,
    capability_key: str,
    inputs: dict | None = None,
    context: dict | None = None,
    episode=None,
    source_message=None,
    expires_at=None,
) -> RamaTask:
    return RamaTask.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        capability_key=capability_key,
        input=inputs or {},
        context=context or {},
        episode=episode,
        source_message=source_message,
        expires_at=expires_at,
    )


@transaction.atomic
def record_receipt(  # noqa: PLR0913
    *,
    task: RamaTask,
    capability_key: str,
    inputs: dict,
    effects: dict,
    verification: dict | None = None,
    entity_refs: list[dict] | None = None,
    links: list[dict] | None = None,
) -> tuple[RamaActionReceipt, bool]:
    key = idempotency_key(task, capability_key, inputs)
    existing = RamaActionReceipt.objects.filter(
        landlord=task.landlord,
        idempotency_key=key,
    ).first()
    if existing is not None:
        return existing, False
    inferred_refs, inferred_links = _receipt_references(effects)
    receipt = RamaActionReceipt.objects.create(
        landlord=task.landlord,
        task=task,
        capability_key=capability_key,
        idempotency_key=key,
        inputs=inputs,
        effects=effects,
        entity_refs=entity_refs if entity_refs is not None else inferred_refs,
        verification=verification or {"verified": True},
        links=links if links is not None else inferred_links,
    )
    return receipt, True


def settle_task(task: RamaTask, outcome: CommandOutcome) -> None:
    if outcome.kind in {OutcomeKind.COMPLETED, OutcomeKind.NOOP, OutcomeKind.ANSWER}:
        status = RamaTask.Status.VERIFIED
    elif outcome.kind == OutcomeKind.PREVIEW:
        status = RamaTask.Status.AWAITING_CONFIRMATION
    elif outcome.kind == OutcomeKind.NEEDS_INPUT:
        status = RamaTask.Status.NEEDS_INPUT
    else:
        status = RamaTask.Status.FAILED
    task.transition_to(
        status,
        outcome=outcome.as_dict(),
        error=outcome.message if status == RamaTask.Status.FAILED else "",
    )
