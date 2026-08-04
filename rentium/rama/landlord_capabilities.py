"""Landlord dashboard capabilities that were previously available only via REST.

These adapters deliberately reuse domain services where they exist.  Every
mutation previews first, scopes through the authenticated landlord, and returns
stable identifiers suitable for plan-step result bindings.
"""

from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .domain_crud import _confirmed
from .domain_crud import _preview
from .domain_crud import _truthy


def _csv(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Expected a JSON array or comma-separated values.",
            ) from exc
        if not isinstance(parsed, list):
            raise ValueError("Expected a JSON array.")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _json_object(value: str, *, label: str) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return parsed


def _local_datetime(landlord, raw: str, *, label: str):
    from rentium.appointments.services import landlord_tz

    parsed = parse_datetime(str(raw or "").replace("Z", "+00:00"))
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(raw or ""))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO date and time.") from exc
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=landlord_tz(landlord))
    return parsed


def _resolve_holding(landlord, query: str):
    from .domain_crud import _resolve_holding as resolve

    holding, error = resolve(landlord, query)
    if error:
        raise ValueError(str(error))
    return holding


def _resolve_document_set(
    landlord,
    *,
    document_ids: str = "",
    document_query: str = "",
    amount: str = "",
    include_trash: bool = False,
):
    from django.db.models import Q

    from .models import RamaDocument

    ids = _csv(document_ids)
    qs = RamaDocument.objects.filter(landlord=landlord).select_related(
        "holding", "ledger_entry",
    )
    if not include_trash:
        qs = qs.filter(deleted_at__isnull=True)
    if ids:
        rows = list(qs.filter(pk__in=ids).order_by("-created_at"))
        if len(rows) != len(set(ids)):
            found = {str(row.pk) for row in rows}
            raise ValueError(
                "Some document IDs were not found in this portfolio: "
                + ", ".join(sorted(set(ids) - found)),
            )
        return rows

    query = str(document_query or "").strip()
    if not query and not str(amount or "").strip():
        raise ValueError("Give a document ID, vendor/title query, or amount.")
    if query:
        tokens = [token for token in query.replace('"', " ").split() if len(token) >= 2]
        for token in tokens[:8]:
            qs = qs.filter(
                Q(title__icontains=token)
                | Q(issuer__icontains=token)
                | Q(reference_number__icontains=token)
                | Q(ocr_text__icontains=token)
                | Q(original_filename__icontains=token),
            )
    if str(amount or "").strip():
        try:
            parsed = Decimal(str(amount).replace("$", "").replace(",", "").strip())
        except InvalidOperation as exc:
            raise ValueError("amount must be a number such as 39.36.") from exc
        qs = qs.filter(amount=parsed)
    rows = list(qs.order_by("-created_at")[:11])
    if not rows:
        raise ValueError("No document matches those selectors.")
    if len(rows) > 1:
        choices = " | ".join(
            f"{row.get_display_title()} (${row.amount or 'unknown'}, {row.document_date or 'no date'}, id {row.pk})"
            for row in rows[:8]
        )
        raise ValueError(f"Several documents match. Choose one by ID: {choices}")
    return rows


def manage_business_documents(
    landlord,
    action: str,
    document_ids: str = "",
    document_query: str = "",
    amount: str = "",
    tags: str = "",
    holding_query: str = "",
    portfolio_wide: str = "",
    paid_on: str = "",
    confirm: str = "",
) -> dict:
    """Tag, trash, restore, re-OCR, move, or mark document-linked expenses paid.

    `document_ids` accepts one or more exact UUIDs.  A text/amount selector must
    resolve exactly one document.  Permanent deletion is intentionally absent.
    Actions: tags_add, tags_replace, tags_remove, trash, restore, reocr, move,
    mark_paid.  Preview first; confirm=yes.
    """
    from . import document_services as services

    op = str(action or "").strip().lower()
    allowed = {
        "tags_add",
        "tags_replace",
        "tags_remove",
        "trash",
        "restore",
        "reocr",
        "move",
        "mark_paid",
    }
    if op not in allowed:
        return {"error": "action must be one of: " + ", ".join(sorted(allowed))}
    try:
        rows = _resolve_document_set(
            landlord,
            document_ids=document_ids,
            document_query=document_query,
            amount=amount,
            include_trash=op == "restore",
        )
        if op == "restore":
            rows = [row for row in rows if row.deleted_at is not None]
            if not rows:
                return {"error": "The selected document is not in the trash."}
        tag_names = _csv(tags)
    except ValueError as exc:
        return {"error": str(exc)}
    if op.startswith("tags_") and not tag_names:
        return {"error": "tags are required for a tag action."}

    holding = None
    to_portfolio = _truthy(portfolio_wide)
    if op == "move" and not to_portfolio:
        try:
            holding = _resolve_holding(landlord, holding_query)
        except ValueError as exc:
            return {"error": str(exc)}

    preview_rows = [
        {
            "document_id": str(row.pk),
            "title": row.get_display_title(),
            "amount": str(row.amount) if row.amount is not None else None,
            "date": str(row.document_date) if row.document_date else None,
            "holding": row.holding.address if row.holding_id else "portfolio",
            "tags": list(row.tags.values_list("slug", flat=True)),
            "linked_expense_id": str(row.ledger_entry_id)
            if row.ledger_entry_id
            else None,
        }
        for row in rows
    ]
    preview = {
        "action": op,
        "documents": preview_rows,
        "tags": tag_names,
        "destination": "portfolio"
        if to_portfolio
        else (holding.address if holding is not None else None),
        "paid_on": paid_on or (str(date.today()) if op == "mark_paid" else None),
        "preserves": "Original files and document hashes are never rewritten.",
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_business_documents",
            preview,
            "Applies only the selected reversible document-library action.",
        )

    results = []
    for row in rows:
        try:
            if op == "trash":
                result = services.delete_document(
                    landlord=landlord, document=row, hard=False,
                )
            elif op == "restore":
                result = services.restore_document(landlord=landlord, document=row)
            elif op == "reocr":
                refreshed = services.reocr_document(landlord=landlord, document=row)
                result = {
                    "document_id": str(refreshed.pk),
                    "status": refreshed.status,
                    "reocr_requested": True,
                }
            elif op == "move":
                result = services.move_document_holding(
                    landlord=landlord,
                    document=row,
                    holding=holding,
                    portfolio_wide=to_portfolio,
                )
            elif op == "mark_paid":
                result = services.mark_document_expense_paid(
                    landlord=landlord,
                    document=row,
                    paid_on=paid_on or None,
                )
            else:
                current = {tag.name: tag for tag in row.tags.all()}
                if op == "tags_replace":
                    applied = services.set_document_tags(row, tag_names, replace=True)
                elif op == "tags_add":
                    applied = services.set_document_tags(row, tag_names, replace=False)
                else:
                    remove = {name.casefold() for name in tag_names}
                    keep = [
                        tag.name
                        for tag in current.values()
                        if tag.name.casefold() not in remove
                        and tag.slug.casefold() not in remove
                    ]
                    applied = services.set_document_tags(row, keep, replace=True)
                result = {
                    "document_id": str(row.pk),
                    "tags": [tag.slug for tag in applied],
                }
            results.append(result)
        except (services.DocumentError, ValueError) as exc:
            results.append({"document_id": str(row.pk), "error": str(exc)})
    failures = [result for result in results if result.get("error")]
    return {
        "updated": len(results) - len(failures),
        "failed": len(failures),
        "action": op,
        "results": results,
    }


def reorder_listing_media(
    landlord,
    property_query: str,
    handles: str,
    confirm: str = "",
) -> dict:
    """Reorder every gallery image using stable `gallery:<uuid>` handles."""
    from rentium.properties import media_services

    from .resolve import resolve_property

    prop, error = resolve_property(landlord, property_query)
    if error:
        return error if isinstance(error, dict) else {"error": str(error)}
    try:
        desired = _csv(handles)
        current = [
            row["handle"]
            for row in media_services.media_manifest(prop)
            if row.get("kind") == "gallery"
        ]
    except (ValueError, media_services.PropertyMediaError) as exc:
        return {"error": str(exc)}
    preview = {
        "property_id": str(prop.pk),
        "property": prop.name,
        "from": current,
        "to": desired,
    }
    if not _confirmed(confirm):
        return _preview(
            "reorder_listing_media",
            preview,
            "Reorders existing gallery rows; no files are deleted.",
        )
    try:
        manifest = media_services.reorder_gallery(property_obj=prop, handles=desired)
    except media_services.PropertyMediaError as exc:
        return {"error": str(exc)}
    return {"updated": True, "property_id": str(prop.pk), "media": manifest}


def _resolve_group(landlord, query: str):
    from rentium.properties.models import PropertyGroup

    raw = str(query or "").strip()
    qs = PropertyGroup.objects.filter(landlord=landlord)
    exact = qs.filter(pk=raw).first()
    if exact:
        return exact
    rows = list(qs.filter(name__icontains=raw)[:6]) if raw else []
    if len(rows) != 1:
        raise ValueError(
            "No group matches that selector."
            if not rows
            else "Several groups match; use the group ID: "
            + ", ".join(f"{g.name} ({g.pk})" for g in rows),
        )
    return rows[0]


def manage_property_group(
    landlord,
    group_query: str,
    action: str = "update",
    name: str = "",
    description: str = "",
    property_query: str = "",
    area_id: str = "",
    area_type: str = "",
    area_name: str = "",
    count: str = "",
    shared_with_landlord: str = "",
    inventory_item_id: str = "",
    inventory_item_query: str = "",
    condition: str = "",
    location: str = "",
    confirm: str = "",
) -> dict:
    """Update a room group, its membership, or its group-wide common areas.

    Actions: update, add_room, remove_room, add_area, update_area, and
    update_shared_inventory. Group, area, and inventory deletion are
    intentionally unavailable in chat.
    """
    from rentium.properties.models import PropertyArea
    from rentium.properties.models import SharedInventoryItem
    from rentium.properties.services import assign_room_to_group
    from rentium.properties.services import create_group_common_area
    from rentium.properties.services import update_group_common_area

    from .resolve import resolve_property

    try:
        group = _resolve_group(landlord, group_query)
    except ValueError as exc:
        return {"error": str(exc)}
    op = str(action or "update").strip().lower()
    if op not in {
        "update",
        "add_room",
        "remove_room",
        "add_area",
        "update_area",
        "update_shared_inventory",
    }:
        return {
            "error": (
                "action must be update, add_room, remove_room, add_area, "
                "update_area, or update_shared_inventory."
            ),
        }
    prop = None
    if op in {"add_room", "remove_room"}:
        prop, error = resolve_property(landlord, property_query)
        if error:
            return error if isinstance(error, dict) else {"error": str(error)}
    area = None
    if op == "update_area":
        areas = PropertyArea.objects.filter(
            property__group=group, is_group_common=True,
        ).distinct()
        area = areas.filter(pk=area_id).first() if area_id else None
        if area is None and area_name:
            matches = list(areas.filter(name__icontains=area_name)[:3])
            area = matches[0] if len(matches) == 1 else None
        if area is None:
            return {"error": "Choose one group common area by exact area_id."}
    inventory_item = None
    if op == "update_shared_inventory":
        inventory_qs = SharedInventoryItem.objects.filter(group=group)
        if inventory_item_id:
            inventory_item = inventory_qs.filter(pk=inventory_item_id).first()
        elif inventory_item_query:
            matches = list(
                inventory_qs.filter(name__icontains=inventory_item_query)[:4],
            )
            if len(matches) == 1:
                inventory_item = matches[0]
            elif len(matches) > 1:
                return {
                    "error": "Several shared inventory items match; use inventory_item_id.",
                    "candidates": [
                        {"id": str(item.pk), "name": item.name} for item in matches
                    ],
                }
        if inventory_item is None:
            return {"error": "Choose one shared inventory item by ID or unique name."}
    classification = None
    if shared_with_landlord != "":
        classification = _truthy(shared_with_landlord)
    if op == "add_area" and classification is None:
        return {
            "needs_input": True,
            "question_for_user": (
                "Will the landlord or the landlord's relatives share this common area? "
                "Answer yes or no."
            ),
            "resume_tool": "manage_property_group",
        }
    preview = {
        "group_id": str(group.pk),
        "group": group.name,
        "action": op,
        "name": name or None,
        "description": description if description != "" else None,
        "property": prop.name if prop else None,
        "area_id": str(area.pk) if area else None,
        "area_type": area_type or None,
        "area_name": area_name or None,
        "count": count or None,
        "shared_with_landlord": classification,
        "inventory_item_id": str(inventory_item.pk) if inventory_item else None,
        "condition": condition or None,
        "location": location if location != "" else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_property_group",
            preview,
            "Uses the shared property-group services and preserves membership invariants.",
        )
    try:
        with transaction.atomic():
            if op == "update":
                if name:
                    group.name = name[:100]
                if description != "":
                    group.description = description
                group.full_clean()
                group.save()
                result = {
                    "group_id": str(group.pk),
                    "name": group.name,
                    "description": group.description,
                }
            elif op in {"add_room", "remove_room"}:
                updated = assign_room_to_group(
                    prop, group if op == "add_room" else None,
                )
                result = {
                    "group_id": str(group.pk),
                    "property_id": str(updated.pk),
                    "group": updated.group.name if updated.group_id else None,
                }
            elif op == "add_area":
                created_area, created = create_group_common_area(
                    group,
                    area_type=str(area_type or "").upper(),
                    name=area_name,
                    count=int(count or 1),
                    description=description,
                    shared_with_landlord=bool(classification),
                )
                result = {
                    "area_id": str(created_area.pk),
                    "created": created,
                    "area": created_area.name or created_area.get_area_type_display(),
                }
            elif op == "update_area":
                updated_area = update_group_common_area(
                    group,
                    area,
                    count=int(count) if count else None,
                    description=description if description != "" else None,
                    shared_with_landlord=classification,
                )
                result = {
                    "area_id": str(updated_area.pk),
                    "updated": True,
                    "shared_with_landlord": updated_area.shared_with_landlord,
                }
            else:
                inventory_changes = {}
                if name:
                    inventory_changes["name"] = name[:200]
                if description != "":
                    inventory_changes["description"] = description[:2000]
                if count:
                    inventory_changes["quantity"] = max(1, int(count))
                if condition:
                    normalized = condition.strip().upper()
                    if normalized not in SharedInventoryItem.ItemCondition.values:
                        raise ValueError(
                            "condition must be one of: "
                            + ", ".join(SharedInventoryItem.ItemCondition.values),
                        )
                    inventory_changes["condition"] = normalized
                if location != "":
                    inventory_changes["location_description"] = location[:255]
                if not inventory_changes:
                    raise ValueError(
                        "Provide at least one shared inventory field to change.",
                    )
                for field, value in inventory_changes.items():
                    setattr(inventory_item, field, value)
                inventory_item.full_clean()
                inventory_item.save()
                result = {
                    "inventory_item_id": str(inventory_item.pk),
                    "name": inventory_item.name,
                    "updated_fields": sorted(inventory_changes),
                }
    except (ValidationError, ValueError) as exc:
        return {"error": str(exc)}
    return {"updated": True, **result}


def update_lease_roster(
    landlord,
    lease_query: str,
    tenant_row_id: str = "",
    tenant_query: str = "",
    rent_amount: str = "",
    cleaning_deposit: str = "",
    is_primary_tenant: str = "",
    invited_name: str = "",
    invited_email: str = "",
    invited_phone: str = "",
    individual_start_date: str = "",
    individual_end_date: str = "",
    room_query: str = "",
    tenant_notes: str = "",
    confirm: str = "",
) -> dict:
    """Edit one unsigned tenant/invite row on a draft or pending lease."""
    from rentium.leases.models import LeaseTenant

    from .domain_crud import _resolve_lease
    from .resolve import resolve_property

    lease, error = _resolve_lease(
        landlord, lease_number=lease_query, property_query=lease_query,
    )
    if error:
        return error if isinstance(error, dict) else {"error": str(error)}
    if lease.is_locked():
        return {"error": "This lease is active/executed and its roster is locked."}
    qs = LeaseTenant.objects.filter(lease=lease).select_related("tenant__user", "room")
    row = qs.filter(pk=tenant_row_id).first() if tenant_row_id else None
    if row is None and tenant_query:
        from django.db.models import Q

        matches = list(
            qs.filter(
                Q(invited_name__icontains=tenant_query)
                | Q(invited_email__icontains=tenant_query)
                | Q(tenant__user__name__icontains=tenant_query)
                | Q(tenant__user__email__icontains=tenant_query),
            )[:4],
        )
        if len(matches) == 1:
            row = matches[0]
        elif len(matches) > 1:
            return {
                "error": "Several roster rows match; use tenant_row_id.",
                "candidates": [
                    {
                        "id": str(item.pk),
                        "name": item.display_name,
                        "rent": str(item.rent_amount),
                    }
                    for item in matches
                ],
            }
    if row is None:
        return {
            "error": "No lease roster row matches. Use the row ID from list_lease_roster.",
        }

    changes = {}
    try:
        if rent_amount != "":
            changes["rent_amount"] = Decimal(
                str(rent_amount).replace("$", "").replace(",", ""),
            )
        if cleaning_deposit != "":
            changes["cleaning_deposit"] = Decimal(
                str(cleaning_deposit).replace("$", "").replace(",", ""),
            )
    except InvalidOperation:
        return {"error": "Rent and deposit amounts must be numbers."}
    if is_primary_tenant != "":
        changes["is_primary_tenant"] = _truthy(is_primary_tenant)
    for field, value in {
        "invited_name": invited_name,
        "invited_email": invited_email,
        "invited_phone": invited_phone,
        "tenant_notes": tenant_notes,
    }.items():
        if value != "":
            changes[field] = value
    for field, value in {
        "individual_start_date": individual_start_date,
        "individual_end_date": individual_end_date,
    }.items():
        if value != "":
            try:
                changes[field] = (
                    date.fromisoformat(value)
                    if value.lower() not in {"none", "clear", "null"}
                    else None
                )
            except ValueError:
                return {"error": f"{field} must be YYYY-MM-DD or clear."}
    if room_query:
        room, room_error = resolve_property(landlord, room_query)
        if room_error:
            return (
                room_error
                if isinstance(room_error, dict)
                else {"error": str(room_error)}
            )
        changes["room"] = room
    if not changes:
        return {"error": "Provide at least one roster field to change."}
    if (
        row.has_signed
        and "rent_amount" in changes
        and changes["rent_amount"] != row.rent_amount
    ):
        return {
            "error": "This tenant already signed at the current rent; that amount is locked.",
        }

    preview = {
        "lease_id": str(lease.pk),
        "lease_number": lease.lease_number,
        "tenant_row_id": str(row.pk),
        "tenant": row.display_name,
        "changes": {
            key: (
                str(value.pk)
                if hasattr(value, "pk")
                else str(value)
                if isinstance(value, (Decimal, date))
                else value
            )
            for key, value in changes.items()
        },
    }
    if not _confirmed(confirm):
        return _preview(
            "update_lease_roster",
            preview,
            "Updates one exact roster row; signed rent amounts remain locked.",
        )
    try:
        with transaction.atomic():
            locked = LeaseTenant.objects.select_for_update().get(pk=row.pk, lease=lease)
            if (
                locked.has_signed
                and "rent_amount" in changes
                and changes["rent_amount"] != locked.rent_amount
            ):
                return {
                    "error": "This tenant signed after the preview; rent is now locked.",
                }
            if changes.get("is_primary_tenant"):
                LeaseTenant.objects.filter(lease=lease, is_primary_tenant=True).exclude(
                    pk=locked.pk,
                ).update(is_primary_tenant=False)
            for field, value in changes.items():
                setattr(locked, field, value)
            locked.full_clean()
            locked.save()
    except ValidationError as exc:
        return {"error": str(exc)}
    return {
        "updated": True,
        "lease_id": str(lease.pk),
        "tenant_row_id": str(row.pk),
        "tenant": row.display_name,
    }


def schedule_appointment(
    landlord,
    property_query: str,
    starts_at: str,
    kind: str = "VIEWING",
    ends_at: str = "",
    lease_query: str = "",
    work_order_id: str = "",
    contact_name: str = "",
    contact_email: str = "",
    contact_phone: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Schedule VIEWING, INSPECTION, CONTRACTOR, or OTHER appointments."""
    from rentium.appointments.models import Appointment
    from rentium.appointments.services import schedule_viewing
    from rentium.maintenance.models import WorkOrder

    from .domain_crud import _resolve_lease
    from .resolve import resolve_property

    prop, error = resolve_property(landlord, property_query)
    if error:
        return error if isinstance(error, dict) else {"error": str(error)}
    appointment_kind = str(kind or "VIEWING").upper()
    if appointment_kind not in Appointment.Kind.values:
        return {"error": "kind must be VIEWING, INSPECTION, CONTRACTOR, or OTHER."}
    try:
        starts = _local_datetime(landlord, starts_at, label="starts_at")
        ends = _local_datetime(landlord, ends_at, label="ends_at") if ends_at else None
    except ValueError as exc:
        return {"error": str(exc)}
    if ends and ends <= starts:
        return {"error": "ends_at must be after starts_at."}
    lease = None
    if lease_query:
        lease, lease_error = _resolve_lease(
            landlord, lease_number=lease_query, property_query=lease_query,
        )
        if lease_error:
            return (
                lease_error
                if isinstance(lease_error, dict)
                else {"error": str(lease_error)}
            )
        if lease.property_id and lease.property_id != prop.pk:
            return {"error": "The selected lease belongs to a different listing."}
    work_order = None
    if work_order_id:
        work_order = WorkOrder.objects.filter(
            pk=work_order_id, landlord=landlord,
        ).first()
        if work_order is None:
            return {"error": "No work order with that ID exists in this portfolio."}
    preview = {
        "property_id": str(prop.pk),
        "property": prop.name,
        "kind": appointment_kind,
        "starts_at": starts.isoformat(),
        "ends_at": ends.isoformat() if ends else None,
        "lease_id": str(lease.pk) if lease else None,
        "work_order_id": str(work_order.pk) if work_order else None,
        "contact": {
            "name": contact_name,
            "email": contact_email,
            "phone": contact_phone,
        },
        "notes": notes,
    }
    if not _confirmed(confirm):
        return _preview(
            "schedule_appointment",
            preview,
            "Creates the appointment and publishes the normal appointment event.",
        )
    try:
        if appointment_kind == Appointment.Kind.VIEWING:
            appointment = schedule_viewing(
                landlord=landlord,
                property_obj=prop,
                starts_at=starts,
                ends_at=ends,
                contact_name=contact_name,
                contact_email=contact_email,
                contact_phone=contact_phone,
                notes=notes,
            )
        else:
            appointment = Appointment.objects.create(
                landlord=landlord,
                property=prop,
                lease=lease,
                work_order=work_order,
                kind=appointment_kind,
                status=Appointment.Status.SCHEDULED,
                starts_at=starts,
                ends_at=ends,
                contact_name=contact_name[:200],
                contact_email=contact_email,
                contact_phone=contact_phone,
                notes=notes,
            )
            appointment.stamp_time_class()
            appointment.save(update_fields=["time_class"])
            appointment.publish_event("appointment.scheduled")
    except ValidationError as exc:
        return {"error": str(exc)}
    return {
        "created": True,
        "appointment_id": str(appointment.pk),
        "kind": appointment.kind,
        "status": appointment.status,
        "starts_at": appointment.starts_at.isoformat(),
    }


def manage_viewing_availability(
    landlord,
    action: str,
    window_id: str = "",
    weekday: str = "",
    start: str = "",
    end: str = "",
    property_query: str = "",
    specific_date: str = "",
    confirm: str = "",
) -> dict:
    """Add, replace, or remove a viewing-hours row by stable window ID."""
    from rentium.appointments.models import AvailabilityWindow

    from .domain_actions import set_viewing_availability
    from .resolve import resolve_property

    op = str(action or "").lower()
    if op == "add":
        return set_viewing_availability(
            landlord,
            weekday=weekday,
            start=start,
            end=end,
            property_query=property_query,
            specific_date=specific_date,
            confirm=confirm,
        )
    if op not in {"remove", "replace"}:
        return {"error": "action must be add, replace, or remove."}
    window = (
        AvailabilityWindow.objects.filter(pk=window_id, landlord=landlord)
        .select_related("property")
        .first()
    )
    if window is None:
        return {"error": "No viewing-hours window with that ID exists."}
    replacement = None
    if op == "replace":
        prop = window.property
        if property_query:
            prop, error = resolve_property(landlord, property_query)
            if error:
                return error if isinstance(error, dict) else {"error": str(error)}
        from datetime import time as time_cls

        from rentium.rama.domain_actions import _WEEKDAYS

        try:
            day = date.fromisoformat(specific_date) if specific_date else None
            wd = day.weekday() if day else _WEEKDAYS[str(weekday).lower()]
            sh, sm = start.split(":")
            eh, em = end.split(":")
            start_t, end_t = time_cls(int(sh), int(sm)), time_cls(int(eh), int(em))
        except (ValueError, KeyError):
            return {
                "error": "Replacement needs weekday or specific_date plus HH:MM start/end.",
            }
        if end_t <= start_t:
            return {"error": "end must be after start."}
        replacement = {
            "property": prop,
            "weekday": wd,
            "specific_date": day,
            "start_time": start_t,
            "end_time": end_t,
        }
    before = {
        "window_id": str(window.pk),
        "property": window.property.name if window.property_id else None,
        "weekday": window.weekday,
        "specific_date": str(window.specific_date) if window.specific_date else None,
        "start": str(window.start_time),
        "end": str(window.end_time),
    }
    preview = {
        "action": op,
        "before": before,
        "after": {
            key: str(value.pk)
            if hasattr(value, "pk")
            else str(value)
            if value is not None
            else None
            for key, value in (replacement or {}).items()
        }
        or None,
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_viewing_availability",
            preview,
            "Stores the prior row in the receipt so the change is auditable and reconstructible.",
        )
    if op == "remove":
        window.delete()
        return {"removed": True, "window_id": window_id, "previous": before}
    for field, value in replacement.items():
        setattr(window, field, value)
    window.full_clean()
    window.save()
    return {"updated": True, "window_id": str(window.pk), "previous": before}


def manage_agenda_event(
    landlord,
    action: str,
    event_id: str = "",
    event_query: str = "",
    title: str = "",
    notes: str = "",
    kind: str = "",
    start_date: str = "",
    end_date: str = "",
    property_query: str = "",
    lease_query: str = "",
    confirm: str = "",
) -> dict:
    """Create, edit, archive, or restore a landlord's manual calendar event."""
    from django.db.models import Q

    from rentium.agenda.models import AgendaEvent

    from .domain_crud import _resolve_lease
    from .resolve import resolve_property

    op = str(action or "").lower()
    if op not in {"create", "update", "archive", "restore"}:
        return {"error": "action must be create, update, archive, or restore."}
    event = None
    if op != "create":
        qs = AgendaEvent.objects.filter(owner=landlord)
        event = qs.filter(pk=event_id).first() if event_id else None
        if event is None and event_query:
            matches = list(
                qs.filter(
                    Q(title__icontains=event_query) | Q(notes__icontains=event_query),
                )[:5],
            )
            if len(matches) == 1:
                event = matches[0]
            elif len(matches) > 1:
                return {
                    "error": "Several calendar events match; use event_id.",
                    "candidates": [
                        {
                            "id": str(row.pk),
                            "title": row.title,
                            "date": str(row.start_date),
                        }
                        for row in matches
                    ],
                }
        if event is None:
            return {"error": "No manual calendar event matches."}
    prop = getattr(event, "property", None)
    lease = getattr(event, "lease", None)
    if property_query:
        prop, error = resolve_property(landlord, property_query)
        if error:
            return error if isinstance(error, dict) else {"error": str(error)}
    if lease_query:
        lease, error = _resolve_lease(
            landlord, lease_number=lease_query, property_query=lease_query,
        )
        if error:
            return error if isinstance(error, dict) else {"error": str(error)}
    try:
        start = (
            date.fromisoformat(start_date)
            if start_date
            else getattr(event, "start_date", None)
        )
        end = (
            date.fromisoformat(end_date)
            if end_date
            else getattr(event, "end_date", None)
        )
    except ValueError:
        return {"error": "start_date and end_date must be YYYY-MM-DD."}
    if op in {"create", "update"} and not (title or getattr(event, "title", "")):
        return {"error": "title is required."}
    if op == "create" and start is None:
        return {"error": "start_date is required."}
    event_kind = (kind or getattr(event, "kind", AgendaEvent.Kind.CUSTOM)).upper()
    if event_kind not in AgendaEvent.Kind.values:
        return {"error": "kind must be CUSTOM, INSPECTION, REMINDER, or MOVE."}
    preview = {
        "action": op,
        "event_id": str(event.pk) if event else None,
        "title": title or getattr(event, "title", ""),
        "kind": event_kind,
        "start_date": str(start) if start else None,
        "end_date": str(end) if end else None,
        "property_id": str(prop.pk) if prop else None,
        "lease_id": str(lease.pk) if lease else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_agenda_event",
            preview,
            "Archive and restore are reversible; no manual event is hard-deleted.",
        )
    if op == "archive":
        event.archived_at = timezone.now()
        event.save(update_fields=["archived_at"])
        return {"archived": True, "event_id": str(event.pk), "title": event.title}
    if op == "restore":
        event.archived_at = None
        event.save(update_fields=["archived_at"])
        return {"restored": True, "event_id": str(event.pk), "title": event.title}
    if event is None:
        event = AgendaEvent(owner=landlord)
    if title:
        event.title = title[:200]
    if notes != "":
        event.notes = notes
    event.kind = event_kind
    event.start_date = start
    event.end_date = end
    event.property = prop
    event.lease = lease
    event.full_clean()
    event.save()
    return {
        "created": op == "create",
        "updated": op == "update",
        "event_id": str(event.pk),
        "title": event.title,
    }


def update_condition_inspection(
    landlord,
    inspection_id: str,
    action: str = "update_header",
    changes: str = "{}",
    section: str = "",
    label: str = "",
    key_rows: str = "[]",
    confirm: str = "",
) -> dict:
    """Edit condition-report headers, add a custom row, or save key counts."""
    from rentium.leases.inspections import ConditionInspection
    from rentium.leases.inspections import InspectionItem
    from rentium.leases.inspections import InspectionKeyRow
    from rentium.leases.inspections import InspectionPass

    inspection = (
        ConditionInspection.objects.filter(pk=inspection_id, lease__landlord=landlord)
        .select_related("lease")
        .first()
    )
    if inspection is None:
        return {
            "error": "No condition inspection with that ID exists in this portfolio.",
        }
    op = str(action or "update_header").lower()
    if op not in {"update_header", "add_custom_item", "save_keys"}:
        return {"error": "action must be update_header, add_custom_item, or save_keys."}
    try:
        payload = _json_object(changes, label="changes")
        rows = (
            json.loads(key_rows or "[]") if not isinstance(key_rows, list) else key_rows
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}
    if not isinstance(rows, list):
        return {"error": "key_rows must be a JSON array."}
    allowed_header = {
        "possession_date",
        "move_in_inspection_date",
        "tenant_agent_move_in",
        "repairs_required_at_start",
        "move_out_date",
        "move_out_inspection_date",
        "tenant_agent_move_out",
        "tenant_responsible_damage",
        "tenant_forwarding_address",
    }
    if op == "update_header":
        unknown = set(payload) - allowed_header
        if unknown:
            return {
                "error": "Unsupported inspection fields: " + ", ".join(sorted(unknown)),
            }
        move_in = {
            "possession_date",
            "move_in_inspection_date",
            "tenant_agent_move_in",
            "repairs_required_at_start",
        }
        move_out = allowed_header - move_in
        if inspection.pass_is_locked(InspectionPass.MOVE_IN) and set(payload) & move_in:
            return {"error": "The move-in pass is fully signed and locked."}
        if (
            inspection.pass_is_locked(InspectionPass.MOVE_OUT)
            and set(payload) & move_out
        ):
            return {"error": "The move-out pass is fully signed and locked."}
    if op == "add_custom_item" and (not section.strip() or not label.strip()):
        return {"error": "section and label are required for a custom item."}
    preview = {
        "inspection_id": str(inspection.pk),
        "lease": inspection.lease.lease_number,
        "action": op,
        "changes": payload,
        "custom_item": {"section": section, "label": label}
        if op == "add_custom_item"
        else None,
        "key_rows": rows if op == "save_keys" else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "update_condition_inspection",
            preview,
            "Signed inspection passes remain immutable.",
        )
    try:
        with transaction.atomic():
            inspection = ConditionInspection.objects.select_for_update().get(
                pk=inspection.pk,
            )
            if op == "update_header":
                for field, value in payload.items():
                    model_field = inspection._meta.get_field(field)
                    if model_field.get_internal_type() == "DateField" and value:
                        value = date.fromisoformat(str(value))
                    setattr(
                        inspection,
                        field,
                        value if value not in {"clear", "null"} else None,
                    )
                inspection.full_clean()
                inspection.save()
                result = {
                    "inspection_id": str(inspection.pk),
                    "updated_fields": sorted(payload),
                }
            elif op == "add_custom_item":
                if inspection.status == ConditionInspection.Status.COMPLETED:
                    return {"error": "This inspection is completed and locked."}
                last = inspection.items.order_by("-sort_order").first()
                item = InspectionItem.objects.create(
                    inspection=inspection,
                    section=section[:60],
                    label=label[:200],
                    sort_order=(last.sort_order + 10) if last else 10,
                    is_custom=True,
                )
                result = {
                    "inspection_id": str(inspection.pk),
                    "item_id": str(item.pk),
                    "created": True,
                }
            else:
                existing = {str(row.pk): row for row in inspection.key_rows.all()}
                out = []
                next_sort = (
                    max((row.sort_order for row in existing.values()), default=0) + 10
                )
                for data in rows:
                    if (
                        not isinstance(data, dict)
                        or not str(data.get("key_type") or "").strip()
                    ):
                        raise ValueError(
                            "Every key row needs key_type and issued_count.",
                        )
                    key = existing.get(str(data.get("id") or ""))
                    if key is None:
                        key = InspectionKeyRow(
                            inspection=inspection, sort_order=next_sort,
                        )
                        next_sort += 10
                    key.key_type = str(data["key_type"])[:120]
                    key.issued_count = int(data.get("issued_count") or 0)
                    key.returned_count = (
                        int(data["returned_count"])
                        if data.get("returned_count") not in (None, "")
                        else None
                    )
                    key.full_clean()
                    key.save()
                    out.append(str(key.pk))
                result = {
                    "inspection_id": str(inspection.pk),
                    "key_row_ids": out,
                    "updated": True,
                }
    except (ValidationError, ValueError) as exc:
        return {"error": str(exc)}
    return result


def manage_import_rows(
    landlord,
    action: str,
    batch_id: str = "",
    row_id: str = "",
    mapping: str = "{}",
    changes: str = "{}",
    reason: str = "",
    attachment_id: str = "",
    label: str = "",
    confirm: str = "",
) -> dict:
    """Upload/map/edit/exclude/restore historical ledger import staging rows."""
    from django.core.files.base import ContentFile

    from rentium.ledger import import_services
    from rentium.ledger.models import ImportBatch
    from rentium.ledger.models import StagedLedgerEntry
    from rentium.properties.models import Property

    from .models import RamaAttachment

    op = str(action or "").lower()
    if op not in {"upload", "apply_mapping", "edit", "exclude", "restore"}:
        return {
            "error": "action must be upload, apply_mapping, edit, exclude, or restore.",
        }
    batch = (
        ImportBatch.objects.filter(pk=batch_id, landlord=landlord).first()
        if batch_id
        else None
    )
    row = (
        StagedLedgerEntry.objects.filter(pk=row_id, batch__landlord=landlord)
        .select_related("batch")
        .first()
        if row_id
        else None
    )
    attachment = None
    headers = []
    if op == "upload":
        attachment = RamaAttachment.objects.filter(
            pk=attachment_id, batch__landlord=landlord,
        ).first()
        if attachment is None:
            return {
                "error": "Attach the CSV to this message and pass its attachment_id.",
            }
        attachment.original.open("rb")
        try:
            content = attachment.original.read()
        finally:
            attachment.original.close()
        try:
            headers, _ = import_services.read_csv_rows(content)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Could not read that attachment as CSV: {exc}"}
        if not headers:
            return {"error": "The CSV contains no columns."}
    elif batch is None:
        return {"error": "No editable import batch with that ID exists."}
    if batch is not None and batch.status != ImportBatch.Status.DRAFT:
        return {"error": f"Batch is {batch.status}, not editable."}
    if op in {"edit", "exclude", "restore"} and (
        row is None or row.batch_id != batch.pk
    ):
        return {"error": "No staged row with that ID exists in this batch."}
    try:
        map_data = _json_object(mapping, label="mapping")
        change_data = _json_object(changes, label="changes")
    except ValueError as exc:
        return {"error": str(exc)}
    preview = {
        "action": op,
        "batch_id": str(batch.pk) if batch else None,
        "row_id": str(row.pk) if row else None,
        "label": label,
        "headers": headers,
        "guessed_map": import_services.guess_column_map(headers) if headers else None,
        "mapping": map_data if op == "apply_mapping" else None,
        "changes": change_data if op == "edit" else None,
        "reason": reason if op == "exclude" else None,
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_import_rows",
            preview,
            "Only mutable staging data changes; committed ledger rows remain append-only.",
        )
    try:
        if op == "upload":
            batch = ImportBatch.objects.create(
                landlord=landlord,
                label=label[:200],
                source_filename=attachment.original_filename[:255],
                created_by=landlord.user,
            )
            batch.source_file.save(
                attachment.original_filename, ContentFile(content), save=True,
            )
            return {
                "created": True,
                "batch_id": str(batch.pk),
                "headers": headers,
                "guessed_map": import_services.guess_column_map(headers),
                "target_fields": list(import_services.TARGET_FIELDS),
            }
        if op == "apply_mapping":
            if not batch.source_file:
                return {"error": "This batch has no source CSV."}
            batch.source_file.open("rb")
            try:
                headers, raw_rows = import_services.read_csv_rows(
                    batch.source_file.read(),
                )
            finally:
                batch.source_file.close()
            rows = import_services.stage_rows(batch, headers, raw_rows, map_data)
            batch.column_map = map_data
            batch.save(update_fields=["column_map"])
            return {"updated": True, "batch_id": str(batch.pk), "row_count": len(rows)}
        if op == "exclude":
            row.excluded_at = timezone.now()
            row.exclusion_reason = (reason or "Excluded by landlord")[:255]
            row.save(update_fields=["excluded_at", "exclusion_reason", "updated_at"])
            return {
                "excluded": True,
                "batch_id": str(batch.pk),
                "row_id": str(row.pk),
                "raw_preserved": True,
            }
        if op == "restore":
            row.excluded_at = None
            row.exclusion_reason = ""
            row.save(update_fields=["excluded_at", "exclusion_reason", "updated_at"])
            return {"restored": True, "batch_id": str(batch.pk), "row_id": str(row.pk)}
        allowed = {
            "entry_type",
            "amount",
            "due_date",
            "effective_date",
            "paid_on",
            "property_id",
            "category",
            "vendor",
            "description",
            "payment_method",
            "settles_row_id",
        }
        unknown = set(change_data) - allowed
        if unknown:
            return {"error": "Unsupported staged fields: " + ", ".join(sorted(unknown))}
        for field, value in change_data.items():
            if field == "amount":
                row.amount = Decimal(str(value)) if value not in (None, "") else None
            elif field in {"due_date", "effective_date", "paid_on"}:
                setattr(
                    row,
                    field,
                    import_services._parse_date(str(value)) if value else None,
                )
            elif field == "property_id":
                row.property = (
                    Property.objects.filter(pk=value, landlord=landlord).first()
                    if value
                    else None
                )
            elif field == "settles_row_id":
                row.settles_row = batch.rows.filter(pk=value).first() if value else None
            else:
                setattr(row, field, value or "")
        row.issues = import_services.validate_row(row)
        row.save()
        return {
            "updated": True,
            "batch_id": str(batch.pk),
            "row_id": str(row.pk),
            "issues": row.issues,
        }
    except (InvalidOperation, ValueError) as exc:
        return {"error": str(exc)}


def manage_showcase_settings(
    landlord,
    slug: str = "",
    display_name: str = "",
    bio: str = "",
    contact_email: str = "",
    is_public: str = "",
    attachment_id: str = "",
    confirm: str = "",
) -> dict:
    """Update the landlord's public showcase settings and optional profile image."""
    from django.core.files.base import ContentFile
    from django.utils.text import slugify

    from rentium.properties.models import Property
    from rentium.showcase import services
    from rentium.showcase.models import Showcase

    from .models import RamaAttachment

    showcase = Showcase.for_landlord(landlord)
    clean_slug = slugify(slug)[:60] if slug else showcase.slug
    if slug and not services.slug_is_available(clean_slug, exclude_showcase=showcase):
        return {"error": "That public URL is not available."}
    publish = _truthy(is_public) if is_public != "" else showcase.is_public
    if publish and not clean_slug:
        return {"error": "Choose a public slug before publishing the page."}
    attachment = None
    if attachment_id:
        attachment = RamaAttachment.objects.filter(
            pk=attachment_id, batch__landlord=landlord,
        ).first()
        if attachment is None:
            return {
                "error": "No attached image with that ID exists on this conversation.",
            }
        if not str(attachment.content_type or "").lower().startswith("image/"):
            return {"error": "The showcase profile photo must be an image file."}
    blocked = []
    if publish:
        for prop in Property.objects.filter(
            landlord=landlord,
            is_publicly_visible=True,
            status=Property.PropertyStatus.AVAILABLE,
        ):
            reasons = prop.publish_blockers()
            if reasons:
                blocked.append(
                    {
                        "property_id": str(prop.pk),
                        "property": prop.name,
                        "reasons": reasons,
                    },
                )
    preview = {
        "slug": clean_slug,
        "display_name": display_name if display_name != "" else showcase.display_name,
        "bio": bio if bio != "" else showcase.bio,
        "contact_email": contact_email
        if contact_email != ""
        else showcase.contact_email,
        "is_public": publish,
        "photo_attachment_id": attachment_id or None,
        "blocked_properties": blocked,
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_showcase_settings",
            preview,
            "Publishes only eligible listings; blocked listings remain private and are named in the preview.",
        )
    if slug and clean_slug != showcase.slug:
        services.rename_slug(showcase, clean_slug)
    for field, value in {
        "display_name": display_name,
        "bio": bio,
        "contact_email": contact_email,
    }.items():
        if value != "":
            setattr(showcase, field, value)
    showcase.is_public = publish
    if attachment:
        attachment.original.open("rb")
        try:
            showcase.photo.save(
                attachment.original_filename,
                ContentFile(attachment.original.read()),
                save=False,
            )
        finally:
            attachment.original.close()
    showcase.full_clean()
    showcase.save()
    return {
        "updated": True,
        "showcase_id": str(showcase.pk),
        "slug": showcase.slug,
        "is_public": showcase.is_public,
        "blocked_properties": blocked,
    }


def manage_insight(
    landlord,
    insight_id: str,
    action: str,
    confirm: str = "",
) -> dict:
    """Acknowledge, dismiss, or reopen one RAMA insight by exact ID."""
    from .models import RamaInsight

    insight = RamaInsight.objects.filter(pk=insight_id, landlord=landlord).first()
    if insight is None:
        return {"error": "No insight with that ID exists."}
    op = str(action or "").lower()
    states = {
        "acknowledge": RamaInsight.Status.ACKED,
        "dismiss": RamaInsight.Status.DISMISSED,
        "reopen": RamaInsight.Status.OPEN,
    }
    if op not in states:
        return {"error": "action must be acknowledge, dismiss, or reopen."}
    preview = {
        "insight_id": str(insight.pk),
        "kind": insight.kind,
        "severity": insight.severity,
        "from_status": insight.status,
        "to_status": states[op],
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_insight",
            preview,
            "Changes only the landlord's review state; the original facts and analysis stay intact.",
        )
    insight.status = states[op]
    insight.save(update_fields=["status", "updated_at"])
    return {"updated": True, "insight_id": str(insight.pk), "status": insight.status}


def manage_notification_channel(
    landlord,
    action: str,
    channel_id: str = "",
    channel_type: str = "TELEGRAM",
    categories: str = "",
    morning_briefing: str = "",
    is_active: str = "",
    confirm: str = "",
) -> dict:
    """Mint a link code or update/deactivate/reactivate a landlord channel."""
    from django.conf import settings

    from rentium.comms.models import ChannelAccount

    op = str(action or "").lower()
    if op not in {"create_link_code", "update", "deactivate", "reactivate"}:
        return {
            "error": "action must be create_link_code, update, deactivate, or reactivate.",
        }
    account = None
    if op != "create_link_code":
        account = ChannelAccount.objects.filter(
            pk=channel_id, landlord=landlord,
        ).first()
        if account is None:
            return {"error": "No landlord notification channel with that ID exists."}
    prefs = dict(account.prefs or {}) if account else {}
    if categories != "":
        try:
            prefs["categories"] = [value.upper() for value in _csv(categories)]
        except ValueError as exc:
            return {"error": str(exc)}
    if morning_briefing != "":
        prefs["briefing"] = _truthy(morning_briefing)
    active = account.is_active if account else True
    if is_active != "":
        active = _truthy(is_active)
    if op == "deactivate":
        active = False
    if op == "reactivate":
        active = True
    preview = {
        "action": op,
        "channel_id": str(account.pk) if account else None,
        "channel_type": channel_type.upper(),
        "prefs": prefs,
        "is_active": active,
        "never": "Unlinking/deleting a channel remains UI-only.",
    }
    if not _confirmed(confirm):
        return _preview(
            "manage_notification_channel",
            preview,
            "Channel preference changes are reversible; no channel is deleted.",
        )
    if op == "create_link_code":
        ctype = channel_type.upper()
        if ctype not in ChannelAccount.ChannelType.values:
            return {"error": "Unsupported channel type."}
        account = ChannelAccount.mint_link_code(landlord, ctype)
        bot = str(getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "")
        return {
            "created": True,
            "channel_id": str(account.pk),
            "link_code": account.link_code,
            "expires_at": account.link_code_expires,
            "instructions": f"Message @{bot}: /link {account.link_code}"
            if bot
            else f"Send /link {account.link_code} to the Rentium bot.",
        }
    account.prefs = prefs
    account.is_active = active
    account.save(update_fields=["prefs", "is_active", "updated_at"])
    return {
        "updated": True,
        "channel_id": str(account.pk),
        "prefs": account.prefs,
        "is_active": account.is_active,
    }


def update_treasurer_settings(
    landlord,
    consent: str = "",
    marginal_rate: str = "",
    employment_income_band: str = "",
    other_income_band: str = "",
    filing_situation: str = "",
    tax_province: str = "",
    confirm: str = "",
) -> dict:
    """Update consent and privacy-minimised tax inputs used by the Treasurer."""
    from .models import LandlordFinancialProfile

    profile, _ = LandlordFinancialProfile.objects.get_or_create(landlord=landlord)
    changes = {}
    if consent != "":
        changes["consented_at"] = timezone.now() if _truthy(consent) else None
    if marginal_rate != "":
        try:
            rate = Decimal(marginal_rate.replace("%", "").strip())
        except InvalidOperation:
            return {"error": "marginal_rate must be a percentage between 0 and 100."}
        if rate < 0 or rate > 100:
            return {"error": "marginal_rate must be between 0 and 100."}
        changes["self_reported_marginal_rate"] = rate
    for field, raw, valid in (
        (
            "employment_income_band",
            employment_income_band,
            LandlordFinancialProfile.IncomeBand.values,
        ),
        (
            "other_income_band",
            other_income_band,
            LandlordFinancialProfile.IncomeBand.values,
        ),
        ("filing_situation", filing_situation, LandlordFinancialProfile.Filing.values),
    ):
        if raw:
            value = raw.upper()
            if value not in valid:
                return {"error": f"Invalid {field}. Allowed: {', '.join(valid)}"}
            changes[field] = value
    if tax_province:
        changes["tax_province"] = tax_province.upper()[:2]
    if not changes:
        return {"error": "Provide at least one Treasurer setting."}
    preview = {
        "changes": {
            key: (
                bool(value)
                if key == "consented_at"
                else str(value)
                if isinstance(value, Decimal)
                else value
            )
            for key, value in changes.items()
        },
        "privacy": "Tax fields are ignored unless consent is active.",
    }
    if not _confirmed(confirm):
        return _preview(
            "update_treasurer_settings",
            preview,
            "Updates only the landlord's consented financial profile.",
        )
    for field, value in changes.items():
        setattr(profile, field, value)
    profile.full_clean()
    profile.save()
    return {
        "updated": True,
        "consented": profile.usable,
        "marginal_rate": str(profile.self_reported_marginal_rate)
        if profile.self_reported_marginal_rate is not None
        else None,
    }


_WORKFLOW_VARIABLE_KEYS = frozenset(
    {
        "property_query",
        "holding_name",
        "holding_query",
        "lease_number",
        "lease_query",
        "room_name",
        "room_query",
        "document_id",
        "document_query",
        "entry_id",
        "tenant_query",
        "tenant_row_id",
        "email",
        "amount",
        "paid_on",
        "effective_date",
        "payment_date",
        "starts_at",
        "ends_at",
        "description",
        "name",
        "title",
        "contact_name",
        "contact_email",
        "contact_phone",
    },
)

_WORKFLOW_FORBIDDEN_KEYS = frozenset(
    {
        "attachment_id",
        "attachment_ids",
        "upload_id",
        "file",
        "document_file",
        "api_key",
        "token",
        "secret",
        "password",
        "confirm",
    },
)


def _parameterise_arguments(arguments: dict, step_id: str, schema: dict) -> dict:
    result = {}
    for key, value in arguments.items():
        if key == "confirm":
            continue
        if (
            key in _WORKFLOW_VARIABLE_KEYS
            and value not in (None, "")
            and not isinstance(value, (dict, list))
        ):
            parameter = f"{step_id}_{key}"
            schema[parameter] = {"required": True, "source": key, "type": "string"}
            result[key] = {"$param": parameter}
        else:
            result[key] = value
    return result


def _fill_parameters(value, supplied: dict, missing: set[str]):
    if isinstance(value, dict):
        if set(value) == {"$param"}:
            key = str(value["$param"])
            if key not in supplied or supplied[key] in (None, ""):
                missing.add(key)
                return value
            return supplied[key]
        return {
            key: _fill_parameters(child, supplied, missing)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_fill_parameters(child, supplied, missing) for child in value]
    return value


def list_saved_workflows(landlord, include_archived: str = "") -> dict:
    """List explicitly saved landlord workflows and their required parameters."""
    from .models import RamaSavedWorkflow

    qs = RamaSavedWorkflow.objects.filter(landlord=landlord)
    if not _truthy(include_archived):
        qs = qs.filter(archived_at__isnull=True)
    return {
        "workflows": [
            {
                "workflow_id": str(row.pk),
                "name": row.name,
                "version": row.version,
                "parameters": row.parameter_schema,
                "archived": bool(row.archived_at),
            }
            for row in qs
        ],
    }


def save_last_workflow(
    landlord,
    name: str,
    confirm: str = "",
) -> dict:
    """Explicitly save the latest successful multi/single-step RAMA task as a macro."""
    from .capability_contract import CHAT_EXCLUSIONS
    from .models import RamaSavedWorkflow
    from .models import RamaTask

    clean_name = str(name or "").strip()[:120]
    if not clean_name:
        return {"error": "Give the workflow a name."}
    task = (
        RamaTask.objects.filter(
            landlord=landlord, status=RamaTask.Status.VERIFIED, receipts__isnull=False,
        )
        .exclude(
            capability_key__in={
                "save_last_workflow",
                "rename_saved_workflow",
                "archive_saved_workflow",
                "restore_saved_workflow",
            },
        )
        .distinct()
        .order_by("-created_at")
        .first()
    )
    if task is None:
        return {
            "error": "There is no successful RAMA action in this portfolio to save yet.",
        }
    receipts = list(task.receipts.order_by("created_at"))
    blocked = [
        receipt.capability_key
        for receipt in receipts
        if receipt.capability_key in CHAT_EXCLUSIONS
    ]
    if blocked:
        return {
            "error": "That action contains a UI-only capability and cannot be saved: "
            + ", ".join(blocked),
        }
    source_steps = list((task.input or {}).get("steps") or [])
    if len(source_steps) != len(receipts):
        source_steps = [
            {
                "step_id": f"step-{index}",
                "tool": receipt.capability_key,
                "arguments": receipt.inputs or {},
                "target": receipt.capability_key,
                "item_key": f"step-{index}",
                "depends_on": [],
            }
            for index, receipt in enumerate(receipts, start=1)
        ]
    unsafe = sorted(
        {
            key
            for source in source_steps
            for key in (source.get("arguments") or {})
            if key.lower() in _WORKFLOW_FORBIDDEN_KEYS
            or any(
                marker in key.lower() for marker in ("password", "secret", "api_key")
            )
        },
    )
    if unsafe:
        return {
            "error": (
                "Workflows containing files, confirmation state, or credentials "
                "cannot be saved: " + ", ".join(unsafe)
            ),
        }
    schema = {}
    steps = []
    for index, source in enumerate(source_steps, start=1):
        step_id = str(source.get("step_id") or f"step-{index}")
        receipt = receipts[index - 1]
        steps.append(
            {
                "step_id": step_id,
                "tool": receipt.capability_key,
                "arguments": _parameterise_arguments(
                    source.get("arguments") or {}, step_id, schema,
                ),
                "target": source.get("target") or receipt.capability_key,
                "item_key": source.get("item_key") or step_id,
                "depends_on": list(source.get("depends_on") or []),
                "requires_own_confirm": bool(source.get("requires_own_confirm")),
            },
        )
    preview = {
        "name": clean_name,
        "source_task_id": str(task.pk),
        "steps": [row["tool"] for row in steps],
        "parameters": schema,
        "rule": "Runs always produce a fresh preview and confirmation.",
    }
    if not _confirmed(confirm):
        return _preview(
            "save_last_workflow",
            preview,
            "Saves capability keys and typed parameters only; no files, secrets, or confirmation state.",
        )
    if RamaSavedWorkflow.objects.filter(
        landlord=landlord, name=clean_name, archived_at__isnull=True,
    ).exists():
        return {
            "error": "A live saved workflow already has that name. Rename or archive it first.",
        }
    workflow = RamaSavedWorkflow.objects.create(
        landlord=landlord,
        name=clean_name,
        parameter_schema=schema,
        steps=steps,
        created_from_task=task,
    )
    return {
        "created": True,
        "workflow_id": str(workflow.pk),
        "name": workflow.name,
        "parameters": workflow.parameter_schema,
    }


def _resolve_workflow(landlord, query: str, *, include_archived: bool = False):
    from .models import RamaSavedWorkflow

    qs = RamaSavedWorkflow.objects.filter(landlord=landlord)
    if not include_archived:
        qs = qs.filter(archived_at__isnull=True)
    row = qs.filter(pk=query).first()
    if row:
        return row
    matches = list(qs.filter(name__icontains=query)[:4])
    if len(matches) != 1:
        raise ValueError(
            "No saved workflow matches."
            if not matches
            else "Several saved workflows match; use workflow_id.",
        )
    return matches[0]


def run_saved_workflow(
    landlord,
    workflow_query: str,
    parameters: str = "{}",
    confirm: str = "",
) -> dict:
    """Compile a saved workflow with fresh parameters into a normal pending plan."""
    from .plan_runner import validate_plan

    try:
        workflow = _resolve_workflow(landlord, workflow_query)
        supplied = _json_object(parameters, label="parameters")
    except ValueError as exc:
        return {"error": str(exc)}
    missing: set[str] = set()
    steps = []
    for saved in workflow.steps:
        step = dict(saved)
        step["arguments"] = _fill_parameters(
            saved.get("arguments") or {}, supplied, missing,
        )
        steps.append(step)
    if missing:
        return {
            "needs_input": True,
            "question_for_user": "This workflow needs: " + ", ".join(sorted(missing)),
            "missing_parameters": sorted(missing),
            "workflow_id": str(workflow.pk),
        }
    errors = validate_plan(steps, landlord)
    if errors:
        return {
            "error": "Saved workflow is stale or invalid: " + " ".join(errors),
            "workflow_id": str(workflow.pk),
        }
    plan = {
        "operation": f"saved_workflow:{workflow.pk}",
        "summary": f"Run saved workflow {workflow.name!r} ({len(steps)} step(s)).",
        "steps": steps,
        "blocked": [],
        "workflow_id": str(workflow.pk),
        "workflow_version": workflow.version,
    }
    return {
        "needs_confirm": True,
        "plan": plan,
        "preview": {
            "workflow_id": str(workflow.pk),
            "name": workflow.name,
            "parameters": supplied,
            "steps": [
                {
                    "step_id": row.get("step_id"),
                    "tool": row["tool"],
                    "target": row.get("target", ""),
                }
                for row in steps
            ],
        },
        "instruction": "Show the complete compiled workflow and wait for confirmation.",
    }


def rename_saved_workflow(
    landlord, workflow_query: str, new_name: str, confirm: str = "",
) -> dict:
    """Rename one explicitly saved workflow."""
    from .models import RamaSavedWorkflow

    try:
        workflow = _resolve_workflow(landlord, workflow_query)
    except ValueError as exc:
        return {"error": str(exc)}
    clean = str(new_name or "").strip()[:120]
    if not clean:
        return {"error": "new_name is required."}
    if (
        RamaSavedWorkflow.objects.filter(
            landlord=landlord, name=clean, archived_at__isnull=True,
        )
        .exclude(pk=workflow.pk)
        .exists()
    ):
        return {"error": "Another live workflow already has that name."}
    preview = {"workflow_id": str(workflow.pk), "from": workflow.name, "to": clean}
    if not _confirmed(confirm):
        return _preview(
            "rename_saved_workflow", preview, "Renames only the saved workflow label.",
        )
    previous = workflow.name
    workflow.name = clean
    workflow.save(update_fields=["name", "updated_at"])
    return {
        "updated": True,
        "workflow_id": str(workflow.pk),
        "previous_name": previous,
        "name": workflow.name,
    }


def archive_saved_workflow(landlord, workflow_query: str, confirm: str = "") -> dict:
    """Archive a saved workflow without deleting its definition."""
    try:
        workflow = _resolve_workflow(landlord, workflow_query)
    except ValueError as exc:
        return {"error": str(exc)}
    preview = {
        "workflow_id": str(workflow.pk),
        "name": workflow.name,
        "action": "archive",
    }
    if not _confirmed(confirm):
        return _preview(
            "archive_saved_workflow", preview, "The workflow can be restored later.",
        )
    workflow.archived_at = timezone.now()
    workflow.save(update_fields=["archived_at", "updated_at"])
    return {"archived": True, "workflow_id": str(workflow.pk), "name": workflow.name}


def restore_saved_workflow(landlord, workflow_query: str, confirm: str = "") -> dict:
    """Restore an archived saved workflow when its name is still available."""
    from .models import RamaSavedWorkflow

    try:
        workflow = _resolve_workflow(landlord, workflow_query, include_archived=True)
    except ValueError as exc:
        return {"error": str(exc)}
    if not workflow.archived_at:
        return {
            "already_done": True,
            "workflow_id": str(workflow.pk),
            "name": workflow.name,
        }
    if (
        RamaSavedWorkflow.objects.filter(
            landlord=landlord, name=workflow.name, archived_at__isnull=True,
        )
        .exclude(pk=workflow.pk)
        .exists()
    ):
        return {
            "error": "A live workflow now uses that name; rename one before restoring.",
        }
    preview = {
        "workflow_id": str(workflow.pk),
        "name": workflow.name,
        "action": "restore",
    }
    if not _confirmed(confirm):
        return _preview(
            "restore_saved_workflow",
            preview,
            "Restores the saved definition; it still previews before every run.",
        )
    workflow.archived_at = None
    workflow.save(update_fields=["archived_at", "updated_at"])
    return {"restored": True, "workflow_id": str(workflow.pk), "name": workflow.name}


# ---------------------------------------------------------------------------
# Lease form packs
# ---------------------------------------------------------------------------

_FORM_ACTIONS = {"catalog", "list", "attach", "send", "status", "void", "classify"}

#: Plain-English names for the three things a form can be for. The stage is the
#: fact that decides whether an unsigned form holds up a lease, so RAMA has to be
#: able to say it out loud rather than passing an enum around.
_STAGE_WORDS = {
    "WITH_LEASE": "signed with the lease (the lease can't activate until it is)",
    "ADDENDUM": "signed any time during the tenancy",
    "MOVE_OUT": "signed to end the tenancy",
    "UNCLASSIFIED": "not classified yet",
}


def _form_stage(raw: str) -> str:
    value = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "LEASE": "WITH_LEASE",
        "WITH_THE_LEASE": "WITH_LEASE",
        "SIGNING": "WITH_LEASE",
        "DURING": "ADDENDUM",
        "ANYTIME": "ADDENDUM",
        "END": "MOVE_OUT",
        "MOVEOUT": "MOVE_OUT",
        "END_OF_TENANCY": "MOVE_OUT",
    }
    return aliases.get(value, value)


def _form_row(form) -> dict:
    outstanding = [
        signer.display_name
        for signer in form.signers.all()
        if not signer.has_signed and not signer.declined_at
    ]
    return {
        "form_id": str(form.pk),
        "title": form.title,
        "lease": form.lease.lease_number or str(form.lease_id),
        "status": form.status,
        "stage": str(form.template.stage),
        "what_it_is_for": _STAGE_WORDS.get(str(form.template.stage), ""),
        "blocking_the_lease": form.blocks_activation,
        "signed_by": [s.display_name for s in form.signers.all() if s.has_signed],
        "waiting_on": outstanding,
    }


def _template_row(template) -> dict:
    return {
        "form_code": template.code or str(template.pk),
        "name": template.name,
        "purpose": template.purpose,
        "jurisdiction": template.jurisdiction or "any",
        "stage": str(template.stage),
        "what_it_is_for": _STAGE_WORDS.get(str(template.stage), ""),
        "available": template.is_selectable,
        "note": (
            ""
            if template.is_selectable
            else "Catalogued but not shipped yet — it can't be attached."
        ),
    }


def _resolve_form_template(landlord, form_code: str):
    from rentium.leases.form_services import catalog_for

    wanted = str(form_code or "").strip()
    if not wanted:
        raise ValueError("Say which form — pass form_code, e.g. BC_RTB8.")
    rows = catalog_for(landlord)
    exact = [
        row
        for row in rows
        if row.code.casefold() == wanted.casefold() or str(row.pk) == wanted
    ]
    if exact:
        return exact[0]
    partial = [row for row in rows if wanted.casefold() in row.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(
            f"No form matching {wanted!r}. Use action=catalog to see what exists."
        )
    names = "; ".join(f"{row.code or row.name}" for row in partial[:6])
    raise ValueError(f"More than one form matches {wanted!r}: {names}.")


def _params_lease_forms(fn):
    fn.param_docs = {
        "action": (
            "catalog (what forms exist), list (forms on a lease), attach, send, "
            "status, void, or classify (say what an uploaded form is for)."
        ),
        "lease_query": "Property name or address whose lease this is about.",
        "lease_number": "Exact lease number, if you have it.",
        "form_code": "Catalogue code such as BC_RTB8, or part of the form's name.",
        "attachment_id": (
            "The id from an attached PDF in this chat, to add it as a NEW custom "
            "form instead of picking one from the catalogue."
        ),
        "stage": (
            "What the form is for: WITH_LEASE, ADDENDUM, or MOVE_OUT. Required "
            "when attaching a PDF the landlord just sent — never guess it."
        ),
        "title": "What to call this form on the lease.",
        "signer_name": "Name of a signer who is not on the lease yet.",
        "signer_email": "Where to send that person's signing link.",
        "form_id": "Exact id of an already-attached form (for send/void).",
        "confirm": "Pass yes to run the previewed action.",
    }
    return fn


@_params_lease_forms
def manage_lease_forms(  # noqa: PLR0913 - explicit public tool fields
    landlord,
    action: str,
    lease_query: str = "",
    lease_number: str = "",
    form_code: str = "",
    attachment_id: str = "",
    stage: str = "",
    title: str = "",
    signer_name: str = "",
    signer_email: str = "",
    form_id: str = "",
    confirm: str = "",
) -> dict:
    """Attach extra documents to a lease and send them for signature.

    Use for RTB-8 (BC mutual agreement to end a tenancy), addendums, and any PDF
    the landlord sends you. Each form has a STAGE that decides what it does:
    WITH_LEASE holds up activation until signed, ADDENDUM is signed any time,
    MOVE_OUT ends the tenancy. Never guess the stage of an uploaded PDF — ask.
    Actions: catalog, list, attach, send, status, void, classify. Preview first,
    then confirm=yes.
    """
    from rentium.leases import form_services as forms

    op = str(action or "").strip().lower()
    if op not in _FORM_ACTIONS:
        return {"error": "action must be one of: " + ", ".join(sorted(_FORM_ACTIONS))}

    if op == "catalog":
        rows = forms.catalog_for(landlord)
        return {
            "forms": [_template_row(row) for row in rows],
            "note": (
                "Only forms marked available can be attached. To use one that "
                "isn't listed, ask the landlord to send the PDF in this chat."
            ),
        }

    try:
        return _run_lease_form_action(
            landlord,
            op,
            lease_query=lease_query,
            lease_number=lease_number,
            form_code=form_code,
            attachment_id=attachment_id,
            stage=stage,
            title=title,
            signer_name=signer_name,
            signer_email=signer_email,
            form_id=form_id,
            confirm=confirm,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except ValidationError as exc:
        return {"error": "; ".join(exc.messages)}


def _lease_form_by_id(landlord, form_id: str):
    from rentium.leases.lease_forms import LeaseForm

    form = (
        LeaseForm.objects.filter(pk=str(form_id).strip(), lease__landlord=landlord)
        .select_related("lease", "template")
        .first()
    )
    if form is None:
        raise ValueError(f"No attached form with id {form_id!r} on your leases.")
    return form


def _resolve_form_lease(landlord, *, lease_query: str, lease_number: str):
    from .domain_crud import _resolve_lease

    lease, error = _resolve_lease(
        landlord, property_query=lease_query, lease_number=lease_number
    )
    if error:
        raise ValueError(str(error))
    return lease


def _run_lease_form_action(  # noqa: PLR0911, PLR0912 - one branch per action
    landlord,
    op: str,
    *,
    lease_query: str,
    lease_number: str,
    form_code: str,
    attachment_id: str,
    stage: str,
    title: str,
    signer_name: str,
    signer_email: str,
    form_id: str,
    confirm: str,
) -> dict:
    from rentium.leases import form_services as forms
    from rentium.leases.lease_forms import LeaseForm

    if op in {"send", "void", "status"} and str(form_id or "").strip():
        form = _lease_form_by_id(landlord, form_id)
    else:
        form = None

    # ------------------------------------------------------------- list
    if op == "list":
        lease = _resolve_form_lease(
            landlord, lease_query=lease_query, lease_number=lease_number
        )
        rows = lease.lease_forms.select_related("template").prefetch_related("signers")
        return {
            "lease": lease.lease_number,
            "forms": [_form_row(row) for row in rows],
            "blocking_activation": [
                str(row.title) for row in forms.blocking_forms(lease)
            ],
        }

    # ----------------------------------------------------------- status
    if op == "status":
        if form is None:
            lease = _resolve_form_lease(
                landlord, lease_query=lease_query, lease_number=lease_number
            )
            return {
                "lease": lease.lease_number,
                "lease_status": lease.status,
                "waiting_on": [str(r) for r in forms.activation_blockers(lease)],
                "forms": [
                    _form_row(row)
                    for row in lease.lease_forms.select_related("template")
                ],
            }
        payload = _form_row(form)
        if form.moveout_request_id:
            payload["ends_tenancy_on"] = str(form.moveout_request.requested_end_date)
        return payload

    # --------------------------------------------------------- classify
    if op == "classify":
        template = _resolve_form_template(landlord, form_code)
        wanted = _form_stage(stage)
        if wanted not in {"WITH_LEASE", "ADDENDUM", "MOVE_OUT"}:
            return _ask_form_stage(template)
        if template.landlord_id is None:
            return {"error": "Built-in forms already have a purpose set."}
        if not _confirmed(confirm):
            return _preview(
                "manage_lease_forms",
                {
                    "action": "classify",
                    "form": template.name,
                    "stage": wanted,
                    "means": _STAGE_WORDS[wanted],
                },
                "Only records what the form is for; nothing is sent to anyone.",
            )
        template.stage = wanted
        template.save(update_fields=["stage", "updated_at"])
        return {
            "classified": True,
            "form_code": template.code or str(template.pk),
            "stage": wanted,
        }

    # ----------------------------------------------------------- attach
    if op == "attach":
        lease = _resolve_form_lease(
            landlord, lease_query=lease_query, lease_number=lease_number
        )
        if str(attachment_id or "").strip():
            template, question = _template_from_attachment(
                landlord, attachment_id, stage=stage, title=title
            )
            if question is not None:
                return question
        else:
            template = _resolve_form_template(landlord, form_code)
            if str(template.stage) == "UNCLASSIFIED":
                return _ask_form_stage(template)
        if not template.is_selectable:
            return {
                "error": (
                    f"{template.name} is in the catalogue but isn't shipped yet, "
                    "so it can't be attached."
                )
            }

        preview = {
            "action": "attach",
            "form": template.name,
            "lease": lease.lease_number,
            "property": lease.property.name if lease.property_id else "",
            "stage": str(template.stage),
            "means": _STAGE_WORDS.get(str(template.stage), ""),
            "holds_up_activation": (
                str(template.stage) == "WITH_LEASE"
                and lease.status != lease.LeaseStatus.ACTIVE
            ),
        }
        if not _confirmed(confirm):
            return _preview(
                "manage_lease_forms",
                preview,
                "Attaches the blank form to the lease. Nobody is emailed until "
                "you send it.",
            )
        attached = forms.attach_form(
            lease,
            template,
            title=title,
            created_via=LeaseForm.CreatedVia.RAMA,
            source_attachment_id=attachment_id,
        )
        # "created", not "attached": the claimed-write guard reads result flags
        # to decide whether a turn really wrote anything, and `attached` is
        # already used elsewhere as descriptive data about a file. A write it
        # cannot see gets reported to the landlord as not having happened.
        return {"created": True, **_form_row(attached)}

    # ------------------------------------------------------------- send
    if op == "send":
        if form is None:
            lease = _resolve_form_lease(
                landlord, lease_query=lease_query, lease_number=lease_number
            )
            pending = list(
                lease.lease_forms.filter(status=LeaseForm.Status.DRAFT).select_related(
                    "template"
                )
            )
            if not pending:
                return {
                    "already_done": True,
                    "message": "Every form on that lease has already been sent.",
                }
            if len(pending) > 1:
                return {
                    "needs_input": True,
                    "question_for_user": (
                        "Which form should I send? "
                        + "; ".join(f"{row.title}" for row in pending)
                    ),
                    "candidates": [_form_row(row) for row in pending],
                    "relay_instruction": (
                        "Ask question_for_user VERBATIM, then STOP and wait."
                    ),
                }
            form = pending[0]

        manual = {}
        if signer_email:
            manual["TENANT:0"] = {"name": signer_name, "email": signer_email}

        roster = forms.roster_candidates(form.lease)
        recipients = []
        for role, index in forms.required_roles(form):
            details = roster.get((role, index)) or {}
            name = details.get("name") or signer_name
            email = details.get("email") or signer_email
            recipients.append(
                {"role": role, "name": name or "(nobody assigned)", "email": email}
            )

        if not _confirmed(confirm):
            return _preview(
                "manage_lease_forms",
                {
                    "action": "send",
                    "form": form.title,
                    "lease": form.lease.lease_number,
                    "emails": recipients,
                },
                "Emails each person a personal signing link they can use without "
                "a Rentium account.",
            )
        forms.send_form(form, manual_signers=manual)
        form.refresh_from_db()
        return {"sent": True, **_form_row(form)}

    # ------------------------------------------------------------- void
    if form is None:
        raise ValueError("Pass form_id to void a specific form.")
    if not _confirmed(confirm):
        return _preview(
            "manage_lease_forms",
            {"action": "void", "form": form.title, "lease": form.lease.lease_number},
            "Withdraws the form. Signatures already collected are kept as "
            "evidence but the document is no longer live.",
        )
    forms.void_form(form)
    form.refresh_from_db()
    return {"voided": True, **_form_row(form)}


def _ask_form_stage(template) -> dict:
    """Ask what a form is for, offering the reading OCR came up with.

    A yes/no ("is this an RTB-8?") invites a weak model to answer on the
    landlord's behalf. Three named options do not.
    """
    hint = ""
    if template.suggested_stage:
        hint = (
            f" It reads like something {_STAGE_WORDS.get(template.suggested_stage, '')}"
            f"{' — ' + template.suggested_purpose if template.suggested_purpose else ''}"
        )
    return {
        "needs_input": True,
        "question_for_user": (
            f"What is {template.name} for?{hint} Is it signed with the lease, "
            f"signed any time during the tenancy, or signed to end the tenancy?"
        ),
        "form_code": template.code or str(template.pk),
        "suggested_stage": template.suggested_stage or None,
        "relay_instruction": (
            "Ask the landlord question_for_user VERBATIM, then STOP and wait. Do "
            "NOT attach or classify anything yet, and do NOT pick a stage yourself."
        ),
    }


def _template_from_attachment(landlord, attachment_id: str, *, stage: str, title: str):
    """Turn a PDF the landlord sent in chat into a reusable form template.

    Returns (template, question). A question means we read the file but still do
    not know what it is for, and guessing that would decide whether an unsigned
    document holds up somebody's tenancy.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from rentium.leases import form_services as forms

    from .models import RamaAttachment

    attachment = (
        RamaAttachment.objects.select_related("batch")
        .filter(pk=str(attachment_id).strip(), batch__landlord=landlord)
        .first()
    )
    if attachment is None:
        raise ValueError(
            f"No attachment {attachment_id!r} in this conversation. Ask the "
            "landlord to send the PDF again."
        )
    if not str(attachment.original_filename or "").casefold().endswith(".pdf"):
        raise ValueError("Lease forms have to be PDFs.")

    attachment.original.open("rb")
    try:
        data = attachment.original.read()
    finally:
        attachment.original.close()

    template, _created = forms.upload_template(
        landlord,
        SimpleUploadedFile(
            attachment.original_filename, data, content_type="application/pdf"
        ),
        name=title,
        stage=_form_stage(stage) if stage else "",
    )
    attachment.classification = RamaAttachment.Classification.DOCUMENT
    attachment.target_type = "leases.LeaseFormTemplate"
    attachment.target_id = str(template.pk)
    attachment.save(update_fields=["classification", "target_type", "target_id"])

    if str(template.stage) == "UNCLASSIFIED":
        return template, _ask_form_stage(template)
    return template, None
