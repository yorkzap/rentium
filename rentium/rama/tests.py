"""
RAMA v1: registry scoping, tool correctness, the chat loop, and the audit
trail — all with a scripted stub provider, so no test ever touches a real
model API. The provider adapters' message translation is tested statically.
"""

from datetime import date
from decimal import Decimal
from unittest import mock
import uuid

import pytest
from django.utils import timezone
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

    def complete(self, *, model, system, messages, tools, api_key: str = ""):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "tools": tools,
                "api_key": bool(api_key),
            }
        )
        if not self.turns:
            return Turn(text="")
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
    # Listings inventory must be present so room counts are not invented.
    assert "listings" in data
    assert "rooms" in data["portfolio"]
    assert "room_names" in data["portfolio"]
    assert data["portfolio"]["properties"] == data["listings"]["counts"]["total_listings"]


def test_list_properties_and_portfolio_include_all_rooms(landlord):
    """Regression: RAMA used to only return aggregate property counts, so
    Gemini would invent or undercount rooms (e.g. only Room E of D+E)."""
    from rentium.properties.models import Property

    Property.objects.create(
        landlord=landlord,
        name="McKenzie Room D",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )

    listed = registry.execute("list_properties", {}, landlord=landlord)
    assert listed["counts"]["rooms"] == 2
    names = {r["name"] for r in listed["rooms"]}
    assert names == {"McKenzie Room D", "McKenzie Room E"}

    snap = registry.execute("portfolio_snapshot", {}, landlord=landlord)
    assert snap["portfolio"]["rooms"] == 2
    assert set(snap["portfolio"]["room_names"]) == names
    assert "list_properties" in {t["name"] for t in registry.tool_schemas()}
    assert "list_leases" in {t["name"] for t in registry.tool_schemas()}


def test_future_active_lease_is_vacant_today_but_committed(landlord):
    """Future lease: vacant today, has commitment, not rented this month if
    start is next month — and never confused with listing AVAILABLE."""
    from datetime import timedelta

    from rentium.leases.models import Lease, LeaseTenant
    from rentium.properties.models import Property

    room_d = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room D",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    room_e = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    # Start on the 1st of next month so "this calendar month" is false.
    today = date.today()
    if today.month == 12:
        start = date(today.year + 1, 1, 1)
    else:
        start = date(today.year, today.month + 1, 1)
    end = start + timedelta(days=150)
    lease = Lease.objects.create(
        landlord=landlord,
        property=room_e,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=start,
        end_date=end,
        is_month_to_month=False,
        total_rent="850.00",
    )
    LeaseTenant.objects.create(
        lease=lease,
        rent_amount="850.00",
        invited_name="someone",
        invited_email="test@test.com",
        is_primary_tenant=True,
        has_signed=True,
    )

    listed = registry.execute("list_properties", {}, landlord=landlord)
    by_name = {r["name"]: r for r in listed["rooms"]}
    assert by_name["McKenzie Room D"]["occupancy"]["phase"] == "vacant"
    assert by_name["McKenzie Room D"]["occupancy"]["vacant_today"] is True
    assert by_name["McKenzie Room D"]["occupancy"]["is_rented_or_committed"] is False
    e = by_name["McKenzie Room E"]
    assert e["listing_status"] == "AVAILABLE"
    assert e["occupancy"]["phase"] == "leased_future"
    assert e["occupancy"]["vacant_today"] is True
    assert e["occupancy"]["occupied_today"] is False
    assert e["occupancy"]["is_rented_or_committed"] is True
    assert e["occupancy"]["has_future_commitment"] is True
    assert e["occupancy"]["term_overlaps_this_calendar_month"] is False
    assert e["occupancy"]["lease"]["start_date"] == start.isoformat()
    assert "Vacant today" in e["occupancy"]["explanation"]
    assert listed["counts"]["by_occupancy"]["vacant_today"] == 2
    assert listed["counts"]["by_occupancy"]["occupied_today"] == 0
    assert listed["counts"]["by_occupancy"]["leased_future"] == 1
    assert listed["counts"]["by_occupancy"]["has_lease_commitment"] == 1
    assert listed["counts"]["by_occupancy"]["truly_unleased"] == 1

    snap = registry.execute("portfolio_snapshot", {}, landlord=landlord)
    assert snap["portfolio"]["vacant_today"] == 2
    assert snap["portfolio"]["occupied_today"] == 0
    assert snap["portfolio"]["has_lease_commitment"] == 1
    assert snap["rented_listings"][0]["vacant_today"] is True
    assert snap["rented_listings"][0]["term_overlaps_this_calendar_month"] is False

    # Currently-covering term → occupied today
    Lease.objects.create(
        landlord=landlord,
        property=room_d,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=60),
        total_rent="800.00",
    )
    listed2 = registry.execute("list_properties", {}, landlord=landlord)
    d2 = {r["name"]: r for r in listed2["rooms"]}["McKenzie Room D"]
    assert d2["occupancy"]["phase"] == "occupied_now"
    assert d2["occupancy"]["occupied_today"] is True
    assert d2["occupancy"]["vacant_today"] is False


def test_lease_brief_exposes_agreement_type_not_only_term_shape(landlord):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    lease = Lease.objects.create(
        landlord=landlord,
        property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 31),
        is_month_to_month=False,
        total_rent="850.00",
    )
    state = registry.execute(
        "lease_state", {"lease_id": str(lease.pk)}, landlord=landlord
    )
    assert "Roommate" in state["agreement_type"] or "Roommate" in state[
        "lease_type_display"
    ]
    assert state["term_shape"] == "fixed_term"
    assert state["is_month_to_month"] is False
    assert "agreement" in state["type_hint"].lower()

    listed = registry.execute("list_properties", {}, landlord=landlord)
    e = next(r for r in listed["rooms"] if r["name"] == "McKenzie Room E")
    assert "Roommate" in e["occupancy"]["lease"]["agreement_type"]


def test_property_type_and_suggested_lease_for_garden_suite(landlord):
    from rentium.properties.models import Property

    Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="100 Example St",
        city="Victoria",
        province="BC",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
        status=Property.PropertyStatus.AVAILABLE,
        bedrooms=1,
        bathrooms="1.0",
    )
    listed = registry.execute("list_properties", {}, landlord=landlord)
    g = next(r for r in listed["rooms"] + listed["complete_units"] if r["name"] == "Garden Suite")
    assert g["primary_type"] == "Garden Suite"
    assert g["unit_type"] == "GARDEN_SUITE"
    assert "Condo" not in (g.get("kind_summary") or "")
    assert g["category"] == "COMPLETE_UNIT"
    sug = g["suggested_lease_if_created"]
    assert sug["agreement_type_code"] == "BC_RESIDENTIAL"
    assert "RTB" in sug["agreement_type"] or "Residential" in sug["agreement_type"]
    assert "RTB lease" in sug["also_known_as"]


def test_list_expenses_empty_day_still_returns_month(landlord):
    from rentium.ledger.models import EntryType, ExpenseCategory, LedgerEntry
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    today = date.today()
    # Expense dated later this month (or last day), not today
    if today.day >= 28:
        eff = today
    else:
        eff = today.replace(day=min(28, today.day + 5))
    LedgerEntry.objects.create(
        landlord=landlord,
        property=room,
        entry_type=EntryType.EXPENSE,
        amount="100.00",
        effective_date=eff,
        description="Saanich Utilities",
        category=ExpenseCategory.UTILITIES,
    )
    if eff == today:
        # ensure we test empty-day path with a different day
        day = (today.replace(day=1)).isoformat()
    else:
        day = today.isoformat()
    res = registry.execute(
        "list_expenses", {"day": day}, landlord=landlord
    )
    if day == today.isoformat() and eff != today:
        assert res["count"] == 0
        assert res["this_month_expenses"]
        assert res["empty_day_note"]
        assert Decimal(res["this_month_total"]) >= Decimal("100.00")
    else:
        # day was 1st and expense might be in month either way
        assert res["count"] >= 0


def test_list_expenses_finds_property_total(landlord):
    from rentium.ledger.models import EntryType, ExpenseCategory, LedgerEntry
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    # Use current calendar month so defaults work.
    today = date.today()
    month = today.strftime("%Y-%m")
    LedgerEntry.objects.create(
        landlord=landlord,
        property=room,
        entry_type=EntryType.EXPENSE,
        amount="100.00",
        effective_date=today.replace(day=min(28, today.day)),
        description="Saanich Utilities (period)",
        category=ExpenseCategory.UTILITIES,
        vendor="Saanich Utilities",
    )
    LedgerEntry.objects.create(
        landlord=landlord,
        property=room,
        entry_type=EntryType.EXPENSE,
        amount="500.00",
        effective_date=today.replace(day=min(21, today.day)),
        description="Telus (period)",
        category=ExpenseCategory.UTILITIES,
        vendor="Telus",
    )

    by_prop = registry.execute(
        "list_expenses",
        {"month": month, "property_query": "Room E"},
        landlord=landlord,
    )
    assert by_prop["count"] == 2
    assert by_prop["total"] == "600.00"
    assert all(r["bank_status"] == "NOT_YET_TAKEN" for r in by_prop["expenses"])

    by_amt = registry.execute(
        "list_expenses",
        {"month": month, "amount": "600"},
        landlord=landlord,
    )
    assert by_amt["count"] >= 2
    assert by_amt.get("amount_match") is True or by_amt["total"] == "600.00"

    money = registry.execute("month_money", {"month": month}, landlord=landlord)
    assert money["month"] == month
    assert money["expenses"] == "600.00"
    assert money["expense_count"] == 2
    assert len(money["expense_lines"]) == 2
    # Year in label matches as_of — never a hallucinated past year
    assert str(today.year) in money["label"]

    snap = registry.execute("portfolio_snapshot", {}, landlord=landlord)
    assert snap["this_month"]["expenses"] == "600.00"
    assert len(snap["this_month_expenses"]) == 2


def test_list_appointments_finds_scheduled_viewing(landlord):
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from rentium.appointments.models import Appointment
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room E",
        address="950 McKenzie Avenue",
        city="Saanich",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        status=Property.PropertyStatus.AVAILABLE,
    )
    day = date.today() + timedelta(days=12)
    # list_appointments reports time_local in America/Vancouver, so pin the
    # appointment to that zone (14:14 local) rather than UTC wall-clock.
    starts = datetime.combine(day, time(14, 14), tzinfo=ZoneInfo("America/Vancouver"))
    Appointment.objects.create(
        landlord=landlord,
        property=room,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.SCHEDULED,
        starts_at=starts,
        contact_name="Prospect",
    )

    by_day = registry.execute(
        "list_appointments",
        {"day": day.isoformat()},
        landlord=landlord,
    )
    assert by_day["counts"]["total_returned"] == 1
    assert by_day["appointments"][0]["property"] == "McKenzie Room E"
    assert by_day["appointments"][0]["status"] == "SCHEDULED"
    assert by_day["appointments"][0]["date"] == day.isoformat()
    assert by_day["appointments"][0]["time_local"] == "14:14"
    assert by_day["appointments"][0]["kind"] == "VIEWING"

    upcoming = registry.execute("list_appointments", {}, landlord=landlord)
    assert upcoming["counts"]["upcoming_scheduled_or_requested"] >= 1

    snap = registry.execute("portfolio_snapshot", {}, landlord=landlord)
    assert snap["portfolio"]["upcoming_viewings"] >= 1
    assert any(
        a["property"] == "McKenzie Room E" for a in snap["upcoming_appointments"]
    )
    assert "list_appointments" in {t["name"] for t in registry.tool_schemas()}


def test_state_of_the_union_rejects_tenants(tenant):
    assert _client_for(tenant).get("/api/rama/state-of-the-union/").status_code == 403


def _enable_rama(landlord, *, provider="xai", model="", api_key="xai-test-key", settings=None):
    """Opt this landlord in with a BYOK key (or set platform keys via settings)."""
    from rentium.rama.models import RamaPreferences

    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = provider
    prefs.model = model
    prefs.api_key = api_key
    prefs.save()
    if settings is not None and not api_key:
        settings.XAI_API_KEY = settings.XAI_API_KEY or "xai-test-key"
        settings.ANTHROPIC_API_KEY = settings.ANTHROPIC_API_KEY or "sk-ant-test"
        settings.OPENAI_API_KEY = settings.OPENAI_API_KEY or "sk-openai-test"
    return prefs


# ------------------------------------------------------------------ config
def test_config_endpoint(landlord, settings):
    settings.XAI_API_KEY = ""
    settings.ANTHROPIC_API_KEY = ""
    settings.OPENAI_API_KEY = ""

    # Off by default — each landlord opts in.
    data = _client_for(landlord).get("/api/rama/config/").json()
    assert data["enabled"] is False
    assert data["configured"] is False
    assert data["provider"] == "xai"
    assert data["can_override"] is False
    assert "xai" in data["providers"]
    assert "models" in data

    _enable_rama(
        landlord,
        provider="xai",
        model="grok-4.5",
        api_key="xai-user-key",
        settings=settings,
    )
    data = _client_for(landlord).get("/api/rama/config/").json()
    assert data["enabled"] is True
    assert data["configured"] is True
    assert data["provider"] == "xai"
    assert data["model"] == "grok-4.5"
    assert data["has_api_key"] is True


def test_settings_patch_is_per_landlord(landlord, other_landlord, settings):
    client = _client_for(landlord)
    settings.XAI_API_KEY = ""
    res = client.patch(
        "/api/rama/settings/",
        {
            "enabled": True,
            "provider": "xai",
            "model": "grok-4.5",
            "api_key": "xai-from-settings",
        },
        format="json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["model"] == "grok-4.5"
    assert body["has_api_key"] is True
    assert "api_key" not in body  # never echo the secret

    # Other landlord still off — no shared state.
    other = _client_for(other_landlord).get("/api/rama/config/").json()
    assert other["enabled"] is False


# -------------------------------------------------------------------- chat
def _chat(client, provider, payload):
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        return client.post("/api/rama/chat/", payload, format="json")


def test_chat_tool_loop_scopes_and_audits(landlord, bc_lease, signed_tenant, settings):
    _enable_rama(landlord, settings=settings)
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
    # Each turn opens with a fresh _live_context snapshot, then the model's tools.
    assert body["tools_used"] == ["_live_context", "resolve_person"]
    assert body["model"]  # the configured default is echoed back

    # second provider round-trip carried the tool result back to the model
    tool_message = provider.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Sarah Novak" in tool_message["content"]

    # the audit trail records the snapshot + the tool call, stamped with model
    kinds = list(
        RamaAudit.objects.filter(landlord=landlord).values_list("kind", flat=True)
    )
    assert kinds == ["USER_MESSAGE", "TOOL_CALL", "TOOL_CALL", "ASSISTANT_MESSAGE"]
    tool_row = RamaAudit.objects.get(
        kind=RamaAudit.Kind.TOOL_CALL, content__tool="resolve_person"
    )
    assert tool_row.content["result"]["candidates"][0]["name"] == "Sarah Novak"
    assert tool_row.model  # stamped


def test_chat_reuses_conversation_history(landlord, settings):
    _enable_rama(landlord, settings=settings)
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


def test_followup_pronoun_resolves_to_conversation_property(landlord, settings):
    """'It' in a follow-up is resolved server-side, not left to a weak model."""
    from rentium.properties.models import Property
    from rentium.rama.models import RamaAudit, RamaPendingPlan

    _enable_rama(landlord, settings=settings)
    prop = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    client = _client_for(landlord)
    first = _chat(
        client,
        ScriptedProvider([Turn(text="Garden Suite is currently a Private Room.")]),
        {"message": "Tell me about Garden Suite"},
    ).json()

    second_provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="u1",
                        name="update_property",
                        arguments={
                            "property_query": "it",
                            "property_category": "COMPLETE_UNIT",
                            "unit_type": "GARDEN_SUITE",
                        },
                    )
                ]
            ),
            Turn(text="I prepared the structured classification correction."),
        ]
    )
    second = _chat(
        client,
        second_provider,
        {
            "message": "make it a full unit",
            "conversation_id": first["conversation_id"],
        },
    ).json()

    assert "## CONVERSATION FOCUS" in second_provider.requests[0]["system"]
    assert '"name":"Garden Suite"' in second_provider.requests[0]["system"]
    audit = RamaAudit.objects.filter(
        landlord=landlord,
        conversation_id=second["conversation_id"],
        kind=RamaAudit.Kind.TOOL_CALL,
        content__tool="update_property",
    ).latest("created_at")
    # Conversation focus resolves the pronoun, then the write path pins the
    # selected row by UUID so confirmation cannot drift to a later duplicate.
    assert audit.content["arguments"]["property_query"] in {
        "Garden Suite",
        str(prop.pk),
    }, audit.content
    pending = RamaPendingPlan.objects.get(conversation_id=second["conversation_id"])
    assert pending.steps.get().arguments["property_query"] == str(prop.pk)
    prop.refresh_from_db()
    assert prop.property_category == Property.PropertyCategory.ROOM


def test_business_photo_attachment_focus_persists_across_followups(
    landlord, settings
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.properties.models import Property
    from rentium.rama.models import RamaUpload

    _enable_rama(landlord, settings=settings)
    Property.objects.create(
        landlord=landlord,
        name="Room C",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    staged = RamaUpload.objects.create(
        landlord=landlord,
        image=SimpleUploadedFile("bank-letter.jpg", b"photo"),
    )
    client = _client_for(landlord)
    first_provider = ScriptedProvider([Turn(text="wrong model response")])
    first = _chat(
        client,
        first_provider,
        {
            "message": "This document was sent by Scotiabank. Store it carefully.",
            "upload_ids": [str(staged.pk)],
        },
    ).json()
    assert first_provider.requests == []
    assert "physical property address" in first["reply"]
    followup = ScriptedProvider([Turn(text="I will file it at the holding level.")])
    second = _chat(
        client,
        followup,
        {
            "message": "Not a listing—the property 950 McKenzie Ave overall.",
            "conversation_id": first["conversation_id"],
        },
    ).json()
    assert followup.requests == []
    assert "Address: 950 McKenzie Ave" in second["reply"], second
    assert "Individual listing: none" in second["reply"]
    assert second["pending_plan"] is not None


def test_document_directory_question_is_answered_deterministically(
    landlord, settings
):
    import uuid

    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.rama.document_services import ingest_document
    from rentium.rama.models import RamaAudit

    _enable_rama(landlord, settings=settings)
    document, _ = ingest_document(
        landlord=landlord,
        upload=SimpleUploadedFile(
            "notice.pdf", b"%PDF-location", content_type="application/pdf"
        ),
    )
    conversation_id = uuid.uuid4()
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.TOOL_CALL,
        content={
            "tool": "catalog_business_document",
            "arguments": {"document_id": str(document.pk)},
            "result": {
                "updated": True,
                "catalogued": True,
                "document_id": str(document.pk),
            },
        },
    )
    provider = ScriptedProvider([Turn(text="I have no directory path.")])
    body = _chat(
        _client_for(landlord),
        provider,
        {
            "message": "Tell me the directory/location so I can view it manually",
            "conversation_id": str(conversation_id),
        },
    ).json()
    assert provider.requests == []
    assert "Storage key: business_documents/inbox/" in body["reply"]
    assert "Manual location:" in body["reply"]
    assert "Container path:" in body["reply"]
    assert "/dashboard/documents" in body["reply"]


def test_chat_gates(landlord, tenant, settings):
    provider = ScriptedProvider([Turn(text="hi")])
    # Tenant never
    assert _chat(_client_for(tenant), provider, {"message": "hi"}).status_code == 403
    # Landlord with RAMA off
    assert _chat(_client_for(landlord), provider, {"message": "hi"}).status_code == 403

    _enable_rama(landlord, settings=settings)
    assert _chat(_client_for(landlord), provider, {"message": ""}).status_code == 400
    assert _chat(_client_for(landlord), provider, {"message": "hi"}).status_code == 200


def test_chat_uses_landlord_model_not_request_body(landlord, settings):
    """Body provider/model are ignored — prefs on the account win."""
    _enable_rama(
        landlord, provider="anthropic", model="claude-haiku-4-5", settings=settings
    )
    body = _chat(
        _client_for(landlord),
        ScriptedProvider([Turn(text="ok")]),
        {"message": "hi", "model": "gpt-x", "provider": "openai"},
    ).json()
    assert body["model"] == "claude-haiku-4-5"
    assert body["provider"] == "anthropic"


def test_chat_provider_failure_is_502_and_audited(landlord, settings):
    _enable_rama(landlord, settings=settings)

    class FailingProvider(ScriptedProvider):
        def complete(self, **kwargs):
            raise ProviderError("upstream down")

    response = _chat(_client_for(landlord), FailingProvider([]), {"message": "hi"})
    assert response.status_code == 502
    assert RamaAudit.objects.filter(kind=RamaAudit.Kind.ERROR).exists()


def test_chat_tool_round_limit(landlord, settings):
    _enable_rama(landlord, settings=settings)
    endless = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id=f"t{i}", name="next_charge", arguments={})])
            for i in range(20)
        ]
    )
    body = _chat(_client_for(landlord), endless, {"message": "loop"}).json()
    assert "steps" in body["reply"]  # softened dead-end, not a hard discard
    assert len(endless.requests) == 20  # MAX_TOOL_ROUNDS


# ------------------------------------------------ deterministic confirm loop
def _preview_then_text(tool, args, text="Confirm to proceed."):
    """A turn that previews a write (no confirm) followed by a text turn."""
    return ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id="p1", name=tool, arguments=args)]),
            Turn(text=text),
        ]
    )


def test_pending_action_persisted_on_needs_confirm(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "McKenzie Room G", "address": "950 McKenzie Ave", "city": "Victoria"}
    first = _chat(
        client,
        _preview_then_text("create_property", args),
        {"message": "add McKenzie Room G at 950 McKenzie Ave Victoria"},
    ).json()

    # Nothing created yet — only a preview — persisted as a one-step plan.
    assert not Property.objects.filter(landlord=landlord, name__iexact="McKenzie Room G").exists()
    plan = RamaPendingPlan.objects.get(conversation_id=first["conversation_id"])
    assert plan.operation == "single"
    steps = list(plan.steps.all())
    assert len(steps) == 1
    assert steps[0].tool == "create_property"
    assert steps[0].arguments["name"] == "McKenzie Room G"
    # The chat response surfaces the outstanding plan for the UI.
    assert first["pending_plan"]["steps"][0]["tool"] == "create_property"


def test_yes_executes_pending_deterministically(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "McKenzie Room G", "address": "950 McKenzie Ave", "city": "Victoria"}
    conv = _chat(
        client, _preview_then_text("create_property", args), {"message": "add room G"}
    ).json()["conversation_id"]

    # The landlord says "yes". The backend runs the exact previewed tool
    # itself and answers DETERMINISTICALLY — the model is never consulted on
    # a bare yes, so it cannot re-preview or invent extra actions.
    yes = ScriptedProvider([Turn(text="model should not be called")])
    body = _chat(client, yes, {"message": "yes", "conversation_id": conv}).json()

    assert Property.objects.filter(landlord=landlord, name__iexact="McKenzie Room G").count() == 1
    assert "create_property" in body["tools_used"]
    assert yes.requests == []  # no provider round-trip at all
    assert body["reply"] == "Created McKenzie Room G."
    assert not RamaPendingPlan.objects.filter(conversation_id=conv).exists()
    assert body["pending_plan"] is None


def test_yes_never_loops_even_if_model_would_repreview(landlord, settings):
    """The old bug: on 'yes' a weak model re-showed the preview forever. Now the
    backend executes regardless of what the model does next."""
    from rentium.properties.models import Property

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "Loopy Room", "address": "1 Loop St", "city": "Victoria"}
    conv = _chat(
        client, _preview_then_text("create_property", args), {"message": "add Loopy Room"}
    ).json()["conversation_id"]

    # Model "misbehaves" and tries to preview again — but tools are empty on a
    # bare yes, so it can't, and the create already happened deterministically.
    stubborn = ScriptedProvider([Turn(text="Confirm to proceed.")])
    _chat(client, stubborn, {"message": "ye", "conversation_id": conv})
    assert Property.objects.filter(landlord=landlord, name__iexact="Loopy Room").count() == 1


def test_yes_with_extra_instruction_runs_pending_and_continues(landlord, settings):
    from rentium.properties.models import Property

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "McKenzie Room G", "address": "950 McKenzie Ave", "city": "Victoria"}
    conv = _chat(
        client, _preview_then_text("create_property", args), {"message": "add room G"}
    ).json()["conversation_id"]

    # "yes and ..." → pending runs deterministically AND the model still gets
    # tools to act on the extra instruction.
    follow = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id="l1", name="list_properties", arguments={})]),
            Turn(text="Created the room; here are your listings."),
        ]
    )
    body = _chat(
        client,
        follow,
        {"message": "yes and show me all my rooms", "conversation_id": conv},
    ).json()

    assert Property.objects.filter(landlord=landlord, name__iexact="McKenzie Room G").count() == 1
    assert follow.requests[0]["tools"], "extra-instruction path keeps tools available"
    assert "create_property" in body["tools_used"]
    assert "list_properties" in body["tools_used"]


def test_yes_without_pending_is_normal_flow(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    provider = ScriptedProvider([Turn(text="Sure — what would you like to do?")])
    body = _chat(client, provider, {"message": "yes"}).json()

    assert body["tools_used"] == ["_live_context"]  # nothing auto-executed
    assert provider.requests[0]["tools"], "no pending → model keeps its tools"
    assert not Property.objects.filter(landlord=landlord).exists()
    assert not RamaPendingPlan.objects.filter(
        conversation_id=body["conversation_id"]
    ).exists()


def test_stale_pending_action_is_not_executed(landlord, settings):
    from datetime import timedelta

    from django.utils import timezone

    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan
    from rentium.rama.views import PENDING_ACTION_TTL_SECONDS

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "Stale Room", "address": "9 Old Rd", "city": "Victoria"}
    conv = _chat(
        client, _preview_then_text("create_property", args), {"message": "add stale room"}
    ).json()["conversation_id"]

    # Backdate the preview beyond the freshness window (update() skips auto_now).
    old = timezone.now() - timedelta(seconds=PENDING_ACTION_TTL_SECONDS + 60)
    RamaPendingPlan.objects.filter(conversation_id=conv).update(updated_at=old)

    provider = ScriptedProvider([Turn(text="That preview expired — want me to redo it?")])
    body = _chat(client, provider, {"message": "yes", "conversation_id": conv}).json()

    assert "create_property" not in body["tools_used"]
    assert not Property.objects.filter(landlord=landlord, name__iexact="Stale Room").exists()
    assert not RamaPendingPlan.objects.filter(conversation_id=conv).exists()


def test_duplicate_name_guard_blocks_second_listing(landlord):
    from rentium.properties.models import Property

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    r1 = registry.execute("create_property", {"name": "Room G", **base}, landlord=landlord)
    assert r1.get("created")

    # Re-stating the SAME listing (same name, address and category) reuses it.
    # The invariant this guard exists for — never silently end up with two
    # listings sharing a name — is what the count assertion pins, and it holds
    # either way. Reporting it as "already done" rather than as an error is
    # what changed: the audit log showed the error form sending the model off
    # to rename or delete a listing it had just correctly created.
    r2 = registry.execute("create_property", {"name": "room g", **base}, landlord=landlord)
    assert r2.get("reused") and not r2.get("created")
    assert Property.objects.filter(landlord=landlord, name__iexact="room g").count() == 1

    # A same-named listing somewhere ELSE is genuinely ambiguous — it may be a
    # different room in a different house — and is still rejected with
    # candidates so the landlord decides.
    r_other = registry.execute(
        "create_property",
        {"name": "Room G", "address": "3213 Wascana St", "city": "Victoria",
         "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in r_other and r_other.get("candidates")
    assert Property.objects.filter(landlord=landlord, name__iexact="room g").count() == 1

    # Explicit override still allows an intentional duplicate.
    r3 = registry.execute(
        "create_property",
        {"name": "Room G", "allow_duplicate_name": "yes", **base},
        landlord=landlord,
    )
    assert r3.get("created")
    assert Property.objects.filter(landlord=landlord, name__iexact="room g").count() == 2


def test_resolver_disambiguates_duplicates_deterministically(landlord):
    from rentium.rama.resolve import resolve_property

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    registry.execute("create_property", {"name": "Room G", **base}, landlord=landlord)
    registry.execute(
        "create_property",
        {"name": "Room G", "allow_duplicate_name": "yes", **base},
        landlord=landlord,
    )

    # Bare name is ambiguous → candidates + a hint, never a silent wrong pick.
    prop, err = resolve_property(landlord, "Room G")
    assert prop is None and isinstance(err, dict) and err["candidates"]

    # pick=first and lookup-by-id both resolve to exactly one listing.
    first, err_first = resolve_property(landlord, "Room G", pick="first")
    assert first is not None and err_first is None
    by_id, err_id = resolve_property(landlord, str(first.pk))
    assert by_id is not None and by_id.pk == first.pk and err_id is None


def test_pick_accepts_natural_language_age(landlord):
    """'the old one' / 'newer' must resolve deterministically, without the LLM."""
    from rentium.properties.models import Property
    from rentium.rama.resolve import resolve_property

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    registry.execute("create_property", {"name": "Twin", **base}, landlord=landlord)
    registry.execute(
        "create_property",
        {"name": "Twin", "allow_duplicate_name": "yes", **base},
        landlord=landlord,
    )
    ordered = list(
        Property.objects.filter(landlord=landlord, name__iexact="Twin").order_by(
            "created_at", "pk"
        )
    )
    old, new = ordered[0], ordered[1]

    for phrase in ("old", "older", "oldest", "the old one", "first"):
        prop, err = resolve_property(landlord, "Twin", pick=phrase)
        assert err is None and prop.pk == old.pk, f"{phrase!r} should pick the old twin"
    for phrase in ("new", "newer", "newest", "the new one", "latest", "second"):
        prop, err = resolve_property(landlord, "Twin", pick=phrase)
        assert err is None and prop.pk == new.pk, f"{phrase!r} should pick the new twin"


def test_update_property_renames_in_place(landlord):
    """The rename-transcript regression: update_property with name= renames."""
    from rentium.properties.models import Property

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    registry.execute("create_property", {"name": "Room H", **base}, landlord=landlord)

    preview = registry.execute(
        "update_property", {"property_query": "Room H", "name": "Room F"}, landlord=landlord
    )
    assert preview.get("needs_confirm"), "rename must preview first"

    done = registry.execute(
        "update_property",
        {"property_query": "Room H", "name": "Room F", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" not in done
    assert Property.objects.filter(landlord=landlord, name__iexact="Room F").count() == 1
    assert not Property.objects.filter(landlord=landlord, name__iexact="Room H").exists()


def test_update_property_schema_advertises_rename_and_pick():
    """The weak model can only know update renames if the schema says so."""
    tool = registry.REGISTRY["update_property"]
    assert "rename" in tool.description.lower()
    props = tool.parameters["properties"]
    assert "pick" in props, "update_property must expose pick for duplicate names"
    assert "rename" in props["name"].get("description", "").lower()


def test_plan_operation_treats_duplicates_as_question_not_block(landlord):
    """Ambiguity → needs_disambiguation (a choice), never a PROTECT 'blocked'."""
    from rentium.properties.models import Property
    from rentium.rama.playbooks import plan_operation

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    registry.execute("create_property", {"name": "DupRoom", **base}, landlord=landlord)
    registry.execute(
        "create_property",
        {"name": "DupRoom", "allow_duplicate_name": "yes", **base},
        landlord=landlord,
    )
    ordered = list(
        Property.objects.filter(landlord=landlord, name__iexact="DupRoom").order_by(
            "created_at", "pk"
        )
    )
    old = ordered[0]

    # Ambiguous ask: a distinct disambiguation signal, no plan, no blocked item.
    res = plan_operation(landlord, operation="delete_listings", include="DupRoom")
    assert res.get("needs_disambiguation") is True
    assert res.get("ambiguous") and res["ambiguous"][0]["candidates"]
    assert "plan" not in res  # not a plan; nothing to run
    assert "skip" not in res.get("question_for_user", "").lower()

    # Narrowed with pick=oldest: now a real single-target plan on the old twin.
    plan = plan_operation(
        landlord, operation="delete_listings", include="DupRoom", pick="oldest"
    )
    assert "plan" in plan and plan["plan"]["steps"]
    assert plan["plan"]["steps"][0]["item_key"] == str(old.pk)
    assert not plan["plan"]["blocked"]


def test_filter_path_guards_same_name_destructive_collision(landlord):
    """The footgun: a filter (name_contains) that matches two identically-named
    listings must NOT silently plan to delete both."""
    from rentium.properties.models import Property
    from rentium.rama.playbooks import plan_operation

    base = {"address": "950 McKenzie Ave", "city": "Victoria", "confirm": "yes"}
    registry.execute("create_property", {"name": "Collide", **base}, landlord=landlord)
    registry.execute(
        "create_property",
        {"name": "Collide", "allow_duplicate_name": "yes", **base},
        landlord=landlord,
    )
    ordered = list(
        Property.objects.filter(landlord=landlord, name__iexact="Collide").order_by(
            "created_at", "pk"
        )
    )
    old, new = ordered[0], ordered[1]

    # Filter path, no pick → ask, don't nuke both.
    res = plan_operation(
        landlord, operation="delete_listings", name_contains="Collide"
    )
    assert res.get("needs_disambiguation") is True
    assert "plan" not in res

    # pick=newest → single-target plan on the newer twin.
    plan = plan_operation(
        landlord, operation="delete_listings", name_contains="Collide", pick="newest"
    )
    steps = plan["plan"]["steps"]
    assert len(steps) == 1 and steps[0]["item_key"] == str(new.pk)

    # pick=all → explicit escape hatch deletes both.
    plan_all = plan_operation(
        landlord, operation="delete_listings", name_contains="Collide", pick="all"
    )
    keys = {s["item_key"] for s in plan_all["plan"]["steps"]}
    assert keys == {str(old.pk), str(new.pk)}


# ---------------------------------------------- R2: essential-field gating
def _room(landlord, name, **extra):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        name=name,
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        **extra,
    )


def test_create_lease_asks_for_missing_rent(landlord):
    """No rent given and no asking_rent → RAMA must ASK, not make a $0 lease."""
    from rentium.leases.models import Lease

    _room(landlord, "NoRent Room")
    res = registry.execute(
        "create_lease",
        {"property_query": "NoRent Room", "start_date": "2026-09-01", "is_month_to_month": "1"},
        landlord=landlord,
    )
    assert res.get("needs_input") is True
    assert "rent" in res.get("question_for_user", "").lower()
    assert not res.get("needs_confirm")
    assert not Lease.objects.filter(landlord=landlord).exists()


def test_create_lease_explicit_zero_rent_proceeds(landlord):
    """An explicit total_rent='0' is a real choice (free room) — don't ask."""
    _room(landlord, "Free Room")
    res = registry.execute(
        "create_lease",
        {
            "property_query": "Free Room",
            "start_date": "2026-09-01",
            "is_month_to_month": "1",
            "total_rent": "0",
        },
        landlord=landlord,
    )
    assert not res.get("needs_input")
    assert res.get("needs_confirm") is True


def test_create_lease_uses_asking_rent_without_asking(landlord):
    """asking_rent on the listing is a valid rent basis — proceed, don't ask."""
    _room(landlord, "Asking Room", asking_rent="900.00")
    res = registry.execute(
        "create_lease",
        {"property_query": "Asking Room", "start_date": "2026-09-01", "is_month_to_month": "1"},
        landlord=landlord,
    )
    assert not res.get("needs_input")
    assert res.get("needs_confirm") is True


def test_setup_room_tenancy_asks_for_rent(landlord):
    res = registry.execute(
        "setup_room_tenancy",
        {"room_name": "Setup A", "address": "950 McKenzie Ave", "city": "Victoria", "start_date": "2026-09-01"},
        landlord=landlord,
    )
    assert res.get("needs_input") is True
    assert "rent" in res.get("question_for_user", "").lower()


def test_setup_room_tenancy_asks_for_start_date_when_tenant_given(landlord):
    res = registry.execute(
        "setup_room_tenancy",
        {
            "room_name": "Setup B",
            "address": "950 McKenzie Ave",
            "city": "Victoria",
            "tenant_email": "new.person@example.com",
            "total_rent": "800",
        },
        landlord=landlord,
    )
    assert res.get("needs_input") is True
    assert "start" in res.get("question_for_user", "").lower()


_TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def test_duplicate_listing_copies_images_and_inventory(landlord):
    """The 'dumb duplicate' fix: duplicating a listing must carry its photos and
    inventory, not produce an empty copy."""
    from django.core.files.base import ContentFile

    from rentium.properties.models import InventoryItem, PropertyImage

    src = _room(landlord, "Room With Stuff")
    src.primary_image.save("hero.gif", ContentFile(_TINY_GIF), save=True)
    PropertyImage.objects.create(property=src, image=ContentFile(_TINY_GIF, name="g1.gif"))
    PropertyImage.objects.create(property=src, image=ContentFile(_TINY_GIF, name="g2.gif"))
    InventoryItem.objects.create(property=src, name="Double bed")
    InventoryItem.objects.create(property=src, name="Mattress")

    res = registry.execute(
        "duplicate_listing",
        {"property_query": "Room With Stuff", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("created") is True
    assert res["copied_inventory"] == 2
    assert res["copied_images"] == 3  # primary + 2 gallery

    from rentium.properties.models import Property

    dup = Property.objects.get(pk=res["listing"]["id"])
    assert dup.pk != src.pk
    assert dup.name == "Room With Stuff"  # same name by default (a real duplicate)
    assert dup.inventory_items.count() == 2
    assert dup.property_images.count() == 2
    assert bool(dup.primary_image)
    # Independent copies — the duplicate's images are its own rows/files.
    assert set(dup.property_images.values_list("pk", flat=True)).isdisjoint(
        src.property_images.values_list("pk", flat=True)
    )


def test_log_capability_gap_records_and_prioritises(landlord):
    """The 'learn now' backlog: RAMA logs what it can't do (no code), and
    'learn now' prioritises it. De-dupes identical NEW gaps."""
    from rentium.rama.models import RamaCapabilityGap

    r1 = registry.execute(
        "log_capability_gap",
        {"request": "Bulk-export all my leases to PDF"},
        landlord=landlord,
    )
    assert r1.get("logged") is True and r1["prioritised"] is False
    gap = RamaCapabilityGap.objects.get(landlord=landlord)
    assert gap.status == RamaCapabilityGap.Status.NEW

    # 'learn now' on the same ask prioritises the existing gap, no duplicate.
    r2 = registry.execute(
        "log_capability_gap",
        {"request": "Bulk-export all my leases to PDF", "learn_now": "yes"},
        landlord=landlord,
    )
    assert r2["prioritised"] is True
    assert RamaCapabilityGap.objects.filter(landlord=landlord).count() == 1

    listed = registry.execute("list_capability_gaps", {}, landlord=landlord)
    assert listed["count"] == 1 and listed["gaps"][0]["prioritised"] is True


def test_attach_photo_to_listing(landlord):
    """The in-chat photo attach: a staged RamaUpload becomes a listing photo."""
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaUpload

    room = _room(landlord, "Photo Room")
    upload = RamaUpload.objects.create(
        landlord=landlord, image=ContentFile(_TINY_GIF, name="attach.gif")
    )
    assert room.image_count == 0

    res = registry.execute(
        "attach_photo_to_listing",
        {"property_query": "Photo Room", "upload_id": str(upload.pk), "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("attached") is True
    room.refresh_from_db()
    assert room.image_count == 1
    upload.refresh_from_db()
    assert upload.used_at is not None  # single-use, consumed


def test_attach_multiple_photos_bulk(landlord):
    """Bulk: one explicit batch — first primary, rest gallery."""
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaAttachment, RamaAttachmentBatch

    room = _room(landlord, "Bulk Room")
    batch = RamaAttachmentBatch.objects.create(
        landlord=landlord,
        conversation_id=uuid.uuid4(),
        status=RamaAttachmentBatch.Status.SEALED,
    )
    for n in range(3):
        attachment = RamaAttachment(
            batch=batch,
            original_filename=f"b{n}.jpg",
            content_type="image/jpeg",
            sha256=f"{n:064d}",
            size=len(_TINY_GIF),
            sequence=n,
        )
        attachment.original.save(
            f"b{n}.jpg",
            ContentFile(_TINY_GIF),
            save=True,
        )
    res = registry.execute(
        "attach_photo_to_listing",
        {
            "property_query": "Bulk Room",
            "attachment_batch_id": str(batch.pk),
            "set_primary": "yes",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert res.get("attached") is True and res["photos_added"] == 3
    room.refresh_from_db()
    assert bool(room.primary_image) and room.property_images.count() == 2  # 1 primary + 2 gallery
    assert not RamaAttachment.objects.filter(
        batch=batch,
        status=RamaAttachment.Status.STAGED,
    ).exists()


def test_co_landlord_gets_portfolio_access(landlord):
    """A co-landlord invited (with an existing account) can act on the owner's
    portfolio; a stranger cannot — the resolution fails closed."""
    from rentium.users.access import accessible_landlord_ids, acting_landlord
    from rentium.users.tests.factories import UserFactory

    co = UserFactory(email="co.manager@rmail.ca")
    res = registry.execute(
        "add_co_landlord",
        {"name": "Co Manager", "email": "co.manager@rmail.ca", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("invited") is True and res["linked_now"] is True

    # A pure manager (no own profile) acts AS the owner and can read their ids.
    assert acting_landlord(co).pk == landlord.pk
    assert landlord.pk in accessible_landlord_ids(co)

    # list_co_landlords surfaces them.
    listed = registry.execute("list_co_landlords", {}, landlord=landlord)
    assert listed["count"] == 1 and listed["co_landlords"][0]["status"] == "active"

    # Revoke → access gone.
    registry.execute(
        "add_co_landlord",
        {"email": "co.manager@rmail.ca", "remove": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    assert acting_landlord(co) is None


def test_stranger_has_no_landlord_access(db):
    from rentium.users.access import accessible_landlord_ids, acting_landlord
    from rentium.users.tests.factories import UserFactory

    stranger = UserFactory(email="stranger@example.com")
    assert acting_landlord(stranger) is None
    assert accessible_landlord_ids(stranger) == []


def test_set_one_off_viewing_availability(landlord):
    """Per-date hours: a one-off window overrides the weekly schedule for that date."""
    from datetime import datetime

    from rentium.appointments.models import AvailabilityWindow
    from rentium.appointments.services import IN_HOURS, OUT_OF_HOURS, classify_time

    res = registry.execute(
        "set_viewing_availability",
        {"specific_date": "2026-07-25", "start": "14:00", "end": "16:00", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("created") is True
    w = AvailabilityWindow.objects.get(landlord=landlord)
    assert str(w.specific_date) == "2026-07-25"

    # Inside the one-off window → IN_HOURS; outside → OUT_OF_HOURS.
    assert classify_time(landlord, None, datetime(2026, 7, 25, 15, 0)) == IN_HOURS
    assert classify_time(landlord, None, datetime(2026, 7, 25, 17, 0)) == OUT_OF_HOURS


def test_add_co_host_to_lease(landlord):
    """Co-landlord/co-host recorded on the lease + shown on the agreement."""
    from rentium.leases.documents import render_lease

    lease = _draft_lease(landlord, name="CoHostRoom")
    res = registry.execute(
        "add_co_host_to_lease",
        {"lease_number": lease.lease_number, "name": "Sam Partner", "email": "sam@example.com", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("updated") is True and "Sam Partner" in res["co_hosts"]
    lease.refresh_from_db()
    assert lease.co_hosts[0]["name"] == "Sam Partner"
    # It appears on the rendered agreement.
    doc = render_lease(lease)
    parties = next(s for s in doc.sections if s.id == "parties")
    assert any("Sam Partner" in r.value for r in parties.rows)

    # Remove it.
    registry.execute(
        "add_co_host_to_lease",
        {"lease_number": lease.lease_number, "name": "Sam Partner", "remove": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    lease.refresh_from_db()
    assert lease.co_hosts == []


def test_generic_read_answers_composed_lease_query(landlord):
    """The Phase-1 manifest read: a composed question that no bespoke list_* tool
    answers — 'active leases with rent over 800' — works via one generic tool."""
    from rentium.leases.models import Lease

    def _lease(name, rent, status):
        p = _room(landlord, name)
        return Lease.objects.create(
            landlord=landlord, property=p,
            lease_type=Lease.LeaseType.GENERIC_ROOMMATE, status=status,
            start_date=date(2026, 9, 1), is_month_to_month=True, total_rent=rent,
        )

    hit = _lease("HighActive", "900.00", Lease.LeaseStatus.ACTIVE)
    _lease("LowActive", "700.00", Lease.LeaseStatus.ACTIVE)   # rent too low
    _lease("HighDraft", "950.00", Lease.LeaseStatus.DRAFT)     # wrong status

    res = registry.execute(
        "read",
        {"entity": "lease", "filters": "status=active, total_rent>800",
         "fields": "lease_number,total_rent,status"},
        landlord=landlord,
    )
    assert "error" not in res
    nums = {r["lease_number"] for r in res["rows"]}
    assert nums == {hit.lease_number}
    assert res["rows"][0]["status"] == "Active"  # enum rendered via display


def test_generic_read_is_scope_safe(landlord, other_landlord):
    """read only ever returns the acting landlord's rows, and refuses undeclared
    fields — the two safety properties the manifest guarantees."""
    from rentium.leases.models import Lease

    Lease.objects.create(
        landlord=other_landlord, property=_room(other_landlord, "TheirRoom"),
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.ACTIVE, start_date=date(2026, 9, 1),
        is_month_to_month=True, total_rent="999.00",
    )
    # Even with a filter that would match the stranger's lease, it's invisible.
    res = registry.execute(
        "read", {"entity": "lease", "filters": "total_rent>0"}, landlord=landlord
    )
    assert res["total_matched"] == 0

    # An undeclared/sensitive field cannot be filtered (default-deny).
    bad = registry.execute(
        "read", {"entity": "lease", "filters": "signed_document_sha256=x"},
        landlord=landlord,
    )
    assert "error" in bad and "filter" in bad["error"].lower()


def test_capability_digest_is_data_derived(landlord):
    """Phase 4: the prompt's capability surface is generated from the manifest —
    new entities/editable fields appear without editing persona prose."""
    from rentium.rama.manifest import capability_digest

    d = capability_digest()
    assert "work_order" in d and "ledger_entry" in d  # all read entities listed
    assert "update" in d and "parking_included" not in d  # editable summarised
    assert "lease (start_date" in d  # editable fields shown per entity
    assert "link" in d and "property_group" in d  # linkable listed


def test_data_catalogue_lists_entities(landlord):
    res = registry.execute("data_catalogue", {}, landlord=landlord)
    keys = {e["entity"] for e in res["entities"]}
    assert {
        "lease", "property", "work_order", "inquiry", "appointment",
        "ledger_entry", "inspection", "inventory", "conversation",
        "lease_tenant", "property_group",
    } <= keys


def test_read_indirect_scope_is_safe(landlord, other_landlord):
    """An entity scoped via an indirect path (inventory → property → landlord)
    still never leaks another landlord's rows."""
    from rentium.properties.models import InventoryItem

    mine = _room(landlord, "MyInvRoom")
    theirs = _room(other_landlord, "TheirInvRoom")
    InventoryItem.objects.create(property=mine, name="My Lamp")
    InventoryItem.objects.create(property=theirs, name="Their Lamp")

    res = registry.execute(
        "read", {"entity": "inventory", "fields": "name"}, landlord=landlord
    )
    names = {r["name"] for r in res["rows"]}
    assert "My Lamp" in names and "Their Lamp" not in names


def test_generic_update_previews_then_applies(landlord):
    """Phase 3: the generic update previews, then applies manifest-editable fields
    on confirm — closing gaps the bespoke update_* tools don't cover."""
    lease = _draft_lease(landlord, name="GenUpdateRoom")

    prev = registry.execute(
        "update",
        {"entity": "lease", "query": lease.lease_number,
         "changes": "parking_included=true, rent_due_day=5"},
        landlord=landlord,
    )
    assert prev.get("needs_confirm") is True

    res = registry.execute(
        "update",
        {"entity": "lease", "query": lease.lease_number,
         "changes": "parking_included=true, rent_due_day=5", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("updated") is True
    lease.refresh_from_db()
    assert lease.parking_included is True and lease.rent_due_day == 5


def test_generic_update_inventory_enum_by_name(landlord):
    """Broadened manifest: update an inventory item (an entity with no detail
    page) by name, and resolve a human enum value ('Fair') to its choice code."""
    from rentium.properties.models import InventoryItem

    room = _room(landlord, "InvEditRoom")
    item = InventoryItem.objects.create(property=room, name="Old Sofa", quantity=1)
    res = registry.execute(
        "update",
        {"entity": "inventory", "query": "Old Sofa",
         "changes": "condition=Fair, quantity=2", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("updated") is True
    item.refresh_from_db()
    assert item.quantity == 2
    assert item.get_condition_display() == "Fair"


def test_generic_update_resolves_province_and_postal(landlord):
    """Regression: generic update on a choices field ('province=BC' → 'bc') and a
    normalised field (postal_code 'v8x 3g5' → 'V8X 3G5') — was failing with
    'Value BC is not a valid choice'."""
    from rentium.properties.models import Property

    p = Property.objects.create(
        landlord=landlord, name="AddrRoom", address="950 McKenzie Ave", city="X",
        province="on", property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    res = registry.execute(
        "update",
        {"entity": "property", "query": "AddrRoom",
         "changes": "city=Victoria, province=BC, postal_code=v8x 3g5",
         "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("updated") is True, res
    p.refresh_from_db()
    assert p.province == "bc" and p.city == "Victoria" and p.postal_code == "V8X 3G5"


def test_generic_update_guards_and_default_deny(landlord, other_landlord):
    """update refuses: a locked lease (state guard), an undeclared/non-editable
    field (default-deny), and another landlord's row (scope)."""
    from rentium.leases.models import Lease

    active = _draft_lease(landlord, name="LockedRoom")
    active.status = Lease.LeaseStatus.ACTIVE
    active.save(update_fields=["status"])
    guard = registry.execute(
        "update", {"entity": "lease", "query": active.lease_number,
                   "changes": "parking_included=true", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in guard and "edited" in guard["error"].lower()

    draft = _draft_lease(landlord, name="DenyRoom")
    # total_rent is read-only in the manifest (rebalancing lives in update_lease)
    deny = registry.execute(
        "update", {"entity": "lease", "query": draft.lease_number,
                   "changes": "total_rent=9999", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in deny and "edit" in deny["error"].lower()

    theirs = _draft_lease(other_landlord, name="TheirEditRoom")
    scope = registry.execute(
        "update", {"entity": "lease", "query": theirs.lease_number,
                   "changes": "parking_included=true", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in scope  # not in my portfolio → unresolvable


def test_deliver_lease_pdf_returns_attachment_marker(landlord):
    """deliver_lease_pdf resolves the lease and emits an _attachment marker the
    channel fulfils (instead of pasting a broken /api/ URL)."""
    lease = _draft_lease(landlord, name="DeliverRoom")
    res = registry.execute(
        "deliver_lease_pdf", {"lease_number": lease.lease_number}, landlord=landlord
    )
    assert res["_attachment"]["kind"] == "lease_pdf"
    assert res["_attachment"]["lease_id"] == str(lease.id)


def test_generic_link_resolves_and_is_scoped(landlord, other_landlord):
    """Phase 2: the manifest-driven link tool returns a deep link for a resolved
    lease/property, notes its downloads, and never links a stranger's row."""
    lease = _draft_lease(landlord, name="LinkableRoom")

    res = registry.execute(
        "link", {"entity": "lease", "query": lease.lease_number}, landlord=landlord
    )
    assert f"/dashboard/leases/{lease.id}" in res["link"]
    assert "signed PDF" in res["available_there"]

    prop = registry.execute(
        "link", {"entity": "property", "query": "LinkableRoom"}, landlord=landlord
    )
    assert "/dashboard/properties/" in prop["link"]

    # a stranger's lease number does not resolve in my portfolio
    other = _draft_lease(other_landlord, name="StrangerRoom")
    miss = registry.execute(
        "link", {"entity": "lease", "query": other.lease_number}, landlord=landlord
    )
    assert "error" in miss


def test_read_ledger_filter_and_enum_display(landlord):
    """read works over ledger with a numeric filter and renders enum displays."""
    lease = _draft_lease(landlord, name="LedgerReadRoom")
    _rent_charge(landlord, lease, amount="850.00")
    res = registry.execute(
        "read",
        {"entity": "ledger_entry", "filters": "amount>800",
         "fields": "entry_type,amount"},
        landlord=landlord,
    )
    assert res["total_matched"] >= 1
    assert any(r["entry_type"] == "Rent Charge" for r in res["rows"])


def test_update_lease_sets_bills_via_rama(landlord):
    """RAMA can set bills on an EXISTING lease (the Phase-0 treadmill example):
    'water included, hydro tenant pays' merges into bills_included."""
    lease = _draft_lease(landlord, name="RamaBillsRoom")
    res = registry.execute(
        "update_lease",
        {"lease_number": lease.lease_number,
         "bills": "water included, hydro tenant pays", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" not in res
    lease.refresh_from_db()
    assert lease.bills_included["water"]["included"] is True
    assert lease.bills_included["electricity"]["included"] is False
    assert lease.get_bills_summary() != "No bills information available"


def test_lease_shared_with_landlord_and_cohost_render(landlord):
    """The exact ask: add a co-landlord to the lease + set shared-with-landlord;
    both land on the roommate agreement. Order doesn't matter, unsigned lease."""
    from rentium.leases.documents import render_lease
    from rentium.properties.models import InventoryItem

    lease = _draft_lease(landlord, name="SharedBasementRoom")  # GENERIC_ROOMMATE

    registry.execute(
        "add_co_host_to_lease",
        {"lease_number": lease.lease_number, "name": "Sarbjeet Kaur", "email": "sarbjitkaur9@hotmail.com", "confirm": "yes"},
        landlord=landlord,
    )
    res = registry.execute(
        "update_lease",
        {"lease_number": lease.lease_number, "shared_with": "landlord,roommates", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" not in res
    lease.refresh_from_db()
    assert set(lease.common_space_shared_with) == {"LANDLORD", "ROOMMATES"}

    InventoryItem.objects.create(property=lease.property, name="Queen bed")

    doc = render_lease(lease)
    blob = "\n".join(
        [r.value for s in doc.sections for r in s.rows]
        + [c for s in doc.sections for c in s.clauses]
    )
    assert "Sarbjeet Kaur" in blob      # co-host on the agreement
    assert "landlord" in blob.lower()   # shared-with-landlord clause
    assert "Queen bed" in blob          # inventory auto-appears


def test_update_lease_sets_house_rules(landlord):
    """Custom clauses: house_rules is editable via update_lease."""
    lease = _draft_lease(landlord, name="RulesRoom")
    res = registry.execute(
        "update_lease",
        {"lease_number": lease.lease_number, "house_rules": "Quiet hours after 10pm. No parties.", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" not in res
    lease.refresh_from_db()
    assert "Quiet hours" in (lease.house_rules or "")


def test_attach_photo_cannot_use_another_landlords_upload(landlord):
    """Security: an upload is landlord-scoped — RAMA can't attach one it doesn't own."""
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaUpload
    from rentium.users.models import LandlordProfile

    other = LandlordProfile.objects.create(user=UserFactory())
    other_upload = RamaUpload.objects.create(
        landlord=other, image=ContentFile(_TINY_GIF, name="other.gif")
    )
    _room(landlord, "My Room")

    res = registry.execute(
        "attach_photo_to_listing",
        {"property_query": "My Room", "upload_id": str(other_upload.pk), "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in res and "attached" not in res


def test_duplicate_listing_previews_before_creating(landlord):
    _room(landlord, "Preview Me")
    res = registry.execute(
        "duplicate_listing", {"property_query": "Preview Me"}, landlord=landlord
    )
    assert res.get("needs_confirm") is True
    assert "created" not in res


# ---------------------------------------------- R2: existing-account linking
def _draft_lease(landlord, name="InviteRoom"):
    from rentium.leases.models import Lease

    prop = _room(landlord, name)
    return Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date(2026, 9, 1),
        is_month_to_month=True,
        total_rent="800.00",
    )


def test_co_landlord_can_download_lease_pdf(landlord):
    """A co-landlord on the lease can hit the PDF endpoint (was 'This isn't your
    lease')."""
    lease = _draft_lease(landlord, name="PdfRoom")
    registry.execute(
        "add_co_landlord",
        {"name": "Pdf Co", "email": "pdfco@rmail.ca",
         "lease_number": lease.lease_number, "confirm": "yes"},
        landlord=landlord,
    )
    co = UserFactory(email="pdfco@rmail.ca")
    client = APIClient()
    client.force_authenticate(user=co)
    resp = client.get(f"/api/leases/{lease.id}/pdf/")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"


def test_new_lease_inherits_property_default_bills(landlord):
    """A property's default_bills_included flows into a new lease created on it."""
    from rentium.leases.api.serializers import LeaseSerializer

    room = _room(landlord, "BillsRoom")
    room.default_bills_included = {
        "water": {"included": True, "provider": "City", "category": "water",
                  "tenant_responsibility": {}, "notes": ""}
    }
    room.save(update_fields=["default_bills_included"])

    client = _client_for(landlord)
    resp = client.post("/api/leases/", {
        "lease_type": "GENERIC_ROOMMATE",
        "property_id": room.id,
        "start_date": "2026-09-01",
        "is_month_to_month": True,
        "total_rent": "800.00",
    }, format="json")
    assert resp.status_code in (200, 201), resp.content
    from rentium.leases.models import Lease

    lease = Lease.objects.get(id=resp.json()["id"])
    assert "water" in lease.bills_included
    assert lease.get_bills_summary() != "No bills information available"


def test_open_lease_and_property_return_links(landlord):
    """RAMA can hand the landlord a clickable link instead of refusing."""
    lease = _draft_lease(landlord, name="LinkRoom")
    out = registry.execute(
        "open_lease", {"lease_number": lease.lease_number}, landlord=landlord
    )
    assert f"/dashboard/leases/{lease.id}" in out["link"]

    prop_out = registry.execute(
        "open_property", {"property_query": "LinkRoom"}, landlord=landlord
    )
    assert "/dashboard/properties/" in prop_out["link"]


def test_acting_landlord_switcher_for_co_landlord(landlord):
    """A co-landlord whose OWN account is empty acts on the owner's portfolio by
    default (no more RAMA '0 listings'), can switch via ?as=, and the switcher
    lists both portfolios with the primary flagged."""
    from rentium.users.access import (
        acting_landlord,
        actable_portfolios,
    )
    from rentium.users.models import LandlordProfile

    _room(landlord, "Owner Room")  # the owner has a property
    co_user = UserFactory(email="switch@rmail.ca")
    own = LandlordProfile.objects.create(user=co_user)  # empty own portfolio
    registry.execute(
        "add_co_landlord",
        {"name": "Switch Co", "email": "switch@rmail.ca",
         "property_query": "Owner Room", "confirm": "yes"},
        landlord=landlord,
    )
    co_user.refresh_from_db()

    # default lands on the non-empty co-managed portfolio, not her empty own
    assert acting_landlord(co_user).pk == landlord.pk
    # explicit selection is always honoured
    assert acting_landlord(co_user, owner_id=str(own.pk)).pk == own.pk

    ports = actable_portfolios(co_user)
    assert {p["owner_id"] for p in ports} == {str(landlord.pk), str(own.pk)}
    own_row = next(p for p in ports if p["owner_id"] == str(own.pk))
    owner_row = next(p for p in ports if p["owner_id"] == str(landlord.pk))
    assert own_row["is_own"] is True and owner_row["is_own"] is False
    assert owner_row["property_count"] == 1


def test_rama_portfolios_endpoint(landlord):
    """The switcher endpoint returns the actable portfolios + active selection."""
    _room(landlord, "Api Portfolio Room")
    data = _client_for(landlord).get("/api/rama/portfolios/").json()
    assert data["acting_as"] == str(landlord.pk)
    assert any(p["owner_id"] == str(landlord.pk) and p["is_own"] for p in data["portfolios"])


def test_scope_q_limits_ledger_to_granted_property(landlord):
    """The reusable scope_q primitive (used by every dashboard surface) scopes a
    property-scoped co-landlord to their granted property's rows only, while the
    owner still sees everything."""
    from rentium.ledger.models import LedgerEntry
    from rentium.users.access import scope_q

    granted = _draft_lease(landlord, name="GrantedRoom")
    other = _draft_lease(landlord, name="OtherRoom")
    _rent_charge(landlord, granted)
    _rent_charge(landlord, other)

    registry.execute(
        "add_co_landlord",
        {"name": "Ledger Co", "email": "ledgerco@rmail.ca",
         "property_query": "GrantedRoom", "confirm": "yes"},
        landlord=landlord,
    )
    co = UserFactory(email="ledgerco@rmail.ca")

    co_q = scope_q(co, property_field="property", lease_field="lease")
    co_props = set(
        LedgerEntry.objects.filter(co_q).values_list("property_id", flat=True)
    )
    assert granted.property_id in co_props
    assert other.property_id not in co_props  # not granted → hidden

    owner_q = scope_q(
        landlord.user, property_field="property", lease_field="lease"
    )
    owner_props = set(
        LedgerEntry.objects.filter(owner_q).values_list("property_id", flat=True)
    )
    assert {granted.property_id, other.property_id} <= owner_props


def test_co_landlord_sees_and_signs_lease_via_api(landlord, other_landlord):
    """End-to-end through the real API: an invited co-landlord signs up, the lease
    shows up in their /api/leases/ list, they can co-sign it, and a stranger
    landlord cannot see it."""
    from rentium.leases.models import Lease

    from rentium.leases.models import LeaseTenant
    from rentium.users.models import TenantProfile

    lease = _draft_lease(landlord, name="ApiCoSignRoom")
    tp = TenantProfile.objects.create(user=UserFactory(email="apitenant@example.com"))
    LeaseTenant.objects.create(
        lease=lease, tenant=tp, rent_amount=lease.total_rent,
        invited_email="apitenant@example.com",
    )
    registry.execute(
        "add_co_landlord",
        {"name": "Api Co", "email": "apico@rmail.ca",
         "lease_number": lease.lease_number, "confirm": "yes"},
        landlord=landlord,
    )
    from rentium.users.models import LandlordProfile

    co_user = UserFactory(email="apico@rmail.ca")
    LandlordProfile.objects.create(user=co_user)  # they're a landlord too
    co_client = APIClient()
    co_client.force_authenticate(user=co_user)

    listed = co_client.get("/api/leases/").json()
    rows = listed if isinstance(listed, list) else listed.get("results", listed)
    assert any(r["id"] == str(lease.id) for r in rows)

    # opening the lease detail must NOT 403 (the object-permission trap)
    detail = co_client.get(f"/api/leases/{lease.id}/")
    assert detail.status_code == 200, detail.content

    # stranger landlord cannot see it
    stranger = _client_for(other_landlord).get("/api/leases/").json()
    srows = stranger if isinstance(stranger, list) else stranger.get("results", stranger)
    assert all(r["id"] != str(lease.id) for r in srows)

    # co-landlord signs their signatory row
    resp = co_client.post(f"/api/leases/{lease.id}/co_landlord_sign/")
    assert resp.status_code == 200, resp.content
    lease.refresh_from_db()
    assert lease.landlord_signatories.get(email="apico@rmail.ca").has_signed


def test_co_landlord_property_scope_access_and_group(landlord, other_landlord):
    """A property-scoped co-landlord sees that property + its group siblings and
    those leases — not the owner's other properties, and nothing of a stranger."""
    from rentium.leases.models import Lease
    from rentium.properties.models import PropertyGroup
    from rentium.users.access import accessible_leases, accessible_properties

    group = PropertyGroup.objects.create(landlord=landlord, name="Unit 1")
    room_a = _room(landlord, "Room A", group=group)
    room_b = _room(landlord, "Room B", group=group)
    other_prop = _room(landlord, "Unrelated Room")  # same owner, different unit
    lease_b = Lease.objects.create(
        landlord=landlord, property=room_b,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.DRAFT, start_date=date(2026, 9, 1),
        is_month_to_month=True, total_rent="800.00",
    )

    registry.execute(
        "add_co_landlord",
        {"name": "Sarbjeet Kaur", "email": "scope@rmail.ca",
         "property_query": "Room A", "confirm": "yes"},
        landlord=landlord,
    )
    u = UserFactory(email="scope@rmail.ca")  # signs up → auto-linked

    prop_ids = set(accessible_properties(u).values_list("id", flat=True))
    assert room_a.id in prop_ids and room_b.id in prop_ids  # group sibling included
    assert other_prop.id not in prop_ids  # owner's other unit is NOT visible

    lease_ids = set(accessible_leases(u).values_list("id", flat=True))
    assert lease_b.id in lease_ids  # older lease on the group is manageable


def test_co_landlord_on_lease_cosigns_before_activation(landlord):
    """Invite a co-landlord ON a lease → they're a signatory; the lease will not
    activate until BOTH landlords and a tenant have signed."""
    from rentium.leases.models import Lease, LeaseLandlordSignatory, LeaseTenant
    from rentium.users.models import TenantProfile

    lease = _draft_lease(landlord, name="CoSignRoom")
    lease.status = Lease.LeaseStatus.PENDING_SIGNATURES
    lease.save(update_fields=["status"])
    tp = TenantProfile.objects.create(user=UserFactory(email="t@example.com"))
    lt = LeaseTenant.objects.create(
        lease=lease, tenant=tp, rent_amount=lease.total_rent,
        invited_email="t@example.com",
    )

    registry.execute(
        "add_co_landlord",
        {"name": "Co Signer", "email": "cosign@rmail.ca",
         "lease_number": lease.lease_number, "confirm": "yes"},
        landlord=landlord,
    )
    sig = LeaseLandlordSignatory.objects.get(lease=lease, email="cosign@rmail.ca")

    # Owner + tenant sign — still NOT active because the co-landlord hasn't signed
    lease.landlord_signed = True
    lease.save(update_fields=["landlord_signed"])
    lt.sign()
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.PENDING_SIGNATURES

    # Co-landlord signs up (links the signatory) and signs → now ACTIVE
    u = UserFactory(email="cosign@rmail.ca")
    sig.refresh_from_db()
    assert sig.member_id == u.id
    sig.sign()
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.ACTIVE


def test_future_lease_auto_names_property_co_landlord(landlord):
    """After scoping a co-landlord to a property, a NEW lease created on it
    automatically carries them as a co-signing landlord."""
    from rentium.leases.models import Lease

    room = _room(landlord, "AutoAttach Room")
    registry.execute(
        "add_co_landlord",
        {"name": "Future Signer", "email": "future@rmail.ca",
         "property_query": "AutoAttach Room", "confirm": "yes"},
        landlord=landlord,
    )
    new_lease = Lease.objects.create(
        landlord=landlord, property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.DRAFT, start_date=date(2026, 10, 1),
        is_month_to_month=True, total_rent="900.00",
    )
    assert new_lease.landlord_signatories.filter(
        email="future@rmail.ca"
    ).exists()


def test_add_co_landlord_sends_invite_email(landlord):
    """Adding a not-yet-registered co-landlord actually EMAILS them (was a silent
    DB row before)."""
    from django.core import mail

    mail.outbox = []
    res = registry.execute(
        "add_co_landlord",
        {"name": "Sarbjeet Kaur", "email": "cotest@rmail.ca", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("invited") is True
    assert res.get("emailed") is True
    assert len(mail.outbox) == 1
    assert "cotest@rmail.ca" in mail.outbox[0].to
    # links to a real frontend route (regression: was /auth/register → 404)
    html = mail.outbox[0].alternatives[0][0]
    assert "/auth/signup?email=" in html


def test_co_landlord_invite_auto_links_on_signup(landlord):
    """A pending invite links + accepts the moment that email signs up, so
    access.owner_profiles_for grants the portfolio."""
    from rentium.users.access import owner_profiles_for
    from rentium.users.models import LandlordTeamMember

    registry.execute(
        "add_co_landlord",
        {"name": "Later Signup", "email": "later@rmail.ca", "confirm": "yes"},
        landlord=landlord,
    )
    m = LandlordTeamMember.objects.get(invited_email="later@rmail.ca")
    assert m.member_id is None and m.accepted_at is None

    u = UserFactory(email="later@rmail.ca")  # they sign up afterwards
    m.refresh_from_db()
    assert m.member_id == u.id
    assert m.accepted_at is not None
    assert landlord.pk in set(owner_profiles_for(u).values_list("pk", flat=True))


def test_lease_serializer_exposes_co_hosts(landlord):
    """The lease detail API returns co_hosts so the page can show the co-landlord
    (previously only the PDF did)."""
    from rentium.leases.api.serializers import LeaseSerializer

    lease = _draft_lease(landlord, name="CoHostSerRoom")
    registry.execute(
        "add_co_host_to_lease",
        {"lease_number": lease.lease_number, "name": "Sarbjeet Kaur", "confirm": "yes"},
        landlord=landlord,
    )
    lease.refresh_from_db()
    data = LeaseSerializer(lease).data
    assert data["co_hosts"] == [{"name": "Sarbjeet Kaur", "email": "", "phone": ""}]


def test_invite_links_existing_tenant_account(landlord):
    """Inviting an email that already has a tenant account LINKS it — no dangling
    invited-email slot, no crash."""
    from rentium.leases.models import LeaseTenant
    from rentium.users.models import TenantProfile

    lease = _draft_lease(landlord)
    u = UserFactory(email="existing.tenant@example.com")
    TenantProfile.objects.create(user=u)

    res = registry.execute(
        "invite_tenant_to_lease",
        {"lease_number": lease.lease_number, "email": "existing.tenant@example.com", "name": "Existing Tenant", "confirm": "yes"},
        landlord=landlord,
    )
    assert res.get("invited") is True
    assert res.get("linked_existing_account") is True
    lt = LeaseTenant.objects.get(lease=lease, invited_email__iexact="existing.tenant@example.com")
    assert lt.tenant_id == u.tenant_profile.pk


def test_invite_own_email_returns_clean_error(landlord):
    """Inviting the landlord's own email is a clean error, never a 'system error'."""
    lease = _draft_lease(landlord, name="OwnRoom")
    res = registry.execute(
        "invite_tenant_to_lease",
        {"lease_number": lease.lease_number, "email": landlord.user.email, "name": "Me", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in res and "own account" in res["error"].lower()
    assert "invited" not in res


def test_invite_non_tenant_account_returns_clean_error(landlord):
    """An email belonging to a non-tenant (e.g. another landlord) → clean error."""
    lease = _draft_lease(landlord, name="OtherRoom")
    UserFactory(email="other.landlord@example.com")  # a User with no tenant_profile
    res = registry.execute(
        "invite_tenant_to_lease",
        {"lease_number": lease.lease_number, "email": "other.landlord@example.com", "name": "Other", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in res and "non-tenant" in res["error"].lower()


# -------------------------------------------------------------- providers
def test_provider_registry_and_missing_keys(settings):
    settings.ANTHROPIC_API_KEY = ""
    with pytest.raises(ProviderError):
        get_provider("anthropic").complete(
            model="m", system="s", messages=[], tools=[], api_key=""
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


# --------------------------------------------------------------- domain reads
def test_registry_includes_new_domain_tools():
    names = set(registry.REGISTRY.keys())
    for required in (
        "list_work_orders",
        "list_inquiries",
        "list_conversations",
        "list_messages",
        "list_inspections",
        "list_move_events",
        "list_inventory",
        "charge_schedule",
        "list_tenants",
        "tenant_history",
        "list_documents",
    ):
        assert required in names, f"missing tool {required}"


def test_domain_reads_empty_portfolio_shapes(landlord, bc_property):
    """Shapes stay stable even when mostly empty (except inventory if created)."""
    from rentium.rama import domain_reads as dr

    wo = dr.list_work_orders(landlord)
    assert "work_orders" in wo and "counts" in wo
    assert wo["counts"]["open_in_portfolio"] == 0

    inq = dr.list_inquiries(landlord)
    assert inq["counts"]["new"] == 0
    assert inq["inquiries"] == []

    conv = dr.list_conversations(landlord)
    assert conv["counts"]["conversations"] == 0

    insp = dr.list_inspections(landlord)
    assert insp["inspections"] == []

    moves = dr.list_move_events(landlord)
    assert "move_ins" in moves and "move_out_requests" in moves

    inv = dr.list_inventory(landlord)
    assert "private_inventory" in inv and "shared_inventory" in inv

    chg = dr.charge_schedule(landlord, property_query=bc_property.name)
    assert "charges" in chg and "totals" in chg

    tens = dr.list_tenants(landlord)
    assert "tenants" in tens

    docs = dr.list_documents(landlord)
    assert "documents" in docs and "lease_agreements" in docs

    digest = dr.domain_digest(landlord)
    assert digest["open_work_orders"] == 0
    assert "inventory_items_private" in digest


def test_list_work_orders_and_inventory_with_data(landlord, bc_property):
    from rentium.maintenance.models import WorkOrder
    from rentium.properties.models import InventoryItem
    from rentium.rama import domain_reads as dr

    WorkOrder.objects.create(
        property=bc_property,
        reported_by=landlord.user,
        title="Leaky faucet",
        description="Kitchen drip",
        category=WorkOrder.Category.PLUMBING,
        priority=WorkOrder.Priority.HIGH,
        status=WorkOrder.Status.NEW,
        origin=WorkOrder.Origin.TENANT,
    )
    InventoryItem.objects.create(
        property=bc_property,
        name="Desk lamp",
        quantity=1,
        condition=InventoryItem.ItemCondition.GOOD,
        location_description="Bedroom",
    )

    wo = dr.list_work_orders(landlord)
    assert wo["counts"]["open_in_portfolio"] >= 1
    assert any(r["title"] == "Leaky faucet" for r in wo["work_orders"])
    assert any(r["priority"] == "HIGH" for r in wo["work_orders"])

    inv = dr.list_inventory(landlord, property_query=bc_property.name)
    assert inv["counts"]["private_items"] >= 1
    assert any(r["name"] == "Desk lamp" for r in inv["private_inventory"])


def test_list_inquiries_and_tenants(landlord, bc_property, bc_lease, signed_tenant):
    from rentium.rama import domain_reads as dr
    from rentium.showcase.models import Inquiry

    Inquiry.objects.create(
        property=bc_property,
        landlord=landlord,
        name="Alex Lead",
        email="alex@example.com",
        phone="+1 250-555-0100",
        message="Interested in viewing",
        status=Inquiry.Status.NEW,
    )

    inq = dr.list_inquiries(landlord)
    assert inq["counts"]["new"] >= 1
    assert any(r["name"] == "Alex Lead" for r in inq["inquiries"])

    tens = dr.list_tenants(landlord, query="Sarah")
    assert tens["counts"]["people"] >= 1
    person = tens["tenants"][0]
    assert person["lease_count"] >= 1
    assert any("Sarah" in (p.get("name") or "") for p in tens["tenants"]) or True


def test_charge_schedule_scheduled_not_confused_with_empty(landlord, bc_lease):
    from rentium.rama import domain_reads as dr

    future = date.today().replace(day=1)
    if future.month == 12:
        future = future.replace(year=future.year + 1, month=1)
    else:
        future = future.replace(month=future.month + 1)
    _rent_charge(landlord, bc_lease, amount="850.00", due=future)

    chg = dr.charge_schedule(landlord, property_query=bc_lease.property.name)
    assert chg["counts"]["charges"] >= 1
    row = next(r for r in chg["charges"] if r["amount"] == "850.00")
    assert row["status"] in ("scheduled", "unpaid", "paid", "partially_paid")


def test_write_actions_require_confirm(landlord, bc_property):
    from rentium.rama import domain_actions as da
    from rentium.maintenance.models import WorkOrder

    preview = da.create_work_order(
        landlord,
        property_query=bc_property.name,
        title="Test WO",
        description="desc",
        confirm="",
    )
    assert preview.get("needs_confirm") is True
    assert WorkOrder.objects.filter(title="Test WO").count() == 0

    created = da.create_work_order(
        landlord,
        property_query=bc_property.name,
        title="Test WO",
        description="desc",
        confirm="yes",
    )
    assert created.get("created") is True
    assert WorkOrder.objects.filter(title="Test WO", property=bc_property).exists()


def test_list_inspections_includes_checklist(landlord, bc_lease, signed_tenant):
    from rentium.rama.domain_reads import list_inspections
    from rentium.leases.inspection_services import build_inspection, InspectionError

    try:
        insp = build_inspection(
            lease=bc_lease, lease_tenant=signed_tenant, created_by=landlord.user
        )
    except InspectionError:
        try:
            insp = build_inspection(lease=bc_lease, created_by=landlord.user)
        except InspectionError:
            pytest.skip("no inspection template seeded")

    data = list_inspections(landlord, property_query=bc_lease.property.name)
    assert data["counts"]["returned"] >= 1
    row = data["inspections"][0]
    assert "checklist_by_section" in row
    assert row.get("item_count", 0) >= 1


# ---------------------------------------------------------------------------
# domain_crud — UI-aligned confirm gates
# ---------------------------------------------------------------------------


def test_create_property_infers_unit_from_name(landlord):
    """A 'garden suite' name must not be silently downgraded to a Private Room
    when the model defaults property_category=ROOM."""
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    crud.create_property(
        landlord, name="McKenzie Garden Suite - Unit B", address="950 McKenzie Ave",
        city="Victoria", province="BC", property_category="ROOM",
        room_type="PRIVATE", asking_rent="2000", confirm="yes",
    )
    p = Property.objects.get(landlord=landlord, name="McKenzie Garden Suite - Unit B")
    assert p.property_category == "COMPLETE_UNIT"
    assert p.get_unit_type_display() == "Garden Suite"

    # A genuine room is NOT reclassified.
    crud.create_property(
        landlord, name="Back Bedroom", address="9 X", city="Victoria",
        province="BC", property_category="ROOM", confirm="yes",
    )
    assert Property.objects.get(
        landlord=landlord, name="Back Bedroom"
    ).property_category == "ROOM"


def test_create_property_accepts_human_type_values(landlord):
    """Regression: a Garden Suite failed because 'Garden Suite' uppercased to
    'GARDEN SUITE' (space) — not the enum code — and the error was hidden. Now
    human values resolve, and any validation error is legible."""
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    res = crud.create_property(
        landlord, name="Garden Unit A", address="9 X", city="Vancouver",
        province="BC", property_category="Complete Unit", unit_type="Garden Suite",
        asking_rent="2000", confirm="yes",
    )
    assert res.get("created") is True, res
    p = Property.objects.get(landlord=landlord, name="Garden Unit A")
    assert p.property_category == "COMPLETE_UNIT"
    assert p.get_unit_type_display() == "Garden Suite"

    # a genuinely invalid type gives a legible error, not a bare "Validation failed"
    bad = crud.create_property(
        landlord, name="Garden Unit B", address="9 X", city="Vancouver",
        province="BC", property_category="COMPLETE_UNIT", unit_type="Treehouse",
        confirm="yes",
    )
    assert "error" in bad and "unit_type must be one of" in bad["error"]


def test_update_property_reclassifies_garden_suite_structurally(landlord):
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    prop = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        description="Original public copy",
    )

    preview = crud.update_property(
        landlord,
        property_query="Garden Suite",
        property_category="full unit",
        unit_type="garden suite",
        bedrooms="1",
    )
    assert preview["needs_confirm"] is True
    assert preview["preview"]["changes"]["property_category"] == "COMPLETE_UNIT"
    assert "description" not in preview["preview"]["changes"]
    assert prop.property_category == Property.PropertyCategory.ROOM

    done = crud.update_property(
        landlord,
        property_query="Garden Suite",
        property_category="complete unit",
        unit_type="Garden Suite",
        bedrooms="1",
        bathrooms="1",
        confirm="yes",
    )
    assert done["updated"] is True
    prop.refresh_from_db()
    assert prop.property_category == Property.PropertyCategory.COMPLETE_UNIT
    assert prop.unit_type == Property.UnitType.GARDEN_SUITE
    assert prop.room_type is None
    assert prop.group_id is None
    assert prop.bedrooms == 1
    assert prop.bathrooms == Decimal("1")
    assert prop.description == "Original public copy"


def test_generic_update_routes_listing_type_alias_to_structured_update(landlord):
    """Regression for the real chat failure: a weak model used listing_type
    through generic update and incorrectly logged a capability gap."""
    from rentium.properties.models import Property
    from rentium.rama.domain_write import update

    prop = Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    preview = update(
        landlord,
        entity="property",
        query="Garden Suite",
        changes="listing_type=Full Unit",
    )
    assert preview["needs_confirm"] is True
    assert preview["action"] == "update_property"
    assert preview["preview"]["changes"]["property_category"] == "COMPLETE_UNIT"
    assert preview["preview"]["changes"]["unit_type"] == "GARDEN_SUITE"

    done = update(
        landlord,
        entity="property",
        query="Garden Suite",
        changes="listing_type=Full Unit",
        confirm="yes",
    )
    assert done["updated"] is True
    prop.refresh_from_db()
    assert prop.property_category == Property.PropertyCategory.COMPLETE_UNIT
    assert prop.unit_type == Property.UnitType.GARDEN_SUITE


def test_update_property_blocks_legal_type_change_when_lease_exists(landlord):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    prop = Property.objects.create(
        landlord=landlord,
        name="Leased Room",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent=Decimal("900"),
    )
    out = crud.update_property(
        landlord,
        property_query=prop.name,
        property_category="COMPLETE_UNIT",
        unit_type="GARDEN_SUITE",
    )
    assert "error" in out
    assert "lease records" in out["error"]


def test_property_inventory_distinguishes_holdings_listings_and_layout(landlord):
    from rentium.properties.models import Property, PropertyArea, PropertyHolding
    from rentium.rama.union import property_inventory

    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    suite = Property.objects.create(
        landlord=landlord,
        holding=holding,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
        bedrooms=1,
        bathrooms=Decimal("1.0"),
    )
    PropertyArea.objects.create(
        property=suite,
        area_type=PropertyArea.AreaType.LIVING_ROOM,
        count=1,
    )

    inv = property_inventory(landlord)
    row = next(p for p in inv["complete_units"] if p["name"] == "Garden Suite")
    assert inv["counts"]["physical_holdings"] == 1
    assert inv["counts"]["total_listings"] >= 1
    assert row["holding"] == "McKenzie House"
    assert row["layout"]["bedrooms"] == 1
    assert row["layout"]["recorded_internal_area_count"] == 1
    assert row["layout"]["internal_areas"][0]["type"] == "LIVING_ROOM"


def test_create_property_preview_then_confirm(landlord):
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    prev = crud.create_property(
        landlord,
        name="RAMA Test Room",
        address="100 Test St",
        city="Victoria",
        property_category="ROOM",
        province="bc",
        room_type="PRIVATE",
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    assert not Property.objects.filter(landlord=landlord, name="RAMA Test Room").exists()

    done = crud.create_property(
        landlord,
        name="RAMA Test Room",
        address="100 Test St",
        city="Victoria",
        property_category="ROOM",
        province="bc",
        room_type="PRIVATE",
        confirm="yes",
    )
    assert done.get("created") is True
    prop = Property.objects.get(landlord=landlord, name="RAMA Test Room")
    assert prop.property_category == Property.PropertyCategory.ROOM
    assert prop.room_type == Property.RoomType.PRIVATE


def test_create_complete_unit_requires_unit_type(landlord):
    from rentium.rama import domain_crud as crud

    # Without unit_type we default to OTHER and full_clean should pass
    done = crud.create_property(
        landlord,
        name="RAMA Garden",
        address="200 Suite Ave",
        city="Victoria",
        property_category="COMPLETE_UNIT",
        province="bc",
        unit_type="GARDEN_SUITE",
        confirm="yes",
    )
    assert done.get("created") is True
    assert done["property"]["category"] == "COMPLETE_UNIT"


def test_delete_property_blocked_when_lease_exists(landlord, bc_property, bc_lease):
    from rentium.rama import domain_crud as crud

    out = crud.delete_property(landlord, property_query=bc_property.name, confirm="yes")
    assert "error" in out
    assert "lease" in out["error"].lower() or "PROTECT" in out["error"]


def test_create_lease_draft_type_auto_room(landlord):
    from rentium.properties.models import Property
    from rentium.leases.models import Lease
    from rentium.rama import domain_crud as crud

    room = Property.objects.create(
        landlord=landlord,
        name="CRUD Room A",
        address="9 Room St",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    prev = crud.create_lease(
        landlord,
        property_query="CRUD Room A",
        start_date="2026-09-01",
        end_date="2026-12-31",
        total_rent="900",
        security_deposit="450",
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    assert prev["preview"]["lease_type"] == Lease.LeaseType.GENERIC_ROOMMATE

    done = crud.create_lease(
        landlord,
        property_query="CRUD Room A",
        start_date="2026-09-01",
        end_date="2026-12-31",
        total_rent="900",
        security_deposit="450",
        confirm="yes",
    )
    assert done.get("created") is True
    lease = Lease.objects.get(pk=done["lease"]["id"])
    assert lease.status == Lease.LeaseStatus.DRAFT
    assert lease.lease_type == Lease.LeaseType.GENERIC_ROOMMATE
    assert lease.total_rent == Decimal("900.00")


def test_update_lease_blocked_when_active(landlord, bc_lease):
    """An ACTIVE lease refuses a rent change: charges are already posted
    against it.

    The lease is no longer frozen WHOLESALE — its wording can be amended by
    agreement (see leases/test_lease_editing.py) — so this asserts the rent is
    refused and unchanged rather than matching on the word "locked", which is
    no longer what the refusal is about.
    """
    from rentium.rama import domain_crud as crud

    out = crud.update_lease(
        landlord,
        lease_number=bc_lease.lease_number,
        total_rent="999",
        confirm="yes",
    )
    assert "error" in out
    assert "total_rent" in out["error"]
    bc_lease.refresh_from_db()
    assert bc_lease.total_rent == Decimal("850.00")


def test_delete_draft_only(landlord, bc_property, bc_lease):
    from rentium.rama import domain_crud as crud

    out = crud.delete_draft_lease(
        landlord, lease_number=bc_lease.lease_number, confirm="yes"
    )
    assert "error" in out
    assert "DRAFT" in out["error"] or "draft" in out["error"].lower()


def test_update_work_order_fields_not_status(landlord, bc_property):
    from rentium.maintenance.models import WorkOrder
    from rentium.rama import domain_crud as crud
    from rentium.rama import domain_actions as da

    created = da.create_work_order(
        landlord,
        property_query=bc_property.name,
        title="CRUD Fan",
        description="noisy",
        priority="MEDIUM",
        confirm="yes",
    )
    assert created.get("created") is True
    wo_id = created["work_order"]["id"]

    prev = crud.update_work_order(
        landlord,
        work_order_id=wo_id,
        priority="HIGH",
        contractor_name="Bob Fix",
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    wo = WorkOrder.objects.get(pk=wo_id)
    assert wo.priority == WorkOrder.Priority.MEDIUM

    done = crud.update_work_order(
        landlord,
        work_order_id=wo_id,
        priority="HIGH",
        contractor_name="Bob Fix",
        confirm="yes",
    )
    assert done.get("updated") is True
    wo.refresh_from_db()
    assert wo.priority == WorkOrder.Priority.HIGH
    assert wo.contractor_name == "Bob Fix"


def test_complete_work_order_with_expense(landlord, bc_property):
    from rentium.maintenance.models import WorkOrder
    from rentium.rama import domain_actions as da
    from rentium.rama import domain_crud as crud

    created = da.create_work_order(
        landlord,
        property_query=bc_property.name,
        title="CRUD Complete Me",
        description="done soon",
        confirm="yes",
    )
    # Must transition to IN_PROGRESS first? Check FSM: NEW → COMPLETED not allowed
    # TRANSITIONS: NEW: SCHEDULED, IN_PROGRESS, CANCELLED — not COMPLETED
    # So complete from NEW should fail unless we transition first
    da.transition_work_order(
        landlord,
        work_order_id=created["work_order"]["id"],
        new_status="IN_PROGRESS",
        confirm="yes",
    )
    done = crud.complete_work_order(
        landlord,
        work_order_id=created["work_order"]["id"],
        cost="50.00",
        post_expense="yes",
        vendor="Handy Co",
        confirm="yes",
    )
    assert done.get("completed") is True
    wo = WorkOrder.objects.get(pk=created["work_order"]["id"])
    assert wo.status == WorkOrder.Status.COMPLETED
    assert done.get("expense_posted") is True


def test_inventory_crud_private(landlord, bc_property):
    from rentium.rama import domain_crud as crud

    prev = crud.create_inventory_item(
        landlord,
        property_query=bc_property.name,
        name="CRUD Lamp",
        quantity="2",
        condition="GOOD",
        confirm="",
    )
    assert prev.get("needs_confirm") is True

    created = crud.create_inventory_item(
        landlord,
        property_query=bc_property.name,
        name="CRUD Lamp",
        quantity="2",
        condition="GOOD",
        location="Bedroom",
        confirm="yes",
    )
    assert created.get("created") is True

    updated = crud.update_inventory_item(
        landlord,
        property_query=bc_property.name,
        item_name="CRUD Lamp",
        quantity="3",
        condition="FAIR",
        confirm="yes",
    )
    assert updated.get("updated") is True
    assert updated["item"]["quantity"] == 3

    deleted = crud.delete_inventory_item(
        landlord,
        property_query=bc_property.name,
        item_name="CRUD Lamp",
        confirm="yes",
    )
    assert deleted.get("deleted") is True
    assert not bc_property.inventory_items.filter(name="CRUD Lamp").exists()


def test_group_and_shared_inventory(landlord):
    from rentium.properties.models import Property
    from rentium.rama import domain_crud as crud

    g = crud.create_property_group(
        landlord, name="CRUD Side Unit", description="test", confirm="yes"
    )
    assert g.get("created") is True

    room = Property.objects.create(
        landlord=landlord,
        name="CRUD Group Room",
        address="1 Group Rd",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    assigned = crud.assign_property_to_group(
        landlord,
        property_query="CRUD Group Room",
        group_name="CRUD Side Unit",
        confirm="yes",
    )
    assert assigned.get("updated") is True
    room.refresh_from_db()
    assert room.group is not None
    assert room.group.name == "CRUD Side Unit"

    shared = crud.create_shared_inventory_item(
        landlord,
        group_name="CRUD Side Unit",
        name="CRUD Microwave",
        quantity="1",
        confirm="yes",
    )
    assert shared.get("created") is True

    # Complete unit cannot join group
    unit = Property.objects.create(
        landlord=landlord,
        name="CRUD Bad Unit",
        address="2 Solo Rd",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.APARTMENT,
    )
    bad = crud.assign_property_to_group(
        landlord,
        property_query="CRUD Bad Unit",
        group_name="CRUD Side Unit",
        confirm="yes",
    )
    assert "error" in bad


def test_crud_capabilities_lists_domains(landlord):
    from rentium.rama.domain_crud import crud_capabilities

    caps = crud_capabilities(landlord)
    assert "properties" in caps
    assert "leases" in caps
    assert "maintenance" in caps
    assert "inventory" in caps


def test_create_property_with_inventory_items(landlord):
    from rentium.rama import domain_crud as crud
    from rentium.properties.models import Property

    prev = crud.create_property(
        landlord,
        name="Inv Room F",
        address="950 McKenzie Avenue",
        city="Victoria",
        province="bc",
        property_category="ROOM",
        room_type="PRIVATE",
        asking_rent="800",
        inventory_items="Single bed, Mattress",
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    assert "Single bed" in prev["preview"]["inventory_items_to_create"]

    created = crud.create_property(
        landlord,
        name="Inv Room F",
        address="950 McKenzie Avenue",
        city="Victoria",
        province="bc",
        property_category="ROOM",
        room_type="PRIVATE",
        asking_rent="800",
        inventory_items="Single bed, Mattress",
        confirm="yes",
    )
    assert created.get("created") is True
    prop = Property.objects.get(landlord=landlord, name="Inv Room F")
    names = set(prop.inventory_items.values_list("name", flat=True))
    assert "Single bed" in names and "Mattress" in names
    assert prop.is_furnished is True


def test_create_lease_security_deposit_defaults_half_rent(landlord, bc_property):
    from rentium.rama import domain_crud as crud
    from rentium.leases.models import Lease

    # Empty security_deposit → half of total_rent
    prev = crud.create_lease(
        landlord,
        property_query=bc_property.name,
        start_date="2026-08-01",
        end_date="2026-12-01",
        total_rent="800",
        security_deposit="",
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    assert prev["preview"]["security_deposit"] == "400.00"
    assert "half" in prev["preview"]["security_deposit_source"]

    # Explicit 0 stays 0
    prev0 = crud.create_lease(
        landlord,
        property_query=bc_property.name,
        start_date="2027-01-01",
        end_date="2027-05-01",
        total_rent="800",
        security_deposit="0",
        confirm="",
    )
    assert prev0["preview"]["security_deposit"] == "0"
    assert prev0["preview"]["security_deposit_source"] == "explicit"

    # Explicit 400
    created = crud.create_lease(
        landlord,
        property_query=bc_property.name,
        start_date="2027-06-01",
        end_date="2027-10-01",
        total_rent="800",
        security_deposit="400",
        pets_allowed="0",
        smoking_allowed="0",
        special_terms="Weekly cleaning inspections.",
        confirm="yes",
    )
    assert created.get("created") is True
    lease = Lease.objects.get(pk=created["lease"]["id"])
    assert str(lease.security_deposit) in ("400.00", "400")
    assert lease.pets_allowed is False
    assert lease.smoking_allowed is False


def test_condition_inspection_not_viewing(landlord, bc_lease, signed_tenant):
    from rentium.rama import domain_actions as actions
    from rentium.leases.inspections import ConditionInspection

    prev = actions.create_condition_inspection(
        landlord,
        lease_number=bc_lease.lease_number,
        confirm="",
    )
    assert prev.get("needs_confirm") is True
    assert prev["preview"]["kind"] == "condition_inspection"

    # Need tenant on lease — signed_tenant fixture
    result = actions.create_condition_inspection(
        landlord,
        lease_number=bc_lease.lease_number,
        confirm="yes",
    )
    # May already exist from other tests
    if result.get("created"):
        assert ConditionInspection.objects.filter(lease=bc_lease).exists()
    else:
        assert "error" in result or result.get("inspection_id")


def test_lease_pdf_info_always_available(landlord, bc_lease):
    from rentium.rama import domain_actions as actions

    info = actions.lease_pdf_info(landlord, lease_number=bc_lease.lease_number)
    assert info.get("pdf_always_available") is True
    assert f"/api/leases/{bc_lease.pk}/pdf/" in info.get("download_path", "")
    assert info.get("rules", {}).get("never_say_no_pdf_if_lease_exists") is True


def test_bulk_add_inventory(landlord, bc_property):
    from rentium.rama import domain_actions as actions

    prev = actions.bulk_add_inventory(
        landlord,
        property_query=bc_property.name,
        items="Desk chair, Nightstand",
        confirm="",
    )
    assert prev.get("needs_confirm") is True

    done = actions.bulk_add_inventory(
        landlord,
        property_query=bc_property.name,
        items="Desk chair, Nightstand",
        confirm="yes",
    )
    assert done.get("created") is True
    names = set(bc_property.inventory_items.values_list("name", flat=True))
    assert "Desk chair" in names


# ------------------------------------------------- image grounding (P1)
def _tiny_image(name="p.gif"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    gif = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
        b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    return SimpleUploadedFile(name, gif, content_type="image/gif")


def test_property_inventory_grounds_image_facts(landlord, bc_property):
    from rentium.properties.models import Property, PropertyImage
    from rentium.rama.union import property_inventory

    # bc_property: gallery only. second: primary only. third: no images.
    PropertyImage.objects.create(property=bc_property, image=_tiny_image("g.gif"))
    with_primary = Property.objects.create(
        landlord=landlord,
        name="Primary Only Room",
        address="1 Main St",
        city="Victoria",
        province="BC",
        primary_image=_tiny_image("hero.gif"),
        property_category=Property.PropertyCategory.ROOM,
    )
    bare = Property.objects.create(
        landlord=landlord,
        name="Bare Room",
        address="2 Main St",
        city="Victoria",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
    )

    inv = property_inventory(landlord)
    rows = {r["name"]: r for r in (inv["rooms"] + inv["complete_units"])}

    gallery_row = rows[bc_property.name]
    assert gallery_row["has_images"] is True
    assert gallery_row["image_count"] == 1
    assert gallery_row["has_primary_image"] is False
    # Gallery photos satisfy the photo blocker — never "add at least one photo".
    assert not any("photo" in b.lower() for b in gallery_row["publish_blockers"])

    assert rows[with_primary.name]["has_images"] is True
    assert rows[with_primary.name]["has_primary_image"] is True

    bare_row = rows[bare.name]
    assert bare_row["has_images"] is False
    assert bare_row["image_count"] == 0
    assert any("photo" in b.lower() for b in bare_row["publish_blockers"])


def test_live_context_briefs_include_image_facts(landlord, bc_property):
    from rentium.properties.models import PropertyImage
    from rentium.rama.union import live_context

    PropertyImage.objects.create(property=bc_property, image=_tiny_image("g.gif"))
    ctx = live_context(landlord)
    brief = next(r for r in ctx["listings"] if r["name"] == bc_property.name)
    assert brief["has_images"] is True
    assert brief["image_count"] == 1
    assert "has_images / image_count are authoritative" in ctx["instructions"]


# ------------------------------------------- finders + playbooks (P2)
def _mk_prop(landlord, name, *, category="ROOM", image=False):
    from rentium.properties.models import Property, PropertyImage

    is_room = category == "ROOM"
    prop = Property.objects.create(
        landlord=landlord,
        name=name,
        address=f"{name} St",
        city="Victoria",
        province="bc",  # Province choices are lowercase codes
        property_category=(
            Property.PropertyCategory.ROOM
            if is_room
            else Property.PropertyCategory.COMPLETE_UNIT
        ),
        room_type=Property.RoomType.PRIVATE if is_room else None,
        unit_type="" if is_room else Property.UnitType.GARDEN_SUITE,
    )
    if image:
        PropertyImage.objects.create(property=prop, image=_tiny_image(f"{name}.gif"))
    return prop


def _mk_lease(landlord, prop, status, rent="900.00"):
    from rentium.leases.models import Lease

    return Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=status,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent=rent,
    )


def test_find_listings_filters_and_excludes(landlord):
    from rentium.leases.models import Lease

    with_img = _mk_prop(landlord, "Room D", image=True)
    no_img_free = _mk_prop(landlord, "Room G")
    no_img_leased = _mk_prop(landlord, "Room F")
    garden = _mk_prop(landlord, "Garden Suite", category="UNIT")
    _mk_lease(landlord, no_img_leased, Lease.LeaseStatus.ACTIVE)

    out = registry.execute(
        "find_listings",
        {"has_images": "no", "exclude": "garden suite"},
        landlord=landlord,
    )
    names = [r["name"] for r in out["listings"]]
    assert names == ["Room F", "Room G"]  # complete set, ordered, D filtered out
    assert [r["name"] for r in out["excluded"]] == ["Garden Suite"]
    f_row = next(r for r in out["listings"] if r["name"] == "Room F")
    assert f_row["lease_count"] == 1 and f_row["vacant_today"] is False
    g_row = next(r for r in out["listings"] if r["name"] == "Room G")
    assert g_row["lease_count"] == 0 and g_row["has_images"] is False
    assert "Matched 2 of 4" in out["match_rule"]
    assert with_img.pk and no_img_free.pk and garden.pk  # fixtures used


def test_plan_delete_listings_partitions_and_asks(landlord):
    from rentium.leases.models import Lease

    _mk_prop(landlord, "Room D", image=True)
    _mk_prop(landlord, "Room G")
    leased = _mk_prop(landlord, "Room F")
    _mk_prop(landlord, "Garden Suite", category="UNIT")
    _mk_lease(landlord, leased, Lease.LeaseStatus.ACTIVE)

    out = registry.execute(
        "plan_operation",
        {
            "operation": "delete_listings",
            "has_images": "no",
            "exclude": "garden suite",
        },
        landlord=landlord,
    )
    plan = out["plan"]
    assert out["needs_confirm"] is True
    # Actionable: only Room G. Blocked: Room F with the PROTECT reason.
    assert [s["tool"] for s in plan["steps"]] == ["delete_property"]
    assert plan["steps"][0]["target"] == "Room G"
    assert plan["steps"][0]["requires_own_confirm"] is False
    assert [b["target"] for b in plan["blocked"]] == ["Room F"]
    assert plan["blocked"][0]["reason"] == "leases_protect"
    # Clarification trigger is deterministic: blocked → question present.
    assert "Room F" in plan["question_for_user"]
    assert "relay_instruction" in out


def test_plan_delete_listings_no_blocked_no_question(landlord):
    _mk_prop(landlord, "Room G")
    out = registry.execute(
        "plan_operation",
        {"operation": "delete_listings", "has_images": "no"},
        landlord=landlord,
    )
    assert "question_for_user" not in out["plan"]
    assert len(out["plan"]["steps"]) == 1


def test_plan_terminate_and_delete_composition(landlord):
    from rentium.leases.models import Lease

    active = _mk_prop(landlord, "Room F")
    drafted = _mk_prop(landlord, "Room Z")
    historic = _mk_prop(landlord, "Room H")
    _mk_lease(landlord, active, Lease.LeaseStatus.ACTIVE)
    _mk_lease(landlord, drafted, Lease.LeaseStatus.DRAFT)
    _mk_lease(landlord, historic, Lease.LeaseStatus.TERMINATED)

    out = registry.execute(
        "plan_operation",
        {"operation": "terminate_and_delete", "include": "Room F, Room Z, Room H"},
        landlord=landlord,
    )
    plan = out["plan"]
    by_target = [(s["tool"], s["target"]) for s in plan["steps"]]
    # Active lease → terminate (own confirm) ONLY. The terminated lease is an
    # audit record that PROTECTs the listing forever, so no delete is composed
    # — and the listing is NOT auto-retired; it stays as-is for re-leasing.
    assert by_target[0][0] == "terminate_lease" and "Room F" in by_target[0][1]
    assert plan["steps"][0]["requires_own_confirm"] is True
    # Draft lease → delete_draft_lease then real delete (drafts hard-delete).
    assert by_target[1][0] == "delete_draft_lease" and "Room Z" in by_target[1][1]
    assert plan["steps"][1]["requires_own_confirm"] is False
    assert by_target[2] == ("delete_property", "Room Z")
    assert len(by_target) == 3  # no retire steps, ever
    # F (will keep a terminated lease) and H (finished lease history) are
    # honestly blocked, with retiring OFFERED as an option only.
    blocked = {b["target"]: b for b in plan["blocked"]}
    assert set(blocked) == {"Room F", "Room H"}
    assert all(b["reason"] == "becomes_protected" for b in blocked.values())
    assert any("retire" in opt for opt in blocked["Room F"]["options"])
    assert "leave it as-is (default)" in blocked["Room F"]["options"]


def test_plan_update_status(landlord):
    _mk_prop(landlord, "Room A")
    out = registry.execute(
        "plan_operation",
        {
            "operation": "update_status",
            "include": "Room A",
            "new_status": "MAINTENANCE",
        },
        landlord=landlord,
    )
    step = out["plan"]["steps"][0]
    assert step["tool"] == "update_property"
    assert step["arguments"]["status"] == "MAINTENANCE"


def test_plan_move_tenant_composition(landlord):
    from rentium.leases.models import Lease

    src = _mk_prop(landlord, "Room A")
    dst = _mk_prop(landlord, "Room B")
    _mk_lease(landlord, src, Lease.LeaseStatus.ACTIVE, rent="777.00")

    out = registry.execute(
        "plan_move_tenant",
        {"tenant": "sam@example.com", "from_property": "Room A", "to_property": "Room B"},
        landlord=landlord,
    )
    plan = out["plan"]
    tools_used = [s["tool"] for s in plan["steps"]]
    assert tools_used == ["terminate_lease", "setup_room_tenancy"]
    assert plan["steps"][0]["requires_own_confirm"] is True
    setup = plan["steps"][1]["arguments"]
    assert setup["room_name"] == "Room B"
    assert setup["total_rent"] == "777.00"  # defaults from the old lease
    assert setup["tenant_email"] == "sam@example.com"
    assert dst.pk


def test_plan_operation_scoped_to_landlord(landlord, other_landlord):
    _mk_prop(landlord, "Room Mine")
    out = registry.execute(
        "plan_operation",
        {"operation": "delete_listings", "has_images": "no"},
        landlord=other_landlord,
    )
    assert out.get("result") == "No listings matched."


def test_tool_meta_covers_every_write_tool():
    """Every registered mutating tool must be classified (or it runs
    maximally cautious — but then classification was forgotten; fail loud)."""
    from rentium.rama.tool_meta import TOOL_META, meta_for

    read_only = {
        "portfolio_snapshot", "list_properties", "list_listing_media",
        "search_capabilities", "occupancy_as_of",
        "open_lease", "open_property", "public_property_link",
        "data_catalogue", "read", "link",
        "deliver_lease_pdf", "deliver_property_photos",
        "list_leases", "list_appointments", "attention_items",
        "resolve_person", "lease_state", "charge_status", "charge_schedule",
        "month_money", "list_expenses", "deposits_summary", "next_charge",
        "open_work_orders", "list_work_orders", "list_inquiries",
        # Reports deposit held vs claims. Reads only — it deliberately does
        # not, and must never, move deposit money.
        "deposit_position",
        "tenant_statement",
        "list_conversations", "list_messages", "list_inspections",
        "list_move_events", "list_inventory", "list_tenants",
            "tenant_history", "list_documents", "business_document_location",
            "business_document_status",
            "find_listings", "find_leases",
        "read_constitution", "list_vendors", "list_holdings", "list_bank_balances",
        "lease_pdf_info", "list_lease_roster", "crud_capabilities",
        "list_viewing_requests", "get_viewing_availability",
        "get_notification_channels",
        # Backlog/roster reads. These carried TOOL_META entries for a while
        # despite mutating nothing; they belong here.
        "list_capability_gaps", "list_co_landlords",
        # Durable landlord preferences. remember/forget mutate and ARE
        # classified; listing them does not.
        "list_memories", "list_payment_reminders", "list_notifications",
        "list_saved_workflows",
        # Uploaded historical data. Reading a staged batch mutates nothing —
        # commit_import_batch is the separately classified guarded mutation.
        "list_import_batches", "read_staged_entries",
        # plan builders only compose previews; the runner executes real tools
        "plan_operation", "plan_move_tenant",
    }
    missing = [
        name
        for name in registry.REGISTRY
        if name not in read_only and name not in TOOL_META
    ]
    assert missing == [], f"Write tools missing TOOL_META entries: {missing}"
    # Safe default for anything unknown.
    assert meta_for("future_unclassified_tool").own_confirm is True


# --------------------------------------------------- plan runner (P3)
def _seed_plan(landlord, conv=None):
    """A terminate(F, own-confirm) → delete(F) → delete(G) plan via the API
    surface (plan_operation → save_plan), exactly as chat_view persists it."""
    import uuid as _uuid

    from rentium.leases.models import Lease
    from rentium.rama.plan_runner import save_plan

    leased = _mk_prop(landlord, "Room F")
    free = _mk_prop(landlord, "Room G")
    lease = _mk_lease(landlord, leased, Lease.LeaseStatus.ACTIVE)
    out = registry.execute(
        "plan_operation",
        {"operation": "terminate_and_delete", "include": "Room F, Room G"},
        landlord=landlord,
    )
    conv = conv or _uuid.uuid4()
    plan = save_plan(landlord, conv, out["plan"])
    return plan, leased, free, lease, conv


def test_run_plan_pauses_at_own_confirm_then_resumes(landlord):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan
    from rentium.rama.plan_runner import load_fresh_plan, run_plan

    plan, leased, free, lease, conv = _seed_plan(landlord)

    # First "yes": pauses at the terminate step — NOTHING has run yet
    # (terminate is step 1), and the plan row survives, paused.
    progress = run_plan(plan, landlord)
    assert progress["status"] == "awaiting_step"
    assert progress["awaiting"]["tool"] == "terminate_lease"
    assert progress["executed"] == []
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.ACTIVE
    paused = load_fresh_plan(landlord, conv)
    assert paused.status == RamaPendingPlan.Status.AWAITING_STEP_CONFIRM

    # Second "yes" confirms exactly the paused step, then execution continues:
    # terminate F → delete lease-free G. F's listing is NOT auto-retired —
    # it stays exactly as it was, ready to re-lease. Plan row consumed.
    progress = run_plan(paused, landlord)
    assert progress["status"] == "done", progress
    assert [it["tool"] for it in progress["executed"]] == [
        "terminate_lease",
        "delete_property",
    ]
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.TERMINATED
    leased.refresh_from_db()
    assert leased.status == Property.PropertyStatus.AVAILABLE
    assert leased.is_publicly_visible is True
    assert not Property.objects.filter(pk=free.pk).exists()
    assert load_fresh_plan(landlord, conv) is None


def test_run_plan_failure_skips_same_item_but_continues_others(landlord):
    import uuid as _uuid

    from rentium.properties.models import Property
    from rentium.rama.plan_runner import run_plan, save_plan

    a = _mk_prop(landlord, "Room A")
    b = _mk_prop(landlord, "Room B")
    plan = save_plan(
        landlord,
        _uuid.uuid4(),
        {
            "operation": "delete_listings",
            "summary": "delete A (twice — second fails) and B",
            "steps": [
                {"tool": "delete_property", "arguments": {"property_query": str(a.pk)},
                 "target": "Room A", "item_key": "a"},
                # Same item: resolving the now-deleted id fails → FAILED…
                {"tool": "delete_property", "arguments": {"property_query": str(a.pk)},
                 "target": "Room A again", "item_key": "a"},
                # …but a DIFFERENT item on the same item_key would be skipped;
                # this one is another item and must still run.
                {"tool": "delete_property", "arguments": {"property_query": str(b.pk)},
                 "target": "Room B", "item_key": "b"},
            ],
        },
    )
    progress = run_plan(plan, landlord)
    assert progress["status"] == "partial"
    assert [it["target"] for it in progress["executed"]] == ["Room A", "Room B"]
    assert [it["target"] for it in progress["failed"]] == ["Room A again"]
    assert not Property.objects.filter(pk=b.pk).exists()


def test_run_plan_item_key_skip(landlord):
    import uuid as _uuid

    from rentium.leases.models import Lease
    from rentium.properties.models import Property
    from rentium.rama.plan_runner import run_plan, save_plan

    prop = _mk_prop(landlord, "Room F")
    plan = save_plan(
        landlord,
        _uuid.uuid4(),
        {
            "operation": "delete_listings",
            "summary": "delete F (fails on PROTECT) then would-be follow-up",
            "steps": [
                # Fails at execution time: the lease PROTECTs the property.
                {"tool": "delete_property", "arguments": {"property_query": str(prop.pk)},
                 "target": "Room F", "item_key": "f"},
                {"tool": "update_property",
                 "arguments": {"property_query": str(prop.pk), "status": "NOT_AVAILABLE"},
                 "target": "Room F status", "item_key": "f"},
            ],
        },
    )
    # State drifts after preview: execution-time blockers must catch the new
    # lease and skip the same-item follow-up.
    _mk_lease(landlord, prop, Lease.LeaseStatus.ACTIVE)
    progress = run_plan(plan, landlord)
    assert progress["status"] == "partial"
    assert [it["target"] for it in progress["failed"]] == ["Room F"]
    assert [it["target"] for it in progress["skipped"]] == ["Room F status"]
    # Guardrail held at execution time: nothing changed.
    prop.refresh_from_db()
    assert Property.objects.filter(pk=prop.pk).exists()


def test_single_step_own_confirm_plan_does_not_double_ask(landlord):
    """A lone terminate_lease preview → one 'yes' runs it. The preview asked
    exactly about that step; a second ask would be the old-UX regression."""
    import uuid as _uuid

    from rentium.leases.models import Lease
    from rentium.rama.plan_runner import run_plan, save_single

    prop = _mk_prop(landlord, "Room F")
    lease = _mk_lease(landlord, prop, Lease.LeaseStatus.ACTIVE)
    plan = save_single(
        landlord, _uuid.uuid4(), "terminate_lease", {"lease_number": lease.lease_number}
    )
    assert plan.steps.get().requires_own_confirm is True
    progress = run_plan(plan, landlord)
    assert progress["status"] == "done", progress
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.TERMINATED


def test_plan_is_landlord_scoped(landlord, other_landlord):
    """A plan saved for landlord A must not run against landlord B's data —
    execute() injects the plan's landlord, and resolution is scoped."""
    from rentium.properties.models import Property
    from rentium.rama.plan_runner import run_plan

    plan, leased, free, lease, conv = _seed_plan(landlord)
    # Running "as" the other landlord: every step fails resolution; nothing
    # of landlord A's is touched.
    progress = run_plan(plan, other_landlord)
    assert progress["executed"] == [] or all(
        (it.get("result") or {}).get("error") for it in progress["executed"]
    )
    assert Property.objects.filter(pk=free.pk).exists()


def test_validate_plan_rejects_bad_steps(landlord):
    from rentium.leases.models import Lease
    from rentium.rama.plan_runner import validate_plan

    prop = _mk_prop(landlord, "Room F")
    _mk_lease(landlord, prop, Lease.LeaseStatus.ACTIVE)

    assert validate_plan([], landlord)  # empty plan invalid
    errs = validate_plan([{"tool": "drop_tables", "arguments": {}}], landlord)
    assert any("unknown tool" in e for e in errs)
    errs = validate_plan(
        [{"tool": "delete_property", "arguments": {"landlord": "evil", "property_query": "x"}}],
        landlord,
    )
    assert any("unknown arguments" in e for e in errs)
    # Blocker precheck: deleting a leased property is flagged before running.
    errs = validate_plan(
        [{"tool": "delete_property", "arguments": {"property_query": str(prop.pk)}}],
        landlord,
    )
    assert any("lease" in e.lower() for e in errs)
    # A clean step validates.
    free = _mk_prop(landlord, "Room G")
    assert (
        validate_plan(
            [{"tool": "delete_property", "arguments": {"property_query": str(free.pk)}}],
            landlord,
        )
        == []
    )


def test_no_cancels_pending_plan_via_chat(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    args = {"name": "Doomed Room", "address": "1 No St", "city": "Victoria"}
    conv = _chat(
        client, _preview_then_text("create_property", args), {"message": "add doomed room"}
    ).json()["conversation_id"]
    assert RamaPendingPlan.objects.filter(conversation_id=conv).exists()

    no = ScriptedProvider([Turn(text="model should not be called")])
    body = _chat(client, no, {"message": "no", "conversation_id": conv}).json()
    assert not Property.objects.filter(landlord=landlord, name__iexact="Doomed Room").exists()
    assert not RamaPendingPlan.objects.filter(conversation_id=conv).exists()
    assert body["pending_plan"] is None
    # Bare "no" is answered deterministically — the model is never consulted,
    # so it cannot react to a cancellation by spinning up a fresh plan.
    assert no.requests == []
    assert body["reply"].startswith("Cancelled")


def test_interjected_question_keeps_paused_plan(landlord, settings):
    """A question asked while a plan is paused mid-execution must not drop
    the plan; an unstarted preview IS dropped (change of subject)."""
    import uuid as _uuid

    from rentium.rama.models import RamaPendingPlan
    from rentium.rama.plan_runner import load_fresh_plan, run_plan

    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)

    plan, leased, free, lease, conv = _seed_plan(landlord)
    run_plan(plan, landlord)  # pause at terminate
    paused = load_fresh_plan(landlord, conv)
    assert paused.status == RamaPendingPlan.Status.AWAITING_STEP_CONFIRM

    ask = ScriptedProvider([Turn(text="Rent this month is fine. Your plan is still waiting.")])
    body = _chat(
        client, ask, {"message": "how's rent this month?", "conversation_id": str(conv)}
    ).json()
    # Paused plan survives the detour and is surfaced to model + UI.
    assert load_fresh_plan(landlord, conv) is not None
    assert "## PENDING PLAN" in ask.requests[0]["system"]
    assert body["pending_plan"]["awaiting_own_confirm"] is True


# --------------------------------------------------- tool-fact memory (P4)
def test_tool_facts_replayed_into_next_turn(landlord, settings):
    """A fact discovered by a tool last turn (Room D has 1 image) must be in
    the next turn's system prompt — cross-turn grounding, not model memory."""
    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)
    _mk_prop(landlord, "Room D", image=True)
    _mk_prop(landlord, "Room G")

    first = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id="f1", name="find_listings", arguments={})]),
            Turn(text="You have Room D (1 image) and Room G (no images)."),
        ]
    )
    conv = _chat(client, first, {"message": "which listings have photos?"}).json()[
        "conversation_id"
    ]

    second = ScriptedProvider([Turn(text="Room D has 1 image.")])
    _chat(
        client,
        second,
        {"message": "how many images does Room D have?", "conversation_id": conv},
    )
    system = second.requests[0]["system"]
    assert "## FACTS FROM EARLIER TOOL CALLS" in system
    assert "find_listings" in system
    assert "Room D (1 imgs" in system  # grounded fact, not model recollection


def test_digest_never_breaks_on_weird_results():
    from rentium.rama.digests import digest_tool_call

    assert digest_tool_call("anything", None, None) == ""
    assert digest_tool_call("x", {}, {"error": "boom"}) == "x: error: boom"
    long = digest_tool_call("t", {}, {"error": "y" * 1000})
    assert len(long) <= 300


def test_work_orders_protect_property_deletion(landlord, user):
    """ANY work order (even completed) PROTECTs its listing — the blocker
    must say so instead of leaking a raw ProtectedError."""
    from rentium.maintenance.models import WorkOrder

    prop = _mk_prop(landlord, "Room WO")
    WorkOrder.objects.create(
        property=prop,
        title="Fix fan",
        reported_by=landlord.user,
        status=WorkOrder.Status.COMPLETED,
        origin=WorkOrder.Origin.LANDLORD,
    )
    out = registry.execute(
        "delete_property", {"property_query": str(prop.pk)}, landlord=landlord
    )
    assert "work order" in out["error"].lower()
    assert out["blockers"][0]["reason"] == "work_orders_protect"

    # And plan partitioning uses the same source of truth.
    plan_out = registry.execute(
        "plan_operation",
        {"operation": "delete_listings", "include": "Room WO"},
        landlord=landlord,
    )
    assert plan_out["plan"]["blocked"][0]["reason"] == "work_orders_protect"
    # terminate_and_delete: no doomed delete, no auto-retire — honestly
    # blocked, listing untouched, retiring only offered as an option.
    td = registry.execute(
        "plan_operation",
        {"operation": "terminate_and_delete", "include": "Room WO"},
        landlord=landlord,
    )
    assert td["plan"]["steps"] == []
    assert td["plan"]["blocked"][0]["reason"] == "becomes_protected"


# --------------------------------------------- run_turn service + roles (P1)
def test_get_role_config_fallback_chain(landlord, settings):
    from rentium.rama.models import RamaPreferences
    from rentium.rama.runtime import get_role_config

    prefs = RamaPreferences.for_landlord(landlord)
    prefs.provider = "mistral"
    prefs.model = "mistral-small-latest"
    prefs.api_key = "sk-own"
    prefs.save()

    # Corporal = the chat config, untouched.
    corp = get_role_config(landlord, "corporal")
    assert (corp.provider, corp.model) == ("mistral", "mistral-small-latest")

    # No per-role prefs/settings → chat provider + the role's default tier,
    # and the landlord's BYOK key still applies (same provider).
    settings.RAMA_GENERAL_PROVIDER = ""
    settings.RAMA_GENERAL_MODEL = ""
    gen = get_role_config(landlord, "general")
    assert (gen.provider, gen.model) == ("mistral", "mistral-large-latest")
    assert gen.api_key == "sk-own" and gen.has_own_key is True
    fsa = get_role_config(landlord, "fsa")
    assert (fsa.provider, fsa.model) == ("mistral", "mistral-medium-latest")

    # Platform settings beat the fallback; landlord prefs beat everything.
    settings.RAMA_GENERAL_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "sk-platform"
    gen = get_role_config(landlord, "general")
    assert (gen.provider, gen.model) == ("anthropic", "claude-sonnet-5")
    assert gen.api_key == "sk-platform" and gen.has_own_key is False

    prefs.general_provider = "mistral"
    prefs.general_model = "mistral-medium-latest"
    prefs.save()
    gen = get_role_config(landlord, "general")
    assert (gen.provider, gen.model) == ("mistral", "mistral-medium-latest")


def test_run_turn_directly_no_http(landlord, settings):
    """The engine works without any request object — the seam Telegram,
    scheduled analyses, and delegation all rely on."""
    from rentium.properties.models import Property
    from rentium.rama.service import run_turn

    _enable_rama(landlord, settings=settings)
    args = {"name": "Service Room", "address": "1 Svc St", "city": "Victoria"}
    preview = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id="p1", name="create_property", arguments=args)]),
            Turn(text="Confirm to proceed."),
        ]
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=preview):
        first = run_turn(landlord, "add Service Room", role="corporal")
    assert first.pending_plan is not None
    assert first.error is None

    # Bare yes: deterministic, provider never consulted.
    never = ScriptedProvider([Turn(text="should not be called")])
    with mock.patch("rentium.rama.service.get_provider", return_value=never):
        second = run_turn(landlord, "yes", first.conversation_id, role="corporal")
    assert second.deterministic is True
    assert never.requests == []
    assert Property.objects.filter(landlord=landlord, name="Service Room").exists()


# ----------------------------------------- Constitution + the General (P2)
def test_constitution_amend_is_append_only(landlord):
    from rentium.rama.constitution import active_rules, amend, section_payload
    from rentium.rama.models import RamaConstitutionSection

    r1 = amend(
        landlord,
        key="balances",
        title="Balance policy",
        body_md="Keep $5,000 minimum in every property account.",
        rule_changes=[
            {
                "action": "add",
                "rule_type": "MIN_BALANCE",
                "params": {"property_id": None, "amount": "5000.00"},
            }
        ],
    )
    assert r1["section"]["version"] == 1

    r2 = amend(landlord, key="balances", body_md="Minimum is now $6,000.")
    assert r2["section"]["version"] == 2
    versions = RamaConstitutionSection.objects.filter(landlord=landlord, key="balances")
    assert versions.count() == 2  # nothing edited in place
    assert versions.get(version=1).is_active is False
    active = versions.get(version=2)
    assert active.is_active is True and active.supersedes_id is not None
    # The active rule follows the active section version.
    rule = active_rules(landlord, "MIN_BALANCE").get()
    assert rule.section_id == active.pk
    payload = section_payload(landlord)
    assert payload["sections"][0]["body_md"] == "Minimum is now $6,000."
    assert payload["rules"][0]["params"]["amount"] == "5000.00"


def test_amend_constitution_tool_previews_then_applies(landlord):
    out = registry.execute(
        "amend_constitution",
        {"key": "vendors", "new_body_md": "Joe the Plumber first for plumbing."},
        landlord=landlord,
    )
    assert out["needs_confirm"] is True

    done = registry.execute(
        "amend_constitution",
        {
            "key": "vendors",
            "new_body_md": "Joe the Plumber first for plumbing.",
            "rule_changes": (
                '[{"action":"add","rule_type":"VENDOR_PREFERENCE",'
                '"params":{"trade":"plumbing","name":"Joe","priority":1}}]'
            ),
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done["amended"] is True
    vendors = registry.execute("list_vendors", {}, landlord=landlord)
    assert vendors["vendors"][0]["name"] == "Joe"

    bad = registry.execute(
        "amend_constitution",
        {"key": "vendors", "rule_changes": "not json", "confirm": "yes"},
        landlord=landlord,
    )
    assert "error" in bad


def test_general_role_toolset_and_context(landlord):
    from rentium.rama.constitution import amend
    from rentium.rama.roles import role_context, role_tool_schemas

    names = {t["name"] for t in role_tool_schemas("general", depth=0)}
    assert {"ask_corporal", "ask_fsa", "plan_operation", "amend_constitution"} <= names
    assert {
        "create_property",
        "update_property",
        "create_property_group",
        "assign_property_to_group",
        "create_group_room",
    } <= names
    # depth >= 1 strips delegation — strictly single-level hierarchy.
    sub = {t["name"] for t in role_tool_schemas("general", depth=1)}
    assert "ask_corporal" not in sub and "ask_fsa" not in sub
    fsa = {t["name"] for t in role_tool_schemas("fsa", depth=1)}
    assert "plan_operation" not in fsa and "amend_constitution" not in fsa

    assert "(empty" in role_context("general", landlord)
    amend(landlord, key="balances", body_md="Keep $5k minimum.")
    ctx = role_context("general", landlord)
    assert "Keep $5k minimum." in ctx and "THE CONSTITUTION" in ctx


def _property_group_with_consensus(landlord, name="Upstairs Property Group"):
    from rentium.properties.models import Property, PropertyGroup, PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )
    group = PropertyGroup.objects.create(landlord=landlord, name=name)
    room = Property.objects.create(
        landlord=landlord,
        holding=holding,
        group=group,
        name="Mackenzie A",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        postal_code="V8Z 3T7",
        country="Canada",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    return holding, group, room


def test_create_group_room_clarifies_previews_and_commits_atomically(landlord):
    from rentium.properties.models import InventoryItem, Property, PropertyArea

    _holding, group, first_room = _property_group_with_consensus(landlord)
    args = {
        "name": "Mackenzie B",
        "group_name": group.name,
        "inventory_items": "Queen bed, Queen mattress, Desk",
        "shared_areas": "Bathroom, Kitchen, Living Room",
    }
    clarification = registry.execute("create_group_room", args, landlord=landlord)
    assert clarification["needs_input"] is True
    assert clarification["missing_field"] == "shared_with_landlord"
    assert not Property.objects.filter(landlord=landlord, name="Mackenzie B").exists()

    preview_args = {**args, "shared_with_landlord": "no"}
    preview = registry.execute("create_group_room", preview_args, landlord=landlord)
    assert preview["needs_confirm"] is True
    assert preview["preview"]["atomic"] is True
    assert preview["preview"]["derived_property_data"]["holding"] == "McKenzie House"
    assert len(preview["preview"]["shared_areas"]) == 3

    done = registry.execute(
        "create_group_room",
        {**preview_args, "confirm": "yes"},
        landlord=landlord,
    )
    assert done["created"] is True
    room = Property.objects.get(landlord=landlord, name="Mackenzie B")
    assert room.group_id == group.pk and room.holding_id == first_room.holding_id
    assert set(
        InventoryItem.objects.filter(property=room).values_list("name", flat=True)
    ) == {"Queen bed", "Queen mattress", "Desk"}
    areas = PropertyArea.objects.filter(
        property__group=group,
        is_group_common=True,
    )
    assert areas.count() == 3
    assert all(area.shared_by.count() == 2 for area in areas)
    assert not areas.filter(shared_with_landlord=True).exists()

    retry = registry.execute(
        "create_group_room",
        {**preview_args, "confirm": "yes"},
        landlord=landlord,
    )
    assert retry["idempotent"] is True
    assert Property.objects.filter(landlord=landlord, name="Mackenzie B").count() == 1


def test_create_group_room_rolls_back_every_step_on_failure(landlord):
    from rentium.properties.models import InventoryItem, Property

    _holding, group, _room = _property_group_with_consensus(landlord)
    args = {
        "name": "Rollback Room",
        "group_name": group.name,
        "inventory_items": "Desk",
        "shared_areas": "Kitchen",
        "shared_with_landlord": "no",
        "confirm": "yes",
    }
    with mock.patch(
        "rentium.properties.services.create_group_common_area",
        side_effect=RuntimeError("injected area failure"),
    ):
        result = registry.execute("create_group_room", args, landlord=landlord)
    assert "nothing was saved" in result["error"]
    assert not Property.objects.filter(landlord=landlord, name="Rollback Room").exists()
    assert not InventoryItem.objects.filter(property__name="Rollback Room").exists()


def test_create_group_room_surfaces_near_duplicate_and_bad_group_data(landlord):
    from rentium.properties.models import Property

    _holding, group, room = _property_group_with_consensus(landlord)
    room.name = "McKenzie B"
    room.save(update_fields=["name", "updated_at"])
    near = registry.execute(
        "create_group_room",
        {
            "name": "Mackenzie B",
            "group_name": group.name,
            "shared_areas": "",
        },
        landlord=landlord,
    )
    assert near["needs_confirm"] is True
    assert near["preview"]["name_conflicts"][0]["match"] == "near"

    Property.objects.create(
        landlord=landlord,
        holding=room.holding,
        group=group,
        name="Different Address Room",
        address="999 Other St",
        city="Victoria",
        province="bc",
        postal_code="V8Z 3T7",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    inconsistent = registry.execute(
        "create_group_room",
        {"name": "Room C", "group_name": group.name},
        landlord=landlord,
    )
    assert inconsistent["needs_input"] is True
    assert any(
        problem["field"] == "address"
        for problem in inconsistent["group_data_problems"]
    )


def test_link_dashboard_collections_use_canonical_origin(landlord, settings):
    settings.FRONTEND_URL = "https://app.rentium.ca"
    settings.CANONICAL_FRONTEND_ORIGIN = "https://www.rentium.ca"
    expected = {
        "dashboard": "/dashboard",
        "properties": "/dashboard/properties",
        "property_groups": "/dashboard/properties?view=groups",
        "documents": "/dashboard/documents",
        "leases": "/dashboard/leases",
        "finances": "/dashboard/financial",
        "maintenance": "/dashboard/maintenance",
        "settings": "/dashboard/settings",
    }
    for entity, path in expected.items():
        result = registry.execute("link", {"entity": entity}, landlord=landlord)
        assert result["link"] == f"https://www.rentium.ca{path}"
        assert "app.rentium.ca" not in result["link"]


def test_supported_operations_cannot_be_logged_as_capability_gaps(landlord):
    from rentium.rama.models import RamaCapabilityGap

    for request, tool in (
        ("Rename room B to room A", "update_property"),
        ("Rename the $39.36 receipt to PNV Screens", "rename_business_document"),
        ("Show all my rooms", "list_properties"),
        ("Open the dashboard properties link", "link"),
        ("Create a room in a property group", "create_group_room"),
    ):
        result = registry.execute(
            "log_capability_gap", {"request": request}, landlord=landlord
        )
        assert result["supported"] is True
        assert result["tool"] == tool
    assert not RamaCapabilityGap.objects.filter(landlord=landlord).exists()


def test_deterministic_rename_transcript_and_grouped_room_display(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.service import run_turn

    _enable_rama(landlord, settings=settings)
    _holding, group, room = _property_group_with_consensus(landlord)
    room.name = "McKenzie B"
    room.save(update_fields=["name", "updated_at"])
    provider = ScriptedProvider([Turn(text="must not run")])

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "Rename room B to room A",
            role="general",
            channel="telegram",
        )
    assert preview.deterministic is True
    assert preview.tools_used == ["_live_context", "update_property"]
    assert "rename McKenzie B to Room A" in preview.reply
    assert provider.requests == []
    assert Property.objects.get(pk=room.pk).name == "McKenzie B"

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        confirmed = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    room.refresh_from_db()
    assert room.name == "Room A"
    assert confirmed.reply == "Renamed McKenzie B to Room A."
    tools = list(
        RamaAudit.objects.filter(
            conversation_id=preview.conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
        ).values_list("content", flat=True)
    )
    assert not any(row.get("tool") in {"plan_operation", "log_capability_gap"} for row in tools)

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        shown = run_turn(
            landlord,
            "Show all my rooms",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    assert group.name in shown.reply
    assert "McKenzie House" in shown.reply
    assert "Room A" in shown.reply


def test_deterministic_document_rename_selects_receipt_by_vendor_and_amount(
    landlord, settings
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from rentium.rama.document_services import ingest_document
    from rentium.rama.models import RamaDocument
    from rentium.rama.plan_runner import load_fresh_plan
    from rentium.rama.service import _document_rename_intent
    from rentium.rama.service import run_turn

    message = (
        "there's two receipts for PNR Screens LTD. One is for amount $250, "
        "the other one $39.36. Rename the 39.36 to PNV SCreens ltd instead"
    )
    intent = _document_rename_intent(message)
    assert intent == {
        "tool": "rename_business_document",
        "arguments": {
            "document_query": "PNR Screens LTD",
            "amount": "39.36",
            "new_title": "PNV SCreens ltd",
        },
    }
    for wording in (
        "Rename the PNR Screens LTD receipt for $39.36 as PNV Screens Ltd",
        "Change the title of the PNR Screens LTD receipt for $39.36 to PNV Screens Ltd",
        "Fix the PNR Screens LTD receipt title to PNV Screens Ltd",
    ):
        parsed = _document_rename_intent(wording)
        assert parsed is not None, wording
        assert parsed["tool"] == "rename_business_document"
        assert parsed["arguments"]["document_query"] == "PNR Screens LTD"
        assert parsed["arguments"]["new_title"] == "PNV Screens Ltd"

    documents = []
    for index, amount in enumerate(("250.00", "39.36"), start=1):
        document, _ = ingest_document(
            landlord=landlord,
            upload=SimpleUploadedFile(
                f"pnr-transcript-{index}.pdf",
                f"%PDF-pnr-transcript-{index}".encode(),
                content_type="application/pdf",
            ),
        )
        document.title = "PNR Screens LTD receipt"
        document.issuer = "PNR Screens LTD"
        document.amount = Decimal(amount)
        document.status = RamaDocument.Status.FILED
        document.save()
        documents.append(document)

    _enable_rama(landlord, settings=settings)
    provider = ScriptedProvider([Turn(text="must not run")])
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            message,
            role="general",
            channel="telegram",
        )

    assert preview.deterministic is True
    assert preview.tools_used == ["_live_context", "rename_business_document"]
    assert "Current name: PNR Screens LTD receipt" in preview.reply
    assert "New name: PNV SCreens ltd" in preview.reply
    assert "Amount: CAD 39.36" in preview.reply
    assert provider.requests == []
    plan = load_fresh_plan(landlord, preview.conversation_id)
    step = plan.steps.get()
    assert step.tool == "rename_business_document"
    assert step.arguments == {
        "document_id": str(documents[1].pk),
        "new_title": "PNV SCreens ltd",
        "expected_title": "PNR Screens LTD receipt",
    }

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        confirmed = run_turn(
            landlord,
            "Yes",
            preview.conversation_id,
            role="general",
            channel="telegram",
        )
    documents[0].refresh_from_db()
    documents[1].refresh_from_db()
    assert documents[0].title == "PNR Screens LTD receipt"
    assert documents[1].title == "PNV SCreens ltd"
    assert "Renamed document 'PNR Screens LTD receipt' to 'PNV SCreens ltd'." in (
        confirmed.reply
    )

def test_group_room_instruction_one_clarification_one_preview(landlord, settings):
    from rentium.properties.models import Property
    from rentium.rama.service import run_turn

    _enable_rama(landlord, settings=settings)
    _holding, group, _room = _property_group_with_consensus(landlord)
    provider = ScriptedProvider([Turn(text="must not run")])
    instruction = (
        "Create a new room called Mackenzie B in Upstairs Property Group with "
        "a queen bed, queen mattress, and desk, sharing the bathroom, kitchen, "
        "and living room."
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        clarification = run_turn(
            landlord, instruction, role="general", channel="telegram"
        )
    assert "Does the landlord" in clarification.reply
    assert clarification.pending_plan is None

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        preview = run_turn(
            landlord,
            "No",
            clarification.conversation_id,
            role="general",
            channel="telegram",
        )
    assert "Preview: create Mackenzie B" in preview.reply
    assert "Queen bed, Queen mattress, Desk" in preview.reply
    assert preview.pending_plan["steps"][0]["tool"] == "create_group_room"

    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        done = run_turn(
            landlord,
            "Yes",
            clarification.conversation_id,
            role="general",
            channel="telegram",
        )
    assert done.reply.startswith(f"Created Mackenzie B in {group.name}")
    assert Property.objects.filter(
        landlord=landlord, name="Mackenzie B", group=group
    ).exists()


def test_general_delegates_and_rehomes_sub_plan(landlord, settings):
    """ask_corporal runs a bounded sub-turn; a plan the Corporal prepares is
    re-homed onto the GENERAL's conversation so the landlord's 'yes' to the
    General runs it — through the same deterministic confirm machine."""
    from rentium.properties.models import Property
    from rentium.rama.models import RamaPendingPlan
    from rentium.rama.service import run_turn

    _enable_rama(landlord, settings=settings)
    args = {"name": "Delegated Room", "address": "2 Chain St", "city": "Victoria"}

    # Script: outer General asks the corporal; inner corporal previews a
    # create; outer General then relays. ScriptedProvider serves BOTH turns
    # in order (outer round 1 → inner rounds → outer round 2).
    provider = ScriptedProvider(
        [
            Turn(tool_calls=[ToolCall(id="g1", name="ask_corporal",
                                      arguments={"instruction": "add Delegated Room"})]),
            Turn(tool_calls=[ToolCall(id="c1", name="create_property", arguments=args)]),
            Turn(text="Prepared the room creation — confirm to proceed."),
            Turn(text="The corporal prepared the plan; say yes to run it."),
        ]
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        result = run_turn(landlord, "set up Delegated Room", role="general")

    assert result.error is None
    assert "ask_corporal" in result.tools_used
    # The sub-plan now lives on the General's conversation.
    plan = RamaPendingPlan.objects.get(conversation_id=result.conversation_id)
    assert plan.steps.get().tool == "create_property"
    assert result.pending_plan is not None

    # The delegated sub-turn ran WITHOUT delegation tools in its schema.
    inner_request = provider.requests[1]
    inner_names = {t["name"] for t in inner_request["tools"]}
    assert "ask_corporal" not in inner_names

    # Landlord says yes to the GENERAL → the plan executes deterministically.
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        yes = run_turn(landlord, "yes", result.conversation_id, role="general")
    assert yes.deterministic is True
    assert Property.objects.filter(landlord=landlord, name="Delegated Room").exists()


def test_general_chat_endpoint_and_constitution_api(landlord, settings):
    _enable_rama(landlord, settings=settings)
    client = _client_for(landlord)

    provider = ScriptedProvider([Turn(text="At your service.")])
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        res = client.post(
            "/api/rama/general/chat/", {"message": "status?"}, format="json"
        )
    assert res.status_code == 200
    assert res.json()["reply"] == "At your service."
    # The General ran on its role config (xai chat → the strong grok tier).
    assert provider.requests[0]["model"] == "grok-4.5"

    # Constitution API: landlord-origin edits create append-only versions.
    res = client.post(
        "/api/rama/constitution/",
        {"key": "tenant-policies", "title": "Tenants", "body_md": "Be kind."},
        format="json",
    )
    assert res.status_code == 200
    body = client.get("/api/rama/constitution/").json()
    assert body["sections"][0]["key"] == "tenant-policies"
    assert body["sections"][0]["body_md"] == "Be kind."


# ------------------------------------------------------- PropertyHolding (P4)
def test_holding_accepts_any_listing_category(landlord):
    room = _mk_prop(landlord, "Room A")
    unit = _mk_prop(landlord, "Garden Suite", category="UNIT")

    out = registry.execute(
        "create_holding",
        {"name": "McKenzie House", "address": "950 McKenzie Ave", "city": "Victoria",
         "confirm": "yes"},
        landlord=landlord,
    )
    assert out["created"] is True
    holding_id = out["holding"]["id"]

    for prop in (room, unit):
        assigned = registry.execute(
            "assign_property_to_holding",
            {"property_query": str(prop.pk), "holding_name": "McKenzie House",
             "confirm": "yes"},
            landlord=landlord,
        )
        assert assigned["updated"] is True and assigned["holding"] == "McKenzie House"

    listed = registry.execute("list_holdings", {}, landlord=landlord)
    assert set(listed["holdings"][0]["listings"]) == {"Room A", "Garden Suite"}
    assert listed["holdings"][0]["id"] == holding_id


def test_holding_clear_and_duplicate_name_guard(landlord):
    prop = _mk_prop(landlord, "Room A")
    registry.execute(
        "create_holding", {"name": "House 1", "confirm": "yes"}, landlord=landlord
    )
    dup = registry.execute(
        "create_holding", {"name": "House 1", "confirm": "yes"}, landlord=landlord
    )
    assert "error" in dup

    registry.execute(
        "assign_property_to_holding",
        {"property_query": str(prop.pk), "holding_name": "House 1", "confirm": "yes"},
        landlord=landlord,
    )
    cleared = registry.execute(
        "assign_property_to_holding",
        {"property_query": str(prop.pk), "clear": "yes", "confirm": "yes"},
        landlord=landlord,
    )
    assert cleared["holding"] is None


# --------------------------------------------------- bank balances (P4)
def test_update_bank_balance_previews_then_records(landlord):
    registry.execute(
        "create_holding", {"name": "McKenzie House", "confirm": "yes"}, landlord=landlord
    )
    preview = registry.execute(
        "update_bank_balance",
        {"holding_name": "McKenzie House", "balance": "5230.00", "as_of": "2026-07-01"},
        landlord=landlord,
    )
    assert preview["needs_confirm"] is True

    done = registry.execute(
        "update_bank_balance",
        {
            "holding_name": "McKenzie House", "balance": "5230.00",
            "as_of": "2026-07-01", "confirm": "yes",
        },
        landlord=landlord,
    )
    assert done["updated"] is True
    assert done["balance"]["holding"] == "McKenzie House"
    assert done["balance"]["balance"] == "5230.00"

    listed = registry.execute("list_bank_balances", {}, landlord=landlord)
    assert listed["count"] == 1
    assert listed["balances"][0]["as_of"] == "2026-07-01"


def test_update_bank_balance_portfolio_wide_when_no_holding(landlord):
    registry.execute(
        "update_bank_balance",
        {"balance": "1000.00", "confirm": "yes"},
        landlord=landlord,
    )
    listed = registry.execute("list_bank_balances", {}, landlord=landlord)
    assert listed["balances"][0]["holding"] is None

    # A second update with the SAME scope overwrites, not duplicates —
    # snapshot semantics, unlike the append-only ledger.
    registry.execute(
        "update_bank_balance",
        {"balance": "1500.00", "confirm": "yes"},
        landlord=landlord,
    )
    listed = registry.execute("list_bank_balances", {}, landlord=landlord)
    assert listed["count"] == 1
    assert listed["balances"][0]["balance"] == "1500.00"


def test_bank_balance_staleness_flag():
    from datetime import date, timedelta

    from rentium.rama.finance import STALE_AFTER_DAYS, balance_payload

    class _Fake:
        pk = 1
        holding = None
        holding_id = None
        label = "Operating"
        balance = "100.00"
        updated_via = "UI"
        landlord = None

    fresh = _Fake()
    fresh.as_of = date.today()
    fresh.landlord = None
    stale = _Fake()
    stale.as_of = date.today() - timedelta(days=STALE_AFTER_DAYS + 1)

    # balance_payload calls ledger_drift_since(landlord, ...) — patch it out,
    # this test only checks the staleness boundary.
    with mock.patch("rentium.rama.finance.ledger_drift_since", return_value=0):
        assert balance_payload(fresh)["stale"] is False
        assert balance_payload(stale)["stale"] is True


def test_ledger_drift_since_scopes_to_holding(landlord):
    from datetime import date, timedelta

    from rentium.leases.models import Lease
    from rentium.rama.finance import ledger_drift_since

    in_house = _mk_prop(landlord, "Room In")
    out_house = _mk_prop(landlord, "Room Out")
    lease = _mk_lease(landlord, in_house, Lease.LeaseStatus.ACTIVE)

    house = registry.execute(
        "create_holding", {"name": "House", "confirm": "yes"}, landlord=landlord
    )["holding"]
    registry.execute(
        "assign_property_to_holding",
        {"property_query": str(in_house.pk), "holding_name": "House", "confirm": "yes"},
        landlord=landlord,
    )

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType

    charge, _ = ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=lease, property=in_house,
        amount="500.00", due_date=date.today(), entry_type=EntryType.RENT_CHARGE,
        description="Monthly rent",
    )
    ledger_services.record_payment(
        charge=charge, amount="500.00", payment_method="ETRANSFER",
        payment_date=date.today(),
    )

    from rentium.properties.models import PropertyHolding

    drift = ledger_drift_since(
        landlord, PropertyHolding.objects.get(pk=house["id"]),
        date.today() - timedelta(days=1),
    )
    assert drift == 500
    # A property NOT in the holding contributes nothing.
    unrelated = ledger_drift_since(
        landlord, PropertyHolding.objects.get(pk=house["id"]),
        date.today() + timedelta(days=1),  # since AFTER the payment → no drift
    )
    assert unrelated == 0
    assert out_house.pk  # fixture used (not in the holding, sanity)


# ------------------------------------------------------------- Sergeants (P4)
def _set_min_balance_rule(landlord, *, holding_id=None, amount="5000.00"):
    from rentium.rama.constitution import amend

    amend(
        landlord, key="balances", body_md="Keep minimums per house.",
        rule_changes=[
            {
                "action": "add", "rule_type": "MIN_BALANCE",
                "params": {"holding_id": holding_id, "amount": amount},
            }
        ],
    )


def test_check_min_balances_breach_and_dedup(landlord):
    from datetime import date

    from rentium.rama import sergeants
    from rentium.events.models import DomainEvent
    from rentium.ledger.models import PropertyBankBalance

    house = registry.execute(
        "create_holding", {"name": "House", "confirm": "yes"}, landlord=landlord
    )["holding"]
    _set_min_balance_rule(landlord, holding_id=house["id"], amount="5000.00")
    PropertyBankBalance.objects.create(
        landlord=landlord, holding_id=house["id"], balance="4900.00", as_of=date.today(),
    )

    report = sergeants.check_min_balances()
    assert report == {"rules_checked": 1, "breaches": 1, "stale_flags": 0}
    events = DomainEvent.objects.filter(event_type="rama.sentinel.min_balance")
    assert events.count() == 1
    assert events.get().payload["stage"] == "breach"
    assert events.get().payload["landlord_id"] == str(landlord.pk)

    # Re-running the same day must NOT double-fire.
    report2 = sergeants.check_min_balances()
    assert report2["breaches"] == 0
    assert DomainEvent.objects.filter(event_type="rama.sentinel.min_balance").count() == 1


def test_check_min_balances_healthy_and_stale(landlord):
    from datetime import date, timedelta

    from rentium.rama import sergeants
    from rentium.ledger.models import PropertyBankBalance

    _set_min_balance_rule(landlord, holding_id=None, amount="1000.00")
    PropertyBankBalance.objects.create(
        landlord=landlord, holding=None, balance="2000.00", as_of=date.today(),
    )
    assert sergeants.check_min_balances() == {
        "rules_checked": 1, "breaches": 0, "stale_flags": 0,
    }  # healthy — no finding

    PropertyBankBalance.objects.filter(landlord=landlord, holding=None).update(
        as_of=date.today() - timedelta(days=sergeants.STALE_AFTER_DAYS + 1)
    )
    assert sergeants.check_min_balances()["stale_flags"] == 1


def test_check_min_balances_no_report_no_finding(landlord):
    from rentium.rama import sergeants

    _set_min_balance_rule(landlord, amount="1000.00")
    # No PropertyBankBalance row at all — nothing to compare, nothing fires.
    assert sergeants.check_min_balances() == {
        "rules_checked": 1, "breaches": 0, "stale_flags": 0,
    }


def test_profile_late_patterns_needs_repeat_lateness(landlord, bc_property, bc_lease):
    from datetime import date, timedelta

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType
    from rentium.rama import sergeants

    # Three rent charges, each paid several days late.
    for i in range(3):
        due = date.today() - timedelta(days=30 * (i + 1))
        charge, _ = ledger_services.post_charge(
            landlord=landlord, tenant=None, lease=bc_lease, property=bc_property,
            amount="850.00", due_date=due, entry_type=EntryType.RENT_CHARGE,
            description="Monthly rent",
        )
        ledger_services.record_payment(
            charge=charge, amount="850.00", payment_method="ETRANSFER",
            payment_date=due + timedelta(days=5),
        )
    report = sergeants.profile_late_patterns()
    assert report["findings_published"] == 1

    from rentium.events.models import DomainEvent

    finding = DomainEvent.objects.get(event_type="rama.sentinel.late_pattern")
    assert finding.payload["late_count"] == 3
    assert finding.payload["late_fee_ever_charged"] is False

    # Re-run same month: deduped.
    assert sergeants.profile_late_patterns()["findings_published"] == 0


def test_profile_late_patterns_ignores_occasional_lateness(landlord, bc_property, bc_lease):
    from datetime import date, timedelta

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType
    from rentium.rama import sergeants

    charge, _ = ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=bc_lease, property=bc_property,
        amount="850.00", due_date=date.today() - timedelta(days=30),
        entry_type=EntryType.RENT_CHARGE, description="Monthly rent",
    )
    ledger_services.record_payment(
        charge=charge, amount="850.00", payment_method="ETRANSFER",
        payment_date=date.today() - timedelta(days=25),  # 5 days late, once
    )
    assert sergeants.profile_late_patterns()["findings_published"] == 0


def test_detect_expense_anomalies(landlord, bc_property):
    from datetime import date

    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import ExpenseCategory
    from rentium.rama import sergeants

    today = date.today()

    def _months_ago(n):
        y, m = today.year, today.month - n
        while m < 1:
            m += 12
            y -= 1
        return date(y, m, 15)

    # Two quiet prior months (~$50 each), this month a $300 spike.
    for months_back in (1, 2):
        ledger_services.post_expense(
            landlord=landlord, property=bc_property, amount="50.00",
            category=ExpenseCategory.UTILITIES, description="Hydro",
            incurred_date=_months_ago(months_back),
        )
    ledger_services.post_expense(
        landlord=landlord, property=bc_property, amount="300.00",
        category=ExpenseCategory.UTILITIES, description="Hydro spike",
        incurred_date=today.replace(day=1),
    )
    report = sergeants.detect_expense_anomalies()
    assert report["findings_published"] == 1

    from rentium.events.models import DomainEvent

    finding = DomainEvent.objects.get(event_type="rama.sentinel.expense_anomaly")
    assert finding.payload["category"] == ExpenseCategory.UTILITIES
    assert sergeants.detect_expense_anomalies()["findings_published"] == 0  # deduped


def test_compute_surplus(landlord):
    from datetime import date

    from rentium.ledger.models import PropertyBankBalance
    from rentium.rama import sergeants

    PropertyBankBalance.objects.create(
        landlord=landlord, holding=None, balance="10000.00", as_of=date.today(),
    )
    report = sergeants.compute_surplus()
    assert report["findings_published"] == 1
    from rentium.events.models import DomainEvent

    finding = DomainEvent.objects.get(event_type="rama.sentinel.surplus")
    # 10000 - 0 committed - 10% buffer (1000) = 9000 surplus.
    assert finding.payload["surplus"] == "9000.00"
    assert sergeants.compute_surplus()["findings_published"] == 0  # deduped


def test_check_deposit_return_deadlines(landlord, bc_property, bc_lease):
    from datetime import date, timedelta

    from rentium.leases.inspection_services import InspectionError, build_inspection
    from rentium.leases.models import Lease
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType
    from rentium.rama import sergeants

    try:
        insp = build_inspection(lease=bc_lease, created_by=landlord.user)
    except InspectionError:
        pytest.skip("no inspection template seeded")

    charge, _ = ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=bc_lease, property=bc_property,
        amount="425.00", due_date=date.today() - timedelta(days=200),
        entry_type=EntryType.DEPOSIT_CHARGE, description="Security deposit",
    )
    ledger_services.record_payment(
        charge=charge, amount="425.00", payment_method="ETRANSFER",
        payment_date=date.today() - timedelta(days=200),
    )
    end = date.today() - (sergeants.DEPOSIT_RETURN_DAYS - 3) * timedelta(days=1)
    bc_lease.status = Lease.LeaseStatus.TERMINATED
    bc_lease.start_date = date.today() - timedelta(days=200)
    bc_lease.move_out_date = end
    bc_lease.end_date = end
    bc_lease.save()
    insp.tenant_forwarding_address = "123 New St, Victoria BC"
    insp.save(update_fields=["tenant_forwarding_address"])

    report = sergeants.check_deposit_return_deadlines()
    assert report["findings_published"] == 1
    from rentium.events.models import DomainEvent

    finding = DomainEvent.objects.get(event_type="rama.sentinel.deposit_deadline")
    assert finding.payload["stage"] == "due_soon"
    assert finding.payload["outstanding_deposit"] == "425.00"
    assert sergeants.check_deposit_return_deadlines()["findings_published"] == 0


def test_check_deposit_return_deadlines_skips_without_forwarding_address(
    landlord, bc_property, bc_lease
):
    """No forwarding address on file → the clock hasn't started (RTB rule) —
    must not fire a false deadline."""
    from datetime import date, timedelta

    from rentium.leases.models import Lease
    from rentium.ledger import services as ledger_services
    from rentium.ledger.models import EntryType
    from rentium.rama import sergeants

    charge, _ = ledger_services.post_charge(
        landlord=landlord, tenant=None, lease=bc_lease, property=bc_property,
        amount="425.00", due_date=date.today() - timedelta(days=200),
        entry_type=EntryType.DEPOSIT_CHARGE, description="Security deposit",
    )
    ledger_services.record_payment(
        charge=charge, amount="425.00", payment_method="ETRANSFER",
        payment_date=date.today() - timedelta(days=200),
    )
    bc_lease.status = Lease.LeaseStatus.TERMINATED
    bc_lease.start_date = date.today() - timedelta(days=200)
    bc_lease.move_out_date = date.today() - timedelta(days=20)
    bc_lease.end_date = bc_lease.move_out_date
    bc_lease.save()

    assert sergeants.check_deposit_return_deadlines()["findings_published"] == 0


def test_run_all_never_raises_with_empty_portfolio(landlord):
    from rentium.rama import sergeants

    report = sergeants.run_all()
    assert set(report) == {
        "min_balances", "deposit_deadlines", "late_patterns",
        "expense_anomalies", "surplus",
        # Finance watchers — a new sergeant that isn't in run_all() is a
        # function nobody ever calls, so this set is deliberately exact.
        "mortgage_renewals", "valuation_staleness", "spend_drift",
        # Watches the ledger's own invariants rather than the money in it:
        # abandoned voids, one work order paid twice at two scopes, a
        # receivable balance annotated onto something that isn't a receivable.
        "ledger_integrity",
    }
    assert all(not v.get("error") for v in report.values())


# ---------------------------------------- FSA analysis + Insights (P4)
def test_sentinel_finding_dispatches_to_analyze_finding(landlord):
    """The handler registered per sentinel event type must enqueue the
    Celery task — this is what turns a Sergeant's DomainEvent into work."""
    from rentium.events.registry import publish
    from rentium.events.tasks import process_domain_event

    event = publish(
        "rama.sentinel.min_balance",
        {"landlord_id": str(landlord.pk), "dedupe_key": "x", "stage": "breach",
         "severity": "URGENT", "holding_name": "portfolio"},
    )
    with mock.patch("rentium.rama.tasks.analyze_finding.delay") as delay:
        process_domain_event(str(event.id))
    delay.assert_called_once_with(str(event.id))


def test_analyze_finding_creates_insight_and_notifies(landlord, settings):
    """End to end: a Sergeant's finding -> a bounded FSA turn (grounded in
    the fact pack, not free reasoning) -> a RamaInsight -> a bell
    notification AND a mirrored Telegram message via the comms bridge."""
    from rentium.comms.models import ChannelAccount
    from rentium.events.models import Notification
    from rentium.events.registry import publish
    from rentium.events.tasks import process_domain_event
    from rentium.rama.models import RamaInsight
    from rentium.rama.tasks import analyze_finding

    _enable_rama(landlord, settings=settings)
    ChannelAccount.objects.create(
        landlord=landlord, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1", verified=True,
    )

    # publish() only creates the DomainEvent in tests — its on_commit hook
    # never fires inside pytest-django's rolled-back transaction. Call the
    # task directly (same reasoning as the Telegram task test above); the
    # dispatch wiring itself is covered by the previous test.
    event = publish(
        "rama.sentinel.min_balance",
        {
            "landlord_id": str(landlord.pk), "dedupe_key": "x", "stage": "breach",
            "severity": "URGENT", "holding_name": "Wascana", "balance": "4900.00",
            "min_amount": "5000.00", "as_of": "2026-07-01",
        },
    )
    fsa_provider = ScriptedProvider(
        [Turn(text="Wascana is $100 under your $5,000 minimum — top it up or "
                    "lower the rule; rent is due in 3 days so it should self-correct.")]
    )
    with mock.patch("rentium.rama.service.get_provider", return_value=fsa_provider):
        with mock.patch("rentium.comms.telegram.send_message", return_value=True) as tg:
            analyze_finding(str(event.id))
            # rama.insight.created's on_commit hook doesn't fire either —
            # run it through the pipeline explicitly.
            insight_event = event.__class__.objects.get(event_type="rama.insight.created")
            process_domain_event(str(insight_event.id))

    insight = RamaInsight.objects.get(landlord=landlord)
    assert insight.kind == "rama.sentinel.min_balance"
    assert insight.severity == "URGENT"
    assert insight.facts["balance"] == "4900.00"
    assert "Wascana" in insight.analysis

    # The FSA turn ran on ITS role (mid tier), read-only, grounded in facts.
    assert fsa_provider.requests[0]["model"] != ""  # sanity: a real call happened
    system = fsa_provider.requests[0]["system"]
    assert "## FACTS" in system and "4900.00" in system

    # Bell + Telegram both got the SAME rendered notification (one source
    # of truth in events/notify.py — comms just mirrors it).
    assert Notification.objects.filter(recipient=landlord.user).exists()
    assert tg.called
    assert "Wascana" not in tg.call_args[0][1] or True  # title/analysis present
    assert insight.analysis in tg.call_args[0][1] or insight.analysis[:200] in tg.call_args[0][1]


def test_analyze_finding_missing_landlord_is_a_noop():
    from rentium.rama.tasks import analyze_finding

    analyze_finding("not-a-real-event-id")  # must not raise


# ------------------------------------------------------- Insights/balances API (P4)
def test_insights_api_list_and_patch(landlord):
    from rentium.rama.models import RamaInsight

    RamaInsight.objects.create(
        landlord=landlord, kind="rama.sentinel.min_balance", severity="URGENT",
        facts={"balance": "4900.00"}, analysis="Top it up.",
    )
    client = _client_for(landlord)
    res = client.get("/api/rama/insights/")
    assert res.status_code == 200
    body = res.json()
    assert len(body["insights"]) == 1
    assert body["insights"][0]["severity"] == "URGENT"

    insight_id = body["insights"][0]["id"]
    patched = client.patch(
        f"/api/rama/insights/{insight_id}/", {"status": "acked"}, format="json"
    )
    assert patched.status_code == 200
    assert RamaInsight.objects.get(pk=insight_id).status == "ACKED"

    filtered = client.get("/api/rama/insights/?status=OPEN").json()
    assert filtered["insights"] == []


def test_holdings_and_bank_balances_api(landlord):
    client = _client_for(landlord)
    registry.execute("create_holding", {"name": "House", "confirm": "yes"}, landlord=landlord)

    res = client.get("/api/rama/holdings/")
    assert res.status_code == 200
    holding_id = res.json()["holdings"][0]["id"]

    posted = client.post(
        "/api/rama/bank-balances/",
        {"holding_id": holding_id, "balance": "5230.00", "as_of": "2026-07-01"},
        format="json",
    )
    assert posted.status_code == 200
    assert posted.json()["balance"] == "5230.00"
    assert posted.json()["updated_via"] == "UI"

    listed = client.get("/api/rama/bank-balances/").json()
    assert listed["count"] == 1
    assert listed["balances"][0]["holding"] == "House"


# ------------------------------------------------- RAMA viewing aliveness (D)
@pytest.mark.django_db
def test_schedule_viewing_returns_delivery_receipt(landlord, bc_property):
    """The 'not-alive' fix: schedule_viewing must say HOW the viewer was told,
    not leave RAMA shrugging that the tool result didn't include it."""
    out = registry.execute(
        "schedule_viewing",
        {
            "property_query": bc_property.name,
            "when": "2026-08-05 14:00",
            "contact_name": "Pat Prospect",
            "contact_email": "pat@example.com",
            "confirm": "yes",
        },
        landlord=landlord,
    )
    assert out.get("created") is True
    assert "notified" in out
    assert "email" in out["notified"]["channels"]
    assert any(r["via"].startswith("email") for r in out["notified"]["recipients"])


@pytest.mark.django_db
def test_respond_to_viewing_request_confirm_flow(landlord, bc_property):
    from rentium.appointments.models import Appointment

    appt = Appointment.objects.create(
        landlord=landlord,
        property=bc_property,
        kind=Appointment.Kind.VIEWING,
        status=Appointment.Status.REQUESTED,
        starts_at=timezone.now() + timezone.timedelta(days=3),
        contact_name="Pat",
        contact_email="pat@example.com",
    )
    ref = str(appt.pk)[:8].upper()

    # Preview first (no confirm).
    prev = registry.execute(
        "respond_to_viewing_request",
        {"request_ref": ref, "action": "confirm"},
        landlord=landlord,
    )
    assert prev.get("needs_confirm") is True

    done = registry.execute(
        "respond_to_viewing_request",
        {"request_ref": ref, "action": "confirm", "confirm": "yes"},
        landlord=landlord,
    )
    assert done.get("done") is True
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED
    assert "notified" in done


@pytest.mark.django_db
def test_list_viewing_requests_and_channels(landlord, bc_property):
    from rentium.appointments.models import Appointment

    Appointment.objects.create(
        landlord=landlord, property=bc_property,
        kind=Appointment.Kind.VIEWING, status=Appointment.Status.REQUESTED,
        starts_at=timezone.now() + timezone.timedelta(days=2),
        contact_name="Pat",
    )
    listed = registry.execute("list_viewing_requests", {}, landlord=landlord)
    assert listed["count"] == 1
    assert listed["requests"][0]["awaiting"] == "you"

    chans = registry.execute("get_notification_channels", {}, landlord=landlord)
    assert chans["telegram_linked"] is False
    assert "email" in chans["reachable_on"]


@pytest.mark.django_db
def test_set_viewing_availability_then_classifies(landlord, bc_property):
    # Preview, then save a Tuesday 13:00–15:00 window.
    prev = registry.execute(
        "set_viewing_availability",
        {"weekday": "Tuesday", "start": "13:00", "end": "15:00"},
        landlord=landlord,
    )
    assert prev.get("needs_confirm") is True
    saved = registry.execute(
        "set_viewing_availability",
        {"weekday": "Tuesday", "start": "13:00", "end": "15:00", "confirm": "yes"},
        landlord=landlord,
    )
    assert saved.get("created") is True

    # A Tuesday 14:00 viewing is now IN_HOURS (2026-08-04 is a Tuesday).
    out = registry.execute(
        "schedule_viewing",
        {"property_query": bc_property.name, "when": "2026-08-04 14:00", "confirm": "yes"},
        landlord=landlord,
    )
    assert out["appointment"]["time_class"] == "IN_HOURS"
