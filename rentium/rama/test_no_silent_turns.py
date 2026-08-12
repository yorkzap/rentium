"""A landlord must never be left looking at their own question with nothing under it.

August 2026: "how many rents did we receive for aug or are due?" was answered
correctly by the model and then lost. A guard fired, the recovery round raised
KeyError inside the provider, nothing caught it, the request 500'd — and because
the inbound row was already written and no outbound ever was, the conversation
showed the question and silence. It was the only one of the previous 57 inbound
messages with no reply, which is the whole point: this fails rarely and
invisibly, so it needs an invariant rather than vigilance.

The invariant: every INBOUND visible message has a later OUTBOUND in the same
conversation. Not "usually", and not "unless something unexpected happened" —
unexpected is exactly when a landlord needs to be told something.
"""

from __future__ import annotations

import uuid

import pytest

from rentium.rama import service
from rentium.rama.models import RamaAudit
from rentium.rama.models import RamaMessage
from rentium.rama.models import RamaPendingPlan
from rentium.rama.models import RamaPreferences

pytestmark = pytest.mark.django_db


def assert_no_silent_turns(landlord) -> None:
    """Every inbound message has a later outbound in the same conversation."""
    silent = []
    inbound = RamaMessage.objects.filter(
        landlord=landlord, direction=RamaMessage.Direction.INBOUND,
    )
    for row in inbound:
        answered = RamaMessage.objects.filter(
            landlord=landlord,
            conversation_id=row.conversation_id,
            direction=RamaMessage.Direction.OUTBOUND,
            created_at__gt=row.created_at,
        ).exists()
        if not answered:
            silent.append((str(row.conversation_id), row.text[:80]))
    assert silent == [], f"inbound messages with no reply: {silent}"


def _enable(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()
    return preferences


class BoomProvider:
    """Fails the way an unforeseen bug fails: not with ProviderError."""

    name = "scripted"
    api_key_setting = "XAI_API_KEY"

    def complete(self, **kwargs):
        raise KeyError("content")


def test_a_crashing_turn_still_answers(landlord, monkeypatch):
    _enable(landlord)
    monkeypatch.setattr(service, "get_provider", lambda name: BoomProvider())
    conversation_id = uuid.uuid4()

    result = service.run_turn(
        landlord, "how many rents did we receive for aug or are due?", conversation_id,
    )

    # The caller gets a reply, not an exception and not a bare error.
    assert result.error is None
    assert result.reply
    assert result.deterministic

    # And the conversation shows it.
    outbound = RamaMessage.objects.filter(
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
    )
    assert outbound.count() == 1
    assert outbound.first().kind == RamaMessage.Kind.RECOVERY
    assert_no_silent_turns(landlord)


def test_a_crash_is_audited_with_its_traceback(landlord, monkeypatch):
    _enable(landlord)
    monkeypatch.setattr(service, "get_provider", lambda name: BoomProvider())
    conversation_id = uuid.uuid4()

    service.run_turn(landlord, "anything owing?", conversation_id)

    crash = RamaAudit.objects.filter(
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.ERROR,
        content__error="turn_crashed",
    ).first()
    assert crash is not None
    assert "KeyError" in crash.content["exception"]
    assert crash.content["traceback"]


def _pending(landlord, conversation_id):
    from rentium.rama.plan_runner import save_single

    return save_single(
        landlord,
        conversation_id,
        "create_property",
        {"name": "Room Z", "address": "1 Test St", "city": "Victoria",
         "province": "bc"},
    )


def test_a_crash_discards_a_plan_the_landlord_never_saw(landlord, monkeypatch):
    """A router persists its plan before the reply is composed.

    Crash in between and the plan outlives the turn that made it, with no
    preview ever shown — so the landlord's next "yes" would execute something
    they were never offered. An unshown plan has no prompt_message bound.
    """
    _enable(landlord)
    conversation_id = uuid.uuid4()
    plan = _pending(landlord, conversation_id)
    assert plan.prompt_message is None
    monkeypatch.setattr(service, "get_provider", lambda name: BoomProvider())

    service.run_turn(landlord, "add a property", conversation_id)

    assert not RamaPendingPlan.objects.filter(
        landlord=landlord, conversation_id=conversation_id,
    ).exists()


def test_a_crash_keeps_a_plan_the_landlord_did_see(landlord, monkeypatch):
    """The other half of the rule, and the more dangerous one to get wrong.

    A preview that already reached the landlord is confirmed intent. A crashed
    side question must not silently destroy it and make them redo the work —
    the same invariant _persist_pending protects.
    """
    from rentium.rama.conversations import bind_plan_prompt
    from rentium.rama.conversations import record_visible_message

    _enable(landlord)
    conversation_id = uuid.uuid4()
    plan = _pending(landlord, conversation_id)
    shown = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text="Create Room Z at 1 Test St? Reply yes to confirm.",
        channel="web",
        role="corporal",
        kind=RamaMessage.Kind.PLAN_PROMPT,
    )
    bind_plan_prompt(plan, shown)
    monkeypatch.setattr(service, "get_provider", lambda name: BoomProvider())

    service.run_turn(landlord, "unrelated side question", conversation_id)

    assert RamaPendingPlan.objects.filter(
        landlord=landlord, conversation_id=conversation_id,
    ).exists()


def test_a_sub_turn_still_reports_failure_to_its_caller(landlord, monkeypatch):
    """depth>0 must keep raising.

    A delegating turn needs to know its helper died so it can say so. Handing it
    an apology string instead would get relayed to the landlord as though it
    were the treasurer's actual answer.
    """
    _enable(landlord)
    monkeypatch.setattr(service, "get_provider", lambda name: BoomProvider())

    with pytest.raises(KeyError):
        service.run_turn(landlord, "anything owing?", uuid.uuid4(), depth=1)


def test_provider_misconfiguration_is_visible_in_the_thread(landlord):
    """A bad API key used to return a bare HTTP status and no message."""
    preferences = _enable(landlord)
    preferences.provider = "xai"
    preferences.api_key = ""
    preferences.save()
    conversation_id = uuid.uuid4()

    result = service.run_turn(landlord, "anything owing?", conversation_id)

    if result.error is not None:  # no platform fallback key configured
        assert result.reply, "an error with no reply is a silent turn"
        assert RamaMessage.objects.filter(
            conversation_id=conversation_id,
            direction=RamaMessage.Direction.OUTBOUND,
        ).exists()
        assert_no_silent_turns(landlord)
