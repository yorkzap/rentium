"""
Durable landlord memory: the guards, the supersession, and the precedence
ladder that keeps it from ever outranking live portfolio data.

The three tests worth reading first are:
- test_memory_never_overrides_live_portfolio
- test_portfolio_numbers_cannot_be_stored
- test_special_category_data_is_refused

Those are the reasons this feature is safe to have at all.
"""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from rentium.rama import memory
from rentium.rama.models import RamaConstitutionRule
from rentium.rama.models import RamaMemory
from rentium.rama.models import RamaPreferences
from rentium.rama.providers import Turn
from rentium.rama.service import _memory_intent
from rentium.rama.service import run_turn

pytestmark = pytest.mark.django_db


class ScriptedProvider:
    name = "scripted"
    api_key_setting = "ANTHROPIC_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools, api_key=""):
        self.requests.append({"system": system, "messages": list(messages)})
        return self.turns.pop(0) if self.turns else Turn(text="")


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def _enable(landlord):
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "xai"
    prefs.api_key = "xai-test-key"
    prefs.save()


def _turn(landlord, message, provider=None, **kwargs):
    provider = provider or ScriptedProvider([])
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        return run_turn(landlord, message, uuid.uuid4(), **kwargs)


# ------------------------------------------------------------- the guards
@pytest.mark.parametrize(
    "body",
    [
        "the rent on Room C is $900",
        "total rent roll is $4,850",
        "I have 7 listings",
        "the lease ends 2026-11-30",
        "Sarah owes 1200.00",
        "Room B is currently vacant",
    ],
)
def test_portfolio_numbers_cannot_be_stored(body):
    """Live data must never be copied — it would go stale and then mislead."""
    reason = memory.rejects(body)
    assert reason is not None
    assert "live data" in reason


@pytest.mark.parametrize(
    "body",
    [
        "the tenant in 2B is on disability",
        "the applicant is pregnant",
        "Ravi is Hindu so no Friday inspections",
        "she has a criminal record",
        "the tenant's immigration status is pending",
    ],
)
def test_special_category_data_is_refused(body):
    reason = memory.rejects(body)
    assert reason is not None
    assert "privacy law" in reason


@pytest.mark.parametrize(
    "body",
    [
        "never do viewings on Sundays",
        "invoices go to my bookkeeper Dana",
        "call the basement suite the Garden",
        "my preferred plumber is Bob",
    ],
)
def test_genuine_preferences_are_allowed(body):
    assert memory.rejects(body) is None


def test_overlong_memory_is_refused():
    assert memory.rejects("x" * 500) is not None


def test_contact_details_are_flagged_not_refused(landlord):
    """Worth remembering, but must be findable for an erasure request."""
    body = "my plumber is Bob at 250-555-0100"
    assert memory.rejects(body) is None
    row = memory.write(landlord, key="plumber", body=body)
    assert row.contains_personal_data is True


# -------------------------------------------------------- supersession
def test_writing_the_same_subject_supersedes_rather_than_duplicating(landlord):
    first = memory.write(landlord, key="viewings", body="never viewings on Sundays")
    second = memory.write(landlord, key="viewings", body="Sundays are fine now")

    active = RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    )
    assert active.count() == 1
    assert active.first().pk == second.pk
    first.refresh_from_db()
    assert first.status == RamaMemory.Status.SUPERSEDED
    assert second.supersedes_id == first.pk


def test_two_active_rows_on_one_key_are_impossible(landlord):
    """Enforced by the database, not by convention."""
    from django.db import IntegrityError
    from django.db import transaction

    memory.write(landlord, key="viewings", body="never on Sundays")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RamaMemory.objects.create(
                landlord=landlord, key="viewings", body="a contradiction",
            )


def test_forget_retires_a_memory(landlord):
    memory.write(landlord, key="viewings", body="never viewings on Sundays")
    row = memory.forget(landlord, "viewings")
    assert row.status == RamaMemory.Status.FORGOTTEN
    assert memory.render_for_prompt(landlord, "viewings") == ""


# ------------------------------------------------------------ retrieval
def test_render_is_bounded_and_never_truncates_a_fact(landlord):
    for i in range(30):
        memory.write(landlord, key=f"pref-{i}", body=f"preference number {i} " + "x" * 60)
    block = memory.render_for_prompt(landlord, "anything")
    assert len(block) < 2000
    # Every emitted line is a whole stored fact, never a cut-off fragment.
    for line in [ln for ln in block.splitlines() if ln.startswith("- ")]:
        assert RamaMemory.objects.filter(
            landlord=landlord, body=line[2:].strip(),
        ).exists()


def test_implied_memories_are_recorded_but_never_injected(landlord):
    memory.write(
        landlord,
        key="guess",
        body="probably prefers morning viewings",
        source=RamaMemory.Source.LANDLORD_IMPLIED,
    )
    assert RamaMemory.objects.filter(landlord=landlord).count() == 1
    assert memory.render_for_prompt(landlord, "viewings") == ""


def test_entity_memories_only_appear_for_their_entity(landlord):
    memory.write(
        landlord,
        key="garden-key",
        body="the Garden Suite key is under the mat",
        scope=RamaMemory.Scope.ENTITY,
        entity_key="Garden Suite",
    )
    assert "under the mat" in memory.render_for_prompt(landlord, "open the Garden Suite")
    assert memory.render_for_prompt(landlord, "how is Room C doing") == ""


def test_memories_are_landlord_scoped(landlord, other_landlord):
    memory.write(landlord, key="viewings", body="mine: never Sundays")
    memory.write(other_landlord, key="viewings", body="theirs: never Mondays")

    mine = memory.render_for_prompt(landlord, "viewings")
    theirs = memory.render_for_prompt(other_landlord, "viewings")
    assert "never Sundays" in mine and "never Mondays" not in mine
    assert "never Mondays" in theirs and "never Sundays" not in theirs


def test_capacity_is_bounded_without_deleting(landlord):
    for i in range(RamaMemory.MAX_ACTIVE_PER_LANDLORD + 5):
        memory.write(landlord, key=f"pref-{i}", body=f"preference {i}")
    active = RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    ).count()
    assert active <= RamaMemory.MAX_ACTIVE_PER_LANDLORD
    # Nothing was destroyed — the overflow was superseded.
    assert RamaMemory.objects.filter(landlord=landlord).count() > active


# ---------------------------------------------------- the precedence ladder
def test_memory_never_overrides_live_portfolio(landlord):
    """A memory contradicting live data must lose, and must say so in-prompt.

    Seeded directly, bypassing the write guard, because this test is about what
    happens when a bad row exists anyway.
    """
    from rentium.properties.models import Property

    _enable(landlord)
    Property.objects.create(
        landlord=landlord,
        name="EvalRoom Hero",
        address="1 Hero St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        asking_rent="900.00",
    )
    RamaMemory.objects.create(
        landlord=landlord, key="hero-rent", body="the rent on EvalRoom Hero is $500",
    )

    provider = ScriptedProvider([Turn(text="EvalRoom Hero rents for $900.")])
    _turn(landlord, "What's the rent on EvalRoom Hero?", provider)

    system = provider.requests[0]["system"]
    ladder = system.index("LANDLORD MEMORY")
    header = system[ladder : ladder + 400]
    # The block states its own rank and forbids the exact failure mode: a
    # stale stored number being quoted as if it were current.
    assert "Subordinate to LIVE PORTFOLIO" in header
    assert "Never quote a number" in header
    # And it sits BELOW the authoritative live card, not above it.
    assert system.index("LIVE PORTFOLIO") < ladder


# -------------------------------------------------- the deterministic router
@pytest.mark.parametrize(
    "message,tool",
    [
        ("Remember that I never do viewings on Sundays", "remember"),
        ("remember my bookkeeper is Dana", "remember"),
        ("From now on send invoices to Dana", "remember"),
        ("Forget that I never do viewings on Sundays", "forget"),
    ],
)
def test_explicit_memory_instructions_route_deterministically(message, tool):
    intent = _memory_intent(message)
    assert intent is not None and intent["tool"] == tool


@pytest.mark.parametrize(
    "message",
    [
        "What do you remember about my viewings?",
        "Do you remember the rent on Room C?",
        "forget it",
        "remember",
    ],
)
def test_questions_and_cancellations_are_not_memory_writes(message):
    assert _memory_intent(message) is None


def test_the_same_topic_twice_produces_one_stable_subject():
    """Why supersession works from chat: the subject must be repeatable."""
    a = _memory_intent("Remember that invoices go to Dana")
    b = _memory_intent("Remember that invoices go to Dana instead")
    assert a["arguments"]["subject"] == b["arguments"]["subject"]


# ---------------------------------------------------------- end to end
def test_a_preference_survives_into_a_new_conversation(landlord):
    """The whole point: conversation two knows what conversation one was told."""
    _enable(landlord)
    first_conversation = uuid.uuid4()
    with mock.patch(
        "rentium.rama.service.get_provider", return_value=ScriptedProvider([]),
    ):
        run_turn(
            landlord,
            "Remember that I never do viewings on Sundays.",
            first_conversation,
        )
        # Autonomy is off by default, so the preference is previewed and stored
        # only once the landlord confirms it.
        run_turn(landlord, "yes", first_conversation)

    assert RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    ).exists()

    provider = ScriptedProvider([Turn(text="You don't do Sunday viewings.")])
    _turn(landlord, "Can you do a showing this Sunday?", provider)

    system = provider.requests[0]["system"]
    assert "never do viewings on Sundays" in system


def test_remember_previews_before_storing_when_autonomy_is_off(landlord):
    _enable(landlord)
    result = _turn(landlord, "Remember that I never do viewings on Sundays.")
    assert result.pending_plan is not None
    assert not RamaMemory.objects.filter(landlord=landlord).exists()


def test_remember_runs_immediately_when_memory_is_pre_authorised(landlord):
    """Memory composes with the autonomy tier — and stays undoable."""
    from rentium.rama.models import RamaAutoAction

    _enable(landlord)
    RamaConstitutionRule.objects.create(
        landlord=landlord,
        rule_type=RamaConstitutionRule.RuleType.AUTONOMY,
        params={"categories": ["memory"], "channels": ["web"]},
    )
    result = _turn(landlord, "Remember that my plumber is Bob at 250-555-0100.")

    row = RamaMemory.objects.get(landlord=landlord, status=RamaMemory.Status.ACTIVE)
    assert row.contains_personal_data is True
    assert result.pending_plan is None
    assert len(result.auto_executed) == 1
    assert RamaAutoAction.objects.filter(landlord=landlord).count() == 1

    undone = _turn(landlord, "undo")
    assert "undone" in undone.reply.lower() or "forg" in undone.reply.lower()
    assert not RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    ).exists()


def test_storing_a_portfolio_number_is_refused_end_to_end(landlord):
    _enable(landlord)
    result = _turn(landlord, "Remember that my total rent roll is $4,850.")
    assert not RamaMemory.objects.filter(landlord=landlord).exists()
    assert result.pending_plan is None
    assert "live data" in result.reply


# ------------------------------------------------------------- API + erasure
from rest_framework.test import APIClient


def _api(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


def test_memory_api_lists_and_erases(landlord):
    from rentium.rama.models import RamaAudit

    row = memory.write(landlord, key="viewings", body="never viewings on Sundays")
    client = _api(landlord)

    listed = client.get("/api/rama/memory/").json()["memories"]
    assert [m["fact"] for m in listed] == ["never viewings on Sundays"]

    assert client.delete(f"/api/rama/memory/{row.pk}/").status_code == 200
    assert not RamaMemory.objects.filter(pk=row.pk).exists()

    # Erasure must not smuggle the body into the append-only audit trail.
    erased = RamaAudit.objects.filter(content__tool="_memory_erased").first()
    assert erased is not None
    assert erased.content["arguments"] == {"key": "viewings"}
    assert "Sundays" not in str(erased.content)


def test_memory_api_is_landlord_scoped(landlord, other_landlord):
    row = memory.write(landlord, key="viewings", body="never viewings on Sundays")
    intruder = _api(other_landlord)
    assert intruder.get("/api/rama/memory/").json()["memories"] == []
    assert intruder.delete(f"/api/rama/memory/{row.pk}/").status_code == 404
    assert RamaMemory.objects.filter(pk=row.pk).exists()


def test_forget_subject_command_is_a_dry_run_without_yes(landlord):
    from django.core.management import call_command

    memory.write(landlord, key="plumber", body="my plumber is Bob at 250-555-0100")
    call_command("rama_forget_subject", "--match", "250-555-0100")
    assert RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    ).exists()

    call_command("rama_forget_subject", "--match", "250-555-0100", "--yes", "--delete")
    assert not RamaMemory.objects.filter(landlord=landlord).exists()
