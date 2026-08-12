"""Visible conversation state for RAMA.

Audit rows answer "what did the engine do?".  These records answer "what did
the person actually see?".  Keeping those questions separate prevents an old
tool trace or attachment from becoming the subject of a new conversation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import RamaConversation
from .models import RamaEpisode
from .models import RamaMessage
from .models import RamaPendingPlan
from .models import RamaTask

EPISODE_IDLE = timedelta(minutes=30)
CLARIFICATION_TTL = timedelta(days=7)
CONFIRMATION_TTL = timedelta(minutes=30)


def ensure_conversation(
    *, landlord, conversation_id: uuid.UUID, channel: str = "web", external_key: str = ""
) -> RamaConversation:
    conversation, created = RamaConversation.objects.get_or_create(
        pk=conversation_id,
        defaults={
            "landlord": landlord,
            "channel": channel or RamaConversation.Channel.WEB,
            "external_key": external_key,
        },
    )
    if conversation.landlord_id != landlord.pk:
        raise ValueError("Conversation does not belong to this portfolio.")
    updates = []
    if channel and conversation.channel != channel:
        conversation.channel = channel
        updates.append("channel")
    if external_key and conversation.external_key != external_key:
        conversation.external_key = external_key
        updates.append("external_key")
    if updates and not created:
        conversation.save(update_fields=[*updates, "updated_at"])
    return conversation


def _close_episode(episode: RamaEpisode, *, at, reason: str) -> None:
    episode.ended_at = at
    episode.end_reason = reason
    episode.save(update_fields=["ended_at", "end_reason"])

    for task in episode.tasks.exclude(status__in=RamaTask.TERMINAL_STATUSES):
        if task.status == RamaTask.Status.AWAITING_CONFIRMATION:
            task.transition_to(RamaTask.Status.EXPIRED)
        else:
            task.transition_to(RamaTask.Status.SUSPENDED)
    # A confirmation is never allowed to cross a topic/idle boundary.
    RamaPendingPlan.objects.filter(episode=episode).delete()


@transaction.atomic
def current_episode(
    conversation: RamaConversation,
    *,
    visible_at=None,
    reset: bool = False,
) -> RamaEpisode:
    at = visible_at or timezone.now()
    conversation = RamaConversation.objects.select_for_update().get(pk=conversation.pk)
    episode = conversation.active_episode
    stale = bool(
        episode
        and (reset or at - episode.last_visible_at >= EPISODE_IDLE or episode.ended_at)
    )
    if stale:
        _close_episode(
            episode,
            at=at,
            reason=(RamaEpisode.EndReason.RESET if reset else RamaEpisode.EndReason.IDLE),
        )
        episode = None
    if episode is None:
        episode = RamaEpisode.objects.create(conversation=conversation)
        # Tests and event imports can provide an older visible timestamp.
        if visible_at is not None:
            RamaEpisode.objects.filter(pk=episode.pk).update(
                started_at=at, last_visible_at=at
            )
            episode.refresh_from_db()
        conversation.active_episode = episode
        conversation.save(update_fields=["active_episode", "updated_at"])
    return episode


@transaction.atomic
def record_visible_message(
    *,
    landlord,
    conversation_id: uuid.UUID,
    direction: str,
    text: str,
    channel: str = "web",
    role: str = "",
    kind: str = RamaMessage.Kind.CHAT,
    message_id: uuid.UUID | str | None = None,
    external_key: str = "",
    external_message_id: str = "",
    reply_to_message_id: uuid.UUID | str | None = None,
    reply_to_external_id: str = "",
    source_event=None,
    attachment_batch=None,
    semantic_payload: dict | None = None,
    entity_refs: list | None = None,
    delivery_status: str = RamaMessage.DeliveryStatus.LOCAL,
    visible_at=None,
    reset: bool = False,
) -> RamaMessage:
    conversation = ensure_conversation(
        landlord=landlord,
        conversation_id=conversation_id,
        channel=channel,
        external_key=external_key,
    )
    episode = current_episode(conversation, visible_at=visible_at, reset=reset)

    resolved_id = uuid.UUID(str(message_id)) if message_id else uuid.uuid4()
    existing = RamaMessage.objects.filter(pk=resolved_id).first()
    if existing:
        if existing.landlord_id != landlord.pk:
            raise ValueError("Message does not belong to this portfolio.")
        return existing

    reply_to = None
    if reply_to_message_id:
        reply_to = RamaMessage.objects.filter(
            pk=reply_to_message_id, conversation=conversation
        ).first()
    elif reply_to_external_id:
        reply_to = (
            RamaMessage.objects.filter(
                conversation=conversation,
                external_message_id=reply_to_external_id,
            )
            .order_by("-created_at")
            .first()
        )

    message = RamaMessage.objects.create(
        id=resolved_id,
        conversation=conversation,
        episode=episode,
        landlord=landlord,
        direction=direction,
        kind=kind,
        text=(text or "").strip(),
        role=role,
        channel=channel,
        external_message_id=external_message_id,
        reply_to=reply_to,
        source_event=source_event,
        attachment_batch=attachment_batch,
        semantic_payload=semantic_payload or {},
        entity_refs=entity_refs or [],
        delivery_status=delivery_status,
    )
    at = visible_at or message.created_at
    RamaEpisode.objects.filter(pk=episode.pk).update(last_visible_at=at)
    RamaConversation.objects.filter(pk=conversation.pk).update(updated_at=at)
    return message


def visible_history(*, landlord, conversation_id: uuid.UUID, limit: int = 20) -> list[dict]:
    conversation = RamaConversation.objects.filter(
        pk=conversation_id, landlord=landlord
    ).select_related("active_episode").first()
    if not conversation or not conversation.active_episode_id:
        return []
    rows = list(
        RamaMessage.objects.filter(
            conversation=conversation,
            episode=conversation.active_episode,
        )
        .select_related("reply_to", "source_event")
        .order_by("-created_at")[:limit]
    )
    rows.reverse()
    return [
        {
            "id": str(row.pk),
            "role": "user" if row.direction == RamaMessage.Direction.INBOUND else "assistant",
            "text": row.text,
            "kind": row.kind,
            "reply_to": str(row.reply_to_id) if row.reply_to_id else None,
            "semantic_payload": row.semantic_payload,
            "entity_refs": row.entity_refs,
        }
        for row in rows
    ]


def reference_context(message: RamaMessage) -> dict:
    """Ground ambiguous replies in visible context, in deterministic order."""
    if message.reply_to_id:
        target = message.reply_to
        return {
            "source": "reply_to",
            "message_id": str(target.pk),
            "text": target.text,
            "semantic_payload": target.semantic_payload,
            "entity_refs": target.entity_refs,
        }
    own_refs = message.entity_refs or []
    if own_refs:
        return {"source": "explicit_entity", "entity_refs": own_refs}
    own_payload = message.semantic_payload or {}
    if own_payload.get("source_event") or own_payload.get("client_context"):
        return {"source": "inbound_context", **own_payload}
    previous = (
        RamaMessage.objects.filter(
            conversation=message.conversation,
            episode=message.episode,
            direction=RamaMessage.Direction.OUTBOUND,
            created_at__lt=message.created_at,
        )
        .order_by("-created_at")
        .first()
    )
    if previous:
        return {
            "source": "last_visible_outbound",
            "message_id": str(previous.pk),
            "text": previous.text,
            "semantic_payload": previous.semantic_payload,
            "entity_refs": previous.entity_refs,
        }
    return {"source": "none"}


def bind_plan_prompt(plan: RamaPendingPlan, message: RamaMessage) -> None:
    plan.episode = message.episode
    plan.prompt_message = message
    plan.expires_at = timezone.now() + CONFIRMATION_TTL
    plan.save(update_fields=["episode", "prompt_message", "expires_at", "updated_at"])
    if plan.task_id:
        plan.task.episode = message.episode
        plan.task.active_prompt = message
        plan.task.expires_at = plan.expires_at
        plan.task.save(
            update_fields=["episode", "active_prompt", "expires_at", "updated_at"]
        )
