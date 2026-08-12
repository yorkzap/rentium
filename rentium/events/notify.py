# rentium/events/notify.py
"""
Fan-out: turn DomainEvents into per-user Notifications.

One handler subscribed to "*" inspects every event and, using ROUTES, creates
Notification rows for the right audience. Recipient resolution (which landlord,
which tenants) is centralised here so business code never needs to know who to
tell. Idempotent via the (event, recipient) unique constraint, so event replay
never double-notifies.
"""

import logging

from .models import Notification
from .registry import on

logger = logging.getLogger(__name__)

# event_type -> (audience, category)
# audience: "LANDLORD" | "TENANT" | "BOTH"
ROUTES = {
    "maintenance.created": ("LANDLORD", Notification.Category.MAINTENANCE),
    "maintenance.sla_breached": ("LANDLORD", Notification.Category.MAINTENANCE),
    "maintenance.status_changed": ("TENANT", Notification.Category.MAINTENANCE),
    "ledger.payment_posted": ("LANDLORD", Notification.Category.PAYMENT),
    "ledger.charge_due_soon": ("TENANT", Notification.Category.PAYMENT),
    "lease.activated": ("BOTH", Notification.Category.LEASE),
    "lease.expired": ("BOTH", Notification.Category.LEASE),
    "lease.status_changed": ("BOTH", Notification.Category.LEASE),
    # --- Condition inspections (RTB-27 flow) ---
    "inspection.created": ("TENANT", Notification.Category.LEASE),
    "inspection.awaiting_signature": ("TENANT", Notification.Category.LEASE),
    "inspection.completed": ("BOTH", Notification.Category.LEASE),
    "inspection.suggestions": ("LANDLORD", Notification.Category.MAINTENANCE),
    "inspection.delivery_due": ("LANDLORD", Notification.Category.LEASE),
    "inquiry.created": ("LANDLORD", Notification.Category.MESSAGE),
    "message.created": (
        "CUSTOM",
        Notification.Category.MESSAGE,
    ),  # recipients in payload
    # RAMA: a Sergeant's finding, analyzed by the FSA — see rama/handlers.py.
    # Portfolio/holding-scoped (landlord_id in payload; see _landlord_user).
    "rama.insight.created": ("LANDLORD", Notification.Category.SYSTEM),
}


# ----------------------------------------------------- recipient resolution
def _lease(event):
    if not event.lease_id:
        return None
    from rentium.leases.models import Lease

    return (
        Lease.objects.filter(pk=event.lease_id).select_related("landlord__user").first()
    )


def _property(event):
    if not event.property_id:
        return None
    from rentium.properties.models import Property

    return (
        Property.objects.filter(pk=event.property_id)
        .select_related("landlord__user")
        .first()
    )


def _landlord_user(event):
    lease = _lease(event)
    if lease and lease.landlord and getattr(lease.landlord, "user_id", None):
        return lease.landlord.user
    prop = _property(event)
    if prop and prop.landlord and getattr(prop.landlord, "user_id", None):
        return prop.landlord.user
    # Portfolio/holding-scoped events (no natural property or lease anchor —
    # e.g. RAMA's Sergeant findings) carry landlord_id directly in payload.
    landlord_id = (event.payload or {}).get("landlord_id")
    if landlord_id:
        from rentium.users.models import LandlordProfile

        landlord = (
            LandlordProfile.objects.filter(pk=landlord_id)
            .select_related("user")
            .first()
        )
        if landlord and landlord.user_id:
            return landlord.user
    return None


def _tenant_users(event):
    """
    Tenants to notify for a lease-scoped event. Inspection events carry a
    lease_tenant_id in their payload when they concern ONE roommate's
    document — in that case only that tenant is notified, not the whole
    household (a roommate shouldn't be pinged to sign someone else's
    room inspection).
    """
    lease = _lease(event)
    if not lease:
        return []
    lease_tenants = lease.lease_tenants.filter(tenant__isnull=False).select_related(
        "tenant__user"
    )
    target_lt = (event.payload or {}).get("lease_tenant_id")
    if target_lt:
        lease_tenants = lease_tenants.filter(pk=target_lt)
    users = []
    for lt in lease_tenants:
        user = getattr(lt.tenant, "user", None)
        if user:
            users.append(user)
    return users


def _recipients(audience, event):
    if audience == "LANDLORD":
        u = _landlord_user(event)
        return [u] if u else []
    if audience == "TENANT":
        return _tenant_users(event)
    if audience == "BOTH":
        out = _tenant_users(event)
        lu = _landlord_user(event)
        if lu:
            out.append(lu)
        return out
    if audience == "CUSTOM":
        # message.created carries explicit recipient user ids
        from django.contrib.auth import get_user_model

        ids = event.payload.get("recipient_ids", [])
        return list(get_user_model().objects.filter(pk__in=ids))
    return []


# --------------------------------------------------------------- rendering
def _render(event):
    """(title, body, url) for the notification, from the event payload."""
    p = event.payload or {}
    t = event.event_type
    if t == "maintenance.created":
        emergency = " (RTA emergency)" if p.get("rta_emergency") else ""
        return (
            f"New maintenance report{emergency}",
            p.get("title", ""),
            "/dashboard/maintenance",
        )
    if t == "maintenance.sla_breached":
        return (
            "Maintenance SLA breached",
            f"{p.get('title', '')} is past its response deadline.",
            "/dashboard/maintenance",
        )
    if t == "maintenance.status_changed":
        return (
            "Maintenance update",
            f"Your request moved to {p.get('to', '').replace('_', ' ').title()}.",
            "/dashboard/maintenance",
        )
    if t == "ledger.payment_posted":
        return (
            "Payment recorded",
            f"${p.get('amount', '')} recorded ({p.get('method', '')}).",
            "/dashboard/financial",
        )
    if t == "ledger.charge_due_soon":
        who = "Your household owes" if p.get("joint") else "You owe"
        return (
            "Rent due soon",
            f"{who} ${p.get('amount_outstanding', '')}, due {p.get('due_date', '')}.",
            "/dashboard/financial",
        )
    if t == "lease.activated":
        lease = _lease(event)
        if lease is not None:
            label = lease.lease_number or f"Draft-{lease.pk.hex[:6]}"
            subject = (
                lease.property.name
                if lease.property_id
                else lease.group.name
                if lease.group_id
                else "the rental"
            )
            return (
                "Lease activated",
                f"Lease {label} for {subject} is now active.",
                f"/dashboard/leases/{lease.pk}",
            )
        return "Lease activated", "A lease is now active.", "/dashboard/leases"
    if t == "lease.expired":
        return "Lease expired", "A lease has reached its end date.", "/dashboard/leases"
    if t == "lease.status_changed":
        return (
            "Lease updated",
            f"Lease status is now {p.get('to', '')}.",
            "/dashboard/leases",
        )
    if t == "inspection.created":
        return (
            "Move-in inspection scheduled",
            "Your landlord has started a condition inspection report — you'll "
            "walk through the unit together and sign it.",
            "/dashboard",
        )
    if t == "inspection.awaiting_signature":
        which = "move-out" if p.get("pass") == "MOVE_OUT" else "move-in"
        return (
            "Inspection ready for your signature",
            f"Please review the {which} condition report and sign "
            "(you can note any disagreements).",
            "/dashboard",
        )
    if t == "inspection.completed":
        which = "Move-out" if p.get("pass") == "MOVE_OUT" else "Move-in"
        disputed = " — with noted disagreements" if p.get("disputed") else ""
        return (
            f"{which} inspection signed",
            f"The {which.lower()} condition report is fully signed{disputed}.",
            "/dashboard",
        )
    if t == "inspection.suggestions":
        count = p.get("count", 0)
        return (
            "Inspection flagged possible maintenance",
            f"{count} item(s) from the inspection may need attention — review "
            "and approve or dismiss.",
            "/dashboard/maintenance",
        )
    if t == "inspection.delivery_due":
        which = "move-out" if p.get("pass") == "MOVE_OUT" else "move-in"
        if p.get("stage") == "overdue":
            return (
                "Inspection report delivery OVERDUE",
                f"The signed {which} report for lease {p.get('lease_number', '')} "
                f"was due to the tenant by {p.get('deadline', '')}. Deliver it and "
                "mark it delivered — missing this deadline can extinguish deposit claims.",
                "/dashboard/leases",
            )
        return (
            "Inspection report delivery due soon",
            f"Deliver the signed {which} report for lease "
            f"{p.get('lease_number', '')} to the tenant by {p.get('deadline', '')} "
            "(RTB deadline), then mark it delivered.",
            "/dashboard/leases",
        )
    if t == "message.created":
        return (
            p.get("title", "New message"),
            p.get("preview", ""),
            "/dashboard/messages",
        )
    if t == "rama.insight.created":
        icon = {"URGENT": "🔴", "WARN": "🟡", "INFO": "🔵"}.get(p.get("severity"), "")
        return (
            f"{icon} {p.get('title', 'RAMA insight')}".strip(),
            p.get("analysis", "")[:280],
            "/dashboard/insights",
        )
    if t == "inquiry.created":
        return (
            f"New inquiry — {p.get('property_name', 'a property')}",
            f"{p.get('name', 'Someone')} is interested. Reply by email, or "
            "schedule a viewing from your inbox.",
            "/dashboard/inquiries",
        )
    return event.event_type, "", ""


@on("*")
def fan_out_to_notifications(event):
    route = ROUTES.get(event.event_type)
    if not route:
        return
    audience, category = route
    title, body, url = _render(event)
    for user in _recipients(audience, event):
        Notification.objects.get_or_create(
            event=event,
            recipient=user,
            defaults={"category": category, "title": title, "body": body, "url": url},
        )
