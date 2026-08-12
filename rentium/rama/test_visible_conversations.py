"""Regressions from the Telegram transcript that mixed a lease notice with an old invoice."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def test_episode_boundary_suspends_clarification_state(landlord):
    from rentium.rama.command_engine import create_task
    from rentium.rama.conversations import record_visible_message
    from rentium.rama.models import RamaEpisode
    from rentium.rama.models import RamaMessage
    from rentium.rama.models import RamaTask

    conversation_id = uuid.uuid4()
    first = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text="file this invoice",
    )
    task = create_task(
        landlord=landlord,
        conversation_id=conversation_id,
        capability_key="catalog_business_document",
        episode=first.episode,
        source_message=first,
    )
    task.transition_to(RamaTask.Status.NEEDS_INPUT)
    RamaEpisode.objects.filter(pk=first.episode_id).update(
        last_visible_at=timezone.now() - timedelta(minutes=31)
    )

    second = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text="hi",
    )

    first.episode.refresh_from_db()
    task.refresh_from_db()
    assert first.episode.ended_at is not None
    assert second.episode_id != first.episode_id
    assert task.status == RamaTask.Status.SUSPENDED


def test_successful_notification_is_visible_and_entity_grounded(landlord, bc_lease):
    from rentium.comms.models import ChannelAccount
    from rentium.comms.services import send_to_landlord
    from rentium.comms.tasks import telegram_conversation_id
    from rentium.events.models import DomainEvent
    from rentium.events.notify import _render
    from rentium.rama.models import RamaMessage

    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="visible-lease-chat",
        verified=True,
    )
    event = DomainEvent.objects.create(
        event_type="lease.activated", lease_id=bc_lease.pk, payload={}
    )
    title, body, url = _render(event)
    with mock.patch("rentium.comms.telegram.send_message", return_value=True):
        sent = send_to_landlord(
            landlord,
            f"{title}\n{body}",
            category="LEASE",
            url=url,
            event=event,
        )

    assert sent == ["TELEGRAM"]
    row = RamaMessage.objects.get(
        conversation_id=telegram_conversation_id("visible-lease-chat")
    )
    assert row.kind == RamaMessage.Kind.NOTIFICATION
    assert row.semantic_payload["source_event"] == "lease.activated"
    assert row.entity_refs == [{"type": "lease", "id": str(bc_lease.pk)}]
    assert bc_lease.lease_number in row.text
    assert str(bc_lease.pk) in row.text


def test_which_one_answers_visible_lease_notice_not_seventy_hour_old_invoice(
    landlord, bc_lease
):
    from rentium.comms.models import ChannelAccount
    from rentium.comms.services import send_to_landlord
    from rentium.comms.tasks import telegram_conversation_id
    from rentium.events.models import DomainEvent
    from rentium.events.notify import _render
    from rentium.rama.models import RamaAudit
    from rentium.rama.service import run_turn

    chat_id = "transcript-chat"
    conversation_id = telegram_conversation_id(chat_id)
    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address=chat_id,
        verified=True,
    )
    # This is the stale context that previously won because audit history had
    # no episode or visibility boundary.
    stale = RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.USER_MESSAGE,
        content={
            "text": "RAMA attachment batch 00000000-0000-0000-0000-000000000123 Pick Up Co Invoice $67.19"
        },
    )
    RamaAudit.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - timedelta(hours=70)
    )

    event = DomainEvent.objects.create(
        event_type="lease.activated", lease_id=bc_lease.pk, payload={}
    )
    title, body, url = _render(event)
    with mock.patch("rentium.comms.telegram.send_message", return_value=True):
        send_to_landlord(
            landlord,
            f"{title}\n{body}",
            category="LEASE",
            url=url,
            event=event,
        )

    with mock.patch("rentium.rama.service.get_provider", return_value=object()):
        result = run_turn(
            landlord,
            "which one",
            conversation_id,
            role="general",
            channel="telegram",
            external_key=chat_id,
            external_message_id="102",
        )

    assert result.deterministic is True
    assert bc_lease.lease_number in result.reply
    assert str(bc_lease.pk) in result.reply
    assert "invoice" not in result.reply.casefold()
    assert "catalog_business_document" not in result.tools_used


def test_new_visible_outbound_supersedes_an_old_confirmation_prompt(landlord):
    from rentium.rama.conversations import bind_plan_prompt
    from rentium.rama.conversations import record_visible_message
    from rentium.rama.models import RamaMessage
    from rentium.rama.models import RamaTask
    from rentium.rama.plan_runner import load_fresh_plan
    from rentium.rama.plan_runner import save_plan

    conversation_id = uuid.uuid4()
    record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text="rename the listing",
    )
    plan = save_plan(
        landlord,
        conversation_id,
        {
            "operation": "rename",
            "summary": "Rename Oak to Cedar",
            "steps": [
                {
                    "tool": "update_property",
                    "arguments": {"property_query": "Oak", "name": "Cedar"},
                    "target": "Oak",
                }
            ],
        },
    )
    prompt = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text="Confirm rename?",
        kind=RamaMessage.Kind.PLAN_PROMPT,
    )
    bind_plan_prompt(plan, prompt)
    record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text="Lease activated",
        kind=RamaMessage.Kind.NOTIFICATION,
    )

    assert load_fresh_plan(landlord, conversation_id) is None
    plan.task.refresh_from_db()
    assert plan.task.status == RamaTask.Status.EXPIRED


def test_explicit_confirm_endpoint_executes_only_matching_plan_prompt(landlord):
    from rest_framework.test import APIClient

    from rentium.rama.conversations import bind_plan_prompt
    from rentium.rama.conversations import record_visible_message
    from rentium.rama.models import RamaMessage
    from rentium.rama.plan_runner import save_plan

    conversation_id = uuid.uuid4()
    record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text="remember that viewings should be in the afternoon",
    )
    plan = save_plan(
        landlord,
        conversation_id,
        {
            "operation": "remember",
            "summary": "Remember the viewing preference",
            "steps": [
                {
                    "tool": "remember",
                    "arguments": {
                        "subject": "viewings",
                        "fact": "Prefer afternoon viewings.",
                    },
                    "target": "viewings",
                }
            ],
        },
    )
    prompt = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text="Confirm this preference?",
        kind=RamaMessage.Kind.PLAN_PROMPT,
    )
    bind_plan_prompt(plan, prompt)

    client = APIClient()
    client.force_authenticate(landlord.user)
    response = client.post(
        f"/api/rama/plans/{plan.plan_id}/confirm/",
        {
            "prompt_message_id": str(prompt.pk),
            "message_id": str(uuid.uuid4()),
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    from rentium.rama.models import RamaMemory

    assert RamaMemory.objects.filter(
        landlord=landlord,
        key="viewings",
        status=RamaMemory.Status.ACTIVE,
    ).exists(), response.data
    assert response.data["pending_plan"] is None
    assert response.data["model"] == "plan-runner"
