"""Running out of budget must cost the answer's polish, never the answer.

Observed August 2026. The landlord asked whether two named tenants had received
a discount. The turn ran ten successful reads — including the exact rows for both
tenants — hit the step limit, threw all of it away, and replied:

    "That took more steps than I can do in one turn — ask me to continue, or
     break it into smaller steps."

They said "continue". It did the same thing again. The data had been in hand both
times; only the sentence was missing. So when either budget runs out, the turn
now spends its last round converting what it already read into an answer.
"""

from __future__ import annotations

import uuid

import pytest

from rentium.rama import service
from rentium.rama.models import RamaAudit
from rentium.rama.models import RamaPreferences
from rentium.rama.providers.base import Turn
from rentium.rama.providers.base import ToolCall

pytestmark = pytest.mark.django_db


def _enable(landlord):
    preferences = RamaPreferences.for_landlord(landlord)
    preferences.enabled = True
    preferences.provider = "xai"
    preferences.api_key = "test-key"
    preferences.save()


class _Recorder:
    """Calls a tool every round until it is asked for prose."""

    name = "scripted"
    api_key_setting = "XAI_API_KEY"

    def __init__(self, *, wrap_up_text="", fail_wrap_up=False):
        self.requests = []
        self.wrap_up_text = wrap_up_text
        self.fail_wrap_up = fail_wrap_up

    def complete(self, *, model, system, messages, tools, api_key=""):
        self.requests.append({"messages": list(messages), "tools": tools})
        if not tools:  # the wrap-up round
            if self.fail_wrap_up:
                from rentium.rama.providers.base import ProviderError

                raise ProviderError("upstream down")
            return Turn(text=self.wrap_up_text)
        return Turn(
            tool_calls=[
                ToolCall(
                    id=f"t{len(self.requests)}",
                    name="data_catalogue",
                    arguments={},
                ),
            ],
        )


def test_step_limit_answers_from_what_it_gathered(landlord, monkeypatch):
    _enable(landlord)
    answer = "Naveen's August rent was adjusted to $400.00 (lease RMT652523-C281)."
    provider = _Recorder(wrap_up_text=answer)
    monkeypatch.setattr(service, "get_provider", lambda name: provider)

    result = service.run_turn(
        landlord, "did aishwarya and naveen get a discount?", uuid.uuid4(),
    )

    assert answer in result.reply
    assert "more steps than I can do" not in result.reply


def test_the_wrap_up_round_is_asked_for_prose_not_tools(landlord, monkeypatch):
    _enable(landlord)
    provider = _Recorder(wrap_up_text="Two leases, $1,325.00 outstanding.")
    monkeypatch.setattr(service, "get_provider", lambda name: provider)

    service.run_turn(landlord, "summarise august", uuid.uuid4())

    last = provider.requests[-1]
    assert last["tools"] == [], "the final round must not be able to call a tool"
    assert last["messages"][-1]["content"] == service._WRAP_UP_NOW
    assert last["messages"][-1]["role"] == "user"
    # Every earlier round DID have tools — the wrap-up is the exception.
    assert all(r["tools"] for r in provider.requests[:-1])


def test_wrap_up_failure_falls_back_instead_of_raising(landlord, monkeypatch):
    """Running out of budget must not become an exception on top of it."""
    _enable(landlord)
    provider = _Recorder(fail_wrap_up=True)
    monkeypatch.setattr(service, "get_provider", lambda name: provider)

    result = service.run_turn(landlord, "summarise august", uuid.uuid4())

    assert result.reply, "a failed wrap-up still owes the landlord a sentence"
    assert "steps" in result.reply


def test_silent_wrap_up_falls_back(landlord, monkeypatch):
    """A model with nothing to say gets the old message, not an empty reply."""
    _enable(landlord)
    provider = _Recorder(wrap_up_text="   ")
    monkeypatch.setattr(service, "get_provider", lambda name: provider)

    result = service.run_turn(landlord, "summarise august", uuid.uuid4())

    assert result.reply.strip()
    assert "steps" in result.reply


def test_recovery_is_audited_as_such(landlord, monkeypatch):
    """The exhaustion is still recorded — it is a real limit being hit, and
    silently papering over it would hide the thing worth tuning."""
    _enable(landlord)
    provider = _Recorder(wrap_up_text="Here are the four August leases.")
    monkeypatch.setattr(service, "get_provider", lambda name: provider)

    service.run_turn(landlord, "summarise august", uuid.uuid4())

    kinds = [
        row.content.get("error")
        for row in RamaAudit.objects.filter(kind=RamaAudit.Kind.ERROR)
    ]
    assert "turn_step_budget_exhausted" in kinds
    recovered = RamaAudit.objects.filter(
        kind=RamaAudit.Kind.ERROR,
        content__recovered=True,
    )
    assert recovered.exists()


def test_time_budget_exhaustion_also_wraps_up(landlord, monkeypatch):
    """The other exhaustion path had the identical defect."""
    _enable(landlord)
    provider = _Recorder(wrap_up_text="August: 2 rent charges, $2,750.00.")
    monkeypatch.setattr(service, "get_provider", lambda name: provider)
    # Zero seconds of budget: the deadline is blown before the first round.
    monkeypatch.setattr(service, "_turn_budget_seconds", lambda: 0.0)

    result = service.run_turn(landlord, "summarise august", uuid.uuid4())

    assert "2,750.00" in result.reply
    assert "kept looking" not in result.reply
    assert len(provider.requests) == 1, "no tool rounds should have run at all"
    assert provider.requests[0]["tools"] == []
