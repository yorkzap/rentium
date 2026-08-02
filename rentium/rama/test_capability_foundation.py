"""Capability-contract, dependency, and saved-workflow regressions."""

import uuid
from datetime import date
from datetime import timedelta

import pytest

from rentium.rama import registry
from rentium.rama.capability_contract import capability_specs
from rentium.rama.landlord_capabilities import run_saved_workflow
from rentium.rama.landlord_capabilities import save_last_workflow
from rentium.rama.models import RamaSavedWorkflow
from rentium.rama.plan_runner import run_plan
from rentium.rama.plan_runner import save_plan
from rentium.rama.plan_runner import validate_plan
from rentium.rama.registry import REGISTRY
from rentium.rama.roles import GENERAL_TOOLS
from rentium.rama.roles import READ_TOOLS

pytestmark = pytest.mark.django_db


def _dependent_property_plan(*, first_name="Dependency Room", final_name="Bound Room"):
    return {
        "operation": "dependency-test",
        "summary": "Create and then rename one listing by returned ID.",
        "steps": [
            {
                "step_id": "create-room",
                "tool": "create_property",
                "arguments": {
                    "name": first_name,
                    "address": "950 McKenzie Ave",
                    "city": "Victoria",
                },
                "target": first_name,
                "item_key": "room",
            },
            {
                "step_id": "rename-room",
                "depends_on": ["create-room"],
                "tool": "update_property",
                "arguments": {
                    "property_query": {
                        "$step": "create-room",
                        "path": "property.id",
                    },
                    "name": final_name,
                },
                "target": final_name,
                "item_key": "room",
            },
        ],
    }


def test_general_capability_contract_is_fail_closed_and_hides_hard_deletes():
    specs = capability_specs(READ_TOOLS)

    assert "manage_business_documents" in GENERAL_TOOLS
    assert "run_saved_workflow" in GENERAL_TOOLS
    assert "delete_property" not in GENERAL_TOOLS
    assert "delete_draft_lease" not in GENERAL_TOOLS
    assert specs["delete_property"].exclusion_reason
    assert set(REGISTRY) - set(GENERAL_TOOLS) == {
        "delete_property",
        "delete_draft_lease",
        "delete_inventory_item",
        "delete_shared_inventory_item",
        "triage_capability_gap",
    }


def test_plan_binds_later_arguments_to_verified_step_results(landlord):
    payload = _dependent_property_plan()
    assert validate_plan(payload["steps"], landlord) == []

    plan = save_plan(landlord, uuid.uuid4(), payload)
    result = run_plan(plan, landlord)

    assert result["status"] == "done"
    assert [row["step_id"] for row in result["executed"]] == [
        "create-room",
        "rename-room",
    ]
    listing = landlord.properties.get(name="Bound Room")
    assert listing.address == "950 McKenzie Ave"
    receipts = result["task"]["id"]
    assert receipts


def test_plan_rejects_forward_or_unknown_step_references(landlord):
    payload = _dependent_property_plan()
    payload["steps"][0]["arguments"]["address"] = {
        "$step": "rename-room",
        "path": "property.id",
    }

    errors = validate_plan(payload["steps"], landlord)

    assert any("must name an earlier step" in error for error in errors)


def test_explicit_saved_workflow_preserves_step_bindings_and_repreviews(landlord):
    source = save_plan(landlord, uuid.uuid4(), _dependent_property_plan())
    completed = run_plan(source, landlord)
    assert completed["status"] == "done"

    preview = save_last_workflow(landlord, "Create and rename room")
    assert preview["needs_confirm"] is True
    saved = save_last_workflow(
        landlord,
        "Create and rename room",
        confirm="yes",
    )
    workflow = RamaSavedWorkflow.objects.get(pk=saved["workflow_id"])
    binding = workflow.steps[1]["arguments"]["property_query"]
    assert binding == {"$step": "create-room", "path": "property.id"}

    missing = run_saved_workflow(landlord, str(workflow.pk), parameters="{}")
    assert missing["needs_input"] is True
    parameter_values = {
        key: (
            "Workflow Source Two" if key == "create-room_name" else "Workflow Final Two"
        )
        for key in workflow.parameter_schema
    }
    compiled = run_saved_workflow(
        landlord,
        str(workflow.pk),
        parameters=__import__("json").dumps(parameter_values),
    )

    assert compiled["needs_confirm"] is True
    assert compiled["plan"]["steps"][1]["arguments"]["property_query"] == binding
    assert compiled["plan"]["steps"][0]["arguments"]["name"] == "Workflow Source Two"


def test_saved_workflow_refuses_file_or_secret_arguments(landlord):
    payload = {
        "operation": "unsafe-workflow",
        "summary": "An attachment-backed action.",
        "steps": [
            {
                "step_id": "photo",
                "tool": "attach_photo_to_listing",
                "arguments": {
                    "property_query": "missing listing",
                    "attachment_id": str(uuid.uuid4()),
                },
                "item_key": "photo",
            },
        ],
    }
    # A full tool execution is unnecessary here: construct the same verified
    # receipt/task shape save_last_workflow consumes.
    from rentium.rama.command_engine import create_task
    from rentium.rama.command_engine import record_receipt
    from rentium.rama.models import RamaTask

    task = create_task(
        landlord=landlord,
        conversation_id=uuid.uuid4(),
        capability_key="unsafe-workflow",
        inputs=payload,
    )
    task.transition_to(RamaTask.Status.EXECUTING)
    record_receipt(
        task=task,
        capability_key="attach_photo_to_listing",
        inputs=payload["steps"][0]["arguments"],
        effects={"attached": True},
        verification={"verified": True},
    )
    task.transition_to(RamaTask.Status.VERIFIED)

    result = save_last_workflow(landlord, "Unsafe upload", confirm="yes")

    assert "cannot be saved" in result["error"]
    assert not RamaSavedWorkflow.objects.filter(name="Unsafe upload").exists()


def test_general_can_edit_full_draft_lease_contact_and_term_fields(landlord, bc_lease):
    from rentium.leases.models import Lease

    bc_lease.status = Lease.LeaseStatus.DRAFT
    bc_lease.save(update_fields=["status"])
    move_in = bc_lease.start_date + timedelta(days=1)
    arguments = {
        "lease_number": bc_lease.lease_number,
        "pet_deposit": "125.00",
        "move_in_date": str(move_in),
        "co_hosts": '[{"name":"Pat Owner","email":"pat@example.ca","phone":"2505550101"}]',
        "landlord_service_address": "PO Box 12, Victoria BC",
        "landlord_service_email": "service@example.ca",
        "custom_tenant_notice_months": "2",
        "fixed_term_end_reason": "Owner occupancy",
        "fixed_term_end_regulation_section": "13.1",
    }

    preview = registry.execute("update_lease", arguments, landlord=landlord)
    assert preview["needs_confirm"] is True
    result = registry.execute(
        "update_lease", {**arguments, "confirm": "yes"}, landlord=landlord,
    )

    assert result["updated"] is True
    bc_lease.refresh_from_db()
    assert bc_lease.pet_deposit == 125
    assert bc_lease.move_in_date == move_in
    assert bc_lease.co_hosts[0]["name"] == "Pat Owner"
    assert bc_lease.landlord_service_email == "service@example.ca"
    assert bc_lease.custom_tenant_notice_months == 2


def test_group_shared_inventory_edit_is_exact_and_confirmed(landlord):
    from rentium.properties.models import PropertyGroup
    from rentium.properties.models import SharedInventoryItem

    group = PropertyGroup.objects.create(landlord=landlord, name="McKenzie Basement")
    item = SharedInventoryItem.objects.create(
        group=group,
        name="Stove",
        quantity=1,
        condition=SharedInventoryItem.ItemCondition.FAIR,
    )
    arguments = {
        "group_query": str(group.pk),
        "action": "update_shared_inventory",
        "inventory_item_id": str(item.pk),
        "name": "Stainless stove",
        "condition": "GOOD",
        "location": "Basement kitchen",
    }

    preview = registry.execute("manage_property_group", arguments, landlord=landlord)
    assert preview["needs_confirm"] is True
    result = registry.execute(
        "manage_property_group", {**arguments, "confirm": "yes"}, landlord=landlord,
    )

    assert result["updated"] is True
    item.refresh_from_db()
    assert (item.name, item.condition, item.location_description) == (
        "Stainless stove",
        "GOOD",
        "Basement kitchen",
    )


def test_manual_calendar_events_archive_and_restore_instead_of_delete(landlord):
    arguments = {
        "action": "create",
        "title": "Check McKenzie stove",
        "kind": "REMINDER",
        "start_date": str(date.today() + timedelta(days=2)),
    }
    created = registry.execute(
        "manage_agenda_event", {**arguments, "confirm": "yes"}, landlord=landlord,
    )
    event_id = created["event_id"]

    archived = registry.execute(
        "manage_agenda_event",
        {"action": "archive", "event_id": event_id, "confirm": "yes"},
        landlord=landlord,
    )
    assert archived["archived"] is True
    restored = registry.execute(
        "manage_agenda_event",
        {"action": "restore", "event_id": event_id, "confirm": "yes"},
        landlord=landlord,
    )

    assert restored["restored"] is True
    assert landlord.agenda_events.get(pk=event_id).archived_at is None
