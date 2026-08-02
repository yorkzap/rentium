"""Authoritative landlord capability policy shared by roles and parity checks.

The registry answers *what code exists*.  This contract answers which RAMA role
may use it, what sort of operation it is, whether it is reversible, and which
dashboard/API actions it is intended to cover.  A newly registered mutation is
not exposed to the General until it is added here; exclusions are explicit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    tool: str
    operation: str
    entity: str = ""
    audiences: tuple[str, ...] = ("corporal",)
    reversible: bool = False
    ui_actions: tuple[str, ...] = ()
    exclusion_reason: str = ""


# Read capabilities are supplied by roles.READ_TOOLS.  This is the complete,
# fail-closed write/action surface available to the landlord-facing General.
GENERAL_WRITE_TOOLS: tuple[str, ...] = (
    "plan_operation",
    "plan_move_tenant",
    "amend_constitution",
    "log_capability_gap",
    "remember",
    "forget",
    "record_treasurer_fact",
    "record_holding_financials",
    "record_valuation",
    "record_mortgage",
    "attach_photo_to_listing",
    "remove_photo_from_listing",
    "remove_photos_from_listing",
    "add_co_landlord",
    "add_co_host_to_lease",
    "update_lease",
    "update",
    "bulk_add_inventory",
    "create_inventory_item",
    "update_inventory_item",
    "create_shared_inventory_item",
    "invite_tenant_to_lease",
    "resend_lease_invite",
    "cancel_lease_invite",
    "replace_lease_invite",
    "add_roommate_to_lease",
    "rebalance_lease_rents",
    "create_lease",
    "landlord_sign_lease",
    "terminate_lease",
    "adjust_lease",
    "renew_lease",
    "settle_moveout",
    "complete_inspection_package",
    "apply_rent_adjustment",
    "record_utility_bill",
    "convert_inquiry_to_viewing",
    "mark_inquiry_replied",
    "void_ledger_entry",
    "mark_ledger_paid",
    "correct_ledger_entry",
    "post_ledger_credit",
    "post_one_off_charge",
    "update_inspection_items",
    "approve_inspection_suggestion",
    "dismiss_inspection_suggestion",
    "mark_inspection_delivered",
    "cancel_viewing",
    "mark_cleaning_deposit_paid",
    "record_deposit_deduction",
    "return_deposits",
    "create_payment_reminder",
    "mark_payment_reminder_sent",
    "update_inquiry",
    "commit_import_batch",
    "discard_import_batch",
    "mark_notifications_read",
    "schedule_viewing",
    "reschedule_viewing",
    "respond_to_viewing_request",
    "record_payment",
    "create_expense",
    "reallocate_expense",
    "catalog_business_document",
    "rename_business_document",
    "file_business_document",
    "manage_business_documents",
    "create_work_order",
    "transition_work_order",
    "update_work_order",
    "complete_work_order",
    "attribute_work_order",
    "add_work_order_comment",
    "create_property",
    "update_property",
    "create_property_group",
    "manage_property_group",
    "assign_property_to_group",
    "create_house_layout",
    "create_group_room",
    "create_property_structure",
    "update_unit_layout",
    "set_unit_rental_mode",
    "configure_unit_room_offerings",
    "reorder_listing_media",
    "create_holding",
    "assign_property_to_holding",
    "update_lease_roster",
    "schedule_appointment",
    "manage_viewing_availability",
    "manage_agenda_event",
    "update_condition_inspection",
    "manage_import_rows",
    "manage_showcase_settings",
    "manage_insight",
    "manage_notification_channel",
    "update_treasurer_settings",
    "update_bank_balance",
    "duplicate_listing",
    "setup_room_tenancy",
    "create_condition_inspection",
    "send_tenant_message",
    "mark_messages_read",
    "set_viewing_availability",
    "save_last_workflow",
    "run_saved_workflow",
    "rename_saved_workflow",
    "archive_saved_workflow",
    "restore_saved_workflow",
)


# These operations may remain registered for REST/admin/backwards-compatible
# internals, but no chat-facing role may retrieve or invoke them.
CHAT_EXCLUSIONS: dict[str, str] = {
    "delete_property": "Permanent property deletion is UI-only; retire it instead.",
    "delete_draft_lease": "Permanent lease-row deletion is UI-only.",
    "delete_inventory_item": "Inventory has no recoverable archive yet.",
    "delete_shared_inventory_item": "Inventory has no recoverable archive yet.",
    "triage_capability_gap": (
        "Capability-backlog triage is an internal engineering control."
    ),
}


REVERSIBLE_ACTIONS = frozenset(
    {
        "remember",
        "forget",
        "update_inventory_item",
        "update_property",
        "update_lease",
        "update",
        "mark_ledger_paid",
        "void_ledger_entry",
        "manage_business_documents",
        "manage_viewing_availability",
        "manage_agenda_event",
        "manage_insight",
        "manage_notification_channel",
        "rename_saved_workflow",
        "archive_saved_workflow",
        "restore_saved_workflow",
    },
)


def capability_specs(read_tools: tuple[str, ...] = ()) -> dict[str, CapabilitySpec]:
    """Return the effective contract and fail if policy names drift from code."""
    from .registry import REGISTRY

    known = set(REGISTRY)
    configured = set(read_tools) | set(GENERAL_WRITE_TOOLS) | set(CHAT_EXCLUSIONS)
    unknown = configured - known
    if unknown:
        raise RuntimeError(
            "RAMA capability contract names unregistered tools: "
            + ", ".join(sorted(unknown)),
        )

    specs: dict[str, CapabilitySpec] = {}
    for name in sorted(known):
        is_read = name in set(read_tools)
        is_general = is_read or name in GENERAL_WRITE_TOOLS
        excluded = CHAT_EXCLUSIONS.get(name, "")
        specs[name] = CapabilitySpec(
            key=name,
            tool=name,
            operation="read" if is_read else "action",
            audiences=("corporal", "general") if is_general else ("corporal",),
            reversible=name in REVERSIBLE_ACTIONS,
            exclusion_reason=excluded,
        )
    return specs


def general_tool_names(read_tools: tuple[str, ...]) -> tuple[str, ...]:
    specs = capability_specs(read_tools)
    return tuple(
        name
        for name in _registry_order(read_tools)
        if "general" in specs[name].audiences and not specs[name].exclusion_reason
    )


def _registry_order(read_tools: tuple[str, ...]) -> tuple[str, ...]:
    """Stable role ordering: familiar reads first, then guarded actions."""
    return tuple(dict.fromkeys((*read_tools, *GENERAL_WRITE_TOOLS)))
