"""
Confirmed write actions for RAMA (L4).

Every mutating tool:
1) Without confirm=yes → returns needs_confirm + preview (no DB write).
2) With confirm=yes → performs the write, returns result.
3) Always scoped to the authenticated landlord.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


def _confirmed(confirm: str) -> bool:
    return str(confirm or "").strip().lower() in ("yes", "true", "1", "y", "confirm")


def _preview(action: str, preview: dict, how: str) -> dict:
    return {
        "needs_confirm": True,
        "action": action,
        "preview": preview,
        "instruction": (
            f"Show this preview to the landlord. If they approve, call {action} "
            f"again with the same arguments AND confirm=yes. {how}"
        ),
    }


def _resolve_property(landlord, property_query: str, pick: str = ""):
    from .resolve import resolve_property

    return resolve_property(landlord, property_query, pick=pick)


def _prop_err(err):
    if isinstance(err, dict):
        return err if "error" in err else {"error": err}
    return {"error": str(err)}


def _parse_when(when: str):
    """Parse 'YYYY-MM-DD HH:MM' (or ISO) into a tz-aware datetime in the launch
    market's timezone. Returns None if unparseable. Mirrors schedule_viewing."""
    from zoneinfo import ZoneInfo

    when_s = (when or "").strip()
    if not when_s:
        return None
    tz = ZoneInfo("America/Vancouver")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(when_s.replace("Z", "")[:19], fmt)
            if fmt == "%Y-%m-%d":
                naive = datetime.combine(naive.date(), time(14, 0))
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Work orders
# ---------------------------------------------------------------------------


def _resolve_maintenance_target(landlord, query: str):
    """(property, unit, area, error) for "where is the fault?".

    A listing, a whole unit, or a named space inside one. The unit and area
    paths exist because a shared washroom belongs to no single room: with only
    property_query available, RAMA could not say "the shared washroom in
    McKenzie Basement" and kept re-offering one of the three rooms that share
    it while the landlord said no.
    """
    from rentium.properties.models import PropertyUnit

    q = (query or "").strip()
    if not q:
        return None, None, None, {"error": "Say which listing or unit this is for."}

    # A listing name is the most specific thing, so try it first.
    prop, err = _resolve_property(landlord, q)
    if not err:
        return prop, None, None, None

    units = list(
        PropertyUnit.objects.filter(landlord=landlord)
        .select_related("holding")
        .filter(name__icontains=q)[:6]
    )
    if not units:
        # "McKenzie Basement" reads as holding + unit; match on both together.
        tokens = [t for t in q.replace(",", " ").split() if t]
        candidates = PropertyUnit.objects.filter(landlord=landlord).select_related(
            "holding"
        )
        for token in tokens:
            from django.db.models import Q as _Q

            candidates = candidates.filter(
                _Q(name__icontains=token) | _Q(holding__name__icontains=token)
            )
        units = list(candidates[:6])

    if len(units) == 1:
        return None, units[0], None, None
    if len(units) > 1:
        return None, None, None, {
            "error": f"Several units match {q!r} — which one?",
            "candidates": [f"{u.name} ({u.holding.name})" for u in units],
        }
    return None, None, None, _prop_err(err)


def _resolve_expense_scope(landlord, query: str, holding_name: str = ""):
    """(property, unit, holding, error) for "what does this cost belong to?".

    Expenses have a scope work orders do not: the WHOLE physical property. A
    roof, a tax bill or a load of mulch belongs to the holding, not to any one
    listing or unit inside it — and `ledger.LedgerEntry.holding` exists exactly
    for that.

    Kept separate from _resolve_maintenance_target rather than widening it,
    because a work order is always raised against a place someone can be sent
    to; there is no "fix the whole holding".

    Order matters. A holding is tried BEFORE the token-based unit fallback,
    which is what fixes the reported bug: asked for "950 McKenzie Ave", the
    token fallback matched every unit in that holding (each token hit
    holding__name__icontains), found exactly one, and returned it as though
    the landlord had named it — silently overriding an explicit "not the
    garden suite, the whole property". A second unit in the holding would have
    turned the same request into an error instead.
    """
    from rentium.properties.models import PropertyUnit

    from .domain_crud import _holding_for_location, _resolve_holding

    if (holding_name or "").strip():
        holding, err = _resolve_holding(landlord, holding_name)
        if err:
            return None, None, None, {"error": err}
        return None, None, holding, None

    q = (query or "").strip()
    if not q:
        # Portfolio-wide: a cost that belongs to the business, not a place.
        return None, None, None, None

    # A listing name is the most specific thing, so try it first.
    prop, err = _resolve_property(landlord, q)
    if not err:
        return prop, None, None, None

    # Then an exactly-named unit ("Basement").
    named_units = list(
        PropertyUnit.objects.filter(landlord=landlord)
        .select_related("holding")
        .filter(name__icontains=q)[:6]
    )
    if len(named_units) == 1:
        return None, named_units[0], None, None
    if len(named_units) > 1:
        return None, None, None, {
            "error": f"Several units match {q!r} — which one?",
            "candidates": [f"{u.name} ({u.holding.name})" for u in named_units],
        }

    # Then the whole holding, by name or by address. This is the branch that
    # did not exist.
    holding, holding_err = _resolve_holding(landlord, q)
    if holding is None and not holding_err:
        holding, holding_err = _holding_for_location(landlord, q)
    if holding_err:
        return None, None, None, {"error": holding_err}
    if holding is not None:
        return None, None, holding, None

    # Finally "McKenzie Basement" — holding and unit named together.
    tokens = [t for t in q.replace(",", " ").split() if t]
    candidates = PropertyUnit.objects.filter(landlord=landlord).select_related("holding")
    for token in tokens:
        from django.db.models import Q as _Q

        candidates = candidates.filter(
            _Q(name__icontains=token) | _Q(holding__name__icontains=token)
        )
    units = list(candidates[:6])
    if len(units) == 1:
        return None, units[0], None, None
    if len(units) > 1:
        return None, None, None, {
            "error": (
                f"{q!r} could mean the whole property or one unit in it — "
                f"which did you mean?"
            ),
            "candidates": [f"{u.name} ({u.holding.name})" for u in units],
        }
    return None, None, None, _prop_err(err)


def _expense_scope_label(prop, unit, holding) -> str | None:
    """What the landlord is shown, and what lands in the plan's target_label."""
    if prop is not None:
        return prop.name
    if unit is not None:
        # Mirror create_work_order: say which of the two readings we took,
        # rather than hardcoding "shared" for a unit let as a whole home.
        from rentium.properties.models import PropertyUnit as _PU

        qualifier = (
            "whole unit" if unit.rental_mode == _PU.RentalMode.WHOLE_UNIT else "shared"
        )
        return f"{unit.name} ({unit.holding.name}) — {qualifier}"
    if holding is not None:
        return f"{holding.name} — whole property"
    return None


def _resolve_expense_entry(landlord, query: str, amount: str = ""):
    """(entry, error) for "which expense do you mean?".

    Ambiguity returns candidates rather than a guess. Picking the wrong row
    here voids the wrong money, and unlike a mis-scoped expense that is not
    something a second correction tidies up.
    """
    from rentium.ledger.models import EntryType, LedgerEntry

    q = (query or "").strip()
    if not q and not (amount or "").strip():
        return None, {"error": "Which expense? Name it, or give its amount."}

    qs = LedgerEntry.objects.filter(
        landlord=landlord, entry_type=EntryType.EXPENSE
    ).not_voided()
    if q:
        qs = qs.filter(description__icontains=q)
    if (amount or "").strip():
        try:
            qs = qs.filter(
                amount=Decimal(str(amount).replace("$", "").replace(",", "").strip())
            )
        except (InvalidOperation, ValueError):
            return None, {"error": f"Invalid amount {amount!r}."}

    matches = list(qs.select_related("property", "holding").order_by("-effective_date")[:6])
    if not matches:
        return None, {"error": f"No live expense matches {q or amount!r}."}
    if len(matches) > 1:
        return None, {
            "error": (
                f"{q or amount!r} matches {len(matches)} expenses — which one?"
            ),
            "candidates": [
                {
                    "id": str(m.pk),
                    "amount": str(m.amount),
                    "description": m.description[:80],
                    "where": (
                        m.property.name
                        if m.property_id
                        else (m.holding.name if m.holding_id else "portfolio-wide")
                    ),
                    "date": m.effective_date.isoformat(),
                }
                for m in matches
            ],
        }
    return matches[0], None


def reallocate_expense(
    landlord,
    *,
    expense_query: str = "",
    amount: str = "",
    property_query: str = "",
    holding_name: str = "",
    reason: str = "",
    confirm: str = "",
) -> dict:
    """Move a posted expense to the place it actually belongs.

    The capability this closes: there was no void tool and no reallocation
    helper, so a cost booked against the wrong place could only be "fixed" by
    posting a second expense somewhere else and voiding the first through a
    different API — leaving two unlinked rows and no recorded reason. Composing
    primitives is how a $19.78 shower knob ended up occupying three ledger
    lines.
    """
    from rentium.ledger import services as ledger_services

    if not (reason or "").strip():
        return {"error": "A reason is required — it goes on the audit trail."}

    entry, err = _resolve_expense_entry(landlord, expense_query, amount)
    if err:
        return err

    if not property_query and not holding_name:
        return {
            "error": (
                "Where should it go instead? Name a listing, or the address for "
                "a cost that belongs to the whole property."
            )
        }

    prop, unit, holding, err = _resolve_expense_scope(
        landlord, property_query, holding_name
    )
    if err:
        return err

    target_holding = (
        prop.holding if prop is not None else (unit.holding if unit else holding)
    )
    where = _expense_scope_label(prop, unit, holding)
    was = (
        entry.property.name
        if entry.property_id
        else (entry.holding.name if entry.holding_id else "portfolio-wide")
    )

    preview = {
        "amount": str(entry.amount),
        "description": entry.description[:200],
        "from": was,
        "to": where or "portfolio-wide",
        "reason": reason.strip()[:200],
    }
    if not _confirmed(confirm):
        return _preview(
            "reallocate_expense",
            preview,
            "Voids the expense and re-posts it against the new scope, linked to "
            "the entry it replaces.",
        )

    try:
        replacement = ledger_services.reallocate_entry(
            entry,
            property=prop,
            holding=target_holding,
            reason=reason.strip()[:200],
            created_by=landlord.user,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not reallocate expense: {exc}"}

    return {
        "created": True,
        "expense": {
            "id": str(replacement.pk),
            "amount": str(replacement.amount),
            "description": replacement.description[:200],
            "scope": where or "portfolio-wide",
            "previous_scope": was,
            "replaces": str(entry.pk),
        },
    }


def _resolve_area(landlord, target_property, target_unit, area_query: str):
    """A named space on the target, or (None, error)."""
    from django.db.models import Q

    from rentium.properties.models import PropertyArea

    q = (area_query or "").strip()
    if not q:
        return None, None

    scope = Q(pk__in=[])
    unit_id = target_unit.pk if target_unit else (
        target_property.unit_id if target_property else None
    )
    if unit_id:
        scope |= Q(unit_id=unit_id)
    if target_property is not None:
        scope |= Q(property_id=target_property.pk)
        if target_property.group_id:
            scope |= Q(group_id=target_property.group_id)
    if target_unit is not None:
        group = getattr(target_unit, "room_group", None)
        if group is not None:
            scope |= Q(group_id=group.pk)

    matches = list(
        PropertyArea.objects.filter(scope)
        .filter(Q(name__icontains=q) | Q(area_type__icontains=q.replace(" ", "_")))
        .distinct()[:6]
    )
    if not matches:
        return None, {
            "error": f"No space called {q!r} is recorded there.",
            "hint": (
                "Record it first with update_unit_layout, or leave the space out "
                "and describe it in the work order text."
            ),
        }
    if len(matches) > 1:
        exact = [a for a in matches if (a.name or "").casefold() == q.casefold()]
        if len(exact) == 1:
            return exact[0], None
        return None, {
            "error": f"Several spaces match {q!r} — which one?",
            "candidates": [a.label for a in matches],
        }
    return matches[0], None


def create_work_order(
    landlord,
    *,
    property_query: str,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    category: str = "OTHER",
    area: str = "",
    confirm: str = "",
) -> dict:
    from rentium.maintenance.models import WorkOrder

    prop, unit, _a, err = _resolve_maintenance_target(landlord, property_query)
    if err:
        return err
    area_obj, area_err = _resolve_area(landlord, prop, unit, area)
    if area_err:
        return area_err
    title = (title or "").strip()
    if not title:
        return {"error": "title is required."}
    pr = (priority or "MEDIUM").strip().upper()
    cat = (category or "OTHER").strip().upper()
    if pr not in WorkOrder.Priority.values:
        pr = WorkOrder.Priority.MEDIUM
    if cat not in WorkOrder.Category.values:
        cat = WorkOrder.Category.OTHER

    if prop is not None:
        where = prop.name
    else:
        # "(shared)" for a by-room unit, "(whole unit)" for one let as a home —
        # the landlord needs to see which of the two we understood.
        from rentium.properties.models import PropertyUnit as _PU

        qualifier = (
            "whole unit" if unit.rental_mode == _PU.RentalMode.WHOLE_UNIT else "shared"
        )
        where = f"{unit.name} ({unit.holding.name}) — {qualifier}"
    shared_note = None
    if unit is not None or (area_obj is not None and area_obj.unit_id):
        target_unit = unit or (prop.unit if prop is not None else None)
        if target_unit is not None:
            sharers = [
                p.name
                for p in target_unit.offerings.filter(is_active_offering=True)
            ]
            if len(sharers) > 1:
                shared_note = (
                    "Shared space — everyone renting "
                    + ", ".join(sharers)
                    + " will see this."
                )

    preview = {
        "property": where,
        "area": area_obj.label if area_obj is not None else None,
        "title": title,
        "description": (description or "")[:500],
        "priority": pr,
        "category": cat,
        "origin": "LANDLORD",
    }
    if shared_note:
        preview["note"] = shared_note
    if not _confirmed(confirm):
        return _preview(
            "create_work_order",
            preview,
            "Creates an open NEW work order.",
        )

    wo = WorkOrder.objects.create(
        property=prop,
        unit=unit,
        area=area_obj,
        reported_by=landlord.user,
        title=title[:200],
        description=(description or title)[:5000],
        priority=pr,
        category=cat,
        origin=WorkOrder.Origin.LANDLORD,
        status=WorkOrder.Status.NEW,
    )
    return {
        "created": True,
        "work_order": {
            "id": str(wo.pk),
            "title": wo.title,
            # place_name covers both targets — a shared-space job has no listing.
            "property": wo.place_name,
            "area": area_obj.label if area_obj is not None else None,
            "status": wo.status,
            "priority": wo.priority,
            "sla_due_at": wo.sla_due_at.isoformat() if wo.sla_due_at else None,
        },
    }


def transition_work_order(
    landlord,
    *,
    work_order_id: str = "",
    title_query: str = "",
    new_status: str,
    confirm: str = "",
) -> dict:
    from rentium.maintenance.models import WorkOrder

    wo = None
    if work_order_id:
        try:
            wo = (
                WorkOrder.objects.for_landlord(landlord)
                .select_related("property", "unit")
                .get(pk=work_order_id)
            )
        except (WorkOrder.DoesNotExist, ValueError):
            return {"error": f"No work order {work_order_id!r}."}
    elif title_query:
        qs = WorkOrder.objects.for_landlord(landlord).filter(
            title__icontains=title_query.strip()
        ).exclude(
            status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED]
        )
        if qs.count() != 1:
            return {
                "error": f"Need exactly one open WO matching {title_query!r} "
                f"(found {qs.count()}). Pass work_order_id."
            }
        wo = qs.select_related("property").first()
    else:
        return {"error": "Pass work_order_id or title_query."}

    st = (new_status or "").strip().upper()
    if st not in WorkOrder.Status.values:
        return {
            "error": f"Invalid status {new_status!r}. "
            f"Use one of: {list(WorkOrder.Status.values)}"
        }
    preview = {
        "id": str(wo.pk),
        "title": wo.title,
        "property": wo.property.name,
        "from_status": wo.status,
        "to_status": st,
    }
    if not _confirmed(confirm):
        return _preview("transition_work_order", preview, "Changes work order status.")

    try:
        old, new = wo.transition_to(st, by=landlord.user)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Transition failed: {exc}"}
    return {
        "updated": True,
        "work_order": {
            "id": str(wo.pk),
            "title": wo.title,
            "from": old,
            "to": new,
            "status": wo.status,
        },
    }


# ---------------------------------------------------------------------------
# Inquiries
# ---------------------------------------------------------------------------


def mark_inquiry_replied(
    landlord, *, inquiry_id: str = "", name_query: str = "", confirm: str = ""
) -> dict:
    from rentium.showcase.models import Inquiry

    def _new_list():
        return [
            {
                "id": str(i.pk),
                "name": i.name,
                "email": i.email,
                "property": i.property.name if i.property_id else "",
                "status": i.status,
            }
            for i in Inquiry.objects.filter(landlord=landlord)
            .exclude(status__in=[Inquiry.Status.ARCHIVED, Inquiry.Status.SPAM])
            .select_related("property")
            .order_by("-created_at")[:10]
        ]

    inq = None
    if inquiry_id and str(inquiry_id).strip() not in ("", "null", "none", "undefined"):
        try:
            inq = Inquiry.objects.select_related("property").get(
                pk=inquiry_id, landlord=landlord
            )
        except (Inquiry.DoesNotExist, ValueError, Exception):
            # Fall through to name_query / list
            inq = None
            if not name_query:
                return {
                    "error": f"No inquiry {inquiry_id!r}.",
                    "available_inquiries": _new_list(),
                    "hint": "Pass name_query e.g. 'Demo Lead' or a real id from available_inquiries.",
                }
    if inq is None and name_query:
        qs = Inquiry.objects.filter(
            landlord=landlord, name__icontains=name_query.strip()
        ).exclude(status__in=[Inquiry.Status.ARCHIVED, Inquiry.Status.SPAM])
        if qs.count() != 1:
            return {
                "error": f"Need one inquiry matching {name_query!r} (found {qs.count()}).",
                "available_inquiries": _new_list(),
            }
        inq = qs.select_related("property").first()
    if inq is None:
        # Single NEW inquiry → use it
        news = Inquiry.objects.filter(
            landlord=landlord, status=Inquiry.Status.NEW
        ).select_related("property")
        if news.count() == 1:
            inq = news.first()
        else:
            return {
                "error": "Pass inquiry_id or name_query (e.g. Demo Lead).",
                "available_inquiries": _new_list(),
            }

    preview = {
        "id": str(inq.pk),
        "name": inq.name,
        "email": inq.email,
        "property": inq.property.name if inq.property_id else "",
        "from_status": inq.status,
        "to_status": "REPLIED",
    }
    if not _confirmed(confirm):
        return _preview("mark_inquiry_replied", preview, "Marks inquiry as replied.")

    inq.mark_replied()
    return {"updated": True, "inquiry": preview | {"status": inq.status}}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def send_tenant_message(
    landlord,
    *,
    body: str,
    tenant_query: str = "",
    conversation_id: str = "",
    property_query: str = "",
    subject: str = "",
    confirm: str = "",
) -> dict:
    from rentium.leases.models import LeaseTenant
    from rentium.messaging.models import Conversation
    from rentium.messaging.services import send_message

    body = (body or "").strip()
    if not body:
        return {"error": "body is required."}

    conv = None
    if conversation_id:
        try:
            conv = Conversation.objects.select_related("tenant__user", "lease").get(
                pk=conversation_id, landlord=landlord
            )
        except (Conversation.DoesNotExist, ValueError):
            return {"error": f"No conversation {conversation_id!r}."}
    else:
        tq = (tenant_query or "").strip()
        if not tq:
            return {"error": "Pass conversation_id or tenant_query (name/email)."}
        lts = (
            LeaseTenant.objects.filter(lease__landlord=landlord)
            .filter(
                models_q_name_email(tq)
            )
            .select_related("tenant__user", "lease", "lease__property")
            .order_by("-lease__start_date")
        )
        # Prefer tenant with user account
        lt = None
        for candidate in lts[:10]:
            if candidate.tenant_id:
                lt = candidate
                break
        if not lt or not lt.tenant_id:
            return {
                "error": (
                    f"No linked tenant account for {tq!r}. "
                    "Tenant must have a user account to message."
                )
            }
        lease = lt.lease
        conv, _ = Conversation.objects.get_or_create(
            landlord=landlord,
            tenant=lt.tenant,
            lease=lease,
            defaults={"subject": (subject or f"Re: {lease.property.name if lease.property_id else 'lease'}")[:200]},
        )

    tenant_name = ""
    if conv.tenant_id and conv.tenant.user_id:
        tenant_name = conv.tenant.user.name or conv.tenant.user.email
    preview = {
        "conversation_id": str(conv.pk),
        "to_tenant": tenant_name,
        "subject": conv.subject,
        "body_preview": body[:300],
    }
    if not _confirmed(confirm):
        return _preview(
            "send_tenant_message",
            preview,
            "Sends a message in the tenant thread (notifies tenant).",
        )

    msg = send_message(conv, landlord.user, body)
    return {
        "sent": True,
        "message_id": str(msg.pk),
        "conversation_id": str(conv.pk),
        "to_tenant": tenant_name,
        "body_preview": body[:200],
    }


def models_q_name_email(q: str):
    from django.db.models import Q

    return (
        Q(invited_name__icontains=q)
        | Q(invited_email__icontains=q)
        | Q(tenant__user__name__icontains=q)
        | Q(tenant__user__email__icontains=q)
    )


def mark_messages_read(
    landlord,
    *,
    conversation_id: str = "",
    confirm: str = "",
) -> dict:
    """Mark tenant→landlord messages as read in a thread (or all threads)."""
    from rentium.messaging.models import Conversation, Message

    if conversation_id:
        try:
            conv_ids = [
                Conversation.objects.get(
                    pk=conversation_id, landlord=landlord
                ).pk
            ]
        except (Conversation.DoesNotExist, ValueError):
            return {"error": f"No conversation {conversation_id!r}."}
    else:
        conv_ids = list(
            Conversation.objects.filter(landlord=landlord).values_list("id", flat=True)
        )

    qs = (
        Message.objects.filter(
            conversation_id__in=conv_ids, read_at__isnull=True
        )
        .exclude(sender_id=landlord.user_id)
    )
    count = qs.count()
    preview = {
        "messages_to_mark_read": count,
        "conversation_id": conversation_id or "ALL",
    }
    if not _confirmed(confirm):
        return _preview("mark_messages_read", preview, "Sets read_at on unread tenant messages.")

    now = timezone.now()
    updated = qs.update(read_at=now)
    return {"updated": True, "marked_read": updated}


# ---------------------------------------------------------------------------
# Appointments / viewings
# ---------------------------------------------------------------------------


def schedule_viewing(
    landlord,
    *,
    property_query: str,
    when: str,
    contact_name: str = "",
    contact_email: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Schedule a viewing. when = ISO datetime or 'YYYY-MM-DD HH:MM' (local Vancouver-ish)."""
    from zoneinfo import ZoneInfo

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    when_s = (when or "").strip()
    if not when_s:
        return {"error": "when is required (e.g. 2026-08-05 14:00)."}

    tz = ZoneInfo("America/Vancouver")
    starts = None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            naive = datetime.strptime(when_s.replace("Z", "")[:19], fmt)
            if fmt == "%Y-%m-%d":
                naive = datetime.combine(naive.date(), time(14, 0))
            starts = naive.replace(tzinfo=tz)
            break
        except ValueError:
            continue
    if starts is None:
        return {"error": f"Could not parse when={when!r}. Use YYYY-MM-DD HH:MM."}

    preview = {
        "property": prop.name,
        "starts_at": starts.isoformat(),
        "kind": "VIEWING",
        "contact_name": contact_name or "",
        "contact_email": contact_email or "",
        "notes": (notes or "")[:200],
    }
    if not _confirmed(confirm):
        return _preview("schedule_viewing", preview, "Creates a SCHEDULED viewing.")

    from rentium.appointments.services import notification_receipt
    from rentium.appointments.services import schedule_viewing as schedule_viewing_service

    try:
        appt = schedule_viewing_service(
            landlord=landlord,
            property_obj=prop,
            starts_at=starts,
            contact_name=(contact_name or "")[:200],
            contact_email=(contact_email or "")[:150],
            notes=(notes or "")[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not create viewing: {exc}"}

    receipt = notification_receipt(appt)
    # Pin the real landlord UI path so weak models never invent an
    # "Appointments" menu. Viewings live on Calendar.
    from rentium.rama.links import url_for_path

    calendar_link = url_for_path("/dashboard/calendar")
    status_link = url_for_path(f"/viewing/status/{appt.public_token}")
    return {
        "created": True,
        "appointment": {
            "id": str(appt.pk),
            "property": prop.name,
            "starts_at": starts.isoformat(),
            "status": appt.status,
            "kind": appt.kind,
            "time_class": appt.time_class,
            "contact_name": appt.contact_name,
            "contact_email": appt.contact_email,
        },
        # Grounded delivery facts so RAMA can truthfully answer "how were they
        # notified?" — the exact gap that made it feel not-alive.
        "notified": receipt,
        "calendar_link": calendar_link,
        "prospect_status_link": status_link,
        "note": (
            f"Viewing is on your Calendar ({calendar_link}). "
            f"Prospect tracking page: {status_link}. "
            "Email is sent asynchronously when the appointment.scheduled event "
            "is processed; if that event has no error, the send was attempted."
        ),
    }


_PENDING_VIEWING = ("REQUESTED", "AWAITING_REQUESTER")


def list_viewing_requests(landlord, scope: str = "pending") -> dict:
    """List viewing requests with negotiation state. scope=pending (default) is
    what's awaiting action; scope=all includes scheduled/cancelled."""
    from rentium.appointments.models import Appointment

    qs = Appointment.objects.filter(
        landlord=landlord, kind=Appointment.Kind.VIEWING
    ).select_related("property")
    if scope != "all":
        qs = qs.filter(status__in=_PENDING_VIEWING)
    rows = []
    for a in qs.order_by("starts_at")[:50]:
        local = timezone.localtime(a.starts_at)
        rows.append(
            {
                "ref": str(a.pk)[:8].upper(),
                "id": str(a.pk),
                "property": a.property.name,
                "who": a.contact_name or "someone",
                "contact_email": a.contact_email or "",
                "when": local.strftime("%Y-%m-%d %H:%M"),
                "weekday": local.strftime("%A"),
                "status": a.status,
                "time_class": a.time_class,
                "tenant_consent": a.tenant_consent,
                "awaiting": (
                    "the requester"
                    if a.status == "AWAITING_REQUESTER"
                    else "you"
                    if a.status == "REQUESTED"
                    else "—"
                ),
            }
        )
    return {"count": len(rows), "requests": rows}


def _find_viewing(landlord, request_ref: str, *, any_status: bool = False):
    import uuid as _uuid

    from rentium.appointments.models import Appointment

    ref = (request_ref or "").strip()
    if not ref:
        return None
    qs = Appointment.objects.filter(
        landlord=landlord, kind=Appointment.Kind.VIEWING
    ).select_related("property")
    # Full UUID → direct hit; anything else is treated as the short ref.
    try:
        hit = qs.filter(pk=_uuid.UUID(ref)).first()
        if hit is not None:
            return hit
    except (ValueError, AttributeError):
        pass
    return _match_short_ref(qs, ref, any_status=any_status)


def _match_short_ref(qs, ref: str, *, any_status: bool = False):
    """Match the 8-char reference shown in list tools."""
    scoped = qs if any_status else qs.filter(status__in=_PENDING_VIEWING)
    for a in scoped.order_by("-starts_at")[:200]:
        if str(a.pk)[:8].upper() == ref.upper():
            return a
    return None


def _resolve_scheduled_viewing(
    landlord,
    *,
    appointment_ref: str = "",
    property_query: str = "",
    contact: str = "",
):
    """Find one SCHEDULED viewing to reschedule.

    Prefer appointment_ref (uuid or 8-char). Fall back to property + contact
    email/name so "reschedule Hitakshi's Room D viewing" works without the id.
    """
    from rentium.appointments.models import Appointment

    if appointment_ref.strip():
        appt = _find_viewing(landlord, appointment_ref, any_status=True)
        if appt is None:
            return None, {"error": f"No viewing matches ref={appointment_ref!r}."}
        return appt, None

    qs = Appointment.objects.filter(
        landlord=landlord,
        kind=Appointment.Kind.VIEWING,
    ).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.COMPLETED),
    ).select_related("property")

    if property_query.strip():
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return None, _prop_err(err)
        qs = qs.filter(property=prop)

    contact_s = (contact or "").strip()
    if contact_s:
        from django.db.models import Q

        qs = qs.filter(
            Q(contact_email__icontains=contact_s)
            | Q(contact_name__icontains=contact_s),
        )

    matches = list(qs.order_by("starts_at")[:6])
    if not matches:
        return None, {
            "error": (
                "No open viewing matched. Pass appointment_ref from "
                "list_appointments, or property_query + contact email/name."
            ),
        }
    if len(matches) > 1:
        return None, {
            "error": "Several viewings match — which one?",
            "candidates": [
                {
                    "ref": str(a.pk)[:8].upper(),
                    "id": str(a.pk),
                    "property": a.property.name,
                    "when": timezone.localtime(a.starts_at).strftime(
                        "%Y-%m-%d %H:%M %Z",
                    ),
                    "who": a.contact_name or a.contact_email or "—",
                    "status": a.status,
                }
                for a in matches
            ],
            "hint": "Pass appointment_ref=<id or 8-char ref>.",
        }
    return matches[0], None


def reschedule_viewing(
    landlord,
    *,
    when: str,
    appointment_ref: str = "",
    property_query: str = "",
    contact: str = "",
    notes: str = "",
    confirm: str = "",
) -> dict:
    """Move an existing viewing to a new date/time. Prefer appointment_ref
    (from list_appointments). Or property_query + contact (email/name).
    when = 'YYYY-MM-DD HH:MM'. Preview first; confirm=yes to apply. Emails the
    prospect the new time and keeps their status link working."""
    from rentium.appointments.services import notification_receipt
    from rentium.appointments.services import reschedule_viewing as reschedule_service
    from rentium.rama.links import url_for_path

    new_start = _parse_when(when)
    if new_start is None:
        return {"error": f"Could not parse when={when!r}. Use YYYY-MM-DD HH:MM."}

    appt, err = _resolve_scheduled_viewing(
        landlord,
        appointment_ref=appointment_ref,
        property_query=property_query,
        contact=contact,
    )
    if err:
        return err

    previous_local = timezone.localtime(appt.starts_at)
    new_local = timezone.localtime(new_start)
    preview = {
        "ref": str(appt.pk)[:8].upper(),
        "appointment_id": str(appt.pk),
        "property": appt.property.name,
        "contact_name": appt.contact_name,
        "contact_email": appt.contact_email,
        "from": previous_local.strftime("%A, %Y-%m-%d %H:%M %Z"),
        "to": new_local.strftime("%A, %Y-%m-%d %H:%M %Z"),
        "status": appt.status,
        "notes": (notes or "")[:200],
    }
    if not _confirmed(confirm):
        return _preview(
            "reschedule_viewing",
            preview,
            "Updates this viewing's start time and emails the contact.",
        )

    try:
        appt = reschedule_service(
            appointment=appt,
            starts_at=new_start,
            message=notes or "Rescheduled",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not reschedule: {exc}"}

    calendar_link = url_for_path("/dashboard/calendar")
    status_link = url_for_path(f"/viewing/status/{appt.public_token}")
    return {
        "rescheduled": True,
        "appointment": {
            "id": str(appt.pk),
            "ref": str(appt.pk)[:8].upper(),
            "property": appt.property.name,
            "starts_at": appt.starts_at.isoformat(),
            "status": appt.status,
            "contact_name": appt.contact_name,
            "contact_email": appt.contact_email,
        },
        "from": previous_local.strftime("%A, %Y-%m-%d %H:%M %Z"),
        "to": timezone.localtime(appt.starts_at).strftime("%A, %Y-%m-%d %H:%M %Z"),
        "notified": notification_receipt(appt),
        "calendar_link": calendar_link,
        "prospect_status_link": status_link,
        "message": (
            f"Rescheduled {appt.property.name} viewing from "
            f"{previous_local.strftime('%b %d %I:%M %p')} to "
            f"{timezone.localtime(appt.starts_at).strftime('%b %d %I:%M %p')}. "
            f"See Calendar: {calendar_link}"
        ),
    }


def respond_to_viewing_request(
    landlord,
    request_ref: str,
    action: str,
    when: str = "",
    confirm: str = "",
) -> dict:
    """Act on a pending viewing request. action = confirm | counter | decline.
    counter needs when ('YYYY-MM-DD HH:MM'). Preview first; confirm=yes to run.
    Get request_ref from list_viewing_requests."""
    from rentium.appointments.models import Appointment, AppointmentProposal
    from rentium.appointments.services import notification_receipt

    appt = _find_viewing(landlord, request_ref)
    if appt is None:
        return {"error": f"No viewing request matches ref={request_ref!r}."}
    if appt.status not in _PENDING_VIEWING:
        return {"error": f"That viewing is {appt.status}, nothing to act on."}

    act = (action or "").strip().lower()
    if act not in ("confirm", "counter", "decline"):
        return {"error": "action must be confirm, counter, or decline."}

    new_start = None
    if act == "counter":
        new_start = _parse_when(when)
        if new_start is None:
            return {"error": "counter needs when, e.g. 2026-08-05 14:00."}

    preview = {
        "ref": str(appt.pk)[:8].upper(),
        "property": appt.property.name,
        "action": act,
        "current_time": timezone.localtime(appt.starts_at).strftime("%Y-%m-%d %H:%M"),
    }
    if new_start:
        preview["new_time"] = timezone.localtime(new_start).strftime("%Y-%m-%d %H:%M")
    if not _confirmed(confirm):
        verb = {"confirm": "Confirms", "counter": "Proposes a new time for", "decline": "Declines"}[act]
        return _preview("respond_to_viewing_request", preview, f"{verb} this viewing.")

    if act == "confirm":
        appt.transition_to(Appointment.Status.SCHEDULED)
        appt.publish_event("appointment.scheduled")
    elif act == "decline":
        appt.transition_to(Appointment.Status.CANCELLED)
        appt.publish_event("appointment.cancelled", cancelled_by="LANDLORD")
    else:  # counter
        appt.starts_at = new_start
        appt.stamp_time_class()
        appt.transition_to(Appointment.Status.AWAITING_REQUESTER)
        appt.save(update_fields=["starts_at", "time_class"])
        appt.record_proposal(by=AppointmentProposal.By.LANDLORD, starts_at=new_start)
        appt.publish_event("appointment.countered", proposed_by="LANDLORD")

    return {
        "done": True,
        "ref": str(appt.pk)[:8].upper(),
        "status": appt.status,
        "notified": notification_receipt(appt),
    }


def get_viewing_availability(landlord, property_query: str = "") -> dict:
    """The landlord's preferred viewing hours (their default, or a property's
    override if property_query is given)."""
    from rentium.appointments.services import preferred_windows

    prop = None
    if property_query:
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)
    windows = preferred_windows(landlord, prop)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        "scope": prop.name if prop else "default (all properties)",
        "timezone": getattr(landlord, "timezone", "America/Vancouver"),
        "windows": [
            {
                "day": days[w.weekday],
                "from": w.start_time.strftime("%H:%M"),
                "to": w.end_time.strftime("%H:%M"),
            }
            for w in windows
        ],
    }


_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def set_viewing_availability(
    landlord,
    weekday: str = "",
    start: str = "",
    end: str = "",
    property_query: str = "",
    specific_date: str = "",
    confirm: str = "",
) -> dict:
    """Add a preferred viewing window. For RECURRING weekly hours pass weekday (a
    day name, e.g. Tuesday). For a ONE-OFF on a single date (e.g. 'only July 25,
    2–4pm') pass specific_date='YYYY-MM-DD' instead of weekday — it overrides the
    weekly hours for just that date. start/end = 'HH:MM' 24h. Optional
    property_query for a per-property override. Preview; confirm=yes."""
    from datetime import date as _date
    from datetime import time as _time

    from rentium.appointments.models import AvailabilityWindow

    the_date = None
    sd = (specific_date or "").strip()
    if sd:
        try:
            the_date = _date.fromisoformat(sd[:10])
        except ValueError:
            return {"error": "specific_date must be YYYY-MM-DD."}
        wd = the_date.weekday()  # derive so the index/constraint stay valid
    else:
        wd = _WEEKDAYS.get((weekday or "").strip().lower())
        if wd is None:
            return {"error": "Pass a weekday (e.g. Tuesday) OR specific_date=YYYY-MM-DD."}

    def _parse_hhmm(s):
        try:
            h, m = str(s).strip().split(":")
            return _time(int(h), int(m))
        except (ValueError, AttributeError):
            return None

    start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    if start_t is None or end_t is None:
        return {"error": "start and end must be HH:MM, e.g. 17:00."}
    if end_t <= start_t:
        return {"error": "end must be after start."}

    prop = None
    if property_query:
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)

    preview = {
        "when": the_date.isoformat() if the_date else (weekday or "").strip().title(),
        "kind": "one-off date" if the_date else "every week",
        "from": start_t.strftime("%H:%M"),
        "to": end_t.strftime("%H:%M"),
        "scope": prop.name if prop else "default (all properties)",
    }
    if not _confirmed(confirm):
        return _preview(
            "set_viewing_availability", preview, "Adds a preferred viewing window."
        )

    AvailabilityWindow.objects.create(
        landlord=landlord, property=prop, weekday=wd,
        specific_date=the_date, start_time=start_t, end_time=end_t,
    )
    return {"created": True, "window": preview}


def get_notification_channels(landlord) -> dict:
    """How this landlord is reachable outside the app — which channels are
    linked and verified (Telegram today, WhatsApp later), plus the always-on
    in-app + email. Answers "am I on Telegram?" / "how will you reach me?"."""
    linked = []
    try:
        from rentium.comms.models import ChannelAccount

        for c in ChannelAccount.objects.filter(landlord=landlord):
            linked.append(
                {
                    "channel": c.channel_type,
                    "verified": c.verified,
                    "active": c.is_active,
                    "name": c.display_name or "",
                    # Whether the daily 07:00 briefing goes to this channel.
                    "morning_briefing": bool((c.prefs or {}).get("briefing")),
                }
            )
    except Exception:  # noqa: BLE001
        pass
    verified = [c for c in linked if c["verified"] and c["active"]]
    briefing_on = [c["channel"] for c in linked if c["morning_briefing"]]
    return {
        "always_on": ["in-app dashboard", "email"],
        "external_channels": linked,
        "reachable_on": ["dashboard", "email"]
        + [c["channel"].lower() for c in verified],
        "telegram_linked": any(
            c["channel"] == "TELEGRAM" and c["verified"] for c in linked
        ),
        # Rentium DOES send a scheduled daily briefing. RAMA used to answer
        # "I don't send scheduled morning messages" and offer to log a
        # capability gap for a feature that already ships — because nothing in
        # its read surface mentioned it. Now it can say why they aren't
        # arriving instead of denying they exist.
        "morning_briefing": {
            "exists": True,
            "sends_daily_at": "07:00 in the landlord's timezone",
            "enabled_on": briefing_on,
            "status": (
                f"On for {', '.join(briefing_on)}."
                if briefing_on
                else (
                    "Not switched on for any channel yet — that is why no "
                    "morning updates are arriving."
                )
            ),
            "how_to_enable": (
                "Turn on the morning briefing for a linked channel in "
                "Settings > Notifications."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Lease tenant invites (sign link) — aligned with site business logic
# ---------------------------------------------------------------------------
#
# Architecture:
#   - Lease.total_rent is the unit rent. LeaseTenant.rent_amount is a share that
#     should sum to total_rent (compute_rent_split). Never assign full total_rent
#     to each person when multiple active slots exist.
#   - Only one is_primary_tenant per lease (model clean).
#   - Pending invites (unsigned, unlinked) can be removed/replaced by landlord.
#   - High-level replace_lease_invite = cancel old + invite new (one confirm).
# ---------------------------------------------------------------------------


def _resolve_lease(landlord, *, property_query: str = "", lease_number: str = ""):
    from rentium.leases.models import Lease

    ln = (lease_number or "").strip()
    if ln:
        lease = (
            Lease.objects.filter(landlord=landlord, lease_number__iexact=ln)
            .select_related("property", "group")
            .first()
        )
        if not lease:
            return None, f"No lease with number {ln!r}."
        return lease, None
    pq = (property_query or "").strip()
    if not pq:
        return None, "Pass property_query (e.g. Room D) or lease_number."
    prop, err = _resolve_property(landlord, pq)
    if err:
        return None, err
    lease = (
        Lease.objects.filter(landlord=landlord, property=prop)
        .exclude(
            status__in=[
                Lease.LeaseStatus.TERMINATED,
                Lease.LeaseStatus.EXPIRED,
            ]
        )
        .order_by("-created_at")
        .select_related("property", "group")
        .first()
    )
    if not lease:
        return None, f"No open lease on {prop.name}."
    return lease, None


def open_lease(landlord, *, property_query: str = "", lease_number: str = "") -> dict:
    """A clickable in-app link to a lease (to view it or download its PDF)."""
    from .links import url_for_path

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)
    return {
        "lease_number": lease.lease_number,
        "property": lease.property.name if lease.property_id else "",
        "status": lease.get_status_display(),
        "link": url_for_path(f"/dashboard/leases/{lease.id}"),
        "note": (
            f"Open this link to view lease {lease.lease_number} and click "
            "'Download PDF' for the signed agreement."
        ),
    }


def deliver_lease_pdf(landlord, *, lease_number: str = "",
                      property_query: str = "") -> dict:
    """Deliver a lease's signed PDF as an actual file (messaging channels send it
    as an attachment). Returns an `_attachment` marker the channel fulfils."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)
    return {
        "delivering": "lease_pdf",
        "_attachment": {
            "kind": "lease_pdf",
            "lease_id": str(lease.pk),
            "filename": f"lease_{lease.lease_number}.pdf",
        },
        "note": f"Sending the signed PDF for {lease.lease_number} now.",
    }


def deliver_property_photos(landlord, *, property_query: str = "") -> dict:
    """Deliver a property's photos as actual images (messaging channels send them
    as photos). Returns an `_attachment` marker the channel fulfils."""
    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    count = prop.property_images.count() if hasattr(prop, "property_images") else 0
    if not count:
        return {"note": f"{prop.name} has no photos uploaded yet."}
    return {
        "delivering": "property_photos",
        "_attachment": {
            "kind": "property_photos",
            "property_id": str(prop.pk),
            "label": prop.name,
        },
        "count": count,
        "note": f"Sending {prop.name}'s {count} photo(s) now.",
    }


def open_property(landlord, *, property_query: str = "") -> dict:
    """A clickable in-app link to a property's full listing (details + photos)."""
    from .links import url_for_path

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    photos = prop.property_images.count() if hasattr(prop, "property_images") else 0
    return {
        "name": prop.name,
        "address": prop.address,
        "photos": photos,
        "link": url_for_path(f"/dashboard/properties/{prop.id}"),
        "note": f"Open this link to view {prop.name} — its photos and full details.",
    }


def public_property_link(landlord, *, property_query: str = "") -> dict:
    """The real logged-out listing route, never a guessed name-based URL."""
    from .links import public_property_url

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    return public_property_url(prop)


def _place(lease) -> str:
    if lease.property_id:
        return lease.property.name
    if lease.group_id:
        return lease.group.name
    return ""


def _resolve_existing_tenant(landlord, email: str):
    """Given an invite email, return the existing TenantProfile to LINK, or None
    to create a fresh invited-email slot, or a friendly {"error": ...} dict when
    the email can't be a tenant (the landlord's own account, or a non-tenant
    account). Mirrors leases/api/serializers.py:LeaseTenantSerializer.create so
    RAMA links an existing account instead of erroring on a duplicate/invalid
    invited-email slot. `hasattr(user, "tenant_profile")` works because Django's
    reverse-OneToOne missing accessor raises an AttributeError subclass."""
    from rentium.users.models import User

    email = (email or "").strip().lower()
    if not email:
        return None
    landlord_email = (getattr(getattr(landlord, "user", None), "email", "") or "").lower()
    if email == landlord_email:
        return {
            "error": (
                "That's your own account email — invite the TENANT's email "
                "address, not your own."
            )
        }
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return None  # brand-new person → normal invited-email slot
    if hasattr(user, "tenant_profile"):
        return user.tenant_profile  # link the existing tenant account
    return {
        "error": (
            f"{email} already belongs to a non-tenant account (e.g. a landlord), "
            "so it can't be invited as a tenant. Use the tenant's own email."
        )
    }


def add_co_landlord(
    landlord,
    *,
    name: str,
    email: str,
    property_query: str = "",
    lease_number: str = "",
    remove: str = "",
    confirm: str = "",
) -> dict:
    """Invite a co-landlord who signs in, manages, AND co-signs leases. SCOPE:
    pass property_query to tie them to ONE property (and every room/lease in its
    group) — every FUTURE lease there names them as a co-signing landlord; pass
    lease_number to also add them as a co-signer on THAT existing lease (and grant
    the property). Pass NEITHER for whole-portfolio access. Invites by email and
    links immediately if they already have an account. remove=yes revokes."""
    from django.db.models import Q

    from rentium.users.models import LandlordTeamMember, User

    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "A valid email is required for the co-landlord."}
    # RFC-2606 reserved domains are never real inboxes — reject them so a
    # placeholder the model invented ('name@example.com') can't be used as if it
    # were the co-landlord's address. Forces RAMA to ask for the real one.
    if email.rsplit("@", 1)[-1] in ("example.com", "example.org", "example.net", "test", "localhost"):
        return {
            "error": "That looks like a placeholder email — ask the landlord for "
            "the co-landlord's real email address and use exactly that."
        }
    own_email = (getattr(getattr(landlord, "user", None), "email", "") or "").lower()
    if email == own_email:
        return {"error": "That's your own account — you already have access."}

    # Resolve scope. A lease_number implies its property; a property_query scopes
    # to that property (+ its group). Nothing given = whole portfolio.
    lease = None
    scope_property = None
    if lease_number:
        lease, err = _resolve_lease(landlord, lease_number=lease_number)
        if err:
            return _prop_err(err)
        scope_property = lease.property
    elif property_query:
        scope_property, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)

    scope_label = (
        f"{scope_property.name} (and its group)" if scope_property else "whole portfolio"
    )
    existing_user = User.objects.filter(email__iexact=email).first()
    removing = str(remove or "").strip().lower() in ("1", "true", "yes", "y")

    base = LandlordTeamMember.objects.filter(owner=landlord).filter(
        Q(invited_email__iexact=email) | Q(member__email__iexact=email)
    )
    if scope_property is not None:
        base = base.filter(scope_property=scope_property)

    if removing:
        if not base.exists():
            return {"error": f"{email} is not a co-landlord on {scope_label}."}
        if not _confirmed(confirm):
            return _preview(
                "add_co_landlord",
                {"action": f"revoke {email}'s access to {scope_label}"},
                "Removes their access. confirm=yes.",
            )
        base.delete()
        return {"updated": True, "note": f"Revoked {email}'s access to {scope_label}."}

    if base.exists():
        return {"error": f"{email} is already a co-landlord on {scope_label}."}

    if not _confirmed(confirm):
        return _preview(
            "add_co_landlord",
            {
                "co_landlord": name or email,
                "email": email,
                "scope": scope_label,
                "access": "manage + co-sign leases here",
                "co_signs_lease": lease.lease_number if lease else None,
                "future_leases": "will name them as a co-signing landlord"
                if scope_property
                else "n/a (portfolio-wide)",
                "status": "linked now (they have an account)"
                if existing_user
                else "invited — access starts when they sign up with this email",
            },
            "Grants a co-landlord access and co-signing. confirm=yes.",
        )

    from rentium.leases.services import grant_co_landlord

    _member, _created, emailed = grant_co_landlord(
        landlord, name=name, email=email, scope_property=scope_property, lease=lease
    )
    signs_lease = lease.lease_number if lease is not None else None

    where = (
        f"{scope_property.name} and every lease in its group"
        if scope_property
        else "your whole portfolio"
    )
    note = (
        (f"{email} now has access to {where}"
         if existing_user
         else f"Invited {email} to co-manage {where} — access starts when they sign up with that email")
        + (f"; they're a co-signer on {signs_lease}" if signs_lease else "")
        + (f", and an invite email was sent." if emailed
           else ". (Couldn't send the invite email — tell them to sign up with that email.)")
    )
    return {
        "invited": True,
        "linked_now": bool(existing_user),
        "emailed": emailed,
        "scope": scope_label,
        "co_signs_lease": signs_lease,
        "note": note,
    }


def list_co_landlords(landlord) -> dict:
    """The co-landlords / property managers who have (or are invited to) access to
    this portfolio."""
    from rentium.users.models import LandlordTeamMember

    rows = []
    for m in LandlordTeamMember.objects.filter(owner=landlord).select_related("member"):
        rows.append(
            {
                "name": m.invited_name or (m.member.name if m.member_id else ""),
                "email": m.invited_email or (m.member.email if m.member_id else ""),
                "status": "active" if m.accepted_at else "invited (not signed up yet)",
            }
        )
    return {"count": len(rows), "co_landlords": rows}


def add_co_host_to_lease(
    landlord,
    *,
    name: str,
    email: str = "",
    phone: str = "",
    property_query: str = "",
    lease_number: str = "",
    remove: str = "",
    confirm: str = "",
) -> dict:
    """Add (or remove) a co-host / co-landlord recorded on a lease agreement — a
    second landlord party (partner, co-owner, manager) shown on the document and
    reachable for notice. This is a RECORD on the lease, NOT an app login. Use
    when the landlord says 'add a co-host/co-landlord to this lease'. Preview;
    confirm=yes."""
    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not name:
        return {"error": "A co-host name is required."}

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)

    hosts = list(lease.co_hosts or [])
    removing = str(remove or "").strip().lower() in ("1", "true", "yes", "y")
    if removing:
        after = [h for h in hosts if (h.get("name", "").lower() != name.lower() and (not email or h.get("email", "").lower() != email))]
        action = f"remove co-host {name}"
    else:
        if any(h.get("name", "").lower() == name.lower() for h in hosts):
            return {"error": f"{name} is already a co-host on this lease."}
        after = hosts + [{"name": name[:150], "email": email[:254], "phone": (phone or "").strip()[:32]}]
        action = f"add co-host {name}"

    if not _confirmed(confirm):
        return _preview(
            "add_co_host_to_lease",
            {
                "lease_number": lease.lease_number,
                "property": lease.property.name if lease.property_id else "",
                "action": action,
                "co_hosts_after": [h.get("name") for h in after],
            },
            "Records a co-host on the agreement (not an app login). confirm=yes.",
        )

    lease.co_hosts = after
    lease.save(update_fields=["co_hosts", "updated_at"])
    return {
        "updated": True,
        "lease_number": lease.lease_number,
        "co_hosts": [h.get("name") for h in after],
        "note": f"Co-hosts on {lease.lease_number}: {', '.join(h.get('name') for h in after) or 'none'}.",
    }


def _normalise_request(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, so two phrasings of
    the same ask compare on their words rather than their formatting."""
    import re

    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").casefold()).split())


# When two requests are the same ask reworded.
#
# Character similarity alone is the wrong measure here. The real backlog
# accumulated five rows for one co-landlord request, and consecutive pairs
# differed only by an inserted email address or a trailing clause — which barely
# changes the meaning but moves a character ratio a long way. So we also compare
# WORD SETS: if nearly every word of the shorter request appears in the longer
# one, and the two are of comparable length, it is a restatement.
#
# The length guard is what stops containment over-merging: a five-word ask is
# trivially "contained" in an unrelated thirty-word one.
GAP_SEQUENCE_THRESHOLD = 0.82
GAP_CONTAINMENT_THRESHOLD = 0.85
GAP_LENGTH_RATIO_FLOOR = 0.6


def _same_ask(a: str, b: str) -> bool:
    from difflib import SequenceMatcher

    left, right = _normalise_request(a), _normalise_request(b)
    if not left or not right:
        return False
    if SequenceMatcher(None, left, right).ratio() >= GAP_SEQUENCE_THRESHOLD:
        return True

    ltok, rtok = set(left.split()), set(right.split())
    if not ltok or not rtok:
        return False
    smaller, larger = sorted((ltok, rtok), key=len)
    if len(smaller) / len(larger) < GAP_LENGTH_RATIO_FLOOR:
        return False
    containment = len(smaller & larger) / len(smaller)
    return containment >= GAP_CONTAINMENT_THRESHOLD


def _matching_open_gap(landlord, request: str):
    """An already-open gap that is the same ask as `request`, or None.

    Exact match first (cheap and certain), then a similarity pass over the
    landlord's open gaps. Only NEW/REVIEWED rows are candidates — a gap that
    was BUILT or DISMISSED has been decided, and raising it again is new
    information, not a duplicate.
    """
    from rentium.rama.models import RamaCapabilityGap

    open_statuses = (
        RamaCapabilityGap.Status.NEW,
        RamaCapabilityGap.Status.REVIEWED,
    )
    exact = RamaCapabilityGap.objects.filter(
        landlord=landlord, request__iexact=request, status__in=open_statuses
    ).first()
    if exact is not None:
        return exact

    if not _normalise_request(request):
        return None
    for gap in RamaCapabilityGap.objects.filter(
        landlord=landlord, status__in=open_statuses
    ).order_by("-created_at"):
        if _same_ask(request, gap.request):
            return gap
    return None


def triage_capability_gap(
    landlord, *, gap_query: str, status: str = "", prioritise: str = "",
    confirm: str = "",
) -> dict:
    """Move a logged gap through the backlog. Nothing here builds anything —
    it only records a decision a human made."""
    from rentium.rama.models import RamaCapabilityGap

    from .domain_crud import _confirmed, _preview

    query = (gap_query or "").strip()
    if not query:
        return {"error": "gap_query is required (the gap id, or words from it)."}

    qs = RamaCapabilityGap.objects.filter(landlord=landlord)
    gap = None
    try:
        gap = qs.filter(pk=query).first()
    except (ValueError, ValidationError):
        gap = None
    if gap is None:
        matches = list(qs.filter(request__icontains=query)[:6])
        if not matches:
            return {"error": f"No logged gap matching {query!r}."}
        if len(matches) > 1:
            return {
                "error": f"Several gaps match {query!r} — which one?",
                "candidates": [
                    {"id": str(g.pk), "request": g.request[:120]} for g in matches
                ],
            }
        gap = matches[0]

    new_status = (status or "").strip().upper()
    valid = {c for c, _ in RamaCapabilityGap.Status.choices}
    if new_status and new_status not in valid:
        return {"error": f"status must be one of {sorted(valid)}."}

    wants_priority = str(prioritise or "").strip().lower() in (
        "1", "true", "yes", "y", "on",
    )
    preview = {
        # gap_id makes the triage reversible by id rather than by fuzzy text
        # match (tool_meta._undo_triage_capability_gap).
        "gap_id": str(gap.pk),
        "gap": gap.request[:200],
        "from_status": gap.status,
        "to_status": new_status or gap.status,
        "prioritised": wants_priority or gap.prioritised,
    }
    if not _confirmed(confirm):
        return _preview(
            "triage_capability_gap", preview, "Records a triage decision only."
        )

    fields = ["updated_at"]
    if new_status:
        gap.status = new_status
        fields.append("status")
    if wants_priority and not gap.prioritised:
        gap.prioritised = True
        fields.append("prioritised")
    gap.save(update_fields=fields)
    return {"updated": True, **preview}


def log_capability_gap(landlord, *, request: str, detail: str = "", learn_now: str = "") -> dict:
    """Record something RAMA couldn't do as a STRUCTURED gap (never code). The
    safe first half of self-evolving: instead of failing silently, RAMA logs
    what was missing so it becomes a reviewable backlog the team builds from.
    learn_now=yes = the landlord explicitly asked us to build it → prioritise."""
    from rentium.rama.models import RamaCapabilityGap

    req = (request or "").strip()
    if not req:
        return {"error": "request is required — what did the landlord want?"}
    from .capabilities import supported_tool_for_request

    supported_tool = supported_tool_for_request(req)
    if supported_tool:
        return {
            "logged": False,
            "supported": True,
            "tool": supported_tool,
            "note": (
                f"This request is already supported by {supported_tool}. "
                "Do not create a capability gap; retry with that tool."
            ),
        }
    prioritised = str(learn_now or "").strip().lower() in ("1", "true", "yes", "y", "on")

    existing = _matching_open_gap(landlord, req)
    if existing is not None:
        gap = existing
        fields = ["updated_at"]
        if prioritised and not gap.prioritised:
            gap.prioritised = True
            fields.append("prioritised")
        # Keep the fuller description — a restatement often carries more detail
        # than the first attempt did.
        new_detail = (detail or "").strip()
        if len(new_detail) > len(gap.detail or ""):
            gap.detail = new_detail[:2000]
            fields.append("detail")
        gap.save(update_fields=fields)
    else:
        gap = RamaCapabilityGap.objects.create(
            landlord=landlord,
            request=req[:2000],
            detail=(detail or "").strip()[:2000],
            prioritised=prioritised,
        )
    return {
        "logged": True,
        "gap_id": str(gap.pk),
        "prioritised": gap.prioritised,
        "note": (
            "Flagged to build (learn now) — it'll be reviewed, built, and tested "
            "before it's switched on."
            if gap.prioritised
            else "Noted so the team can build it. Say 'learn now' to prioritise it."
        ),
    }


def list_capability_gaps(landlord, *, status: str = "", limit: str = "20") -> dict:
    """The capability gaps RAMA has logged for this landlord (what it couldn't do
    yet). Answers 'what have you flagged to learn / build?'."""
    from rentium.rama.models import RamaCapabilityGap

    qs = RamaCapabilityGap.objects.filter(landlord=landlord)
    st = (status or "").strip().upper()
    if st:
        qs = qs.filter(status=st)
    try:
        n = max(1, min(int(limit or "20"), 100))
    except ValueError:
        n = 20
    rows = [
        {
            "id": str(g.pk),
            "request": g.request,
            "status": g.status,
            "prioritised": g.prioritised,
            "logged": g.created_at.date().isoformat(),
        }
        for g in qs[:n]
    ]
    return {"count": qs.count(), "gaps": rows}


def _active_tenant_slots(lease):
    """Non-declined tenant slots (site treats declined as out of the roster)."""
    return list(
        lease.lease_tenants.filter(declined=False).select_related("tenant__user")
    )


def _slot_label(lt) -> dict:
    from rentium.leases.services import invite_lifecycle

    return {
        "id": str(lt.pk),
        "name": lt.display_name,
        "email": lt.invited_email
        or (lt.tenant.user.email if lt.tenant_id else ""),
        "rent_amount": str(lt.rent_amount),
        "is_primary": bool(lt.is_primary_tenant),
        "has_signed": bool(lt.has_signed),
        "declined": bool(lt.declined),
        "invite_sent_at": lt.invite_sent_at.isoformat() if lt.invite_sent_at else None,
        "linked": bool(lt.tenant_id),
        "invite_lifecycle": invite_lifecycle(lt),
    }


def _parse_money(value: str):
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        return None, None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip()), None
    except (InvalidOperation, ValueError):
        return None, f"Invalid money amount {value!r}."


def rebalance_lease_rent_shares(lease, *, force_equal_unsigned: bool = True):
    """Make active tenant rent_amounts sum to lease.total_rent.

    Matches Manage Tenants intent: signed shares stay fixed; unsigned slots
    share the remainder equally (typical roommate add → $500/$500 on $1000).
    Uses LeaseTenant.objects.update to avoid full_clean primary races mid-loop.
    """
    from decimal import Decimal

    from rentium.leases.models import LeaseTenant

    total = Decimal(lease.total_rent or "0").quantize(Decimal("0.01"))
    active = _active_tenant_slots(lease)
    if not active or total <= 0:
        return []

    signed = [lt for lt in active if lt.has_signed]
    unsigned = [lt for lt in active if not lt.has_signed]
    fixed = sum((Decimal(lt.rent_amount) for lt in signed), Decimal("0"))
    remaining = max(total - fixed, Decimal("0")).quantize(Decimal("0.01"))

    updates = []
    if not unsigned:
        return updates

    if force_equal_unsigned:
        n = len(unsigned)
        per = (remaining / Decimal(n)).quantize(Decimal("0.01"))
        # Last slot absorbs rounding so sum == remaining
        amounts = [per] * (n - 1)
        amounts.append((remaining - per * (n - 1)).quantize(Decimal("0.01")))
        for lt, amt in zip(unsigned, amounts):
            if Decimal(lt.rent_amount) != amt:
                LeaseTenant.objects.filter(pk=lt.pk).update(rent_amount=amt)
                lt.rent_amount = amt
            updates.append(_slot_label(lt))
    return updates


def _compute_new_slot_rent(lease, *, explicit: str = "", excluding_ids=None):
    """Preview rent for a new slot after equal rebalance of unsigned roster.

    On add with N existing unsigned + 1 new → each gets total/(N+1).
    Signed shares stay fixed; remainder split among unsigned including the new row.
    """
    from decimal import Decimal

    excluding_ids = set(excluding_ids or [])
    total = Decimal(lease.total_rent or "0")
    provided, err = _parse_money(explicit)
    if err:
        return None, err
    if provided is not None:
        return provided, None

    if total <= 0:
        return Decimal("0.00"), None

    signed_sum = Decimal("0")
    unsigned_count = 1  # the new slot
    for lt in _active_tenant_slots(lease):
        if lt.pk in excluding_ids:
            continue
        if lt.has_signed:
            signed_sum += Decimal(lt.rent_amount)
        else:
            unsigned_count += 1
    remaining = max(total - signed_sum, Decimal("0"))
    per = (remaining / Decimal(unsigned_count)).quantize(Decimal("0.01"))
    return per, None


def _find_pending_slot(lease, *, email: str = "", name: str = ""):
    from django.db.models import Q

    qs = lease.lease_tenants.filter(declined=False, has_signed=False)
    if email:
        qs = qs.filter(invited_email__iexact=email.strip())
    if name:
        qs = qs.filter(
            Q(invited_name__icontains=name.strip())
            | Q(tenant__user__name__icontains=name.strip())
        )
    return list(qs.select_related("tenant__user"))


def cancel_lease_invite(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    email: str = "",
    name: str = "",
    reason: str = "Landlord cancelled / replaced invite",
    confirm: str = "",
) -> dict:
    """Suspend/remove a pending (unsigned) invite so another person can take the slot.

    Prefer delete for never-signed unlinked slots (frees primary + rent). Soft-decline
    if linked account exists but not signed. Cannot cancel a signed slot.
    """
    from rentium.leases.models import LeaseTenant

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)

    slots = _find_pending_slot(lease, email=email, name=name)
    if not email and not name:
        # If only one pending on lease, target it
        slots = [
            lt
            for lt in _active_tenant_slots(lease)
            if not lt.has_signed
        ]
    if len(slots) != 1:
        return {
            "error": f"Need exactly one pending invite to cancel (found {len(slots)}).",
            "candidates": [_slot_label(s) for s in slots[:10]]
            or [_slot_label(s) for s in _active_tenant_slots(lease)[:10]],
            "hint": "Pass email or name of the invite to suspend (e.g. Jabi Pro).",
        }
    lt = slots[0]
    if lt.has_signed:
        return {"error": "Cannot cancel a signed tenant — use lease termination/move-out."}

    place = _place(lease)
    preview = {
        "action": "cancel_lease_invite",
        "property": place,
        "lease_number": lease.lease_number,
        "removing": _slot_label(lt),
        "method": "delete" if not lt.tenant_id else "soft_decline",
        "reason": reason,
    }
    if not _confirmed(confirm):
        return _preview(
            "cancel_lease_invite",
            preview,
            "Removes the pending invite so you can invite someone else.",
        )

    label = _slot_label(lt)
    with transaction.atomic():
        if not lt.tenant_id and not lt.has_signed:
            lt.delete()
            method = "deleted"
        else:
            lt.is_primary_tenant = False
            lt.declined = True
            lt.declined_at = timezone.now()
            lt.decline_reason = reason or "Landlord cancelled invite"
            # Bypass full_clean primary issues by update_fields path
            LeaseTenant.objects.filter(pk=lt.pk).update(
                is_primary_tenant=False,
                declined=True,
                declined_at=timezone.now(),
                decline_reason=reason or "Landlord cancelled invite",
            )
            method = "soft_declined"

        # If lease has no active tenants left, keep PENDING or drop to DRAFT
        remaining = lease.lease_tenants.filter(declined=False).count()
        from rentium.leases.models import Lease

        if remaining == 0 and lease.status == Lease.LeaseStatus.PENDING_SIGNATURES:
            lease.status = Lease.LeaseStatus.DRAFT
            lease.save(update_fields=["status", "updated_at"])

        # Always rebalance after remove: 2→1 unsigned must go $500+$500 → $1000
        # for the remaining person (same as Manage Tenants equal-split rule).
        rebalance_lease_rent_shares(lease, force_equal_unsigned=True)

    remaining_slots = [_slot_label(a) for a in _active_tenant_slots(lease)]
    return {
        "cancelled": True,
        "method": method,
        "removed": label,
        "lease_number": lease.lease_number,
        "property": place,
        "lease_status": lease.status,
        "lease_total_rent": str(lease.total_rent or "0"),
        "remaining_active_tenants": remaining_slots,
        "rent_note": (
            "Rent rebalanced across remaining unsigned tenants "
            "(sole remaining tenant gets full lease_total_rent)."
        ),
    }


def invite_tenant_to_lease(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    email: str,
    name: str,
    rent_amount: str = "",
    is_primary: str = "auto",
    phone: str = "",
    replace_email: str = "",
    replace_name: str = "",
    mode: str = "add",
    confirm: str = "",
) -> dict:
    """Invite someone to sign a lease (create slot + email link).

    mode:
      - add (default): add another tenant; rent auto-split via compute_rent_split
      - replace: cancel replace_email/replace_name pending invite first, then invite
        as the (usually sole) tenant with full/correct rent share

    Rent: omit rent_amount to auto-fill (sole active → full total_rent; else equal
    share of unallocated). Never assigns full total_rent to each of two people.

    Primary: auto = primary if no other active primary after replace; never stuck
    on 'already has primary' — demotes others when this invite is primary.
    """
    from decimal import Decimal

    from django.conf import settings
    from django.utils import timezone

    from rentium.leases.models import Lease, LeaseTenant
    from rentium.showcase.emails import send_tenant_invite

    email = (email or "").strip().lower()
    name = (name or "").strip()
    if not email or "@" not in email:
        return {"error": "A valid email is required."}
    if not name:
        return {"error": "name is required (tenant legal name)."}

    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)

    if hasattr(lease, "is_locked") and lease.is_locked():
        return {"error": "This lease is fully executed and locked — cannot invite."}

    # Existing-account detection (the fix for "sent it to a pre-existing email
    # and errored"). Mirrors leases/api/serializers.py: if the email already
    # belongs to an account, we LINK it instead of blindly creating a pending
    # invited-email slot — and we refuse cleanly for the landlord's own email or
    # a non-tenant (e.g. another landlord) account rather than throwing.
    linked_tenant = _resolve_existing_tenant(landlord, email)
    if isinstance(linked_tenant, dict):  # a friendly error, not a profile
        return linked_tenant

    mode = (mode or "add").strip().lower()
    # Only enter replace when explicitly requested — NEVER because someone
    # already lives on the lease (that was the bug that deleted roommates).
    if mode not in ("add", "replace"):
        mode = "add"
    if mode == "replace" and not (replace_email or replace_name):
        return {
            "error": "mode=replace requires replace_name or replace_email "
            "(who to remove). For adding a roommate use mode=add."
        }

    place = _place(lease)
    active = _active_tenant_slots(lease)
    to_cancel = None
    if mode == "replace":
        cands = _find_pending_slot(
            lease, email=replace_email, name=replace_name
        )
        if not cands and (replace_email or replace_name):
            return {
                "error": "No pending invite to replace matching "
                f"email={replace_email!r} name={replace_name!r}.",
                "active_tenants": [_slot_label(a) for a in active],
            }
        # Do NOT auto-pick the only tenant — that silently deletes roommates.
        if len(cands) != 1:
            return {
                "error": "Replace mode needs exactly one pending slot to remove.",
                "candidates": [_slot_label(c) for c in cands],
                "active_tenants": [_slot_label(a) for a in active],
            }
        to_cancel = cands[0]
        if to_cancel.has_signed:
            return {"error": "Cannot replace a signed tenant with this tool."}

    exclude_ids = [to_cancel.pk] if to_cancel else []
    rent, rent_err = _compute_new_slot_rent(
        lease, explicit=rent_amount, excluding_ids=exclude_ids
    )
    if rent_err:
        return {"error": rent_err}

    # Primary logic
    remaining_after = [
        a for a in active if not to_cancel or a.pk != to_cancel.pk
    ]
    has_other_primary = any(a.is_primary_tenant for a in remaining_after)
    ip = (is_primary or "auto").strip().lower()
    if ip in ("auto", ""):
        want_primary = not has_other_primary
    else:
        want_primary = ip not in ("0", "false", "no")

    existing_same = (
        LeaseTenant.objects.filter(lease=lease, invited_email__iexact=email)
        .exclude(declined=True)
        .first()
    )

    n_after = len(remaining_after) + (0 if existing_same else 1)
    if existing_same and not to_cancel:
        n_after = len(remaining_after)
    preview = {
        "mode": mode,
        "lease_number": lease.lease_number or str(lease.pk),
        "lease_status": lease.status,
        "property": place,
        "lease_total_rent": str(lease.total_rent or "0"),
        "will_cancel": _slot_label(to_cancel) if to_cancel else None,
        "will_invite": {
            "name": name,
            "email": email,
            "rent_amount_each_approx": str(rent),
            "is_primary_tenant": want_primary,
            "rent_logic": (
                f"After this change, {n_after} active tenant(s) share "
                f"${lease.total_rent} equally among unsigned slots "
                f"(~${rent} each). Signed shares stay fixed."
            ),
        },
        "current_active_tenants": [_slot_label(a) for a in active],
        "keeps_existing_tenants": mode == "add",
        "action": "resend_and_rebalance"
        if existing_same and not to_cancel
        else "create_invite_and_rebalance",
    }
    if not _confirmed(confirm):
        return _preview(
            "invite_tenant_to_lease",
            preview,
            "mode=add keeps everyone and rebalances rent. "
            "mode=replace needs replace_name/email. confirm=yes to apply.",
        )

    cancelled_label = _slot_label(to_cancel) if to_cancel else None
    with transaction.atomic():
        if to_cancel:
            # Cancel without nested confirm
            if not to_cancel.tenant_id and not to_cancel.has_signed:
                to_cancel.delete()
            else:
                LeaseTenant.objects.filter(pk=to_cancel.pk).update(
                    is_primary_tenant=False,
                    declined=True,
                    declined_at=timezone.now(),
                    decline_reason="Landlord replaced invite",
                )

        if want_primary:
            LeaseTenant.objects.filter(lease=lease, is_primary_tenant=True).update(
                is_primary_tenant=False
            )

        # Refresh existing same email after cancel
        existing_same = (
            LeaseTenant.objects.filter(lease=lease, invited_email__iexact=email)
            .exclude(declined=True)
            .first()
        )

        if existing_same:
            lt = existing_same
            lt.invited_name = name or lt.invited_name
            if phone:
                lt.invited_phone = phone
            lt.rent_amount = rent
            lt.is_primary_tenant = want_primary
            lt.declined = False
            if linked_tenant is not None and not lt.tenant_id:
                lt.tenant = linked_tenant
                lt.invite_accepted_at = timezone.now()
            lt.save()
        else:
            room = None
            if (
                lease.group_id
                and lease.property_id
                and getattr(lease.property, "property_category", None) == "ROOM"
            ):
                room = lease.property
            lt = LeaseTenant(
                lease=lease,
                invited_email=email,
                invited_name=name,
                invited_phone=phone or "",
                rent_amount=rent,
                is_primary_tenant=want_primary,
            )
            # Link an existing tenant account rather than leaving a pending
            # invited-email slot (which is what previously errored).
            if linked_tenant is not None:
                lt.tenant = linked_tenant
                lt.invite_accepted_at = timezone.now()
            if room is not None:
                lt.room = room
            try:
                lt.full_clean()
                lt.save()
            except ValidationError as exc:
                # Roll back the whole invite and return a clean message instead
                # of the bare "ValidationError: …" the dispatcher would surface
                # as a "system error".
                transaction.set_rollback(True)
                msgs = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
                return {"error": f"Couldn't add this tenant: {msgs}"}

        if lease.status == Lease.LeaseStatus.DRAFT:
            lease.status = Lease.LeaseStatus.PENDING_SIGNATURES
            lease.save(update_fields=["status", "updated_at"])

        lt.invite_sent_at = timezone.now()
        lt.save(update_fields=["invite_sent_at", "updated_at"])

        # Critical: rebalance ALL unsigned active shares so add-roommate
        # becomes $500/$500 not "$1000 new + leave old at $1000".
        rebalance_lease_rent_shares(lease, force_equal_unsigned=True)
        lt.refresh_from_db()

    from .links import canonical_frontend_origin

    frontend = canonical_frontend_origin()
    invite_url = lt.get_invite_url(frontend)
    email_sent = False
    email_error = None
    try:
        email_sent = bool(send_tenant_invite(lt))
    except Exception as exc:  # noqa: BLE001
        email_error = str(exc)
    linked = bool(getattr(lt, "tenant_id", None))
    from rentium.leases.models import LeaseInviteEvent
    from rentium.leases.services import record_invite_event

    record_invite_event(
        lt,
        LeaseInviteEvent.Kind.SENT,
        actor=getattr(landlord, "user", None),
        metadata={"email_sent": email_sent, "source": "rama"},
    )
    if linked:
        record_invite_event(
            lt,
            LeaseInviteEvent.Kind.ACCOUNT_LINKED,
            actor=getattr(landlord, "user", None),
            metadata={"linked_existing_account": True, "source": "rama"},
        )

    roster = [_slot_label(a) for a in _active_tenant_slots(lease)]
    if linked:
        note = (
            "Linked their existing Rentium account — no invite email needed; "
            "they can sign in and sign the lease directly. Rent rebalanced "
            "across unsigned active tenants."
        )
    else:
        note = (
            ("Email sent. " if email_sent else "Invite saved; email may have failed — use invite_url. ")
            + "Rent rebalanced across unsigned active tenants."
        )
    return {
        "invited": True,
        "linked_existing_account": linked,
        "mode": mode,
        "cancelled_prior": cancelled_label,
        "email_sent": email_sent,
        "email_error": email_error,
        "lease_tenant_id": str(lt.pk),
        "lease_number": lease.lease_number or "",
        "lease_status": lease.status,
        "property": place,
        "tenant_name": lt.invited_name,
        "tenant_email": lt.invited_email,
        "rent_amount": str(lt.rent_amount),
        "is_primary_tenant": lt.is_primary_tenant,
        "lease_total_rent": str(lease.total_rent or "0"),
        "invite_sent_at": lt.invite_sent_at.isoformat() if lt.invite_sent_at else None,
        "invite_url": invite_url,
        "active_tenants_now": roster,
        "rent_shares_sum": str(
            sum((__import__("decimal").Decimal(a["rent_amount"]) for a in roster),
                __import__("decimal").Decimal("0"))
        ),
        "note": note,
    }


def add_roommate_to_lease(
    landlord,
    *,
    email: str,
    name: str,
    property_query: str = "",
    lease_number: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """Add a roommate/co-tenant WITHOUT removing anyone. Always mode=add.

    Use when landlord says 'add another tenant/roommate'. Rebalances rent
    equally among unsigned active tenants (e.g. $1000 → $500 + $500).
    """
    return invite_tenant_to_lease(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        rent_amount="",  # force auto equal split
        is_primary="auto",  # stays non-primary if one exists
        phone=phone,
        mode="add",
        replace_email="",
        replace_name="",
        confirm=confirm,
    )


def rebalance_lease_rents(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Equal-split lease.total_rent across unsigned active tenants (signed fixed)."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)
    before = [_slot_label(a) for a in _active_tenant_slots(lease)]
    preview = {
        "property": _place(lease),
        "lease_number": lease.lease_number,
        "lease_total_rent": str(lease.total_rent),
        "before": before,
        "plan": "Equal split among unsigned active tenants",
    }
    if not _confirmed(confirm):
        return _preview("rebalance_lease_rents", preview, "Updates rent_amount rows.")
    rebalance_lease_rent_shares(lease, force_equal_unsigned=True)
    after = [_slot_label(a) for a in _active_tenant_slots(lease)]
    return {
        "rebalanced": True,
        "lease_total_rent": str(lease.total_rent),
        "before": before,
        "after": after,
    }


def replace_lease_invite(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    remove_email: str = "",
    remove_name: str = "",
    email: str,
    name: str,
    rent_amount: str = "",
    phone: str = "",
    confirm: str = "",
) -> dict:
    """One-shot: remove/suspend a pending invite and invite someone else instead.

    Preferred tool when user says: suspend X, invite Y instead. Rent defaults to
    sole-tenant full total_rent after removal (or auto-split if others remain).
    """
    return invite_tenant_to_lease(
        landlord,
        property_query=property_query,
        lease_number=lease_number,
        email=email,
        name=name,
        rent_amount=rent_amount,
        is_primary="auto",
        phone=phone,
        replace_email=remove_email,
        replace_name=remove_name,
        mode="replace",
        confirm=confirm,
    )


def resend_lease_invite(
    landlord,
    *,
    email: str = "",
    property_query: str = "",
    lease_number: str = "",
    confirm: str = "",
) -> dict:
    """Resend signing invite for an existing pending LeaseTenant slot."""
    from django.conf import settings
    from django.utils import timezone

    from rentium.leases.models import LeaseTenant
    from rentium.showcase.emails import send_tenant_invite

    qs = LeaseTenant.objects.filter(
        lease__landlord=landlord, declined=False, has_signed=False
    )
    if email:
        qs = qs.filter(invited_email__iexact=email.strip())
    if lease_number:
        qs = qs.filter(lease__lease_number__iexact=lease_number.strip())
    if property_query:
        qs = qs.filter(lease__property__name__icontains=property_query.strip())
    qs = qs.select_related("lease", "lease__property")
    if qs.count() != 1:
        return {
            "error": f"Need exactly one pending invite slot (found {qs.count()}). "
            "Pass email and property_query.",
            "candidates": [
                {
                    "email": x.invited_email,
                    "name": x.invited_name,
                    "lease": x.lease.lease_number,
                    "property": x.lease.property.name if x.lease.property_id else "",
                }
                for x in qs[:10]
            ],
        }
    lt = qs.first()
    place = _place(lt.lease)
    preview = {
        "tenant_email": lt.invited_email,
        "tenant_name": lt.invited_name,
        "lease_number": lt.lease.lease_number,
        "property": place,
        "rent_amount": str(lt.rent_amount),
    }
    if not _confirmed(confirm):
        return _preview(
            "resend_lease_invite", preview, "Resends the signing invite email only."
        )

    lt.invite_sent_at = timezone.now()
    lt.save(update_fields=["invite_sent_at", "updated_at"])
    from .links import canonical_frontend_origin

    frontend = canonical_frontend_origin()
    invite_url = lt.get_invite_url(frontend)
    sent = bool(send_tenant_invite(lt))
    from rentium.leases.models import LeaseInviteEvent
    from rentium.leases.services import record_invite_event

    record_invite_event(
        lt,
        LeaseInviteEvent.Kind.RESENT,
        actor=getattr(landlord, "user", None),
        metadata={"email_sent": sent, "source": "rama"},
    )
    return {
        "resent": True,
        "email_sent": sent,
        "invite_url": invite_url,
        "tenant_email": lt.invited_email,
        "property": place,
    }


def list_lease_roster(
    landlord, *, property_query: str = "", lease_number: str = ""
) -> dict:
    """Who is on a lease: signed, pending invites, declined, rent shares, primary."""
    lease, err = _resolve_lease(
        landlord, property_query=property_query, lease_number=lease_number
    )
    if err:
        return _prop_err(err)
    from decimal import Decimal

    active = _active_tenant_slots(lease)
    declined = list(lease.lease_tenants.filter(declined=True)[:20])
    total = lease.total_rent
    allocated = sum((Decimal(a.rent_amount) for a in active), Decimal("0"))
    return {
        "lease_number": lease.lease_number,
        "lease_status": lease.status,
        "property": _place(lease),
        "lease_total_rent": str(total),
        "allocated_rent": str(allocated),
        "unallocated_rent": str(Decimal(total or 0) - allocated),
        "active_tenants": [_slot_label(a) for a in active],
        "declined_or_cancelled": [_slot_label(d) for d in declined],
        "rules": {
            "rent": (
                "Shares should sum to lease_total_rent. One person alone gets full "
                "total; two people typically split equally unless landlord overrides."
            ),
            "primary": "Only one is_primary_tenant per lease.",
            "replace": (
                "To swap invitees use replace_lease_invite or "
                "invite_tenant_to_lease mode=replace — do not resend the old one."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Expenses (simple landlord expense)
# ---------------------------------------------------------------------------


def create_expense(
    landlord,
    *,
    amount: str,
    description: str,
    property_query: str = "",
    holding_name: str = "",
    effective_date: str = "",
    confirm: str = "",
) -> dict:
    from rentium.ledger import services as ledger_services

    try:
        amt = Decimal(str(amount).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return {"error": f"Invalid amount {amount!r}."}
    if amt <= 0:
        return {"error": "amount must be positive."}
    desc = (description or "").strip()
    if not desc:
        return {"error": "description is required."}

    # A LISTING, a whole UNIT, the whole HOLDING, or portfolio-wide. Without
    # the unit path a shared repair could only be booked against one of the
    # rooms that share the space — which is how the $19.78 shower knob ended up
    # charged to Room C alone. Without the holding path, "the whole house" was
    # not expressible at all and quietly resolved to a unit inside it.
    prop = unit = holding = None
    if property_query or holding_name:
        prop, unit, holding, err = _resolve_expense_scope(
            landlord, property_query, holding_name
        )
        if err:
            return err

    day = date.today()
    if effective_date:
        try:
            day = date.fromisoformat(effective_date.strip()[:10])
        except ValueError:
            return {"error": f"effective_date must be YYYY-MM-DD, got {effective_date!r}."}

    where = _expense_scope_label(prop, unit, holding)
    preview = {
        "amount": str(amt),
        "description": desc[:200],
        "property": where,
        "effective_date": day.isoformat(),
    }
    # Shown in the preview so the landlord decides BEFORE the money is on the
    # books, rather than discovering the double entry in a month-end total.
    duplicates = ledger_services.find_duplicate_expense_candidates(
        landlord,
        amount=amt,
        on_date=day,
        property=prop,
        holding=(prop.holding if prop is not None else (unit.holding if unit else holding)),
    )
    if duplicates:
        preview["possible_duplicates"] = duplicates
        preview["duplicate_warning"] = (
            f"You already have {len(duplicates)} expense(s) of ${amt} within "
            f"two weeks of this date. Check this is not the same cost being "
            f"recorded twice before confirming."
        )
    if not _confirmed(confirm):
        return _preview("create_expense", preview, "Posts a landlord expense to the ledger.")

    from rentium.ledger.models import ExpenseCategory

    try:
        result = ledger_services.post_expense(
            landlord=landlord,
            amount=amt,
            category=ExpenseCategory.OTHER,
            description=desc[:255],
            incurred_date=day,
            property=prop,
            # A shared-space or whole-property cost has no listing to charge
            # but always has an address, so it stays attributable instead of
            # landing nowhere. property=None + holding=X is the ledger's
            # first-class representation of a holding-wide cost.
            holding=(
                prop.holding
                if prop is not None
                else (unit.holding if unit is not None else holding)
            ),
            vendor="",
            created_by=landlord.user,
        )
        entry = result[0] if isinstance(result, tuple) else result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not post expense: {exc}"}

    return {
        "created": True,
        "expense": {
            "id": str(getattr(entry, "pk", "")),
            "amount": str(amt),
            "description": desc[:200],
            "property": where,
            # `scope` is what the confirmation message reads back
            # (service._write_label), so a holding-wide cost is reported as the
            # whole property rather than falling back to whichever unit label
            # happened to be on the plan step.
            "scope": where or "portfolio-wide",
            "effective_date": day.isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Condition inspections (RTB-27 style) — NOT the same as schedule_viewing
# ---------------------------------------------------------------------------


def create_condition_inspection(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
    tenant_email: str = "",
    confirm: str = "",
) -> dict:
    """
    Create a real ConditionInspection via build_inspection (same as UI
    Condition Inspections). Requires a lease with at least one active tenant
    for room leases. This is NOT schedule_viewing (showings).
    """
    from rentium.leases.inspection_services import InspectionError, build_inspection
    from rentium.leases.inspections import ConditionInspection
    from rentium.leases.models import Lease, LeaseTenant

    lease = None
    ln = (lease_number or "").strip()
    if ln:
        lease = (
            Lease.objects.filter(landlord=landlord, lease_number__iexact=ln)
            .select_related("property", "group")
            .first()
        )
        if not lease:
            return {"error": f"No lease {ln!r}."}
    else:
        pq = (property_query or "").strip()
        if not pq:
            return {"error": "Pass property_query or lease_number."}
        prop, err = _resolve_property(landlord, pq)
        if err:
            return _prop_err(err)
        lease = (
            Lease.objects.filter(landlord=landlord, property=prop)
            .exclude(status=Lease.LeaseStatus.TERMINATED)
            .order_by("-created_at")
            .first()
        )
        if not lease:
            return {"error": f"No active/pending lease on {prop.name}."}

    # Prefer specific tenant email; else primary; else first active
    lts = list(
        lease.lease_tenants.filter(declined=False).select_related("tenant", "room")
    )
    if not lts:
        return {
            "error": (
                "No tenants on this lease yet. Invite a tenant first "
                "(invite_tenant_to_lease / add_roommate_to_lease), then create "
                "the condition inspection."
            )
        }

    def _lt_email(lt) -> str:
        if getattr(lt, "invited_email", None):
            return (lt.invited_email or "").strip()
        t = getattr(lt, "tenant", None)
        if t is not None:
            u = getattr(t, "user", None)
            if u is not None and getattr(u, "email", None):
                return (u.email or "").strip()
        return ""

    def _lt_name(lt) -> str:
        if getattr(lt, "invited_name", None):
            return (lt.invited_name or "").strip()
        t = getattr(lt, "tenant", None)
        if t is not None:
            u = getattr(t, "user", None)
            if u is not None:
                full = (getattr(u, "get_full_name", lambda: "")() or "").strip()
                if full:
                    return full
        return _lt_email(lt) or "tenant"

    chosen = None
    email_q = (tenant_email or "").strip().lower()
    if email_q:
        for lt in lts:
            em = _lt_email(lt).lower()
            if email_q == em or email_q in em:
                chosen = lt
                break
        if not chosen:
            return {"error": f"No tenant matching {tenant_email!r} on this lease."}
    else:
        chosen = next((lt for lt in lts if lt.is_primary_tenant), lts[0])

    place = lease.property.name if lease.property_id else (lease.group.name if lease.group_id else "")
    inv_count = 0
    if lease.property_id:
        inv_count = lease.property.inventory_items.count()

    already = ConditionInspection.objects.filter(
        lease=lease, lease_tenant=chosen
    ).first()
    if already:
        return {
            "error": (
                f"Condition inspection already exists for this tenant "
                f"(id={already.pk}, status={already.status}). Use list_inspections."
            ),
            "inspection_id": str(already.pk),
        }

    tenant_label = _lt_name(chosen)
    preview = {
        "kind": "condition_inspection",
        "NOT": "This is NOT a calendar viewing — use schedule_viewing only for showings.",
        "lease_number": lease.lease_number,
        "property": place,
        "tenant": tenant_label,
        "tenant_email": _lt_email(chosen),
        "possession_date": str(lease.move_in_date or lease.start_date),
        "inventory_items_on_property": inv_count,
        "warning": (
            None
            if inv_count
            else "Property inventory empty — inspection/agreement will list no furnishings."
        ),
    }
    if not _confirmed(confirm):
        return _preview(
            "create_condition_inspection",
            preview,
            "Creates RTB-style condition inspection report (Condition Inspections panel).",
        )

    try:
        insp = build_inspection(
            lease=lease,
            lease_tenant=chosen,
            created_by=landlord.user,
        )
    except InspectionError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not create inspection: {exc}"}

    return {
        "created": True,
        "inspection": {
            "id": str(insp.pk),
            "status": insp.status,
            "lease_number": lease.lease_number,
            "property": place,
            "tenant": tenant_label,
            "possession_date": str(insp.possession_date)
            if getattr(insp, "possession_date", None)
            else None,
            "item_count": insp.items.count() if hasattr(insp, "items") else None,
        },
        "ui": "Shows under lease → Condition Inspections (not Appointments).",
    }


def lease_pdf_info(
    landlord,
    *,
    property_query: str = "",
    lease_number: str = "",
) -> dict:
    """
    How to get the lease PDF. PDFs are rendered on demand from lease data
    (GET /api/leases/<id>/pdf/) — they do NOT require document_file upload.
    """
    from rentium.leases.models import Lease

    lease = None
    ln = (lease_number or "").strip()
    if ln:
        lease = Lease.objects.filter(landlord=landlord, lease_number__iexact=ln).first()
    elif property_query:
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)
        lease = (
            Lease.objects.filter(landlord=landlord, property=prop)
            .order_by("-created_at")
            .first()
        )
    if not lease:
        return {"error": "Lease not found."}

    has_file = bool(getattr(lease, "document_file", None) and lease.document_file.name)
    return {
        "lease_number": lease.lease_number,
        "lease_id": str(lease.pk),
        "status": lease.status,
        "landlord_signed": bool(lease.landlord_signed),
        "pdf_always_available": True,
        "download_path": f"/api/leases/{lease.pk}/pdf/",
        "ui_hint": (
            "Use Download PDF on the lease page. The PDF is generated from the "
            "lease record + signatures — no separate file upload required."
        ),
        "stored_document_file": has_file,
        "rules": {
            "never_say_no_pdf_if_lease_exists": True,
            "document_file_optional": (
                "document_file is optional storage; live PDF endpoint always works."
            ),
        },
    }


def bulk_add_inventory(
    landlord,
    *,
    property_query: str,
    items: str,
    confirm: str = "",
) -> dict:
    """Add multiple private inventory items. items = 'Bed, Mattress, Desk' or JSON list."""
    from rentium.properties.models import InventoryItem

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    names = []
    raw = (items or "").strip()
    if raw.startswith("["):
        try:
            import json

            data = json.loads(raw)
            names = [str(x).strip() for x in data if str(x).strip()]
        except Exception:  # noqa: BLE001
            return {"error": "items JSON list invalid."}
    else:
        names = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not names:
        return {"error": "items is required (e.g. 'Single bed, Mattress')."}

    preview = {
        "property": prop.name,
        "items": names,
        "count": len(names),
    }
    if not _confirmed(confirm):
        return _preview(
            "bulk_add_inventory",
            preview,
            "Creates private inventory rows (What's in it + agreement + inspection).",
        )

    created = []
    for name in names:
        item = InventoryItem.objects.create(
            property=prop,
            name=name[:200],
            quantity=1,
            condition=InventoryItem.ItemCondition.GOOD,
        )
        created.append({"id": str(item.pk), "name": item.name})
    prop.refresh_from_db()
    return {
        "created": True,
        "property": prop.name,
        "items": created,
        "property_is_furnished": bool(prop.is_furnished),
    }


# ---------------------------------------------------------------------------
# Money IN.
#
# Until this existed RAMA had 115 tools and not one of them could record that a
# payment arrived — it could only ever spend. Asked to record $100 of a $425
# deposit, the General had no tool to reach for and said "Recorded the $100
# payment" anyway. The reply guard in service.py stops the lie; this is the
# capability whose absence caused it.
# ---------------------------------------------------------------------------
_PAYMENT_METHODS = {
    "etransfer": "ETRANSFER", "e-transfer": "ETRANSFER", "e transfer": "ETRANSFER",
    "transfer": "ETRANSFER", "interac": "ETRANSFER",
    "cash": "CASH", "cheque": "CHEQUE", "check": "CHEQUE",
}


def _open_charges(landlord, query: str = "", property_query: str = ""):
    """Charges with something still owing, newest first."""
    from rentium.ledger.models import CHARGE_TYPES, ChargeStatus, LedgerEntry

    rows = (
        LedgerEntry.objects.not_voided()
        .filter(landlord=landlord, entry_type__in=CHARGE_TYPES)
        .select_related("property", "tenant", "tenant__user")
        .order_by("-effective_date")
    )
    for word in (query or "").split():
        rows = rows.filter(description__icontains=word)
    if property_query:
        rows = rows.filter(property__name__icontains=property_query)
    settled = (ChargeStatus.PAID, ChargeStatus.VOIDED)
    return [row for row in rows if row.charge_status() not in settled]


def _outstanding_on(charge) -> Decimal:
    from django.db.models import Sum

    from rentium.ledger.models import SETTLEMENT_TYPES

    paid = charge.settlements.filter(
        entry_type__in=SETTLEMENT_TYPES, reversed_by__isnull=True
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    return charge.amount - paid


def _charge_label(charge) -> str:
    where = charge.property.name if charge.property_id else "portfolio"
    return f"{charge.description[:60]} ({where}, ${charge.amount})"


def record_payment(
    landlord,
    *,
    amount: str,
    charge_query: str = "",
    property_query: str = "",
    payment_method: str = "",
    payment_date: str = "",
    confirm: str = "",
) -> dict:
    from rentium.ledger import services as ledger_services

    try:
        amt = Decimal(str(amount).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return {"error": f"Invalid amount {amount!r}."}
    if amt <= 0:
        return {"error": "A payment must be a positive amount."}

    candidates = _open_charges(landlord, charge_query, property_query)
    if not candidates:
        return {
            "error": "no_matching_charge",
            "message": (
                "I can't find an unpaid charge matching that. Money is always "
                "recorded against the charge it settles, so tell me which "
                "charge this was for — or post the charge first."
            ),
        }
    if len(candidates) > 1:
        return {
            "question_for_user": (
                "Which charge was this payment for?\n"
                + "\n".join(
                    f"• {_charge_label(c)} — ${_outstanding_on(c)} still owing"
                    for c in candidates[:6]
                )
            ),
            "candidates": [_charge_label(c) for c in candidates[:6]],
        }

    charge = candidates[0]
    outstanding = _outstanding_on(charge)

    day = date.today()
    if payment_date:
        try:
            day = date.fromisoformat(payment_date.strip()[:10])
        except ValueError:
            return {
                "error": f"payment_date must be YYYY-MM-DD, got {payment_date!r}."
            }

    # Never guessed silently. A default of "e-Transfer" quietly puts a fact on
    # a financial record that nobody stated, and the landlord only finds out
    # when they reconcile against a bank statement that shows cash. If the
    # model could not pick it up from the conversation, ask once.
    raw_method = (payment_method or "").strip().lower()
    if not raw_method:
        return {
            "question_for_user": (
                f"How did the ${amt} come in — e-transfer, cash, or cheque?"
            ),
            "needs": "payment_method",
        }
    method = _PAYMENT_METHODS.get(raw_method)
    if method is None:
        return {
            "question_for_user": (
                f"I don't recognise {payment_method!r} as a payment method. "
                f"Was it an e-transfer, cash, or a cheque?"
            ),
            "needs": "payment_method",
        }

    preview = {
        "charge": _charge_label(charge),
        "charge_amount": str(charge.amount),
        "already_paid": str(charge.amount - outstanding),
        "this_payment": str(amt),
        # The number the landlord actually wants to see before saying yes.
        "still_owing_after": str(max(outstanding - amt, Decimal("0.00"))),
        "method": method,
        "payment_date": day.isoformat(),
    }
    if amt > outstanding:
        preview["overpayment_warning"] = (
            f"That is ${amt - outstanding} MORE than the ${outstanding} still "
            f"owing on this charge. Confirm only if the tenant genuinely "
            f"overpaid; otherwise re-send with the right amount."
        )
    if not _confirmed(confirm):
        return _preview(
            "record_payment",
            preview,
            "Records money received against this charge. Nothing moves in the "
            "real world — this is the book entry.",
        )

    try:
        entry, created = ledger_services.record_payment(
            charge=charge,
            amount=amt,
            payment_method=method,
            payment_date=day,
            # Same amount, same charge, same day is almost certainly a repeat of
            # one confirmation rather than a second real payment.
            idempotency_key=f"rama-payment:{charge.pk}:{day}:{amt}",
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the landlord
        return {"error": str(exc)}

    remaining = _outstanding_on(charge)
    return {
        "ok": True,
        "duplicate": not created,
        "entry_id": str(entry.pk),
        "charge": _charge_label(charge),
        "amount": str(amt),
        "still_owing": str(remaining),
        "charge_status": charge.charge_status(),
        "message": (
            f"Recorded ${amt} against {charge.description[:60]}. "
            + (
                f"${remaining} still owing."
                if remaining > 0
                else "That charge is now fully paid."
            )
        ),
    }
