"""
RAMA v1: registry scoping, tool correctness, the chat loop, and the audit
trail — all with a scripted stub provider, so no test ever touches a real
model API. The provider adapters' message translation is tested statically.
"""

from datetime import date
from unittest import mock

import pytest
from rest_framework.test import APIClient

from rentium.ledger import services as ledger_services
from rentium.ledger.models import EntryType
from rentium.rama import registry
from rentium.rama.models import RamaAudit
from rentium.rama.providers import ProviderError, ToolCall, Turn, get_provider
from rentium.rama.providers.anthropic import AnthropicProvider
from rentium.rama.providers.openai_compat import OpenAIProvider
from rentium.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------- fixtures
@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile

    return LandlordProfile.objects.create(user=UserFactory())


@pytest.fixture
def signed_tenant(tenant, bc_lease):
    from rentium.leases.models import LeaseTenant

    # display_name prefers the linked account's own name over invited_name
    tenant.user.name = "Sarah Novak"
    tenant.user.save(update_fields=["name"])
    return LeaseTenant.objects.create(
        lease=bc_lease,
        tenant=tenant,
        rent_amount="850.00",
        invited_name="Sarah Novak",
        invited_email="sarah@example.com",
        is_primary_tenant=True,
        has_signed=True,
    )


def _client_for(profile):
    client = APIClient()
    client.force_authenticate(user=profile.user)
    return client


def _rent_charge(landlord, lease, amount="850.00", due=None):
    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        tenant=None,
        lease=lease,
        property=lease.property,
        amount=amount,
        due_date=due or date.today().replace(day=1),
        entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    return charge


class ScriptedProvider:
    """Plays back a fixed sequence of Turns and records what it was sent."""

    name = "scripted"
    api_key_setting = "ANTHROPIC_API_KEY"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, model, system, messages, tools):
        self.requests.append(
            {"model": model, "system": system, "messages": list(messages), "tools": tools}
        )
        return self.turns.pop(0)


# --------------------------------------------------------------- registry
def test_registry_hides_landlord_and_derives_types():
    schemas = {t["name"]: t for t in registry.tool_schemas()}
    resolve = schemas["resolve_person"]
    assert "landlord" not in resolve["parameters"]["properties"]
    assert resolve["parameters"]["required"] == ["name"]
    assert resolve["parameters"]["properties"]["name"] == {"type": "string"}
    # optional args stay out of `required`
    charge = schemas["charge_status"]
    assert charge["parameters"]["required"] == ["lease_id"]
    assert "month" in charge["parameters"]["properties"]
    assert len(schemas) == len(registry.TOOL_FUNCTIONS)


def test_registry_execute_guards(landlord):
    assert "error" in registry.execute("drop_tables", {}, landlord=landlord)
    assert "error" in registry.execute("resolve_person", {}, landlord=landlord)
    # unknown argument names are dropped, not fatal
    out = registry.execute(
        "resolve_person", {"name": "x", "landlord": "evil", "extra": 1}, landlord=landlord
    )
    assert out["candidates"] == []


# ------------------------------------------------------------------ tools
def test_resolve_person_is_scoped_to_landlord(
    landlord, bc_lease, signed_tenant, other_landlord
):
    mine = registry.execute("resolve_person", {"name": "sarah"}, landlord=landlord)
    assert [c["name"] for c in mine["candidates"]] == ["Sarah Novak"]
    assert mine["candidates"][0]["lease_id"] == str(bc_lease.pk)

    theirs = registry.execute(
        "resolve_person", {"name": "sarah"}, landlord=other_landlord
    )
    assert theirs["candidates"] == []


def test_lease_state_and_bad_ids(landlord, bc_lease, signed_tenant, other_landlord):
    state = registry.execute(
        "lease_state", {"lease_id": str(bc_lease.pk)}, landlord=landlord
    )
    assert state["status"] == "ACTIVE"
    assert state["monthly_rent"] == "850.00"
    assert state["tenants"][0]["name"] == "Sarah Novak"

    # someone else's lease and garbage ids both come back as errors
    assert "error" in registry.execute(
        "lease_state", {"lease_id": str(bc_lease.pk)}, landlord=other_landlord
    )
    assert "error" in registry.execute(
        "lease_state", {"lease_id": "not-a-uuid"}, landlord=landlord
    )


def test_charge_status_reports_payment(landlord, bc_lease):
    charge = _rent_charge(landlord, bc_lease)
    ledger_services.record_payment(
        charge=charge, amount="850.00", payment_method="ETRANSFER"
    )
    month = date.today().strftime("%Y-%m")
    out = registry.execute(
        "charge_status",
        {"lease_id": str(bc_lease.pk), "month": month},
        landlord=landlord,
    )
    assert len(out["charges"]) == 1
    row = out["charges"][0]
    assert row["status"] == "paid"
    assert row["outstanding"] == "0.00"

    assert "error" in registry.execute(
        "charge_status", {"lease_id": str(bc_lease.pk), "month": "july"}, landlord=landlord
    )


# --------------------------------------------------- state of the union
def test_state_of_the_union_endpoint(landlord, bc_lease):
    charge = _rent_charge(landlord, bc_lease)
    ledger_services.record_payment(
        charge=charge, amount="850.00", payment_method="ETRANSFER"
    )
    response = _client_for(landlord).get("/api/rama/state-of-the-union/")
    assert response.status_code == 200
    data = response.json()
    assert data["portfolio"]["leases"]["active"] == 1
    assert data["this_month"]["collected_income"] == "850.00"
    assert data["outstanding"]["total"] == "0.00"
    assert "attention" in data


def test_state_of_the_union_rejects_tenants(tenant):
    assert _client_for(tenant).get("/api/rama/state-of-the-union/").status_code == 403


# ------------------------------------------------------------------ config
def test_config_endpoint(landlord, settings):
    settings.RAMA_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = ""
    data = _client_for(landlord).get("/api/rama/config/").json()
    assert data["enabled"] is True
    assert data["configured"] is False  # no key set
    assert data["provider"] == "anthropic"
    assert data["can_override"] is False
    assert "anthropic" in data["providers"]

    settings.ANTHROPIC_API_KEY = "sk-test"
    assert _client_for(landlord).get("/api/rama/config/").json()["configured"] is True


# -------------------------------------------------------------------- chat
def _chat(client, provider, payload):
    with mock.patch("rentium.rama.views.get_provider", return_value=provider):
        return client.post("/api/rama/chat/", payload, format="json")


def test_chat_tool_loop_scopes_and_audits(landlord, bc_lease, signed_tenant):
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(id="t1", name="resolve_person", arguments={"name": "Sarah"})
                ]
            ),
            Turn(text="Sarah Novak is on the active lease at Oak Ave Suite B."),
        ]
    )
    response = _chat(
        _client_for(landlord), provider, {"message": "Who is Sarah?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert "Sarah Novak" in body["reply"]
    assert body["tools_used"] == ["resolve_person"]
    assert body["model"]  # the configured default is echoed back

    # second provider round-trip carried the tool result back to the model
    tool_message = provider.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Sarah Novak" in tool_message["content"]

    # the audit trail has all four kinds of rows, stamped with provider/model
    kinds = list(
        RamaAudit.objects.filter(landlord=landlord).values_list("kind", flat=True)
    )
    assert kinds == ["USER_MESSAGE", "TOOL_CALL", "ASSISTANT_MESSAGE"]
    tool_row = RamaAudit.objects.get(kind=RamaAudit.Kind.TOOL_CALL)
    assert tool_row.content["tool"] == "resolve_person"
    assert tool_row.content["result"]["candidates"][0]["name"] == "Sarah Novak"
    assert tool_row.model  # stamped


def test_chat_reuses_conversation_history(landlord):
    client = _client_for(landlord)
    first = _chat(
        client, ScriptedProvider([Turn(text="Hello!")]), {"message": "Hi"}
    ).json()

    second_provider = ScriptedProvider([Turn(text="Still here.")])
    second = _chat(
        client,
        second_provider,
        {"message": "You there?", "conversation_id": first["conversation_id"]},
    ).json()
    assert second["conversation_id"] == first["conversation_id"]

    sent = second_provider.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "Hi"
    assert sent[1]["text"] == "Hello!"


def test_chat_gates(landlord, tenant, settings):
    provider = ScriptedProvider([Turn(text="hi")])
    assert _chat(_client_for(tenant), provider, {"message": "hi"}).status_code == 403
    assert _chat(_client_for(landlord), provider, {"message": ""}).status_code == 400

    settings.RAMA_ENABLED = False
    assert _chat(_client_for(landlord), provider, {"message": "hi"}).status_code == 403


def test_chat_model_override_is_staff_only(landlord, settings):
    settings.RAMA_MODEL = "claude-haiku-4-5"
    client = _client_for(landlord)
    body = _chat(
        client,
        ScriptedProvider([Turn(text="ok")]),
        {"message": "hi", "model": "gpt-x", "provider": "openai"},
    ).json()
    assert body["model"] == "claude-haiku-4-5"  # ignored for non-staff

    landlord.user.is_staff = True
    landlord.user.save(update_fields=["is_staff"])
    body = _chat(
        client,
        ScriptedProvider([Turn(text="ok")]),
        {"message": "hi", "model": "gpt-x", "provider": "openai"},
    ).json()
    assert body["model"] == "gpt-x"
    assert body["provider"] == "openai"


def test_chat_provider_failure_is_502_and_audited(landlord):
    class FailingProvider(ScriptedProvider):
        def complete(self, **kwargs):
            raise ProviderError("upstream down")

    response = _chat(_client_for(landlord), FailingProvider([]), {"message": "hi"})
    assert response.status_code == 502
    assert RamaAudit.objects.filter(kind=RamaAudit.Kind.ERROR).exists()


def test_chat_tool_round_limit(landlord):
    endless = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id=f"t{i}", name="next_charge", arguments={})])
            for i in range(20)
        ]
    )
    body = _chat(_client_for(landlord), endless, {"message": "loop"}).json()
    assert "narrower" in body["reply"]
    assert len(endless.requests) == 8  # MAX_TOOL_ROUNDS


# -------------------------------------------------------------- providers
def test_provider_registry_and_missing_keys(settings):
    settings.ANTHROPIC_API_KEY = ""
    with pytest.raises(ProviderError):
        get_provider("anthropic").complete(
            model="m", system="s", messages=[], tools=[]
        )
    with pytest.raises(ProviderError):
        get_provider("nope")


def test_anthropic_wire_format():
    to_wire = AnthropicProvider._to_wire
    assert to_wire({"role": "user", "content": "hi"}) == {
        "role": "user",
        "content": "hi",
    }
    assistant = to_wire(
        {
            "role": "assistant",
            "text": "checking",
            "tool_calls": [{"id": "t1", "name": "next_charge", "arguments": {}}],
        }
    )
    assert assistant["content"][0] == {"type": "text", "text": "checking"}
    assert assistant["content"][1]["type"] == "tool_use"
    tool = to_wire(
        {"role": "tool", "tool_call_id": "t1", "name": "next_charge", "content": "{}"}
    )
    assert tool["role"] == "user"
    assert tool["content"][0]["tool_use_id"] == "t1"


def test_openai_wire_format():
    to_wire = OpenAIProvider._to_wire
    assistant = to_wire(
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [{"id": "c1", "name": "next_charge", "arguments": {"a": 1}}],
        }
    )
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    tool = to_wire(
        {"role": "tool", "tool_call_id": "c1", "name": "next_charge", "content": "{}"}
    )
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
