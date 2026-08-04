"""
Strong read-only domain surfaces for RAMA (and reusable without the model).

Each function returns JSON-serializable dicts scoped to one landlord.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Prefetch, Q
from django.utils import timezone


def _prop_name(obj) -> str:
    if obj is None:
        return ""
    return getattr(obj, "name", "") or ""


def _match_property(name: str, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    return q in (name or "").lower()


# ---------------------------------------------------------------------------
# Work orders (strong)
# ---------------------------------------------------------------------------


def _who_pays(wo) -> str:
    """One plain sentence, so a weak model relays rather than reasons.

    "Who pays for this?" is a question about THIS job's record, not about
    policy. Answer it from the record.
    """
    if wo.tenant_chargeable and wo.responsible_tenant_id:
        who = wo.responsible_tenant.user.name
        if wo.cost is None:
            return (
                f"{who} — charged to them once a cost is recorded on the job."
            )
        return (
            f"{who} — ${wo.cost} has been charged to their ledger as a claim "
            "they owe. It is NOT taken from their deposit automatically: that "
            "needs their written agreement or an RTB application within 15 "
            "days of the tenancy ending."
        )
    if wo.responsible_tenant_id:
        return (
            f"{wo.responsible_tenant.user.name} caused it, but it is not marked "
            "chargeable, so the landlord is paying. Use attribute_work_order "
            "with chargeable=yes to claim it from them."
        )
    return (
        "The landlord — nobody has been recorded as responsible. If a tenant "
        "caused it, use attribute_work_order to say who."
    )


def list_work_orders(
    landlord,
    *,
    include_closed: bool = False,
    property_query: str = "",
    status: str = "",
    priority: str = "",
    limit: int = 40,
) -> dict:
    from rentium.maintenance.models import WorkOrder

    today = date.today()
    qs = WorkOrder.objects.for_landlord(landlord).select_related(
        "property", "unit", "unit__holding", "area", "lease",
        "responsible_tenant__user",
    )
    if not include_closed:
        qs = qs.exclude(
            status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED]
        )
    st = (status or "").strip().upper()
    if st:
        qs = qs.filter(status=st)
    pr = (priority or "").strip().upper()
    if pr:
        qs = qs.filter(priority=pr)
    pq = (property_query or "").strip()
    if pq:
        qs = qs.filter(
            Q(property__name__icontains=pq)
            | Q(title__icontains=pq)
            | Q(description__icontains=pq)
        )

    qs = qs.order_by("-priority", "sla_due_at", "-created_at")[
        : max(1, min(limit, 100))
    ]
    rows = []
    open_count = 0
    breached = 0
    for wo in qs:
        is_open = wo.status not in (
            WorkOrder.Status.COMPLETED,
            WorkOrder.Status.CANCELLED,
        )
        if is_open:
            open_count += 1
        sla_breached = bool(getattr(wo, "sla_breached", False))
        if sla_breached:
            breached += 1
        rows.append(
            {
                "id": str(wo.pk),
                "title": wo.title,
                "description": (wo.description or "")[:400],
                # place_name covers both targets; wo.property is null for a
                # fault in shared space, which used to render as "".
                "property": wo.place_name,
                "area": wo.area.name if wo.area_id else "",
                "lease_id": str(wo.lease_id) if wo.lease_id else None,
                "status": wo.status,
                "status_display": wo.get_status_display(),
                "priority": wo.priority,
                "priority_display": wo.get_priority_display(),
                "category": wo.category,
                "category_display": wo.get_category_display(),
                "origin": wo.origin,
                "origin_display": wo.get_origin_display(),
                "scheduled_date": wo.scheduled_date.isoformat()
                if wo.scheduled_date
                else None,
                "completed_date": wo.completed_date.isoformat()
                if wo.completed_date
                else None,
                "cost": str(wo.cost) if wo.cost is not None else None,
                # WHO PAYS. Asked "who will pay for this?", RAMA could not see
                # any of this and proposed a Constitution amendment instead of
                # reading the answer that was already recorded.
                "responsible_tenant": (
                    wo.responsible_tenant.user.name
                    if wo.responsible_tenant_id
                    else None
                ),
                "tenant_chargeable": wo.tenant_chargeable,
                "who_pays": _who_pays(wo),
                "contractor_name": wo.contractor_name or "",
                "contractor_phone": str(wo.contractor_phone or ""),
                "sla_due_at": wo.sla_due_at.isoformat() if wo.sla_due_at else None,
                "sla_breached": sla_breached,
                "is_rta_emergency": bool(getattr(wo, "is_rta_emergency", False)),
                "created_at": wo.created_at.isoformat() if wo.created_at else None,
                "is_open": is_open,
            }
        )

    # Portfolio open counts (not limited by include_closed filter alone)
    base = WorkOrder.objects.for_landlord(landlord)
    counts = {
        "returned": len(rows),
        "open_in_portfolio": base.exclude(
            status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED]
        ).count(),
        "new": base.filter(status=WorkOrder.Status.NEW).count(),
        "in_progress": base.filter(status=WorkOrder.Status.IN_PROGRESS).count(),
        "scheduled": base.filter(status=WorkOrder.Status.SCHEDULED).count(),
        "completed": base.filter(status=WorkOrder.Status.COMPLETED).count(),
    }
    return {
        "as_of": today.isoformat(),
        "include_closed": include_closed,
        "filters": {
            "property_query": property_query or "",
            "status": status or "",
            "priority": priority or "",
        },
        "counts": counts,
        "work_orders": rows,
        "rules": {
            "open": "NEW, SCHEDULED, IN_PROGRESS are open. COMPLETED/CANCELLED are closed.",
            "person": (
                "Answer 'any work orders?' with counts.open_in_portfolio and list titles. "
                "Mention priority, property, SLA breach, contractor if present."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Inquiries / applications (showcase interest)
# ---------------------------------------------------------------------------


def list_inquiries(
    landlord,
    *,
    status: str = "",
    property_query: str = "",
    include_archived: bool = False,
    limit: int = 40,
) -> dict:
    from rentium.showcase.models import Inquiry

    today = date.today()
    qs = Inquiry.objects.filter(landlord=landlord).select_related("property")
    if not include_archived:
        qs = qs.exclude(status__in=[Inquiry.Status.ARCHIVED, Inquiry.Status.SPAM])
    st = (status or "").strip().upper()
    if st:
        qs = qs.filter(status=st)
    pq = (property_query or "").strip()
    if pq:
        qs = qs.filter(
            Q(property__name__icontains=pq)
            | Q(name__icontains=pq)
            | Q(email__icontains=pq)
            | Q(message__icontains=pq)
        )
    qs = qs.order_by("-created_at")[: max(1, min(limit, 100))]
    rows = []
    for inq in qs:
        rows.append(
            {
                "id": str(inq.pk),
                "name": inq.name,
                "email": inq.email,
                "phone": str(inq.phone or ""),
                "property": inq.property.name if inq.property_id else "",
                "message": (inq.message or "")[:500],
                "move_in_target": inq.move_in_target.isoformat()
                if inq.move_in_target
                else None,
                "status": inq.status,
                "status_display": inq.get_status_display(),
                "landlord_notes": (inq.landlord_notes or "")[:300],
                "responded_at": inq.responded_at.isoformat()
                if inq.responded_at
                else None,
                "appointment_id": str(inq.appointment_id)
                if inq.appointment_id
                else None,
                "created_at": inq.created_at.isoformat() if inq.created_at else None,
            }
        )
    base = Inquiry.objects.filter(landlord=landlord)
    return {
        "as_of": today.isoformat(),
        "counts": {
            "returned": len(rows),
            "new": base.filter(status=Inquiry.Status.NEW).count(),
            "replied": base.filter(status=Inquiry.Status.REPLIED).count(),
            "all": base.count(),
        },
        "inquiries": rows,
        "rules": {
            "person": (
                "Inquiries are interest messages from the public listing page "
                "(not signed applications). NEW needs a reply. "
                "If empty, say no inquiries in the system."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Messages / threads
# ---------------------------------------------------------------------------


def list_conversations(
    landlord,
    *,
    tenant_query: str = "",
    limit: int = 30,
) -> dict:
    from rentium.messaging.models import Conversation, Message

    today = date.today()
    qs = (
        Conversation.objects.filter(landlord=landlord)
        .select_related("tenant__user", "lease", "lease__property", "lease__group")
        .annotate(message_count=Count("messages"))
        .order_by("-updated_at")
    )
    tq = (tenant_query or "").strip()
    if tq:
        qs = qs.filter(
            Q(tenant__user__name__icontains=tq)
            | Q(tenant__user__email__icontains=tq)
            | Q(subject__icontains=tq)
            | Q(lease__property__name__icontains=tq)
        )
    qs = qs[: max(1, min(limit, 80))]
    rows = []
    unread_total = 0
    for conv in qs:
        tenant_name = ""
        tenant_email = ""
        if conv.tenant_id and conv.tenant.user_id:
            tenant_name = conv.tenant.user.name or ""
            tenant_email = conv.tenant.user.email or ""
        place = ""
        if conv.lease_id:
            if conv.lease.property_id:
                place = conv.lease.property.name
            elif conv.lease.group_id:
                place = conv.lease.group.name
        last = (
            Message.objects.filter(conversation=conv)
            .select_related("sender")
            .order_by("-created_at")
            .first()
        )
        unread = Message.objects.filter(
            conversation=conv, read_at__isnull=True
        ).exclude(sender_id=landlord.user_id).count()
        unread_total += unread
        rows.append(
            {
                "conversation_id": str(conv.pk),
                "subject": conv.subject or "(no subject)",
                "tenant_name": tenant_name,
                "tenant_email": tenant_email,
                "property": place,
                "lease_id": str(conv.lease_id) if conv.lease_id else None,
                "message_count": conv.message_count,
                "unread_from_tenant": unread,
                "last_message_preview": (last.body or "")[:200] if last else "",
                "last_message_at": last.created_at.isoformat() if last else None,
                "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            }
        )
    return {
        "as_of": today.isoformat(),
        "counts": {
            "conversations": len(rows),
            "unread_messages_approx": unread_total,
        },
        "conversations": rows,
        "rules": {
            "person": (
                "Threads are landlord↔tenant conversations. "
                "Use list_messages(conversation_id=...) for full body. "
                "If empty, say no message threads."
            ),
        },
    }


def list_messages(
    landlord,
    *,
    conversation_id: str = "",
    tenant_query: str = "",
    limit: int = 40,
) -> dict:
    from rentium.messaging.models import Conversation, Message

    today = date.today()
    if conversation_id:
        try:
            conv = Conversation.objects.select_related("tenant__user", "lease").get(
                pk=conversation_id, landlord=landlord
            )
        except (Conversation.DoesNotExist, ValueError):
            return {"error": f"No conversation {conversation_id!r} for this landlord."}
        msgs = (
            Message.objects.filter(conversation=conv)
            .select_related("sender")
            .order_by("-created_at")[: max(1, min(limit, 100))]
        )
        tenant_name = (
            conv.tenant.user.name if conv.tenant_id and conv.tenant.user_id else ""
        )
        rows = []
        unread_ids = []
        for m in reversed(list(msgs)):
            sender_label = "unknown"
            if m.sender_id:
                if m.sender_id == landlord.user_id:
                    sender_label = "landlord"
                else:
                    sender_label = "tenant"
            # Unread for landlord = from tenant, not yet read
            unread_for_landlord = (
                sender_label == "tenant" and m.read_at is None
            )
            if unread_for_landlord:
                unread_ids.append(str(m.pk))
            rows.append(
                {
                    "id": str(m.pk),
                    "body": (m.body or "")[:1000],
                    "sender": sender_label,
                    "sender_name": (m.sender.name if m.sender_id else "") or "",
                    "read_at": m.read_at.isoformat() if m.read_at else None,
                    "is_unread_for_landlord": unread_for_landlord,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
            )
        return {
            "as_of": today.isoformat(),
            "conversation_id": str(conv.pk),
            "tenant_name": tenant_name,
            "subject": conv.subject or "",
            "messages": rows,
            "count": len(rows),
            "unread_for_landlord_count": len(unread_ids),
            "unread_message_ids": unread_ids,
            "rules": {
                "unread": (
                    "is_unread_for_landlord=true means the landlord has not read "
                    "a tenant message. Use mark_messages_read (confirm=yes) to clear."
                ),
            },
        }

    # No id: recent messages across threads
    conv_ids = list(
        Conversation.objects.filter(landlord=landlord).values_list("id", flat=True)[
            :100
        ]
    )
    qs = (
        Message.objects.filter(conversation_id__in=conv_ids)
        .select_related("conversation__tenant__user", "sender")
        .order_by("-created_at")
    )
    tq = (tenant_query or "").strip()
    if tq:
        qs = qs.filter(
            Q(conversation__tenant__user__name__icontains=tq)
            | Q(conversation__tenant__user__email__icontains=tq)
            | Q(body__icontains=tq)
        )
    qs = qs[: max(1, min(limit, 80))]
    rows = []
    for m in qs:
        rows.append(
            {
                "id": str(m.pk),
                "conversation_id": str(m.conversation_id),
                "tenant_name": (
                    m.conversation.tenant.user.name
                    if m.conversation.tenant_id and m.conversation.tenant.user_id
                    else ""
                ),
                "body": (m.body or "")[:400],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )
    return {
        "as_of": today.isoformat(),
        "messages": rows,
        "count": len(rows),
        "hint": "Pass conversation_id for a full thread.",
    }


# ---------------------------------------------------------------------------
# Condition inspections + move-in / move-out
# ---------------------------------------------------------------------------


def list_inspections(
    landlord,
    *,
    property_query: str = "",
    status: str = "",
    include_items: bool = True,
    limit: int = 40,
) -> dict:
    from rentium.leases.inspections import ConditionInspection

    today = date.today()
    qs = (
        ConditionInspection.objects.filter(lease__landlord=landlord)
        .select_related(
            "lease",
            "lease__property",
            "lease__group",
            "lease_tenant",
            "lease_tenant__tenant__user",
        )
        .prefetch_related("items")
        .order_by("-created_at")
    )
    st = (status or "").strip().upper()
    if st:
        qs = qs.filter(status=st)
    pq = (property_query or "").strip()
    if pq:
        qs = qs.filter(
            Q(lease__property__name__icontains=pq)
            | Q(lease__group__name__icontains=pq)
            | Q(lease__lease_number__icontains=pq)
        )
    qs = qs[: max(1, min(limit, 80))]
    rows = []
    for insp in qs:
        place = (
            insp.lease.property.name
            if insp.lease.property_id
            else (insp.lease.group.name if insp.lease.group_id else "")
        )
        tenant = ""
        if insp.lease_tenant_id:
            tenant = insp.lease_tenant.display_name
        row = {
            "id": str(insp.pk),
            "lease_id": str(insp.lease_id),
            "lease_number": insp.lease.lease_number or "",
            "property": place,
            "tenant": tenant,
            "status": insp.status,
            "status_display": insp.get_status_display(),
            "possession_date": insp.possession_date.isoformat()
            if insp.possession_date
            else None,
            "move_in_inspection_date": insp.move_in_inspection_date.isoformat()
            if insp.move_in_inspection_date
            else None,
            "move_out_date": insp.move_out_date.isoformat()
            if insp.move_out_date
            else None,
            "move_out_inspection_date": insp.move_out_inspection_date.isoformat()
            if insp.move_out_inspection_date
            else None,
            "landlord_signed_move_in": bool(insp.landlord_signed_move_in_at),
            "tenant_signed_move_in": bool(insp.tenant_signed_move_in_at),
            "landlord_signed_move_out": bool(insp.landlord_signed_move_out_at),
            "tenant_signed_move_out": bool(insp.tenant_signed_move_out_at),
            "move_in_report_delivered_at": insp.move_in_report_delivered_at.isoformat()
            if insp.move_in_report_delivered_at
            else None,
            "move_out_report_delivered_at": insp.move_out_report_delivered_at.isoformat()
            if insp.move_out_report_delivered_at
            else None,
            "repairs_required_at_start": (insp.repairs_required_at_start or "")[:300],
            "created_at": insp.created_at.isoformat()
            if getattr(insp, "created_at", None)
            else None,
            "item_count": 0,
            "needs_attention_count": 0,
        }
        if include_items:
            by_section: dict[str, list] = {}
            needs = 0
            for item in insp.items.all():
                section = item.section or "Other"
                by_section.setdefault(section, []).append(
                    {
                        "label": item.label,
                        "move_in_condition": item.move_in_condition_code or "",
                        "move_in_cleanliness": item.move_in_cleanliness_code or "",
                        "move_in_comment": (item.move_in_comment or "")[:120],
                        "move_out_condition": item.move_out_condition_code or "",
                        "move_out_cleanliness": item.move_out_cleanliness_code or "",
                        "move_out_comment": (item.move_out_comment or "")[:120],
                        "needs_attention": bool(item.needs_attention),
                        "suggestion_status": item.suggestion_status,
                        "is_custom": bool(item.is_custom),
                    }
                )
                if item.needs_attention:
                    needs += 1
            row["item_count"] = sum(len(v) for v in by_section.values())
            row["needs_attention_count"] = needs
            row["checklist_by_section"] = {
                sec: items[:40] for sec, items in by_section.items()
            }
            row["sections"] = list(by_section.keys())
        rows.append(row)
    return {
        "as_of": today.isoformat(),
        "counts": {"returned": len(rows)},
        "inspections": rows,
        "rules": {
            "person": (
                "Condition inspections are RTB-27 style move-in/move-out reports. "
                "checklist_by_section groups line items (Bedroom, Kitchen…). "
                "needs_attention items may become work orders. "
                "MOVE_IN_IN_PROGRESS = still need to complete/sign. "
                "If empty, say none on file (attention may still flag missing ones)."
            ),
        },
    }


def list_move_events(
    landlord,
    *,
    days_ahead: int = 220,
    days_past: int = 30,
    property_query: str = "",
    limit: int = 50,
) -> dict:
    """Upcoming and recent move-ins (lease start) and move-outs (end / requests)."""
    from rentium.leases.models import Lease
    from rentium.leases.moveout import MoveOutRequest

    today = date.today()
    start_win = today - timedelta(days=max(0, days_past))
    end_win = today + timedelta(days=max(1, days_ahead))
    pq = (property_query or "").strip()

    move_ins = []
    leases = (
        Lease.objects.filter(landlord=landlord)
        .exclude(status=Lease.LeaseStatus.DRAFT)
        .filter(start_date__gte=start_win, start_date__lte=end_win)
        .select_related("property", "group")
        .prefetch_related("lease_tenants")
        .order_by("start_date")
    )
    if pq:
        leases = leases.filter(
            Q(property__name__icontains=pq) | Q(group__name__icontains=pq)
        )
    for lease in leases[:limit]:
        place = (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else "")
        )
        tenants = [lt.display_name for lt in lease.lease_tenants.all()[:5]]
        move_ins.append(
            {
                "kind": "move_in",
                "date": lease.start_date.isoformat() if lease.start_date else None,
                "property": place,
                "lease_id": str(lease.pk),
                "lease_number": lease.lease_number or "",
                "lease_status": lease.status,
                "tenants": tenants,
                "relative": "past"
                if lease.start_date and lease.start_date < today
                else (
                    "today"
                    if lease.start_date == today
                    else "upcoming"
                ),
            }
        )

    move_outs = []
    # Lease end dates
    ending = (
        Lease.objects.filter(landlord=landlord)
        .exclude(status=Lease.LeaseStatus.DRAFT)
        .filter(end_date__isnull=False, end_date__gte=start_win, end_date__lte=end_win)
        .select_related("property", "group")
        .prefetch_related("lease_tenants")
        .order_by("end_date")
    )
    if pq:
        ending = ending.filter(
            Q(property__name__icontains=pq) | Q(group__name__icontains=pq)
        )
    for lease in ending[:limit]:
        place = (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else "")
        )
        move_outs.append(
            {
                "kind": "move_out_lease_end",
                "date": lease.end_date.isoformat() if lease.end_date else None,
                "property": place,
                "lease_id": str(lease.pk),
                "lease_number": lease.lease_number or "",
                "lease_status": lease.status,
                "tenants": [lt.display_name for lt in lease.lease_tenants.all()[:5]],
                "source": "lease_end_date",
            }
        )

    # Explicit move-out requests
    reqs = (
        MoveOutRequest.objects.filter(lease__landlord=landlord)
        .select_related("lease", "lease__property", "lease__group", "lease_tenant")
        .order_by("-created_at")[:limit]
    )
    if pq:
        reqs = reqs.filter(
            Q(lease__property__name__icontains=pq)
            | Q(lease__group__name__icontains=pq)
        )
    request_rows = []
    for req in reqs:
        place = (
            req.lease.property.name
            if req.lease.property_id
            else (req.lease.group.name if req.lease.group_id else "")
        )
        request_rows.append(
            {
                "kind": "move_out_request",
                "id": str(req.pk),
                "status": req.status,
                "status_display": req.get_status_display(),
                "request_kind": req.kind,
                "request_kind_display": req.get_kind_display(),
                "requested_end_date": req.requested_end_date.isoformat()
                if req.requested_end_date
                else None,
                "effective_end_date": req.effective_end_date.isoformat()
                if req.effective_end_date
                else None,
                "property": place,
                "lease_id": str(req.lease_id),
                "lease_number": req.lease.lease_number or "",
                "tenant": req.lease_tenant.display_name if req.lease_tenant_id else "",
                "initiated_by": req.initiated_by,
                "reason": (req.reason or "")[:200],
            }
        )

    return {
        "as_of": today.isoformat(),
        "window": {
            "from": start_win.isoformat(),
            "to": end_win.isoformat(),
        },
        "move_ins": move_ins,
        "move_outs_from_lease_end": move_outs,
        "move_out_requests": request_rows,
        "counts": {
            "move_ins": len(move_ins),
            "lease_ends": len(move_outs),
            "move_out_requests": len(request_rows),
        },
        "rules": {
            "person": (
                "Move-in = lease start_date. Move-out = lease end_date and/or "
                "move_out_requests. Room E starting Aug 1 is an upcoming move-in."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Inventory / furniture
# ---------------------------------------------------------------------------


def list_inventory(
    landlord,
    *,
    property_query: str = "",
    include_shared: bool = True,
    limit: int = 80,
) -> dict:
    from rentium.properties.models import InventoryItem, Property, SharedInventoryItem

    today = date.today()
    pq = (property_query or "").strip()
    prop_qs = Property.objects.filter(landlord=landlord)
    if pq:
        prop_qs = prop_qs.filter(Q(name__icontains=pq) | Q(group__name__icontains=pq))
    prop_ids = list(prop_qs.values_list("id", flat=True))

    private = (
        InventoryItem.objects.filter(property_id__in=prop_ids)
        .select_related("property")
        .order_by("property__name", "location_description", "name")[
            : max(1, min(limit, 150))
        ]
    )
    private_rows = [
        {
            "scope": "private",
            "name": item.name,
            "quantity": item.quantity,
            "condition": item.condition or "",
            "condition_display": item.get_condition_display()
            if item.condition
            else "",
            "location": item.location_description or "",
            "description": (item.description or "")[:200],
            "property": item.property.name if item.property_id else "",
        }
        for item in private
    ]

    shared_rows = []
    if include_shared:
        group_ids = list(
            Property.objects.filter(landlord=landlord, group_id__isnull=False)
            .values_list("group_id", flat=True)
            .distinct()
        )
        if pq:
            from rentium.properties.models import PropertyGroup

            group_ids = list(
                PropertyGroup.objects.filter(
                    landlord=landlord, id__in=group_ids
                )
                .filter(Q(name__icontains=pq) | Q(grouped_properties__name__icontains=pq))
                .values_list("id", flat=True)
                .distinct()
            )
        shared = (
            SharedInventoryItem.objects.filter(group_id__in=group_ids)
            .select_related("group")
            .order_by("group__name", "location_description", "name")[
                : max(1, min(limit, 150))
            ]
        )
        shared_rows = [
            {
                "scope": "shared",
                "name": item.name,
                "quantity": item.quantity,
                "condition": item.condition or "",
                "condition_display": item.get_condition_display()
                if item.condition
                else "",
                "location": item.location_description or "",
                "description": (item.description or "")[:200],
                "group": item.group.name if item.group_id else "",
                "property": "",  # shared across group
            }
            for item in shared
        ]

    return {
        "as_of": today.isoformat(),
        "counts": {
            "private_items": len(private_rows),
            "shared_items": len(shared_rows),
            "total": len(private_rows) + len(shared_rows),
        },
        "private_inventory": private_rows,
        "shared_inventory": shared_rows,
        "rules": {
            "person": (
                "Private inventory is per room/unit. Shared inventory is for "
                "household groups (e.g. kitchen of Side Unit). "
                "If total is 0, say no furniture/inventory recorded."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Charge schedule per property (strong)
# ---------------------------------------------------------------------------


def charge_schedule(
    landlord,
    *,
    property_query: str = "",
    month: str = "",
    include_paid: bool = True,
    limit: int = 80,
) -> dict:
    """All charges for a property (or whole portfolio), scheduled and outstanding."""
    from rentium.ledger.models import CHARGE_TYPES, LedgerEntry
    from rentium.rama.union import _month_bounds

    today = date.today()
    if month:
        try:
            y, m = month.split("-")
            start, end = _month_bounds(date(int(y), int(m), 1))
            month_label = f"{int(y):04d}-{int(m):02d}"
        except (ValueError, TypeError):
            return {"error": f"month must be YYYY-MM, got {month!r}"}
    else:
        # Default: from start of this month through 6 months ahead
        start, _ = _month_bounds(today)
        end = date(
            start.year + (start.month + 5) // 12,
            (start.month + 5) % 12 + 1,
            1,
        )
        month_label = f"{start.isoformat()} → {end.isoformat()} (window)"

    qs = (
        LedgerEntry.objects.with_settlement()
        .filter(
            landlord=landlord,
            entry_type__in=CHARGE_TYPES,
            reversed_by__isnull=True,
            due_date__gte=start,
            due_date__lt=end,
        )
        .select_related("property", "lease")
        .order_by("due_date", "created_at")
    )
    pq = (property_query or "").strip()
    if pq:
        qs = qs.filter(
            Q(property__name__icontains=pq)
            | Q(lease__property__name__icontains=pq)
            | Q(description__icontains=pq)
        )

    rows = []
    total_amount = Decimal("0.00")
    total_due_now = Decimal("0.00")
    for charge in qs[: max(1, min(limit, 150))]:
        outstanding = charge.outstanding
        if not include_paid and outstanding <= 0:
            continue
        if outstanding <= 0:
            status = "paid"
        elif charge.settled_amount > 0:
            status = "partially_paid"
        elif charge.due_date and charge.due_date > today:
            status = "scheduled"
        else:
            status = "unpaid"
        prop = ""
        if charge.property_id:
            prop = charge.property.name
        elif charge.lease_id and charge.lease.property_id:
            prop = charge.lease.property.name
        due_now = (
            outstanding
            if outstanding > 0 and charge.due_date and charge.due_date <= today
            else Decimal("0")
        )
        total_amount += charge.amount or Decimal("0")
        total_due_now += due_now
        rows.append(
            {
                "description": charge.description,
                "type": charge.entry_type,
                "amount": str(charge.amount),
                "due_date": charge.due_date.isoformat() if charge.due_date else None,
                "paid": str(charge.settled_amount),
                "balance_on_charge": str(outstanding),
                "due_now": str(due_now),
                # NO `outstanding` key here, deliberately. It used to alias
                # due_now, while the same key in charge_status and
                # tenant_statement means balance_on_charge — so one word meant
                # two opposite things across three tools the model chooses
                # between for the same question. The alias was redundant
                # (due_now already carries it) and it is the ambiguity that
                # let "is the $100 in?" be answered both ways.
                "status": status,
                "property": prop,
                "lease_id": str(charge.lease_id) if charge.lease_id else None,
                "overdue": bool(
                    due_now > 0 and charge.due_date and charge.due_date < today
                ),
            }
        )

    return {
        "as_of": today.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "month_label": month_label,
        "property_query": property_query or "",
        "counts": {"charges": len(rows)},
        "totals": {
            "amount": str(total_amount),
            "due_now": str(total_due_now),
            "note": (
                "totals.due_now is only unpaid with due_date<=as_of. "
                "Future scheduled rent has status=scheduled and due_now=0."
            ),
        },
        "charges": rows,
        "rules": {
            "person": (
                "status=scheduled means future — say scheduled, not outstanding. "
                "Use due_now for money owed today; balance_on_charge is unpaid "
                "line balance including future. Copy due_date and amount exactly. "
                "These two names exist because they are different numbers: never "
                "report one as the other."
            ),
            "outstanding_vs_schedule": (
                "Portfolio outstanding_total is only unpaid charges due on or before as_of. "
                "Scheduled future charges are not outstanding yet."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Tenants first-class (history across leases)
# ---------------------------------------------------------------------------


def list_tenants(
    landlord,
    *,
    query: str = "",
    include_past: bool = True,
    limit: int = 50,
) -> dict:
    from rentium.leases.models import Lease, LeaseTenant

    today = date.today()
    qs = (
        LeaseTenant.objects.filter(lease__landlord=landlord)
        .select_related(
            "tenant__user",
            "lease",
            "lease__property",
            "lease__group",
        )
        .order_by("-lease__start_date")
    )
    if not include_past:
        qs = qs.filter(
            lease__status__in=[
                Lease.LeaseStatus.ACTIVE,
                Lease.LeaseStatus.PENDING_SIGNATURES,
            ]
        )
    q = (query or "").strip()
    if q:
        qs = qs.filter(
            Q(invited_name__icontains=q)
            | Q(invited_email__icontains=q)
            | Q(tenant__user__name__icontains=q)
            | Q(tenant__user__email__icontains=q)
            | Q(lease__property__name__icontains=q)
            | Q(lease__lease_number__icontains=q)
        )

    # Group by person key (email or name)
    people: dict[str, dict] = {}
    for lt in qs[: max(1, min(limit * 3, 200))]:
        email = (lt.invited_email or (
            lt.tenant.user.email if lt.tenant_id and lt.tenant.user_id else ""
        ) or "").lower()
        name = lt.display_name or ""
        key = email or name.lower() or str(lt.pk)
        place = (
            lt.lease.property.name
            if lt.lease.property_id
            else (lt.lease.group.name if lt.lease.group_id else "")
        )
        lease_row = {
            "lease_id": str(lt.lease_id),
            "lease_number": lt.lease.lease_number or "",
            "lease_status": lt.lease.status,
            "property": place,
            "start_date": lt.lease.start_date.isoformat()
            if lt.lease.start_date
            else None,
            "end_date": lt.lease.end_date.isoformat() if lt.lease.end_date else None,
            "rent_amount": str(lt.rent_amount)
            if getattr(lt, "rent_amount", None) is not None
            else None,
            "is_primary_tenant": lt.is_primary_tenant,
            "has_signed": lt.has_signed,
            "declined": bool(getattr(lt, "declined", False)),
        }
        if key not in people:
            people[key] = {
                "name": name,
                "email": email,
                "lease_count": 0,
                "leases": [],
                "current_properties": [],
                "is_current": False,
            }
        people[key]["lease_count"] += 1
        people[key]["leases"].append(lease_row)
        if lt.lease.status in (
            Lease.LeaseStatus.ACTIVE,
            Lease.LeaseStatus.PENDING_SIGNATURES,
        ):
            people[key]["is_current"] = True
            if place and place not in people[key]["current_properties"]:
                people[key]["current_properties"].append(place)

    rows = list(people.values())[: max(1, min(limit, 80))]
    return {
        "as_of": today.isoformat(),
        "counts": {
            "people": len(rows),
            "current": sum(1 for p in rows if p["is_current"]),
        },
        "tenants": rows,
        "rules": {
            "person": (
                "Tenants are people; each may have multiple leases over time. "
                "Answer 'who is renting Room E?' from leases[]. "
                "History = all leases for that email/name."
            ),
        },
    }


def tenant_history(landlord, *, query: str) -> dict:
    """Deep history for one tenant name/email."""
    base = list_tenants(landlord, query=query, include_past=True, limit=20)
    if not query.strip():
        return {"error": "query is required (name or email)."}
    return {
        **base,
        "query": query,
        "hint": "If multiple people match, list them and ask which one.",
    }


# ---------------------------------------------------------------------------
# Documents / PDFs
# ---------------------------------------------------------------------------


def list_documents(
    landlord,
    *,
    property_query: str = "",
    lease_id: str = "",
    limit: int = 50,
) -> dict:
    from rentium.leases.models import Lease, LeaseDocument, LeaseForm

    today = date.today()
    rows = []

    # Uploaded additional documents
    doc_qs = LeaseDocument.objects.filter(lease__landlord=landlord).select_related(
        "lease", "lease__property", "lease__group"
    )
    if lease_id:
        doc_qs = doc_qs.filter(lease_id=lease_id)
    pq = (property_query or "").strip()
    if pq:
        doc_qs = doc_qs.filter(
            Q(lease__property__name__icontains=pq)
            | Q(lease__group__name__icontains=pq)
            | Q(title__icontains=pq)
            | Q(lease__lease_number__icontains=pq)
        )
    for doc in doc_qs.order_by("-uploaded_at")[: max(1, min(limit, 100))]:
        place = (
            doc.lease.property.name
            if doc.lease.property_id
            else (doc.lease.group.name if doc.lease.group_id else "")
        )
        file_name = ""
        try:
            file_name = doc.document.name.split("/")[-1] if doc.document else ""
        except Exception:  # noqa: BLE001
            file_name = ""
        rows.append(
            {
                "kind": "lease_attachment",
                "id": str(doc.pk),
                "title": doc.title,
                "description": (doc.description or "")[:200],
                "file_name": file_name,
                "has_file": bool(doc.document),
                "is_signed": doc.is_signed,
                "lease_id": str(doc.lease_id),
                "lease_number": doc.lease.lease_number or "",
                "property": place,
                "uploaded_at": doc.uploaded_at.isoformat()
                if doc.uploaded_at
                else None,
            }
        )

    # Form packs: the documents that are actually SIGNABLE, with who still owes
    # a signature. LeaseDocument above is a passive upload with a landlord-set
    # `is_signed` flag; these carry real signature state, which is what a
    # landlord asking "what's outstanding?" means.
    form_qs = LeaseForm.objects.filter(lease__landlord=landlord).select_related(
        "lease", "lease__property", "lease__group", "template"
    )
    if lease_id:
        form_qs = form_qs.filter(lease_id=lease_id)
    if pq:
        form_qs = form_qs.filter(
            Q(lease__property__name__icontains=pq)
            | Q(lease__group__name__icontains=pq)
            | Q(title__icontains=pq)
            | Q(lease__lease_number__icontains=pq)
        )
    for form in form_qs.order_by("-created_at")[: max(1, min(limit, 100))]:
        place = (
            form.lease.property.name
            if form.lease.property_id
            else (form.lease.group.name if form.lease.group_id else "")
        )
        rows.append(
            {
                "kind": "lease_form",
                "id": str(form.pk),
                "title": form.title,
                "form_code": form.template.code or "",
                "stage": str(form.template.stage),
                "purpose": form.template.purpose,
                "status": form.status,
                "blocking_the_lease": form.blocks_activation,
                "signed_by": [s.display_name for s in form.signers.all() if s.has_signed],
                "waiting_on": [
                    s.display_name
                    for s in form.signers.all()
                    if not s.has_signed and not s.declined_at
                ],
                "lease_id": str(form.lease_id),
                "lease_number": form.lease.lease_number or "",
                "property": place,
                "uploaded_at": form.created_at.isoformat() if form.created_at else None,
            }
        )

    # Agreement PDF presence on leases (metadata only — no binary)
    lease_qs = Lease.objects.filter(landlord=landlord).select_related(
        "property", "group"
    )
    if lease_id:
        lease_qs = lease_qs.filter(pk=lease_id)
    if pq:
        lease_qs = lease_qs.filter(
            Q(property__name__icontains=pq)
            | Q(group__name__icontains=pq)
            | Q(lease_number__icontains=pq)
        )
    agreement_rows = []
    for lease in lease_qs.order_by("-start_date")[: max(1, min(limit, 80))]:
        place = (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else "")
        )
        main_name = ""
        has_main = False
        try:
            if lease.document_file and lease.document_file.name:
                has_main = True
                main_name = lease.document_file.name.split("/")[-1]
        except Exception:  # noqa: BLE001
            has_main = False
        if has_main:
            rows.append(
                {
                    "kind": "main_agreement",
                    "id": f"lease-doc-{lease.pk}",
                    "title": f"Main agreement — {lease.lease_number or place}",
                    "description": "Lease.document_file (signed/generated agreement PDF)",
                    "file_name": main_name,
                    "has_file": True,
                    "is_signed": bool(lease.landlord_signed),
                    "lease_id": str(lease.pk),
                    "lease_number": lease.lease_number or "",
                    "property": place,
                    "uploaded_at": None,
                }
            )
        agreement_rows.append(
            {
                "kind": "lease_agreement",
                "lease_id": str(lease.pk),
                "lease_number": lease.lease_number or "",
                "property": place,
                "status": lease.status,
                "landlord_signed": bool(lease.landlord_signed),
                "document_file_name": main_name or None,
                "has_stored_document_file": has_main,
                # Live PDF is always generated even when document_file is empty
                "pdf_download_available": True,
                "pdf_download_path": f"/api/leases/{lease.pk}/pdf/",
            }
        )

    return {
        "as_of": today.isoformat(),
        "counts": {
            "attachments": len([r for r in rows if r["kind"] == "lease_attachment"]),
            "main_agreements_with_file": sum(
                1 for r in agreement_rows if r.get("has_stored_document_file")
            ),
            "leases_with_live_pdf": len(agreement_rows),
            "documents_total": len(rows),
            "leases_checked": len(agreement_rows),
        },
        "documents": rows,
        "lease_agreements": agreement_rows,
        "rules": {
            "person": (
                "Every existing lease has a downloadable PDF via "
                "pdf_download_path / the lease page Download PDF button. "
                "document_file is optional storage — if has_stored_document_file "
                "is false, STILL say the PDF can be downloaded (generated live). "
                "Never say 'no PDF exists' for a real lease. "
                "lease_attachment = extra uploads only."
            ),
            "read_only_list": "list_documents is metadata only; use lease_pdf_info for download path.",
        },
    }


# ---------------------------------------------------------------------------
# Compact digests for LIVE PORTFOLIO
# ---------------------------------------------------------------------------


def domain_digest(landlord) -> dict:
    """Small counts for live_context injection."""
    from rentium.leases.inspections import ConditionInspection, InspectionItem
    from rentium.leases.models import Lease
    from rentium.leases.moveout import MoveOutRequest
    from rentium.maintenance.models import WorkOrder
    from rentium.messaging.models import Conversation, Message
    from rentium.properties.models import InventoryItem, Property, SharedInventoryItem
    from rentium.showcase.models import Inquiry

    today = date.today()
    open_qs = (
        WorkOrder.objects.for_landlord(landlord)
        .exclude(
            status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED]
        )
        .select_related("property")
        .order_by("-priority", "sla_due_at")
    )
    open_wo_rows = [
        {
            "title": w.title,
            "property": w.property.name if w.property_id else "",
            "priority": w.priority,
            "status": w.status,
        }
        for w in open_qs[:15]
    ]
    high_wo = [r for r in open_wo_rows if r["priority"] in ("HIGH", "EMERGENCY")]
    new_inqs = list(
        Inquiry.objects.filter(landlord=landlord, status=Inquiry.Status.NEW)
        .select_related("property")
        .order_by("-created_at")[:10]
    )
    new_inq_rows = [
        {
            "name": i.name,
            "email": i.email,
            "property": i.property.name if i.property_id else "",
            "id": str(i.pk),
        }
        for i in new_inqs
    ]
    conv_n = Conversation.objects.filter(landlord=landlord).count()
    unread = (
        Message.objects.filter(
            conversation__landlord=landlord, read_at__isnull=True
        )
        .exclude(sender_id=landlord.user_id)
        .count()
    )
    insp_open = ConditionInspection.objects.filter(
        lease__landlord=landlord,
        status__in=[
            ConditionInspection.Status.MOVE_IN_IN_PROGRESS,
            ConditionInspection.Status.MOVE_OUT_IN_PROGRESS,
        ],
    ).count()
    attention_items = (
        InspectionItem.objects.filter(
            inspection__lease__landlord=landlord, needs_attention=True
        )
        .select_related("inspection__lease__property")
        .order_by("section", "label")[:20]
    )
    attention_rows = [
        {
            "label": it.label,
            "section": it.section,
            "property": (
                it.inspection.lease.property.name
                if it.inspection.lease.property_id
                else ""
            ),
            "comment": (it.move_in_comment or it.move_out_comment or "")[:120],
        }
        for it in attention_items
    ]
    moveouts_pending = MoveOutRequest.objects.filter(
        lease__landlord=landlord, status=MoveOutRequest.Status.PENDING
    ).count()
    upcoming_move_ins = (
        Lease.objects.filter(landlord=landlord, start_date__gte=today)
        .exclude(status=Lease.LeaseStatus.DRAFT)
        .count()
    )
    # Next lease end within a year (not "60 days" — model must not invent window)
    next_ends = list(
        Lease.objects.filter(
            landlord=landlord,
            end_date__isnull=False,
            end_date__gte=today,
            end_date__lte=today + timedelta(days=400),
        )
        .exclude(status=Lease.LeaseStatus.DRAFT)
        .select_related("property")
        .order_by("end_date")[:5]
    )
    lease_end_rows = [
        {
            "date": l.end_date.isoformat(),
            "property": l.property.name if l.property_id else "",
            "lease_number": l.lease_number or "",
        }
        for l in next_ends
    ]
    prop_ids = Property.objects.filter(landlord=landlord).values_list("id", flat=True)
    inv_private = InventoryItem.objects.filter(property_id__in=prop_ids).count()
    group_ids = (
        Property.objects.filter(landlord=landlord, group_id__isnull=False)
        .values_list("group_id", flat=True)
        .distinct()
    )
    inv_shared = SharedInventoryItem.objects.filter(group_id__in=group_ids).count()

    return {
        "as_of": today.isoformat(),
        "open_work_orders": len(open_wo_rows),
        "open_work_order_list": open_wo_rows,
        "high_or_emergency_work_orders": high_wo,
        "new_inquiries": len(new_inq_rows),
        "new_inquiry_list": new_inq_rows,
        "message_threads": conv_n,
        "unread_messages": unread,
        "inspections_in_progress": insp_open,
        "inspection_items_needing_attention": len(attention_rows),
        "inspection_attention_list": attention_rows,
        "pending_move_out_requests": moveouts_pending,
        "upcoming_move_ins": upcoming_move_ins,
        "upcoming_lease_ends": lease_end_rows,
        "inventory_items_private": inv_private,
        "inventory_items_shared": inv_shared,
        "hint": (
            "Copy open_work_order_list / high_or_emergency_work_orders exactly. "
            "If high_or_emergency_work_orders non-empty, answer yes for HIGH priority. "
            "inspection_attention_list is ground truth for 'items need attention'. "
            "upcoming_lease_ends lists move-out dates (e.g. Dec 31) — do not say none "
            "if this list is non-empty. new_inquiry_list for leads. "
            "Zero means none — do not invent."
        ),
    }


# ---------------------------------------------------------------------------
# Finders — deterministic set-scoping. When the landlord scopes a request
# over a set ("all listings without images except X"), Python enumerates and
# filters; the model only relays. Every finder returns the COMPLETE matching
# set plus an `excluded` echo and a `match_rule` sentence so nothing can be
# silently dropped.
# ---------------------------------------------------------------------------


def _tri(value: str) -> bool | None:
    """Parse a yes/no/'' filter: '' (or 'any') = no filter."""
    s = str(value or "").strip().lower()
    if not s or s == "any":
        return None
    return s in ("1", "true", "yes", "y", "on")


def _exclude_tokens(exclude: str) -> list[str]:
    return [t.strip().lower() for t in (exclude or "").split(",") if t.strip()]


def _is_excluded(name: str, pk, tokens: list[str]) -> str | None:
    """The matching token if this row is excluded, else None."""
    low = (name or "").lower()
    for tok in tokens:
        if tok == str(pk).lower() or tok == low or tok in low:
            return tok
    return None


def find_listings(
    landlord,
    *,
    has_images: str = "",
    vacant_today: str = "",
    has_lease: str = "",
    listing_status: str = "",
    group: str = "",
    name_contains: str = "",
    exclude: str = "",
    include_parked: str = "",
) -> dict:
    """Find listings matching filters. Returns the COMPLETE matching set —
    relay every row; never enumerate or filter listings yourself.
    Filters (all optional; '' = any): has_images yes/no, vacant_today yes/no,
    has_lease yes/no (ANY lease incl. drafts/ended — these block deletion),
    listing_status AVAILABLE|OCCUPIED|MAINTENANCE|NOT_AVAILABLE,
    group <name>, name_contains <text>,
    exclude 'name or id, name or id' (kept OUT of the result, echoed back),
    include_parked yes to also return listings parked by a rental-mode switch
    (off by default — they are not on the market)."""
    from datetime import date as _date

    from rentium.properties.models import Property

    from .union import _active_leases_by_property, _serialize_lease_brief

    today = _date.today()
    # Parked listings are excluded by default. This is the finder the PLAYBOOKS
    # enumerate bulk-operation targets through, so including them would let
    # "delete every listing with no images" reach into listings the landlord
    # took off the market by switching a unit's rental mode.
    base = Property.objects.filter(landlord=landlord)
    if str(include_parked or "").strip().lower() not in ("1", "true", "yes", "y"):
        base = base.filter(is_active_offering=True)
    qs = list(
        base
        .select_related("group")
        .annotate(
            _gallery_count=Count("property_images", distinct=True),
            _lease_count=Count("leases", distinct=True),
            _open_wo=Count(
                "work_orders",
                filter=~Q(work_orders__status__in=["COMPLETED", "CANCELLED"]),
                distinct=True,
            ),
        )
        .order_by("name")
    )
    total = len(qs)
    lease_map = _active_leases_by_property(landlord, [p.pk for p in qs], today)

    want_images = _tri(has_images)
    want_vacant = _tri(vacant_today)
    want_lease = _tri(has_lease)
    status_f = (listing_status or "").strip().upper()
    group_f = (group or "").strip().lower()
    name_f = (name_contains or "").strip().lower()
    tokens = _exclude_tokens(exclude)

    filters_used: list[str] = []
    if want_images is not None:
        filters_used.append(f"has_images={'yes' if want_images else 'no'}")
    if want_vacant is not None:
        filters_used.append(f"vacant_today={'yes' if want_vacant else 'no'}")
    if want_lease is not None:
        filters_used.append(f"has_lease={'yes' if want_lease else 'no'}")
    if status_f:
        filters_used.append(f"listing_status={status_f}")
    if group_f:
        filters_used.append(f"group~{group_f}")
    if name_f:
        filters_used.append(f"name~{name_f}")

    rows: list[dict] = []
    excluded_rows: list[dict] = []
    for prop in qs:
        image_count = prop.image_count  # honours the _gallery_count annotation
        lease = lease_map.get(prop.pk)
        brief = (
            _serialize_lease_brief(lease, today, place_name=prop.name)
            if lease
            else None
        )
        is_vacant = brief["vacant_today"] if brief else True

        tok = _is_excluded(prop.name, prop.pk, tokens)
        row = {
            "id": str(prop.pk),
            "name": prop.name,
            "group": prop.group.name if prop.group_id else None,
            "listing_status": prop.status,
            "image_count": image_count,
            "has_images": image_count > 0,
            # ALL leases incl. drafts and ended — this is the number that
            # blocks deletion (DB PROTECT), not just active occupancy.
            "lease_count": int(getattr(prop, "_lease_count", 0)),
            "current_lease": (
                {
                    "lease_number": brief.get("lease_number"),
                    "status": brief.get("status"),
                    "start_date": brief.get("start_date"),
                    "end_date": brief.get("end_date"),
                }
                if brief
                else None
            ),
            "open_work_orders": int(getattr(prop, "_open_wo", 0)),
            "vacant_today": is_vacant,
        }
        if tok is not None:
            excluded_rows.append({**row, "excluded_by": tok})
            continue

        if want_images is not None and row["has_images"] is not want_images:
            continue
        if want_vacant is not None and is_vacant is not want_vacant:
            continue
        if want_lease is not None and (row["lease_count"] > 0) is not want_lease:
            continue
        if status_f and prop.status != status_f:
            continue
        if group_f and group_f not in (row["group"] or "").lower():
            continue
        if name_f and name_f not in prop.name.lower():
            continue
        rows.append(row)

    return {
        "listings": rows,
        "count": len(rows),
        "total_listings": total,
        "excluded": excluded_rows,
        "match_rule": (
            f"Matched {len(rows)} of {total} listings"
            + (f" (filters: {', '.join(filters_used)})" if filters_used else "")
            + (
                f"; excluded by request: {', '.join(r['name'] for r in excluded_rows)}"
                if excluded_rows
                else ""
            )
            + "."
        ),
        "instruction": (
            "This is the COMPLETE matching set. Present every listing; do not "
            "drop, add, or re-filter items yourself."
        ),
    }


def find_leases(
    landlord,
    *,
    status: str = "",
    property_query: str = "",
    ending_before: str = "",
    include_ended: str = "",
) -> dict:
    """Find leases matching filters. Returns the COMPLETE matching set.
    Filters (optional): status DRAFT|PENDING_SIGNATURES|SIGNED|ACTIVE|
    TERMINATED|EXPIRED|RENEWED, property_query <listing name or id>,
    ending_before YYYY-MM-DD, include_ended yes to include final leases."""
    from datetime import datetime as _dt

    from rentium.leases.models import Lease

    qs = Lease.objects.filter(landlord=landlord).select_related("property")
    status_f = (status or "").strip().upper()
    if status_f:
        qs = qs.filter(status=status_f)
    elif _tri(include_ended) is not True:
        qs = qs.exclude(
            status__in=[Lease.LeaseStatus.TERMINATED, Lease.LeaseStatus.EXPIRED]
        )
    pq = (property_query or "").strip().lower()
    end_cut = None
    raw_cut = (ending_before or "").strip()
    if raw_cut:
        try:
            end_cut = _dt.strptime(raw_cut[:10], "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"Invalid ending_before {ending_before!r}; use YYYY-MM-DD."}
        qs = qs.filter(end_date__isnull=False, end_date__lt=end_cut)

    rows = []
    for lease in qs.order_by("property__name", "-created_at"):
        prop_name = lease.property.name if lease.property_id else ""
        if pq and pq not in prop_name.lower() and pq != str(lease.property_id):
            continue
        rows.append(
            {
                "lease_number": lease.lease_number,
                "property": prop_name,
                "status": lease.status,
                "start_date": str(lease.start_date) if lease.start_date else None,
                "end_date": str(lease.end_date) if lease.end_date else None,
                "total_rent": str(lease.total_rent),
                "tenants": lease.lease_tenants.count(),
            }
        )
    filters = [
        f
        for f in (
            f"status={status_f}" if status_f else "",
            f"property~{pq}" if pq else "",
            f"ending_before={end_cut}" if end_cut else "",
        )
        if f
    ]
    return {
        "leases": rows,
        "count": len(rows),
        "match_rule": (
            f"Matched {len(rows)} lease(s)"
            + (f" (filters: {', '.join(filters)})" if filters else "")
            + "."
        ),
        "instruction": (
            "This is the COMPLETE matching set. Present every lease; do not "
            "drop or re-filter items yourself."
        ),
    }
