"""Tool-owned clarification survives terse visible-chat follow-ups."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from rentium.ledger.models import EntryType
from rentium.ledger.models import LedgerEntry
from rentium.rama.models import RamaPreferences
from rentium.rama.models import RamaTask
from rentium.rama.providers import ToolCall
from rentium.rama.providers import Turn
from rentium.rama.providers.testing import assert_translatable

pytestmark = pytest.mark.django_db
EXPECTED_DEPOSIT_STEPS = 2


class ScriptedProvider:
    name = "scripted"
    api_key_setting = "XAI_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools, api_key=""):
        assert_translatable(messages)
        self.requests.append(messages)
        return self.turns.pop(0)


def _enable(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()


def _deposit(landlord, lease, description, amount):
    from rentium.ledger import services

    charge, _ = services.post_charge(
        landlord=landlord,
        property=lease.property,
        lease=lease,
        tenant=None,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal(amount),
        due_date=timezone.localdate(),
        description=description,
    )
    return charge


def test_one_short_answer_resumes_every_tool_bound_to_the_prompt(
    landlord,
    bc_lease,
):
    """Two deposit writes ask one real-world question. The answer advances
    both saved calls into one plan without another provider round.
    """
    from rentium.rama.models import RamaPendingPlan
    from rentium.rama.service import run_turn

    _enable(landlord)
    security = _deposit(
        landlord,
        bc_lease,
        "Security deposit — due on signing",
        "650.00",
    )
    cleaning = _deposit(
        landlord,
        bc_lease,
        "Cleaning deposit — due on signing",
        "350.00",
    )
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="security",
                        name="record_payment",
                        arguments={
                            "lease_number": bc_lease.lease_number,
                            "charge_query": "security deposit",
                        },
                    ),
                    ToolCall(
                        id="cleaning",
                        name="record_payment",
                        arguments={
                            "lease_number": bc_lease.lease_number,
                            "charge_query": "cleaning deposit",
                        },
                    ),
                ],
            ),
        ],
    )

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        question = run_turn(
            landlord,
            "Record both the security and cleaning deposits on that lease",
            role="general",
            channel="telegram",
        )
        preview = run_turn(
            landlord,
            "e-transfer",
            question.conversation_id,
            role="general",
            channel="telegram",
        )

    assert len(provider.requests) == 1
    assert "e-transfer, cash, or cheque" in question.reply
    tasks = list(RamaTask.objects.order_by("created_at"))
    clarified = [task for task in tasks if task.capability_key == "record_payment"]
    assert len(clarified) == EXPECTED_DEPOSIT_STEPS
    assert all(task.status == RamaTask.Status.VERIFIED for task in clarified)
    plan = RamaPendingPlan.objects.get(conversation_id=question.conversation_id)
    assert plan.steps.count() == EXPECTED_DEPOSIT_STEPS
    assert {step.arguments["charge_ids"] for step in plan.steps.all()} == {
        str(security.pk),
        str(cleaning.pk),
    }
    assert all(
        step.arguments["payment_method"] == "etransfer" for step in plan.steps.all()
    )
    assert "one “Yes” will run all 2 changes" in preview.reply

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        completed = run_turn(
            landlord,
            "yes",
            question.conversation_id,
            role="general",
            channel="telegram",
        )
    assert len(provider.requests) == 1
    assert "Recorded $650.00" in completed.reply
    assert "Recorded $350.00" in completed.reply
    assert {
        row.settles_id
        for row in LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT)
    } == {security.pk, cleaning.pk}


def test_option_matching_uses_structured_aliases_not_old_chat_prose(landlord):
    from rentium.rama.clarifications import answer_for
    from rentium.rama.clarifications import persist_tool_clarification

    task = persist_tool_clarification(
        landlord=landlord,
        conversation_id=uuid.uuid4(),
        episode=None,
        source_message=None,
        tool="record_payment",
        arguments={"charge_query": "deposit"},
        result={
            "question_for_user": "Which household?",
            "needs": "charge_ids",
            "clarification": {
                "field": "charge_ids",
                "options": [
                    {
                        "value": "garden-charge",
                        "label": "Garden Suite security deposit",
                        "aliases": ["Aiswarya Chenthamara", "Naveen Singaravel"],
                    },
                    {
                        "value": "room-c-charge",
                        "label": "Room C security deposit",
                        "aliases": ["Siya Gulati"],
                    },
                ],
            },
        },
    )

    assert answer_for(task, "Aiswarya / Naveen") == (
        "charge_ids",
        "garden-charge",
    )
