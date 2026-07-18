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
    return _prop_err(err)


# ---------------------------------------------------------------------------
# Work orders
# ---------------------------------------------------------------------------


def create_work_order(
    landlord,
    *,
    property_query: str,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    category: str = "OTHER",
    confirm: str = "",
) -> dict:
    from rentium.maintenance.models import WorkOrder

    prop, err = _resolve_property(landlord, property_query)
    if err:
        return _prop_err(err)
    title = (title or "").strip()
    if not title:
        return {"error": "title is required."}
    pr = (priority or "MEDIUM").strip().upper()
    cat = (category or "OTHER").strip().upper()
    if pr not in WorkOrder.Priority.values:
        pr = WorkOrder.Priority.MEDIUM
    if cat not in WorkOrder.Category.values:
        cat = WorkOrder.Category.OTHER

    preview = {
        "property": prop.name,
        "title": title,
        "description": (description or "")[:500],
        "priority": pr,
        "category": cat,
        "origin": "LANDLORD",
    }
    if not _confirmed(confirm):
        return _preview(
            "create_work_order",
            preview,
            "Creates an open NEW work order.",
        )

    wo = WorkOrder.objects.create(
        property=prop,
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
            "property": prop.name,
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
            wo = WorkOrder.objects.select_related("property").get(
                pk=work_order_id, property__landlord=landlord
            )
        except (WorkOrder.DoesNotExist, ValueError):
            return {"error": f"No work order {work_order_id!r}."}
    elif title_query:
        qs = WorkOrder.objects.filter(
            property__landlord=landlord, title__icontains=title_query.strip()
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

    from rentium.appointments.models import Appointment

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

    try:
        appt = Appointment.objects.create(
            landlord=landlord,
            property=prop,
            kind=Appointment.Kind.VIEWING,
            status=Appointment.Status.SCHEDULED,
            starts_at=starts,
            contact_name=(contact_name or "")[:200],
            contact_email=(contact_email or "")[:150],
            notes=(notes or "")[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not create viewing: {exc}"}

    return {
        "created": True,
        "appointment": {
            "id": str(appt.pk),
            "property": prop.name,
            "starts_at": starts.isoformat(),
            "status": appt.status,
            "kind": appt.kind,
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


def _place(lease) -> str:
    if lease.property_id:
        return lease.property.name
    if lease.group_id:
        return lease.group.name
    return ""


def _active_tenant_slots(lease):
    """Non-declined tenant slots (site treats declined as out of the roster)."""
    return list(
        lease.lease_tenants.filter(declined=False).select_related("tenant__user")
    )


def _slot_label(lt) -> dict:
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
            if room is not None:
                lt.room = room
            lt.full_clean()
            lt.save()

        if lease.status == Lease.LeaseStatus.DRAFT:
            lease.status = Lease.LeaseStatus.PENDING_SIGNATURES
            lease.save(update_fields=["status", "updated_at"])

        lt.invite_sent_at = timezone.now()
        lt.save(update_fields=["invite_sent_at", "updated_at"])

        # Critical: rebalance ALL unsigned active shares so add-roommate
        # becomes $500/$500 not "$1000 new + leave old at $1000".
        rebalance_lease_rent_shares(lease, force_equal_unsigned=True)
        lt.refresh_from_db()

    frontend = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
    invite_url = lt.get_invite_url(frontend)
    email_sent = False
    email_error = None
    try:
        email_sent = bool(send_tenant_invite(lt))
    except Exception as exc:  # noqa: BLE001
        email_error = str(exc)

    roster = [_slot_label(a) for a in _active_tenant_slots(lease)]
    return {
        "invited": True,
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
        "note": (
            ("Email sent. " if email_sent else "Invite saved; email may have failed — use invite_url. ")
            + "Rent rebalanced across unsigned active tenants."
        ),
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
    frontend = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
    invite_url = lt.get_invite_url(frontend)
    sent = bool(send_tenant_invite(lt))
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

    prop = None
    if property_query:
        prop, err = _resolve_property(landlord, property_query)
        if err:
            return _prop_err(err)

    day = date.today()
    if effective_date:
        try:
            day = date.fromisoformat(effective_date.strip()[:10])
        except ValueError:
            return {"error": f"effective_date must be YYYY-MM-DD, got {effective_date!r}."}

    preview = {
        "amount": str(amt),
        "description": desc[:200],
        "property": prop.name if prop else None,
        "effective_date": day.isoformat(),
    }
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
            "property": prop.name if prop else None,
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
