"""
The Treasurer: a read-only finance head.

Its read-only guarantee is structural rather than promised — no tool on its
list takes a `confirm` argument, the deterministic write routers refuse it
(service._run_deterministic_tool), and the autonomy tier never approves it. If
any of those three drift apart, one of these fails.
"""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from rentium.rama import autonomy, registry
from rentium.rama.models import RamaPendingPlan, RamaPreferences
from rentium.rama.providers import Turn
from rentium.rama.roles import (
    READ_ONLY_ROLES,
    ROLE_TOOLS,
    TREASURER_TOOLS,
    role_allows_tool,
    role_tool_schemas,
)
from rentium.rama.runtime import get_role_config
from rentium.rama.service import run_turn
from rentium.rama.providers.testing import assert_translatable

pytestmark = pytest.mark.django_db


class ScriptedProvider:
    name = "scripted"
    api_key_setting = "ANTHROPIC_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools, api_key=""):
        assert_translatable(messages)
        self.requests.append({"system": system, "tools": tools, "model": model})
        return self.turns.pop(0) if self.turns else Turn(text="")


def _enable(landlord, provider="xai"):
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = provider
    prefs.api_key = "chat-key"
    prefs.save()
    return prefs


def _turn(landlord, message, provider=None, **kwargs):
    provider = provider or ScriptedProvider([Turn(text="ok")])
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        return run_turn(landlord, message, uuid.uuid4(), **kwargs)


# ------------------------------------------------------------- registration
def test_the_treasurer_is_a_fully_declared_role():
    assert "treasurer" in ROLE_TOOLS
    assert "treasurer" in READ_ONLY_ROLES
    assert role_tool_schemas("treasurer")


def test_the_treasurer_has_no_write_tool():
    """The read-only guarantee, stated as a property of the tool list."""
    writes = [
        t["name"]
        for t in role_tool_schemas("treasurer")
        if "confirm" in t["parameters"]["properties"]
    ]
    assert writes == []


def test_the_treasurer_gets_the_finance_reads_nothing_else_had():
    """deposit_position and tenant_statement were registered but on no role's
    list, so nothing could reach them."""
    offered = {t["name"] for t in role_tool_schemas("treasurer")}
    assert {"deposit_position", "tenant_statement"} <= offered
    assert {"month_money", "list_expenses", "list_bank_balances"} <= offered


def test_the_treasurer_cannot_reach_write_tools_by_name():
    for tool in ("create_expense", "update_property", "terminate_lease", "remember"):
        assert role_allows_tool("treasurer", tool) is False


def test_the_treasurer_sees_no_delegation_tools():
    """One level of hierarchy: staff do not have staff."""
    offered = {t["name"] for t in role_tool_schemas("treasurer")}
    assert not ({"ask_corporal", "ask_fsa", "ask_treasurer"} & offered)


def test_the_general_can_consult_the_treasurer():
    offered = {t["name"] for t in role_tool_schemas("general", depth=0)}
    assert "ask_treasurer" in offered


def test_the_general_prompt_names_the_treasurer():
    """A tool the prompt never mentions is a tool the model never calls."""
    from rentium.rama.roles import GENERAL_PROMPT

    assert "ask_treasurer" in GENERAL_PROMPT


# ------------------------------------------------------------ model routing
def test_the_treasurer_prefers_gemini_when_the_platform_can_call_it(
    landlord, settings
):
    _enable(landlord, provider="xai")
    settings.GEMINI_API_KEY = "gemini-platform-key"

    cfg = get_role_config(landlord, "treasurer")
    assert cfg.provider == "gemini"
    assert cfg.model == "gemini-flash-latest"


def test_it_falls_back_to_the_chat_provider_when_gemini_is_unavailable(
    landlord, settings
):
    """Routing to a provider with no key would just fail at call time."""
    _enable(landlord, provider="xai")
    settings.GEMINI_API_KEY = ""

    cfg = get_role_config(landlord, "treasurer")
    assert cfg.provider == "xai"


def test_an_explicit_landlord_choice_beats_the_default(landlord, settings):
    settings.GEMINI_API_KEY = "gemini-platform-key"
    prefs = _enable(landlord, provider="xai")
    prefs.treasurer_provider = "anthropic"
    prefs.treasurer_model = "claude-haiku-4-5"
    prefs.treasurer_api_key = "byok-anthropic"
    prefs.save()

    cfg = get_role_config(landlord, "treasurer")
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.has_own_key is True


# --------------------------------------------------------- read-only in fact
def test_the_treasurer_cannot_trigger_a_write_router(landlord):
    from rentium.properties.models import Property

    _enable(landlord)
    prop = Property.objects.create(
        landlord=landlord,
        name="EvalRoom Hero",
        address="1 Hero St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )

    result = _turn(
        landlord,
        "Rename EvalRoom Hero to EvalRoom Renamed",
        role="treasurer",
        channel="web",
    )

    assert not RamaPendingPlan.objects.filter(landlord=landlord).exists()
    assert "not permitted" in result.reply
    prop.refresh_from_db()
    assert prop.name == "EvalRoom Hero"


def test_autonomy_never_approves_a_treasurer_turn(landlord):
    from rentium.rama.models import RamaConstitutionRule

    RamaConstitutionRule.objects.create(
        landlord=landlord,
        rule_type=RamaConstitutionRule.RuleType.AUTONOMY,
        params={"categories": ["inventory", "admin", "memory"], "channels": ["web"]},
    )
    decision = autonomy.evaluate_turn(
        landlord,
        [{"kind": "single", "tool": "update_inventory_item", "arguments": {}}],
        role="treasurer",
        channel="web",
        had_pending_plan=False,
    )
    assert decision.approved is False


def test_a_treasurer_turn_writes_nothing_to_the_domain(landlord):
    from rentium.ledger.models import LedgerEntry
    from rentium.properties.models import Property

    _enable(landlord)
    before = (
        Property.objects.filter(landlord=landlord).count(),
        LedgerEntry.objects.filter(landlord=landlord).count(),
    )

    _turn(landlord, "Where am I wasting money?", role="treasurer", channel="web")

    assert (
        Property.objects.filter(landlord=landlord).count(),
        LedgerEntry.objects.filter(landlord=landlord).count(),
    ) == before


def test_the_treasurer_prompt_reaches_the_model(landlord):
    _enable(landlord)
    provider = ScriptedProvider([Turn(text="ok")])
    _turn(landlord, "How is the portfolio doing?", provider, role="treasurer")

    system = provider.requests[0]["system"]
    assert "You are the Treasurer" in system
    assert "planning estimate" in system


# -------------------------------------------------------------- the surface
def test_the_chat_endpoint_is_wired(landlord, settings):
    from rest_framework.test import APIClient

    settings.GEMINI_API_KEY = "gemini-platform-key"
    _enable(landlord)
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    with mock.patch(
        "rentium.rama.service.get_provider",
        return_value=ScriptedProvider([Turn(text="Equity is up.")]),
    ):
        response = client.post(
            "/api/rama/treasurer/chat/", {"message": "equity?"}, format="json"
        )
    assert response.status_code == 200
    assert response.json()["reply"] == "Equity is up."


def test_settings_expose_the_treasurer(landlord):
    from rest_framework.test import APIClient

    _enable(landlord)
    client = APIClient()
    client.force_authenticate(user=landlord.user)

    body = client.get("/api/rama/settings/").json()
    assert "treasurer" in body

    client.patch(
        "/api/rama/settings/",
        {"treasurer": {"provider": "gemini", "model": "gemini-flash-latest"}},
        format="json",
    )
    prefs = RamaPreferences.for_landlord(landlord)
    assert prefs.treasurer_provider == "gemini"
    assert prefs.treasurer_model == "gemini-flash-latest"
