"""Goal-level regressions against real records, not model prose."""

import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from .daily_workflows import plan_daily_workflow
from .daily_workflows import prepare_attention
from .daily_workflows import resume_task
from .models import RamaPreferences
from .models import RamaTask
from .outcomes import CommandOutcome
from .outcomes import OutcomeKind
from .plan_runner import run_plan
from .plan_runner import save_plan

pytestmark = pytest.mark.django_db


@pytest.fixture
def bc_property(bc_property):
    bc_property.province = "bc"
    bc_property.save(update_fields=["province"])
    return bc_property


@pytest.fixture
def workflow_owner(landlord):
    RamaPreferences.objects.create(landlord=landlord, workflows_enabled=True)
    return landlord


def test_discovery_exposes_exact_arguments_for_workflow_planning(workflow_owner):
    from .registry import REGISTRY
    from .tools import search_capabilities

    result = search_capabilities(workflow_owner, "create_expense")
    expense = next(
        row for row in result["capabilities"] if row["key"] == "create_expense"
    )
    assert expense["parameters"] == REGISTRY["create_expense"].parameters
    assert "effective_date" in expense["parameters"]["properties"]
    assert "description" in expense["parameters"]["required"]


def proposal(owner, steps):
    result = plan_daily_workflow(owner, json.dumps(steps), "Daily work")
    assert "plan" in result, result
    return save_plan(owner, uuid.uuid4(), result["plan"])


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"partial": True, "steps_done": [{"created": True}]}, OutcomeKind.PARTIAL),
        ({"queued": True}, OutcomeKind.WAITING),
        ({"needs_input": True}, OutcomeKind.NEEDS_INPUT),
        ({"needs_confirm": True}, OutcomeKind.PREVIEW),
    ],
)
def test_nonterminal_results_are_never_completed(result, expected):
    assert CommandOutcome.from_tool_result(result).kind == expected


def test_property_workflow_verifies_and_duplicate_confirmation_returns_receipt(
    workflow_owner,
    bc_property,
):
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {
                    "property_query": str(bc_property.pk),
                    "name": "Verified Suite",
                },
            },
        ],
    )
    first = run_plan(plan, workflow_owner)
    assert first["status"] == "done", first
    bc_property.refresh_from_db()
    assert bc_property.name == "Verified Suite"
    task = RamaTask.objects.get(pk=first["task"]["id"])
    assert task.workflow_key == "property"
    assert task.receipts.get().verification["verified"] is True
    assert run_plan(plan, workflow_owner) == first
    assert task.receipts.count() == 1


def test_stale_preview_does_not_write(workflow_owner, bc_property):
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {
                    "property_query": str(bc_property.pk),
                    "name": "Proposed",
                },
            },
        ],
    )
    bc_property.name = "Changed elsewhere"
    bc_property.save()
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "failed"
    bc_property.refresh_from_db()
    assert bc_property.name == "Changed elsewhere"


def test_partial_composite_blocks_other_target_dependency(landlord, monkeypatch):
    from . import plan_runner

    calls = []

    def execute(tool, arguments, **kwargs):
        calls.append(tool)
        return {"partial": True, "steps_done": [{"lease_created": True}]}

    monkeypatch.setattr(plan_runner, "execute", execute)
    plan = save_plan(
        landlord,
        uuid.uuid4(),
        {
            "operation": "test",
            "steps": [
                {
                    "step_id": "first",
                    "tool": "create_property",
                    "arguments": {
                        "name": "One",
                        "address": "1 Test",
                        "city": "Victoria",
                    },
                    "item_key": "a",
                },
                {
                    "step_id": "second",
                    "depends_on": ["first"],
                    "tool": "create_property",
                    "arguments": {
                        "name": "Two",
                        "address": "2 Test",
                        "city": "Victoria",
                    },
                    "item_key": "b",
                },
            ],
        },
    )
    result = run_plan(plan, landlord)
    assert result["status"] == "partial"
    assert len(calls) == 1
    assert len(result["skipped"]) == 1
    assert RamaTask.objects.get(pk=result["task"]["id"]).status == "PARTIAL"


def test_fabricated_success_fails_database_verification(
    workflow_owner,
    bc_property,
    monkeypatch,
):
    from . import plan_runner

    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {
                    "property_query": str(bc_property.pk),
                    "name": "Never written",
                },
            },
        ],
    )
    monkeypatch.setattr(
        plan_runner,
        "execute",
        lambda *args, **kwargs: {
            "updated": True,
            "property": {"id": str(bc_property.pk)},
        },
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "partial"
    assert not RamaTask.objects.get(pk=result["task"]["id"]).receipts.exists()


def test_owner_isolation_for_tasks_and_execution(workflow_owner, bc_property, user):
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {"property_query": str(bc_property.pk), "name": "Owned"},
            },
        ],
    )
    from rentium.users.models import LandlordProfile

    other = LandlordProfile.objects.create(user=user)
    assert run_plan(plan, other)["status"] == "failed"
    client = APIClient()
    client.force_authenticate(user=user)
    assert client.get(f"/api/rama/tasks/{plan.task_id}/").status_code == 404
    assert client.get("/api/rama/tasks/").json()["tasks"] == []


def test_move_does_not_select_unrelated_tenant(workflow_owner, bc_property, bc_lease):
    from rentium.leases.models import LeaseTenant
    from rentium.properties.models import Property

    from .daily_workflows import prepare_tenant_move

    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Alice",
        invited_email="alice@example.test",
        rent_amount=850,
    )
    target = Property.objects.create(
        landlord=workflow_owner,
        name="Destination",
        address="2 Test",
        city="Victoria",
    )
    result = prepare_tenant_move(
        workflow_owner,
        "Bob",
        str(bc_property.pk),
        str(target.pk),
        str(timezone.localdate() + timedelta(days=30)),
    )
    assert result["needs_input"]
    bc_lease.refresh_from_db()
    assert bc_lease.status == "ACTIVE"
    assert not bc_lease.moveout_requests.exists()


def test_move_prepares_draft_and_waits_without_terminating(
    workflow_owner,
    bc_property,
    bc_lease,
):
    from rentium.leases.models import Lease
    from rentium.leases.models import LeaseTenant
    from rentium.properties.models import Property

    from .daily_workflows import prepare_tenant_move

    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_name="Alice",
        invited_email="alice@example.test",
        rent_amount=850,
    )
    target = Property.objects.create(
        landlord=workflow_owner,
        name="Destination",
        address="2 Test",
        city="Victoria",
        province="bc",
        property_category="COMPLETE_UNIT",
    )
    args = {
        "tenant": "Alice",
        "from_property": str(bc_property.pk),
        "to_property": str(target.pk),
        "start_date": str(timezone.localdate() + timedelta(days=30)),
        "total_rent": "850",
    }
    preview = prepare_tenant_move(workflow_owner, **args)
    assert preview.get("needs_confirm"), preview
    duplicate_draft = plan_daily_workflow(
        workflow_owner,
        json.dumps(
            [
                {
                    "tool": "create_lease",
                    "arguments": {
                        "property_query": str(target.pk),
                        "start_date": args["start_date"],
                        "total_rent": "850",
                        "is_month_to_month": "yes",
                    },
                },
                {"tool": "prepare_tenant_move", "arguments": args},
            ],
        ),
        "Move Alice",
    )
    assert "already creates the replacement draft" in duplicate_draft["error"]
    assert not Lease.objects.filter(property=target).exists()
    plan = proposal(
        workflow_owner,
        [{"tool": "prepare_tenant_move", "arguments": args}],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "waiting", json.dumps(result, default=str, indent=2)
    bc_lease.refresh_from_db()
    assert bc_lease.status == "ACTIVE"
    assert bc_lease.moveout_requests.get().status == "PENDING"
    assert Lease.objects.get(property=target).status == "DRAFT"


@pytest.mark.parametrize("daily", [True, False])
def test_workflow_model_cannot_bypass_planner(
    workflow_owner, bc_property, monkeypatch, daily
):
    from . import service
    from .models import RamaPendingPlan
    from .providers import ToolCall
    from .providers import Turn
    from .test_batch_confirmation import ScriptedProvider

    prefs = RamaPreferences.objects.get(landlord=workflow_owner)
    prefs.enabled = True
    prefs.provider = "openai"
    prefs.model = "gpt-5-mini"
    prefs.api_key = "test-key"
    prefs.save()
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="wrong",
                        name="update_lease",
                        arguments={
                            "property_query": str(bc_property.pk),
                            "total_rent": "925",
                        },
                    ),
                ],
            ),
            Turn(text="No changes prepared."),
        ],
    )
    monkeypatch.setattr(service, "get_provider", lambda *_: provider)
    result = service.run_turn(
        workflow_owner,
        "Prepare some daily work for this listing."
        if daily
        else "There is a leaking faucet at this listing; create a maintenance request.",
    )
    offered = {row["name"] for row in provider.requests[0]["tools"]}
    if daily:
        assert "plan_daily_workflow" in offered
        assert "update_lease" not in offered
    else:
        assert "plan_daily_workflow" not in offered
        assert "create_work_order" in offered
    assert not RamaPendingPlan.objects.filter(
        conversation_id=result.conversation_id,
    ).exists()


def test_attention_is_deduplicated_and_clears_when_resolved(workflow_owner, bc_lease):
    from .models import RamaInsight

    bc_lease.status = "DRAFT"
    bc_lease.save()
    prepare_attention(workflow_owner)
    prepare_attention(workflow_owner)
    assert (
        RamaInsight.objects.filter(
            kind="rama.workflow.attention",
            status="OPEN",
        ).count()
        == 1
    )
    bc_lease.status = "ACTIVE"
    bc_lease.save()
    prepare_attention(workflow_owner)
    assert not RamaInsight.objects.filter(
        kind="rama.workflow.attention",
        status="OPEN",
    ).exists()


def test_model_chosen_id_cannot_resolve_a_duplicated_user_name(
    workflow_owner, monkeypatch
):
    from rentium.properties.models import Property

    from . import service
    from .models import RamaPendingPlan
    from .providers import ToolCall, Turn
    from .test_batch_confirmation import ScriptedProvider
    from .workflow_contracts import source_target_clarification

    props = [
        Property.objects.create(
            landlord=workflow_owner,
            name="Garden Suite",
            address=address,
            city="Victoria",
            province="bc",
            property_category="COMPLETE_UNIT",
        )
        for address in ("10 Example Road", "20 Example Road")
    ]
    steps = [
        {
            "tool": "update_property",
            "arguments": {
                "property_query": str(props[0].pk),
                "asking_rent": "925",
            },
        }
    ]
    prefs = RamaPreferences.objects.get(landlord=workflow_owner)
    prefs.enabled = True
    prefs.provider = "openai"
    prefs.model = "gpt-5-mini"
    prefs.api_key = "test-key"
    prefs.save()
    provider = ScriptedProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="guess",
                        name="plan_daily_workflow",
                        arguments={
                            "steps_json": json.dumps(steps),
                            "summary": "Set asking rent",
                        },
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(service, "get_provider", lambda *_: provider)
    result = service.run_turn(
        workflow_owner, "Set the asking rent of Garden Suite to $925."
    )
    assert not RamaPendingPlan.objects.filter(
        conversation_id=result.conversation_id
    ).exists()
    assert "10 Example Road" in result.reply
    assert "20 Example Road" in result.reply
    assert not source_target_clarification(
        workflow_owner,
        steps,
        "Set the asking rent of Garden Suite at 10 Example Road to $925.",
    )
    assert source_target_clarification(
        workflow_owner,
        steps,
        "Set the asking rent of Garden Suite at 20 Example Road to $925.",
    )
    continued = service.run_turn(
        workflow_owner,
        "20 Example Road",
        conversation_id=result.conversation_id,
    )
    plan = RamaPendingPlan.objects.get(conversation_id=continued.conversation_id)
    assert plan.steps.get().arguments["property_query"] == str(props[1].pk)
    assert plan.steps.get().arguments["asking_rent"] == "925"
    assert len(provider.requests) == 1


def test_expired_task_resumes_with_new_approval(workflow_owner, bc_property):
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {"property_query": str(bc_property.pk), "name": "Fresh"},
            },
        ],
    )
    task_id = plan.task_id
    plan.expires_at = timezone.now() - timedelta(seconds=1)
    plan.save()
    assert run_plan(plan, workflow_owner)["status"] == "failed"
    fresh = resume_task(workflow_owner, task_id, uuid.uuid4())
    assert fresh.plan_id != plan.plan_id
    assert fresh.task.parent_id == task_id
    bc_property.refresh_from_db()
    assert bc_property.name != "Fresh"
    assert run_plan(fresh, workflow_owner)["status"] == "done"


def test_onboarding_delivery_is_durable_and_retries_do_not_recreate_lease(
    workflow_owner,
    bc_property,
    monkeypatch,
):
    from django.core.management import call_command

    call_command("seed_inspection_templates", verbosity=0)
    from rentium.leases.models import Lease

    from .daily_workflows import onboarding_plan
    from .workflow_delivery import deliver_invitation
    from .workflow_delivery import refresh_waiting

    data = {
        "room_name": str(bc_property.pk),
        "start_date": str(timezone.localdate() + timedelta(days=30)),
        "total_rent": "850",
        "tenant_email": "alice@example.test",
        "tenant_name": "Alice",
        "create_inspection": "1",
    }
    result = onboarding_plan(workflow_owner, data)
    assert "plan" in result, result
    plan = save_plan(workflow_owner, uuid.uuid4(), result["plan"])
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "waiting", json.dumps(result, default=str, indent=2)
    task = RamaTask.objects.get(pk=result["task"]["id"])
    assert len(task.progress) == 3
    assert Lease.objects.filter(property=bc_property).count() == 1
    delivery = task.attempts.get(capability_key="deliver_tenant_invitation")
    sent = []
    monkeypatch.setattr(
        "rentium.showcase.emails.send_tenant_invite",
        lambda slot: sent.append(slot.pk) or True,
    )
    deliver_invitation(delivery.pk)
    deliver_invitation(delivery.pk)
    assert len(sent) == 1
    refresh_waiting(task)
    task.refresh_from_db()
    assert task.status == "WAITING"
    Lease.objects.filter(property=bc_property).update(status="ACTIVE")
    refresh_waiting(task)
    task.refresh_from_db()
    assert task.status == "VERIFIED"
    assert Lease.objects.filter(property=bc_property).count() == 1


def test_payment_workflow_verifies_exact_amount(workflow_owner, bc_lease):
    from decimal import Decimal

    from rentium.ledger.models import LedgerEntry
    from rentium.ledger.services import post_charge

    charge, _ = post_charge(
        landlord=workflow_owner,
        property=bc_lease.property,
        lease=bc_lease,
        entry_type="RENT_CHARGE",
        tenant=None,
        amount=Decimal("850"),
        due_date=timezone.localdate(),
        description="Workflow rent",
    )
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "record_payment",
                "arguments": {
                    "charge_ids": str(charge.pk),
                    "amount": "400",
                    "payment_method": "ETRANSFER",
                },
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    assert (
        LedgerEntry.objects.filter(settles=charge, entry_type="PAYMENT").get().amount
        == 400
    )
    assert (
        RamaTask.objects.get(pk=result["task"]["id"])
        .receipts.get()
        .verification["verified"]
    )


def test_expense_workflow_preserves_incurred_and_cleared_dates(workflow_owner):
    from rentium.ledger.models import LedgerEntry

    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "create_expense",
                "arguments": {
                    "amount": "40",
                    "description": "Portfolio software",
                    "effective_date": "2026-09-29",
                    "paid_on": "2026-10-02",
                    "category": "OTHER",
                },
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    row = LedgerEntry.objects.get(
        landlord=workflow_owner,
        description="Portfolio software",
    )
    assert str(row.effective_date) == "2026-09-29"
    assert str(row.paid_on) == "2026-10-02"


def test_structure_and_mode_switch_verify_real_layout(workflow_owner):
    from rentium.properties.models import PropertyUnit

    units = [
        {
            "name": "Main",
            "rental_mode": "WHOLE_UNIT",
            "spaces": [
                {"name": "Bedroom", "type": "BEDROOM"},
                {"name": "Bathroom", "type": "BATHROOM"},
            ],
        },
    ]
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "create_property_structure",
                "arguments": {
                    "holding_name": "Workflow House",
                    "address": "11 Test Street",
                    "city": "Regina",
                    "province": "sk",
                    "units_json": json.dumps(units),
                },
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    unit = PropertyUnit.objects.get(landlord=workflow_owner, name="Main")
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "set_unit_rental_mode",
                "arguments": {"unit_name": str(unit.pk), "rental_mode": "BY_ROOM"},
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    unit.refresh_from_db()
    assert unit.rental_mode == "BY_ROOM"
    assert not unit.active_offerings().exists()
    assert unit.offerings.filter(is_active_offering=False).exists()


def test_resume_skips_completed_steps_and_binds_their_ids(workflow_owner, monkeypatch):
    from . import plan_runner
    from .registry import execute as real_execute

    plan = proposal(
        workflow_owner,
        [
            {
                "step_id": "create",
                "tool": "create_property",
                "arguments": {
                    "name": "Original",
                    "address": "12 Test",
                    "city": "Victoria",
                },
            },
            {
                "step_id": "rename",
                "tool": "update_property",
                "depends_on": ["create"],
                "arguments": {
                    "property_query": {"$step": "create", "path": "property.id"},
                    "name": "Renamed",
                },
            },
        ],
    )
    monkeypatch.setattr(
        plan_runner,
        "execute",
        lambda tool, args, **kw: {"error": "Temporary failure"}
        if tool == "update_property"
        else real_execute(tool, args, **kw),
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "partial", result
    monkeypatch.setattr(plan_runner, "execute", real_execute)
    fresh = resume_task(workflow_owner, result["task"]["id"], uuid.uuid4())
    assert fresh.steps.count() == 1
    assert run_plan(fresh, workflow_owner)["status"] == "done"
    assert workflow_owner.properties.count() == 1
    assert workflow_owner.properties.get().name == "Renamed"


def test_workflow_composition_refuses_unknown_actions(workflow_owner):
    result = plan_daily_workflow(
        workflow_owner,
        json.dumps([{"tool": "delete_property", "arguments": {"property_query": "1"}}]),
        "Unsafe",
    )
    assert "error" in result
    assert not RamaTask.objects.exists()


def test_payment_preview_expires_when_another_payment_arrives(workflow_owner, bc_lease):
    from decimal import Decimal

    from rentium.ledger.models import LedgerEntry
    from rentium.ledger.services import post_charge
    from rentium.ledger.services import record_payment

    charge, _ = post_charge(
        landlord=workflow_owner,
        property=bc_lease.property,
        lease=bc_lease,
        tenant=None,
        entry_type="RENT_CHARGE",
        amount=Decimal("850"),
        due_date=timezone.localdate(),
        description="Workflow rent",
    )
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "record_payment",
                "arguments": {
                    "charge_ids": str(charge.pk),
                    "amount": "400",
                    "payment_method": "CASH",
                },
            },
        ],
    )
    record_payment(
        charge=charge,
        amount=Decimal("100"),
        payment_method="CASH",
        payment_date=timezone.localdate(),
        idempotency_key="other-payment",
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "failed", result
    assert LedgerEntry.objects.filter(settles=charge, entry_type="PAYMENT").count() == 1


def test_update_verifies_every_approved_field(workflow_owner, bc_property, monkeypatch):
    from . import plan_runner

    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {
                    "property_query": str(bc_property.pk),
                    "asking_rent": "925",
                },
            },
        ],
    )
    monkeypatch.setattr(
        plan_runner,
        "execute",
        lambda *a, **kw: {"updated": True, "property": {"id": str(bc_property.pk)}},
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "partial"
    assert not RamaTask.objects.get(pk=result["task"]["id"]).receipts.exists()


def test_single_write_has_complete_structured_preview(workflow_owner, bc_property):
    from .plan_runner import plan_brief
    from .plan_runner import save_single

    plan = save_single(
        workflow_owner,
        uuid.uuid4(),
        "update_property",
        {"property_query": str(bc_property.pk), "asking_rent": "925"},
    )
    assert plan_brief(plan)["previews"][0]["effects"]["changes"]["asking_rent"] == "925"
    assert run_plan(plan, workflow_owner)["status"] == "done"


@pytest.mark.parametrize(
    "steps",
    [
        [None],
        [{"tool": "update_property", "arguments": []}],
        [
            {
                "tool": "create_expense",
                "arguments": {
                    "amount": "40",
                    "description": "Software",
                    "invented_field": "unsafe",
                },
            },
        ],
    ],
)
def test_malformed_workflow_is_rejected_without_effects(workflow_owner, steps):
    result = plan_daily_workflow(workflow_owner, json.dumps(steps), "Invalid")
    assert "error" in result
    assert not RamaTask.objects.exists()


def test_finished_retry_cannot_be_replayed_from_original(workflow_owner, bc_property):
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "update_property",
                "arguments": {
                    "property_query": str(bc_property.pk),
                    "name": "Retry once",
                },
            },
        ],
    )
    task_id = plan.task_id
    plan.expires_at = timezone.now() - timedelta(seconds=1)
    plan.save()
    run_plan(plan, workflow_owner)
    retry = resume_task(workflow_owner, task_id, uuid.uuid4())
    assert run_plan(retry, workflow_owner)["status"] == "done"
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume_task(workflow_owner, task_id, uuid.uuid4())


def test_delivery_failure_preserves_lease_and_blocks_blind_retry(
    workflow_owner,
    bc_property,
    monkeypatch,
):
    from rentium.leases.models import Lease

    from .daily_workflows import onboarding_plan
    from .workflow_delivery import deliver_invitation
    from .workflow_delivery import refresh_waiting

    data = {
        "room_name": str(bc_property.pk),
        "start_date": str(timezone.localdate() + timedelta(days=30)),
        "total_rent": "850",
        "tenant_email": "alice@example.test",
        "create_inspection": "0",
    }
    proposal_result = onboarding_plan(workflow_owner, data)
    plan = save_plan(workflow_owner, uuid.uuid4(), proposal_result["plan"])
    result = run_plan(plan, workflow_owner)
    task = RamaTask.objects.get(pk=result["task"]["id"])
    delivery = task.attempts.get(capability_key="deliver_tenant_invitation")
    sends = []
    monkeypatch.setattr(
        "rentium.showcase.emails.send_tenant_invite",
        lambda slot: sends.append(slot.pk) or False,
    )
    deliver_invitation(delivery.pk)
    deliver_invitation(delivery.pk)
    refresh_waiting(task)
    task.refresh_from_db()
    assert task.status == "PARTIAL"
    assert len(sends) == 1
    assert Lease.objects.filter(property=bc_property).count() == 1
    assert task.receipts.count() == 2
    with pytest.raises(ValueError):
        resume_task(workflow_owner, task.pk, uuid.uuid4())


def test_invalid_repeat_confirmation_id_returns_error(workflow_owner):
    client = APIClient()
    client.force_authenticate(user=workflow_owner.user)
    response = client.post(
        f"/api/rama/plans/{uuid.uuid4()}/confirm/",
        {"prompt_message_id": "invalid"},
        format="json",
    )
    assert response.status_code < 500


def test_bank_pathway_uses_same_exact_reconciliation_receipt(
    workflow_owner,
    bc_property,
):
    from rentium.ledger.models import LedgerEntry
    from rentium.ledger.test_money_workflow import charge
    from rentium.ledger.test_money_workflow import select_payment
    from rentium.ledger.test_money_workflow import statement

    source = statement(workflow_owner)
    entry = charge(workflow_owner, bc_property)
    row = select_payment(workflow_owner, source, entry)
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "reconcile_bank_statement",
                "arguments": {"batch_id": str(source.batch_id), "row_ids": str(row.pk)},
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    assert run_plan(plan, workflow_owner) == result
    assert LedgerEntry.objects.filter(settles=entry, entry_type="PAYMENT").count() == 1
    assert (
        RamaTask.objects.get(pk=result["task"]["id"])
        .receipts.get()
        .verification["verified"]
    )


def test_dependent_creation_then_update_has_verified_receipts(workflow_owner):
    plan = proposal(
        workflow_owner,
        [
            {
                "step_id": "create",
                "tool": "create_property",
                "arguments": {
                    "name": "Original",
                    "address": "12 Test",
                    "city": "Victoria",
                },
            },
            {
                "step_id": "rename",
                "tool": "update_property",
                "depends_on": ["create"],
                "arguments": {
                    "property_query": {"$step": "create", "path": "property.id"},
                    "name": "Renamed",
                },
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    task = RamaTask.objects.get(pk=result["task"]["id"])
    assert task.receipts.filter(verification__verified=True).count() == 2
    assert workflow_owner.properties.get().name == "Renamed"


def test_equal_but_separate_approved_actions_have_separate_receipts(workflow_owner):
    step = {
        "tool": "create_expense",
        "arguments": {
            "amount": "30",
            "description": "Separate invoices",
            "effective_date": "2026-10-01",
            "category": "OTHER",
        },
    }
    plan = proposal(workflow_owner, [step, step])
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    task = RamaTask.objects.get(pk=result["task"]["id"])
    assert task.receipts.count() == 2
    assert len({row.effects["expense"]["id"] for row in task.receipts.all()}) == 2


@pytest.mark.django_db(transaction=True)
def test_simultaneous_confirmations_return_one_receipt(workflow_owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    from rentium.ledger.models import LedgerEntry
    from rentium.users.models import LandlordProfile

    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "create_expense",
                "arguments": {
                    "amount": "30",
                    "description": "Only once",
                    "effective_date": "2026-10-01",
                    "category": "OTHER",
                },
            },
        ],
    )
    owner_id = workflow_owner.pk
    barrier = Barrier(2)

    def confirm():
        close_old_connections()
        try:
            owner = LandlordProfile.objects.get(pk=owner_id)
            barrier.wait(timeout=10)
            return run_plan(plan, owner)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(lambda _: confirm(), range(2)))
    assert first == second
    assert (
        LedgerEntry.objects.filter(
            landlord=workflow_owner,
            description="Only once",
        ).count()
        == 1
    )
    assert RamaTask.objects.get(pk=first["task"]["id"]).receipts.count() == 1


def test_single_lease_preview_normalizes_domain_translation_values(
    workflow_owner,
    bc_property,
):
    from .plan_runner import save_single

    plan = save_single(
        workflow_owner,
        uuid.uuid4(),
        "create_lease",
        {
            "property_query": str(bc_property.pk),
            "start_date": str(timezone.localdate() + timedelta(days=30)),
            "total_rent": "900",
            "is_month_to_month": "1",
        },
    )
    assert plan.task.input["previews"]
    assert run_plan(plan, workflow_owner)["status"] == "done"


def test_rent_target_pathway_verifies_exact_household_total(workflow_owner, bc_lease):
    from rentium.leases.models import LeaseTenant
    from rentium.ledger.billing import compute_joint_rent_for_due_date

    LeaseTenant.objects.create(
        lease=bc_lease,
        invited_email="alice@example.test",
        invited_name="Alice",
        rent_amount="850",
    )
    due = bc_lease.start_date.replace(day=1)
    plan = proposal(
        workflow_owner,
        [
            {
                "tool": "apply_rent_adjustment",
                "arguments": {
                    "lease_number": bc_lease.lease_number,
                    "target_lease_total": "400",
                    "effective_date": str(due),
                    "reason": "Agreed first period",
                    "is_recurring": "0",
                },
            },
        ],
    )
    result = run_plan(plan, workflow_owner)
    assert result["status"] == "done", result
    assert compute_joint_rent_for_due_date(bc_lease, due) == 400
    assert (
        RamaTask.objects.get(pk=result["task"]["id"])
        .receipts.get()
        .verification["verified"]
    )


def test_advertised_rent_never_compiles_to_a_tenancy_adjustment(
    workflow_owner,
    bc_property,
    bc_lease,
):
    from .capabilities import supported_tool_for_request
    from .service import _first_month_rent_adjustment_intent
    from .service import _rent_period_adjustment_intent

    text = f"Set the asking rent of {bc_property.name} to $925."
    assert supported_tool_for_request(text) == "update_property"
    assert (
        _first_month_rent_adjustment_intent(workflow_owner, uuid.uuid4(), text) is None
    )
    assert _rent_period_adjustment_intent(workflow_owner, uuid.uuid4(), text) is None


def test_compound_rename_cannot_drop_a_second_requested_action(
    workflow_owner,
    bc_property,
):
    from .service import _rename_intent

    assert (
        _rename_intent(
            workflow_owner,
            f"Rename {bc_property.name} to Garden Suite and record a $35 software expense.",
        )
        is None
    )
