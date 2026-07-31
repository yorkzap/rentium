"""
The tool registry — deliberately boring: a dict from tool name to function
plus a JSON schema derived from type hints. The AI never defines tools,
schemas, or SQL; the surface is human-defined here and nowhere else.

`landlord` is injected from the authenticated session at execution time and
is invisible in the schema the model sees — the model cannot name, choose,
or switch landlords.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable

from . import tools as tool_functions

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}

# The whole tool surface, in the order the model sees it.
TOOL_FUNCTIONS: tuple[Callable, ...] = (
    tool_functions.portfolio_snapshot,
    tool_functions.list_properties,
    tool_functions.list_listing_media,
    tool_functions.occupancy_as_of,
    tool_functions.list_leases,
    tool_functions.data_catalogue,
    tool_functions.search_capabilities,
    tool_functions.read,
    tool_functions.update,
    tool_functions.link,
    tool_functions.deliver_lease_pdf,
    tool_functions.deliver_property_photos,
    tool_functions.open_lease,
    tool_functions.open_property,
    tool_functions.public_property_link,
    tool_functions.list_appointments,
    tool_functions.attention_items,
    tool_functions.resolve_person,
    tool_functions.lease_state,
    tool_functions.charge_status,
    tool_functions.charge_schedule,
    tool_functions.month_money,
    tool_functions.list_expenses,
    tool_functions.deposits_summary,
    tool_functions.next_charge,
    tool_functions.open_work_orders,
    tool_functions.list_work_orders,
    tool_functions.list_inquiries,
    tool_functions.list_conversations,
    tool_functions.list_messages,
    tool_functions.list_inspections,
    tool_functions.list_move_events,
    tool_functions.list_inventory,
    tool_functions.list_tenants,
    tool_functions.tenant_history,
    tool_functions.list_documents,
    tool_functions.catalog_business_document,
    tool_functions.business_document_location,
    tool_functions.business_document_status,
    tool_functions.search_business_documents,
    tool_functions.file_business_document,
    tool_functions.find_listings,
    tool_functions.find_leases,
    tool_functions.read_constitution,
    tool_functions.list_vendors,
    tool_functions.list_holdings,
    tool_functions.list_bank_balances,
    tool_functions.list_import_batches,
    tool_functions.read_staged_entries,
    tool_functions.list_viewing_requests,
    tool_functions.get_viewing_availability,
    tool_functions.get_notification_channels,
    tool_functions.list_capability_gaps,
    tool_functions.list_co_landlords,
    tool_functions.deposit_position,
    tool_functions.tenant_statement,
    tool_functions.list_memories,
    # write actions (confirm=yes)
    tool_functions.remember,
    tool_functions.forget,
    tool_functions.record_treasurer_fact,
    tool_functions.record_holding_financials,
    tool_functions.record_valuation,
    tool_functions.record_mortgage,
    tool_functions.log_capability_gap,
    tool_functions.triage_capability_gap,
    tool_functions.plan_operation,
    tool_functions.plan_move_tenant,
    tool_functions.amend_constitution,
    tool_functions.create_holding,
    tool_functions.assign_property_to_holding,
    tool_functions.update_bank_balance,
    tool_functions.create_work_order,
    tool_functions.transition_work_order,
    tool_functions.mark_inquiry_replied,
    tool_functions.send_tenant_message,
    tool_functions.mark_messages_read,
    tool_functions.schedule_viewing,
    tool_functions.reschedule_viewing,
    tool_functions.viewing_invite_status,
    tool_functions.adjust_lease,
    tool_functions.renew_lease,
    tool_functions.settle_moveout,
    tool_functions.complete_inspection_package,
    tool_functions.apply_rent_adjustment,
    tool_functions.record_utility_bill,
    tool_functions.convert_inquiry_to_viewing,
    # API gap-close (ledger / inspection / viewing / collection)
    tool_functions.void_ledger_entry,
    tool_functions.mark_ledger_paid,
    tool_functions.correct_ledger_entry,
    tool_functions.post_ledger_credit,
    tool_functions.post_one_off_charge,
    tool_functions.update_inspection_items,
    tool_functions.approve_inspection_suggestion,
    tool_functions.dismiss_inspection_suggestion,
    tool_functions.mark_inspection_delivered,
    tool_functions.cancel_viewing,
    tool_functions.mark_cleaning_fee_paid,
    tool_functions.list_payment_reminders,
    tool_functions.create_payment_reminder,
    tool_functions.mark_payment_reminder_sent,
    tool_functions.update_inquiry,
    tool_functions.commit_import_batch,
    tool_functions.discard_import_batch,
    tool_functions.list_notifications,
    tool_functions.mark_notifications_read,
    tool_functions.respond_to_viewing_request,
    tool_functions.set_viewing_availability,
    tool_functions.create_condition_inspection,
    tool_functions.lease_pdf_info,
    tool_functions.bulk_add_inventory,
    tool_functions.create_expense,
    tool_functions.record_payment,
    tool_functions.reallocate_expense,
    tool_functions.invite_tenant_to_lease,
    tool_functions.resend_lease_invite,
    tool_functions.cancel_lease_invite,
    tool_functions.replace_lease_invite,
    tool_functions.list_lease_roster,
    tool_functions.add_roommate_to_lease,
    tool_functions.add_co_host_to_lease,
    tool_functions.add_co_landlord,
    tool_functions.rebalance_lease_rents,
    tool_functions.create_property,
    tool_functions.duplicate_listing,
    tool_functions.attach_photo_to_listing,
    tool_functions.remove_photo_from_listing,
    tool_functions.remove_photos_from_listing,
    tool_functions.setup_room_tenancy,
    tool_functions.create_property_structure,
    tool_functions.update_unit_layout,
    tool_functions.set_unit_rental_mode,
    tool_functions.configure_unit_room_offerings,
    tool_functions.create_house_layout,
    tool_functions.create_group_room,
    tool_functions.update_property,
    tool_functions.delete_property,
    tool_functions.create_property_group,
    tool_functions.assign_property_to_group,
    tool_functions.create_lease,
    tool_functions.update_lease,
    tool_functions.delete_draft_lease,
    tool_functions.terminate_lease,
    tool_functions.landlord_sign_lease,
    tool_functions.update_work_order,
    tool_functions.complete_work_order,
    tool_functions.attribute_work_order,
    tool_functions.add_work_order_comment,
    tool_functions.create_inventory_item,
    tool_functions.update_inventory_item,
    tool_functions.delete_inventory_item,
    tool_functions.create_shared_inventory_item,
    tool_functions.delete_shared_inventory_item,
    tool_functions.crud_capabilities,
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable


def _schema_for(fn: Callable) -> dict:
    """JSON schema for a tool's arguments, from its signature and hints."""
    hints = typing.get_type_hints(fn)
    # Optional per-parameter descriptions, attached via tools._params(...). Weak
    # models get almost nothing from a bare {"type": "string"} — a one-line hint
    # per argument is the single highest-leverage nudge (e.g. telling the model
    # that update_property's `name=` RENAMES the listing).
    param_docs: dict[str, str] = getattr(fn, "param_docs", {}) or {}
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if name == "landlord":
            continue  # injected server-side, never exposed to the model
        json_type = _JSON_TYPES.get(hints.get(name, str), "string")
        prop: dict[str, Any] = {"type": json_type}
        doc = param_docs.get(name)
        if doc:
            prop["description"] = doc
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _build() -> dict[str, Tool]:
    registry: dict[str, Tool] = {}
    for fn in TOOL_FUNCTIONS:
        description = inspect.getdoc(fn)
        if not description:
            raise RuntimeError(f"RAMA tool {fn.__name__} needs a docstring.")
        registry[fn.__name__] = Tool(
            name=fn.__name__,
            description=" ".join(description.split()),
            parameters=_schema_for(fn),
            fn=fn,
        )
    return registry


REGISTRY: dict[str, Tool] = _build()


def tool_schemas() -> list[dict]:
    """The neutral-format schema list handed to every provider adapter."""
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in REGISTRY.values()
    ]


def execute(name: str, arguments: dict | None, *, landlord) -> dict[str, Any]:
    """Run one tool call, scoped to `landlord`. Never raises — problems come
    back as {"error": ...} for the model to relay."""
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}."}
    allowed = set(tool.parameters["properties"])
    kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed}
    missing = [k for k in tool.parameters.get("required", []) if k not in kwargs]
    if missing:
        return {"error": f"Missing required arguments: {', '.join(missing)}."}
    try:
        return tool.fn(landlord, **kwargs)
    except Exception as exc:  # noqa: BLE001 — tool failures must not kill the turn
        return {"error": f"{type(exc).__name__}: {exc}"}
