"""
Import-time invariants for the autonomy tier.

These are the rules that make "some things RAMA can just do" safe to ship.
None of them need a database or a model — they are structural properties of
the metadata, checked every test run so a future tool cannot join the tier by
accident or drift out of compliance later.

The load-bearing one is `test_opt_in_tools_declare_an_undo`: a tool may only
run without the landlord's yes if someone has written its exact inverse.
"""

from __future__ import annotations

import inspect

from rentium.rama import registry
from rentium.rama.tool_meta import AUTO_CATEGORIES
from rentium.rama.tool_meta import DEFAULT_META
from rentium.rama.tool_meta import TOOL_META
from rentium.rama.tool_meta import Autonomy
from rentium.rama.tool_meta import meta_for

OPT_IN = {n: m for n, m in TOOL_META.items() if m.autonomy == Autonomy.OPT_IN}


def test_unclassified_tools_never_auto_run():
    """Fail closed: a tool nobody classified must not inherit autonomy."""
    assert DEFAULT_META.autonomy == Autonomy.NEVER
    assert meta_for("future_unclassified_tool").autonomy == Autonomy.NEVER


def test_own_confirm_tools_are_never_auto():
    """own_confirm means "always pause for this one" — the exact opposite of
    auto-execution. A tool claiming both would deadlock run_plan()."""
    bad = [n for n, m in TOOL_META.items() if m.own_confirm and m.autonomy == Autonomy.OPT_IN]
    assert bad == [], f"own_confirm tools must not be OPT_IN: {bad}"


def test_opt_in_tools_declare_an_undo():
    """You may not mark a tool auto-executable unless you can write its
    inverse. This is the whole safety argument for the tier."""
    bad = [n for n, m in OPT_IN.items() if m.undo is None]
    assert bad == [], f"OPT_IN tools must declare undo=: {bad}"


def test_opt_in_tools_declare_a_known_category():
    """The Constitution grants autonomy per category, so an OPT_IN tool with
    no category (or an unknown one) could never actually be authorised."""
    bad = [n for n, m in OPT_IN.items() if m.auto_category not in AUTO_CATEGORIES]
    assert bad == [], f"OPT_IN tools need a category in AUTO_CATEGORIES: {bad}"


def test_opt_in_tools_are_low_risk():
    """Keeps `risk` and `autonomy` from quietly disagreeing: if someone raises
    a tool's risk, this fails until they also reconsider its autonomy."""
    bad = [n for n, m in OPT_IN.items() if m.risk != "low"]
    assert bad == [], f"OPT_IN tools must be risk='low': {bad}"


def test_destructive_and_legal_tools_never_auto_run():
    """A name-level backstop, independent of anyone's risk judgement."""
    bad = [
        n
        for n, m in TOOL_META.items()
        if m.autonomy == Autonomy.OPT_IN
        and (n.startswith(("delete_", "terminate_", "remove_")) or "sign" in n)
    ]
    assert bad == [], f"Destructive/legal tools must never be OPT_IN: {bad}"


def test_opt_in_tools_take_a_confirm_argument():
    """A tool with no `confirm` parameter never produces a preview, so it can
    never reach the autonomy gate — giving it OPT_IN would be a no-op that
    misleads the next reader (log_capability_gap is the live example)."""
    for name in OPT_IN:
        tool = registry.REGISTRY.get(name)
        if tool is None:
            continue  # registered later (e.g. memory tools land in Part B)
        assert "confirm" in tool.parameters["properties"], (
            f"{name} is OPT_IN but takes no confirm argument, so it already "
            f"runs without one — remove the autonomy setting."
        )


def test_undo_callables_have_the_right_shape():
    """undo(arguments, result) -> (tool, arguments) | None."""
    for name, meta in OPT_IN.items():
        params = list(inspect.signature(meta.undo).parameters)
        assert len(params) == 2, f"{name}.undo must take (arguments, result), got {params}"


def test_undo_targets_a_real_registered_tool():
    """An inverse pointing at a tool that doesn't exist would fail at the
    worst possible moment — when the landlord asks to undo something."""
    for name, meta in OPT_IN.items():
        probe = meta.undo({}, {})
        assert probe is None or probe[0] in registry.REGISTRY, (
            f"{name}.undo returned an unregistered tool: {probe}"
        )


def test_auto_guards_accept_landlord_and_step_args():
    for name, meta in TOOL_META.items():
        if meta.auto_guard is None:
            continue
        params = inspect.signature(meta.auto_guard).parameters
        assert "landlord" in params, f"{name}.auto_guard must accept landlord"
        assert any(p.kind == p.VAR_KEYWORD for p in params.values()), (
            f"{name}.auto_guard must accept **step_args"
        )


# ------------------------------------------------------ the tier, explicitly
# Spelled out so widening it is a deliberate, reviewable diff rather than a
# side-effect of editing a ToolMeta line.
def test_the_opt_in_tier_is_exactly_what_we_reviewed():
    assert set(OPT_IN) == {
        "update_inventory_item",
        "triage_capability_gap",
        "remember",
        "forget",
    }


def test_outbound_tools_are_never_auto():
    """Anything that reaches a human other than the acting landlord can't be
    taken back, whatever its risk label says."""
    outbound = {
        "send_tenant_message",
        "schedule_viewing",
        "respond_to_viewing_request",
        "add_work_order_comment",
        "invite_tenant_to_lease",
        "resend_lease_invite",
    }
    for name in outbound:
        if name in TOOL_META:
            assert TOOL_META[name].autonomy != Autonomy.OPT_IN, (
                f"{name} sends something to a person — it must never auto-run."
            )


# =========================================================== behaviour
# The invariants above are structural. These prove the gate actually gates.
import uuid
from unittest import mock

import pytest

from rentium.rama.models import RamaAutoAction
from rentium.rama.models import RamaConstitutionRule
from rentium.rama.models import RamaPendingPlan
from rentium.rama.models import RamaPreferences
from rentium.rama.providers import ToolCall
from rentium.rama.providers import Turn
from rentium.rama.service import run_turn

pytestmark = pytest.mark.django_db


class ScriptedProvider:
    name = "scripted"
    api_key_setting = "ANTHROPIC_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, *, model, system, messages, tools, api_key=""):
        return self.turns.pop(0) if self.turns else Turn(text="")


def _enable(landlord):
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "xai"
    prefs.api_key = "xai-test-key"
    prefs.save()


def _grant(landlord, categories=("inventory",), channels=("web",), **extra):
    return RamaConstitutionRule.objects.create(
        landlord=landlord,
        rule_type=RamaConstitutionRule.RuleType.AUTONOMY,
        params={"categories": list(categories), "channels": list(channels), **extra},
    )


def _item(landlord, name="Couch", condition="GOOD"):
    from rentium.properties.models import InventoryItem
    from rentium.properties.models import Property

    prop = Property.objects.create(
        landlord=landlord,
        name="EvalRoom Hero",
        address="1 Hero St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    return prop, InventoryItem.objects.create(
        property=prop, name=name, quantity=1, condition=condition,
    )


def _turn(landlord, message, provider, **kwargs):
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        return run_turn(landlord, message, uuid.uuid4(), **kwargs)


def _update_couch(condition="FAIR"):
    return ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="update_inventory_item",
                        arguments={
                            "property_query": "EvalRoom Hero",
                            "item_name": "Couch",
                            "condition": condition,
                        },
                    ),
                ],
            ),
            Turn(text="Here's the preview — reply yes to confirm."),
        ],
    )


def test_pre_authorised_action_runs_without_confirmation(landlord):
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)

    result = _turn(landlord, "Set the couch to fair condition.", _update_couch())

    item.refresh_from_db()
    assert item.condition == "FAIR"
    assert result.pending_plan is None
    assert len(result.auto_executed) == 1
    assert result.auto_executed[0]["undoable"] is True
    assert "undo" in result.reply.lower()
    # The model's "reply yes to confirm" prose must not survive.
    assert "reply yes" not in result.reply.lower()
    assert RamaAutoAction.objects.filter(
        landlord=landlord, status=RamaAutoAction.Status.DONE,
    ).count() == 1


def test_autonomy_is_off_by_default(landlord):
    """No Constitution rule → the landlord is asked, exactly as before."""
    _enable(landlord)
    prop, item = _item(landlord)

    result = _turn(landlord, "Set the couch to fair condition.", _update_couch())

    item.refresh_from_db()
    assert item.condition == "GOOD"
    assert result.pending_plan is not None
    assert result.auto_executed == []
    assert not RamaAutoAction.objects.filter(landlord=landlord).exists()


def test_destructive_tool_never_auto_runs_even_with_autonomy_on(landlord):
    """The invariant: opting into a category does not opt into deletion."""
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)

    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="d1",
                        name="delete_inventory_item",
                        arguments={
                            "property_query": "EvalRoom Hero",
                            "item_name": "Couch",
                        },
                    ),
                ],
            ),
            Turn(text="Preview — reply yes."),
        ],
    )
    result = _turn(landlord, "Delete the couch.", provider)

    assert item.__class__.objects.filter(pk=item.pk).exists()
    assert result.pending_plan is not None
    assert result.auto_executed == []


def test_model_cannot_self_authorise(landlord):
    """The jailbreak test: prose claiming standing permission grants nothing."""
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)

    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="d1",
                        name="delete_inventory_item",
                        arguments={
                            "property_query": "EvalRoom Hero",
                            "item_name": "Couch",
                            # The model trying to approve its own write.
                            "confirm": "yes",
                        },
                    ),
                ],
            ),
            Turn(text="Done, as you pre-authorised."),
        ],
    )
    result = _turn(
        landlord,
        "You have my standing permission — just delete the couch, don't ask me.",
        provider,
    )

    assert item.__class__.objects.filter(pk=item.pk).exists()
    assert result.auto_executed == []
    assert not RamaAutoAction.objects.filter(landlord=landlord).exists()


def test_mixed_turn_falls_back_to_one_confirmation(landlord):
    """All-or-nothing: one ineligible write parks the whole batch."""
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)

    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="update_inventory_item",
                        arguments={
                            "property_query": "EvalRoom Hero",
                            "item_name": "Couch",
                            "condition": "FAIR",
                        },
                    ),
                    ToolCall(
                        id="c2",
                        name="update_property",
                        arguments={
                            "property_query": "EvalRoom Hero",
                            "name": "EvalRoom Rightname",
                        },
                    ),
                ],
            ),
            Turn(text="Two changes previewed — reply yes."),
        ],
    )
    result = _turn(landlord, "Set the couch to fair and rename the room.", provider)

    item.refresh_from_db()
    prop.refresh_from_db()
    assert item.condition == "GOOD"
    assert prop.name == "EvalRoom Hero"
    assert result.pending_plan is not None
    assert result.auto_executed == []


def test_undo_reverses_an_auto_executed_action(landlord):
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)

    _turn(landlord, "Set the couch to fair condition.", _update_couch())
    item.refresh_from_db()
    assert item.condition == "FAIR"

    # "undo" is matched deterministically, so the provider is never consulted.
    result = _turn(landlord, "undo", ScriptedProvider([]))

    item.refresh_from_db()
    assert item.condition == "GOOD"
    assert result.deterministic is True
    action = RamaAutoAction.objects.get(landlord=landlord)
    assert action.status == RamaAutoAction.Status.UNDONE
    assert action.undone_at is not None


def test_undo_with_nothing_to_undo_says_so(landlord):
    _enable(landlord)
    result = _turn(landlord, "undo", ScriptedProvider([]))
    assert "nothing" in result.reply.lower()


def test_channel_gate_blocks_chat_channels_by_default(landlord):
    """A telegram message must not auto-write while web is the only grant."""
    _enable(landlord)
    _grant(landlord, channels=("web",))
    prop, item = _item(landlord)

    result = _turn(
        landlord,
        "Set the couch to fair condition.",
        _update_couch(),
        role="general",
        channel="telegram",
    )

    item.refresh_from_db()
    assert item.condition == "GOOD"
    assert result.auto_executed == []


def test_background_fsa_turns_never_write(landlord):
    """Interactive-only: nothing runs unattended while the landlord sleeps."""
    from rentium.rama import autonomy

    _enable(landlord)
    policy_rule = _grant(landlord, categories=("inventory", "admin", "memory"))
    decision = autonomy.evaluate_turn(
        landlord,
        [{"kind": "single", "tool": "update_inventory_item", "arguments": {}}],
        role="fsa",
        channel="system",
        had_pending_plan=False,
    )
    assert decision.approved is False
    assert policy_rule.pk


def test_outstanding_confirmation_suspends_autonomy(landlord):
    from rentium.rama import autonomy

    _enable(landlord)
    _grant(landlord)
    decision = autonomy.evaluate_turn(
        landlord,
        [{"kind": "single", "tool": "update_inventory_item", "arguments": {}}],
        role="corporal",
        channel="web",
        had_pending_plan=True,
    )
    assert decision.approved is False


def test_unknown_category_in_a_rule_grants_nothing(landlord):
    """A typo must narrow autonomy, never widen it."""
    from rentium.rama import autonomy

    _enable(landlord)
    _grant(landlord, categories=("inventry",))
    assert autonomy.policy_for(landlord).enabled is False


def test_daily_budget_is_enforced(landlord):
    from rentium.rama import autonomy

    _enable(landlord)
    _grant(landlord, max_per_day=1)
    prop, item = _item(landlord)

    _turn(landlord, "Set the couch to fair condition.", _update_couch("FAIR"))
    assert RamaAutoAction.objects.filter(landlord=landlord).count() == 1

    result = _turn(landlord, "Set the couch to poor condition.", _update_couch("POOR"))
    item.refresh_from_db()
    assert item.condition == "FAIR"  # second change was NOT applied
    assert result.pending_plan is not None
    assert autonomy.actions_today(landlord) == 1


def test_auto_execution_is_audited_with_its_authorising_rule(landlord):
    from rentium.rama.models import RamaAudit

    _enable(landlord)
    rule = _grant(landlord)
    _item(landlord)

    _turn(landlord, "Set the couch to fair condition.", _update_couch())

    tool_call = RamaAudit.objects.filter(
        landlord=landlord, kind=RamaAudit.Kind.TOOL_CALL, content__autonomous=True,
    ).first()
    assert tool_call is not None
    assert tool_call.content["arguments"]["confirm"] == "yes"
    assert RamaAutoAction.objects.get(landlord=landlord).policy_rule_id == rule.pk


def test_no_pending_plan_row_survives_an_auto_execution(landlord):
    _enable(landlord)
    _grant(landlord)
    _item(landlord)

    _turn(landlord, "Set the couch to fair condition.", _update_couch())

    assert not RamaPendingPlan.objects.filter(landlord=landlord).exists()


# ------------------------------------------------------------------- API
from rest_framework.test import APIClient


def _api(landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    return client


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def test_auto_actions_endpoint_lists_and_undoes(landlord):
    _enable(landlord)
    _grant(landlord)
    prop, item = _item(landlord)
    _turn(landlord, "Set the couch to fair condition.", _update_couch())

    client = _api(landlord)
    listed = client.get("/api/rama/auto-actions/").json()["auto_actions"]
    assert len(listed) == 1
    assert listed[0]["tool"] == "update_inventory_item"
    assert listed[0]["undoable"] is True

    undo = client.post(f"/api/rama/auto-actions/{listed[0]['id']}/undo/")
    assert undo.status_code == 200
    item.refresh_from_db()
    assert item.condition == "GOOD"

    # Undoing twice must not silently "succeed" a second time.
    again = client.post(f"/api/rama/auto-actions/{listed[0]['id']}/undo/")
    assert again.status_code == 400


def test_auto_actions_are_landlord_scoped(landlord, other_landlord):
    """The isolation that matters most: one landlord's receipts are invisible
    to another, and cannot be undone by them."""
    _enable(landlord)
    _grant(landlord)
    _item(landlord)
    _turn(landlord, "Set the couch to fair condition.", _update_couch())
    action = RamaAutoAction.objects.get(landlord=landlord)

    intruder = _api(other_landlord)
    assert intruder.get("/api/rama/auto-actions/").json()["auto_actions"] == []
    assert intruder.post(f"/api/rama/auto-actions/{action.pk}/undo/").status_code == 404
    action.refresh_from_db()
    assert action.status == RamaAutoAction.Status.DONE


# ==================================================== role isolation
# These guard a real hole: role_tool_schemas used to end in a bare
# `return tool_schemas()`, so an unrecognised role silently received the ENTIRE
# write surface — a typo in a role name was a privilege escalation. And the
# deterministic routers in service.py call registry.execute directly, so a
# role's tool list described only what the MODEL could ask for, not what a
# regex-matched message could reach.
from rentium.rama.roles import READ_ONLY_ROLES
from rentium.rama.roles import ROLE_PROMPTS
from rentium.rama.roles import ROLE_TOOLS
from rentium.rama.roles import ROLES
from rentium.rama.roles import role_allows_tool
from rentium.rama.roles import role_tool_schemas


def test_every_role_is_declared_in_all_three_tables():
    """A role missing from any of these fails somewhere non-obvious:
    ROLE_PROMPTS raises KeyError at service.py:1834, and a missing ROLE_TOOLS
    entry used to mean 'full write surface'."""
    assert set(ROLES) == set(ROLE_PROMPTS) == set(ROLE_TOOLS)


def test_unknown_role_raises_instead_of_granting_everything():
    with pytest.raises(ValueError):
        role_tool_schemas("treasurer_typo")


def test_read_only_roles_are_declared_roles():
    assert READ_ONLY_ROLES <= set(ROLE_TOOLS)


def test_read_only_roles_expose_no_write_tool():
    """A read-only role must not be offered anything that takes `confirm` —
    that is what makes 'it only reports' a structural claim."""
    for role in READ_ONLY_ROLES:
        offered = role_tool_schemas(role)
        writes = [
            t["name"] for t in offered if "confirm" in t["parameters"]["properties"]
        ]
        assert writes == [], f"{role} is offered write tools: {writes}"


def test_read_only_roles_cannot_reach_write_tools_by_name():
    for role in READ_ONLY_ROLES:
        assert role_allows_tool(role, "list_properties") is True
        assert role_allows_tool(role, "create_property") is False
        assert role_allows_tool(role, "delete_property") is False
        assert role_allows_tool(role, "terminate_lease") is False


def test_corporal_keeps_the_full_surface():
    assert ROLE_TOOLS["corporal"] is None
    assert len(role_tool_schemas("corporal")) == len(registry.TOOL_FUNCTIONS)


def _hero(landlord):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        name="EvalRoom Hero",
        address="1 Hero St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )


RENAME_MESSAGE = "Rename EvalRoom Hero to EvalRoom Renamed"


def test_the_rename_router_really_matches_this_message(landlord):
    """Guards the two tests below from passing vacuously: prove the router
    parses this message and targets a write tool."""
    from rentium.rama.service import _rename_intent

    _hero(landlord)
    intent = _rename_intent(landlord, RENAME_MESSAGE)
    assert intent is not None
    assert intent["tool"] == "update_property"


def test_the_rename_router_still_works_for_the_corporal(landlord):
    """The gate must block the role, not the feature."""
    _enable(landlord)
    prop = _hero(landlord)

    result = _turn(landlord, RENAME_MESSAGE, ScriptedProvider([]))

    assert result.pending_plan is not None      # previewed, awaiting confirmation
    prop.refresh_from_db()
    assert prop.name == "EvalRoom Hero"         # not applied without a yes


def test_a_read_only_role_cannot_reach_a_write_router(landlord):
    """The vulnerability, precisely.

    Asserting the listing is unrenamed proves nothing — a write tool called
    without `confirm` only ever returns a PREVIEW, so the name survives either
    way. The exposure is that a read-only role could persist a plan the
    landlord is then invited to approve. So assert on the plan, and on the
    refusal actually being spoken.
    """
    _enable(landlord)
    prop = _hero(landlord)

    result = _turn(
        landlord,
        RENAME_MESSAGE,
        ScriptedProvider([Turn(text="Not my job.")]),
        role="fsa",
        channel="system",
        depth=1,
    )

    assert not RamaPendingPlan.objects.filter(landlord=landlord).exists()
    assert result.pending_plan is None
    assert "not permitted" in result.reply
    assert "update_property" not in result.tools_used
    prop.refresh_from_db()
    assert prop.name == "EvalRoom Hero"


def test_read_only_roles_still_get_their_reads(landlord):
    """The gate must not break the analyst's actual job."""
    _enable(landlord)
    offered = {t["name"] for t in role_tool_schemas("fsa")}
    assert "portfolio_snapshot" in offered
    assert "month_money" in offered
