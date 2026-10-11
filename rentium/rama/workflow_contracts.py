"""Executable pathways over the existing guarded domain adapters.

No model may supply a verifier, an execution policy, or an approval. A pathway
is available for composition only when its postconditions are implemented here.
"""

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal

from .outcomes import CommandOutcome
from .outcomes import OutcomeKind

WORKFLOW_VERSION = 1
WORKFLOW_TOOLS = {
    "record_payment": "money",
    "create_expense": "money",
    "reconcile_bank_statement": "money",
    "apply_rent_adjustment": "money",
    "create_lease": "onboarding",
    "prepare_tenant_move": "tenant_move",
    "invite_tenant_to_lease": "onboarding",
    "create_condition_inspection": "onboarding",
    "create_property": "property",
    "update_property": "property",
    "create_property_structure": "property",
    "update_unit_layout": "property",
    "set_unit_rental_mode": "property",
}


def enabled(landlord):
    from .models import RamaPreferences

    return RamaPreferences.objects.filter(
        landlord=landlord,
        workflows_enabled=True,
    ).exists()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode(),
    ).hexdigest()


def target_facts(tool, arguments, landlord, *, lock=False):  # noqa: C901, PLR0912, PLR0915
    """Fresh scope/term evidence; unrelated portfolio changes do not invalidate it."""
    from rentium.leases.models import Lease
    from rentium.ledger.models import BankStatement

    from .domain_crud import _resolve_lease
    from .resolve import resolve_property
    from .unit_structure import _resolve_unit

    def snapshot(row):
        qs = type(row).objects.filter(pk=row.pk)
        if lock:
            qs = qs.select_for_update()
        return qs.values().get()

    facts = {}
    prop_query = arguments.get("property_query")
    if prop_query and not isinstance(prop_query, dict) and tool != "create_expense":
        prop, error = resolve_property(landlord, str(prop_query))
        if error:
            raise ValueError(str(error))
        facts["property"] = snapshot(prop)
        facts["leases"] = list(
            Lease.objects.filter(landlord=landlord, property=prop)
            .order_by("pk")
            .values("id", "status", "start_date", "end_date", "updated_at"),
        )
    number = arguments.get("lease_number")
    if number and not isinstance(number, dict):
        lease, error = _resolve_lease(landlord, lease_number=number)
        if error:
            raise ValueError(str(error))
        snapshot(lease)
        facts["lease"] = {
            "id": str(lease.pk),
            "updated_at": str(lease.updated_at),
            "status": lease.status,
            "rent": str(lease.total_rent),
            "tenants": list(
                lease.lease_tenants.order_by("pk").values(
                    "id",
                    "invited_email",
                    "rent_amount",
                    "has_signed",
                    "declined",
                ),
            ),
        }
    if arguments.get("unit_name"):
        unit, error = _resolve_unit(
            landlord,
            arguments["unit_name"],
            holding=arguments.get("holding", ""),
        )
        if error:
            raise ValueError(str(error))
        facts["unit"] = snapshot(unit)
        facts["areas"] = list(unit.areas.order_by("pk").values())
        facts["offerings"] = list(unit.offerings.order_by("pk").values())
        facts["unit_leases"] = list(
            Lease.objects.filter(landlord=landlord, property__unit=unit)
            .order_by("pk")
            .values("id", "status", "updated_at"),
        )
    if tool == "prepare_tenant_move":
        for key in ("from_property", "to_property"):
            facts[key] = target_facts(
                "create_lease",
                {"property_query": arguments[key]},
                landlord,
                lock=lock,
            )
    if tool == "create_expense":
        from .domain_actions import _resolve_expense_scope

        prop, unit, holding, error = _resolve_expense_scope(
            landlord,
            arguments.get("property_query", ""),
            arguments.get("holding_name", ""),
        )
        if error:
            raise ValueError(str(error))
        facts["expense_scope"] = {
            kind: snapshot(row)
            for kind, row in (("property", prop), ("unit", unit), ("holding", holding))
            if row is not None
        }
    if tool == "record_payment":
        from rentium.ledger.models import LedgerEntry

        ids = [
            value.strip()
            for value in arguments.get("charge_ids", "").replace(";", ",").split(",")
            if value.strip()
        ]
        charges = LedgerEntry.objects.filter(landlord=landlord, pk__in=ids).order_by(
            "pk",
        )
        if charges.count() != len(set(ids)):
            message = "A selected charge is no longer available in this portfolio."
            raise ValueError(message)
        facts["charges"] = [snapshot(row) for row in charges]
        facts["settlements"] = list(
            LedgerEntry.objects.filter(landlord=landlord, settles_id__in=ids)
            .order_by("pk")
            .values(),
        )
    if tool == "reconcile_bank_statement":
        statement = BankStatement.objects.get(
            batch_id=arguments["batch_id"],
            batch__landlord=landlord,
        )
        facts["statement"] = str(statement.batch_id)
        # The reconciliation service owns the stronger exact-effects snapshot.
        facts["preview_id"] = arguments.get("preview_id")
    return json.loads(json.dumps(facts, default=str))


def freeze_arguments(tool, arguments, landlord):
    """Bind textual subjects once. Execution never reselects a same-name row."""
    args = dict(arguments)
    facts = target_facts(tool, args, landlord)
    if "property" in facts:
        args["property_query"] = str(facts["property"]["id"])
    if "unit" in facts:
        args["unit_name"] = facts["unit"]["id"]
        args["holding"] = ""
    for kind, row in facts.get("expense_scope", {}).items():
        args["holding_name" if kind == "holding" else "property_query"] = str(row["id"])
    for key in ("from_property", "to_property"):
        if key in facts:
            args[key] = str(facts[key]["property"]["id"])
    return args, fingerprint(facts)


@dataclass
class VerificationContext:
    tool: str
    arguments: dict
    result: dict
    landlord: object
    expected: dict | None
    checks: list = field(default_factory=list)
    refs: list = field(default_factory=list)

    def check(self, value, label):
        self.checks.append({"check": label, "passed": bool(value)})


def _verify_create_property(context):  # noqa: C901
    from rentium.properties.models import Property

    tool = context.tool
    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    expected = context.expected
    check = context.check
    refs = context.refs
    payload = result.get("property") or {}
    payload = payload if isinstance(payload, dict) else {}
    pk = (
        payload.get("id")
        or result.get("property_id")
        or arguments.get("property_query")
    )
    row = Property.objects.filter(landlord=landlord, pk=pk).first() if pk else None
    check(row is not None, "offering belongs to this landlord")
    if row:
        refs.append({"type": "property", "id": str(row.pk)})
        if tool == "update_property":
            check(
                str(row.pk) == str(arguments["property_query"]),
                "exact approved offering",
            )
        if tool == "update_property":
            changes = (expected or {}).get("changes", {})
            check(bool(changes), "approved field changes available")
            for field, value in changes.items():
                actual = getattr(row, field, None)
                check(
                    actual == value
                    or str(actual) == str(value)
                    or (isinstance(actual, Decimal) and actual == Decimal(str(value))),
                    f"offering {field} matches approved value",
                )
        if tool == "create_property":
            expected = expected or {}
            for field in (
                "province",
                "property_category",
                "unit_type",
                "room_type",
                "status",
                "asking_rent",
                "max_occupancy",
                "square_footage",
                "building_amenities",
                "default_bills_included",
                "is_publicly_visible",
                "furnishing_status",
            ):
                if field in expected:
                    actual, wanted = getattr(row, field), expected[field]
                    check(
                        actual == wanted
                        or (
                            isinstance(actual, Decimal)
                            and actual == Decimal(str(wanted))
                        ),
                        f"offering {field} matches approved value",
                    )
            for item in expected.get("inventory_items_to_create", []):
                check(
                    row.inventory_items.filter(name=item[:200]).exists(),
                    f"private inventory {item}",
                )
        for name in ("name", "address", "city", "description"):
            if arguments.get(name):
                check(
                    getattr(row, name) == arguments[name],
                    f"offering {name} matches",
                )


def _verify_create_lease(context):
    from rentium.leases.models import Lease

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    payload = result.get("lease") or {}
    row = Lease.objects.filter(
        landlord=landlord,
        lease_number=payload.get("lease_number"),
    ).first()
    check(row is not None, "lease persisted in this portfolio")
    if row:
        refs.append({"type": "lease", "id": str(row.pk)})
        check(
            str(row.property_id) == arguments.get("property_query"),
            "exact offering",
        )
        check(str(row.start_date) == arguments.get("start_date"), "start date")
        check(row.total_rent == Decimal(arguments["total_rent"]), "agreed rent")
        check(
            row.status == Lease.LeaseStatus.DRAFT,
            "draft rather than assumed signature",
        )


def _verify_prepare_tenant_move(context):
    from rentium.leases.models import Lease

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    from rentium.leases.models import MoveOutRequest

    agreement = MoveOutRequest.objects.filter(
        pk=result.get("moveout_id"),
        lease__landlord=landlord,
    ).first()
    lease = Lease.objects.filter(
        pk=result.get("lease", {}).get("id"),
        landlord=landlord,
    ).first()
    check(
        agreement is not None and agreement.status == "PENDING",
        "mutual agreement awaits tenant consent",
    )
    check(
        lease is not None and lease.status == "DRAFT",
        "replacement lease remains draft",
    )
    if agreement and lease:
        check(
            str(lease.property_id) == arguments["to_property"],
            "exact destination",
        )
        check(
            str(agreement.lease.property_id) == arguments["from_property"],
            "exact original tenancy",
        )
        check(str(lease.start_date) == arguments["start_date"], "planned move date")
        refs.extend(
            [
                {"type": "lease", "id": str(lease.pk)},
                {"type": "moveout", "id": str(agreement.pk)},
            ],
        )


def _verify_invite_tenant_to_lease(context):
    from rentium.leases.models import LeaseTenant

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    row = LeaseTenant.objects.filter(
        pk=result.get("lease_tenant_id"),
        lease__landlord=landlord,
        lease__lease_number=arguments.get("lease_number"),
        invited_email__iexact=arguments.get("email", ""),
        declined=False,
    ).first()
    check(row is not None, "exact tenant invitation saved")
    if row:
        refs.append({"type": "lease_tenant", "id": str(row.pk)})
        if result.get("delivery_task_id"):
            from .models import RamaTask

            check(
                RamaTask.objects.filter(
                    pk=result["delivery_task_id"],
                    landlord=landlord,
                    input__lease_tenant_id=str(row.pk),
                ).exists(),
                "durable delivery job saved",
            )
        else:
            check(
                bool(row.invite_sent_at)
                and (result.get("email_sent") or result.get("linked_existing_account")),
                "invitation delivery recorded",
            )


def _verify_create_condition_inspection(context):
    from rentium.leases.inspections import ConditionInspection

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    rows = ConditionInspection.objects.filter(
        pk=(result.get("inspection") or {}).get("id"),
        lease__landlord=landlord,
        lease__lease_number=arguments.get("lease_number"),
    )
    if arguments.get("tenant_email"):
        rows = rows.filter(
            lease_tenant__invited_email__iexact=arguments["tenant_email"],
        )
    check(rows.exists(), "inspection saved for the exact lease and tenant")
    refs.extend(
        {"type": "inspection", "id": str(pk)}
        for pk in rows.values_list("pk", flat=True)
    )


def _verify_create_expense(context):
    from rentium.ledger.models import LedgerEntry

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    payload = result.get("expense") or {}
    row = LedgerEntry.objects.filter(
        landlord=landlord,
        pk=payload.get("id"),
    ).first()
    check(row is not None, "expense entry persisted")
    if row:
        check(row.amount == Decimal(arguments["amount"]), "expense amount")
        check(row.entry_type == "EXPENSE", "expense entry type")
        check(
            row.description == arguments["description"].strip()[:255],
            "expense description",
        )
        from .domain_actions import _resolve_expense_scope

        prop, unit, holding, error = _resolve_expense_scope(
            landlord,
            arguments.get("property_query", ""),
            arguments.get("holding_name", ""),
        )
        expected_holding = (
            prop.holding_id
            if prop
            else unit.holding_id
            if unit
            else holding.pk
            if holding
            else None
        )
        check(
            not error
            and row.property_id == (prop.pk if prop else None)
            and row.holding_id == expected_holding,
            "exact expense scope",
        )
        check(
            row.vendor == arguments.get("vendor", "").strip()[:150],
            "expense vendor",
        )
        if arguments.get("effective_date"):
            check(
                str(row.effective_date) == arguments["effective_date"],
                "incurred date",
            )
        if arguments.get("paid_on"):
            check(str(row.paid_on) == arguments["paid_on"], "cleared date")
        refs.append({"type": "ledger_entry", "id": str(row.pk)})


def _verify_record_payment(context):
    from rentium.ledger.models import LedgerEntry

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    payments = result.get("payments") or [result]
    total = Decimal("0")
    charge_ids = {
        value.strip()
        for value in arguments.get("charge_ids", "").replace(";", ",").split(",")
        if value.strip()
    }
    entry_ids = [item.get("entry_id") for item in payments]
    check(len(set(entry_ids)) == len(entry_ids), "distinct payment entries")
    for payment in payments:
        row = LedgerEntry.objects.filter(
            landlord=landlord,
            pk=payment.get("entry_id"),
        ).first()
        check(row is not None, "payment entry saved")
        if row:
            check(row.entry_type == "PAYMENT", "payment entry type")
            check(
                bool(charge_ids) and str(row.settles_id) in charge_ids,
                "exact approved charge",
            )
            if arguments.get("payment_date"):
                check(
                    str(row.effective_date) == arguments["payment_date"],
                    "payment date",
                )
            check(row.amount == Decimal(payment["amount"]), "payment amount")
            if arguments.get("lease_number"):
                check(
                    row.lease.lease_number == arguments["lease_number"],
                    "exact lease",
                )
            total += row.amount
            refs.append({"type": "ledger_entry", "id": str(row.pk)})
    if arguments.get("amount"):
        check(total == Decimal(arguments["amount"]), "full requested total")


def _verify_apply_rent_adjustment(context):
    from rentium.leases.models import Lease
    from rentium.leases.models import RentAdjustment

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    ids = [item["id"] for item in result.get("adjustments", [])]
    rows = RentAdjustment.objects.filter(
        pk__in=ids,
        lease_tenant__lease__landlord=landlord,
    )
    check(bool(ids) and rows.count() == len(set(ids)), "all adjustments persisted")
    check(
        not (result.get("ledger") or {}).get("warning"),
        "ledger reconciliation finished",
    )
    refs.extend({"type": "rent_adjustment", "id": str(pk)} for pk in ids)
    if arguments.get("target_lease_total"):
        from datetime import date

        from rentium.ledger.billing import compute_joint_rent_for_due_date

        lease = Lease.objects.get(
            landlord=landlord,
            lease_number=arguments["lease_number"],
        )
        due = date.fromisoformat(arguments["effective_date"])
        check(
            compute_joint_rent_for_due_date(lease, due)
            == Decimal(arguments["target_lease_total"]),
            "exact period total",
        )


def _verify_reconcile_bank_statement(context):
    from rentium.ledger.models import LedgerEntry

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    from rentium.ledger.models import BankStatement

    statement = BankStatement.objects.get(
        batch_id=arguments["batch_id"],
        batch__landlord=landlord,
    )
    posted = list(
        statement.transactions.filter(result__preview_id=arguments["preview_id"]),
    )
    check(bool(posted), "persisted reconciliation effects")
    for row in posted:
        ids = row.result.get("entry_ids") or []
        check(
            LedgerEntry.objects.filter(landlord=landlord, pk__in=ids).count()
            == len(set(ids)),
            "posted records exist",
        )
    check(
        result.get("preview_id") == arguments["preview_id"],
        "exact approved preview",
    )
    refs.append({"type": "bank_statement", "id": arguments["batch_id"]})


def _verify_create_property_structure(context):
    from rentium.properties.models import PropertyHolding

    arguments = context.arguments
    result = context.result
    landlord = context.landlord
    check = context.check
    refs = context.refs
    holding = PropertyHolding.objects.filter(
        landlord=landlord,
        name=result.get("holding"),
        address=arguments.get("address"),
    ).first()
    check(holding is not None, "holding saved")
    if holding:
        from .unit_structure import _parse_units

        specs, error = _parse_units(arguments.get("units_json") or "[]")
        check(not error, "valid recorded structure")
        for spec in specs:
            unit = holding.units.filter(name=spec["name"]).first()
            check(unit is not None, f"unit {spec['name']} saved")
            if unit:
                check(unit.rental_mode == spec["rental_mode"], "unit rental mode")
                for space in spec["spaces"]:
                    check(
                        unit.areas.filter(
                            name=space["name"],
                            area_type=space["area_type"],
                            is_seeded_default=False,
                        ).exists(),
                        f"recorded space {space['name']}",
                    )
        refs.append({"type": "holding", "id": str(holding.pk)})


def _verify_update_unit_layout(context):
    from .unit_structure import _resolve_unit

    tool = context.tool
    arguments = context.arguments
    landlord = context.landlord
    check = context.check
    refs = context.refs
    unit, error = _resolve_unit(landlord, arguments["unit_name"])
    check(not error and unit is not None, "exact unit exists")
    if unit:
        refs.append({"type": "unit", "id": str(unit.pk)})
        if tool == "set_unit_rental_mode":
            check(unit.rental_mode == arguments["rental_mode"], "rental mode")
        else:
            from .unit_structure import _parse_spaces

            spaces, error = _parse_spaces(
                json.loads(arguments.get("spaces_json") or "[]"),
                unit.name,
            )
            check(not error, "valid recorded layout")
            for space in spaces:
                check(
                    unit.areas.filter(
                        name=space["name"],
                        area_type=space["area_type"],
                        is_seeded_default=False,
                    ).exists(),
                    f"recorded space {space['name']}",
                )


_VERIFIERS = {
    "create_property": _verify_create_property,
    "update_property": _verify_create_property,
    "create_lease": _verify_create_lease,
    "prepare_tenant_move": _verify_prepare_tenant_move,
    "invite_tenant_to_lease": _verify_invite_tenant_to_lease,
    "create_condition_inspection": _verify_create_condition_inspection,
    "create_expense": _verify_create_expense,
    "record_payment": _verify_record_payment,
    "apply_rent_adjustment": _verify_apply_rent_adjustment,
    "reconcile_bank_statement": _verify_reconcile_bank_statement,
    "create_property_structure": _verify_create_property_structure,
    "update_unit_layout": _verify_update_unit_layout,
    "set_unit_rental_mode": _verify_update_unit_layout,
}


def verify(tool, arguments, result, landlord, *, expected=None):
    """Verify persisted postconditions using the registered domain contract."""
    outcome = CommandOutcome.from_tool_result(result)
    if outcome.kind not in {OutcomeKind.COMPLETED, OutcomeKind.NOOP}:
        return {"verified": False, "reason": outcome.message}
    verifier = _VERIFIERS.get(tool)
    if verifier is None:
        return {
            "verified": False,
            "source": "unverified_tool_result",
            "reason": "No database verifier is registered.",
        }
    context = VerificationContext(tool, arguments, result, landlord, expected)
    verifier(context)
    checks, refs = context.checks, context.refs
    from .links import dashboard_collection
    from .links import url_for_path

    collections = {
        "property": "properties",
        "holding": "properties",
        "unit": "properties",
        "lease": "leases",
        "lease_tenant": "leases",
        "inspection": "leases",
        "moveout": "leases",
        "ledger_entry": "finances",
        "rent_adjustment": "finances",
        "bank_statement": "finances",
    }
    links = []
    for key in sorted(
        {collections[ref["type"]] for ref in refs if ref["type"] in collections},
    ):
        label, path = dashboard_collection(key)
        links.append({"label": label, "url": url_for_path(path)})
    return {
        "links": links,
        "verified": bool(checks) and all(item["passed"] for item in checks),
        "source": "database",
        "checks": checks,
        "entity_refs": refs,
    }
