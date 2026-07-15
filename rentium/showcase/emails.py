"""
Real outbound email. Until now events/handlers.py logged a stub and tenant
invites were a `# TODO: fire invite email` — which meant the invite link only
worked if the landlord copy-pasted it by hand, and an inquiry from a stranger
would have reached nobody at all.

Dev: cookiecutter's Mailpit catches everything at http://localhost:8025.
Prod: set the SMTP env vars in config/settings/base.py (Postmark/Mailgun/SES).
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def send(template: str, *, to: list[str], subject: str, context: dict, reply_to=None):
    """Renders emails/<template>.html, derives the text part, sends. Never raises."""
    if not to:
        return False
    ctx = {
        "site_url": settings.PUBLIC_SITE_URL.rstrip("/"),
        "subject": subject,
        **context,
    }
    try:
        html = render_to_string(f"emails/{template}.html", ctx)
        msg = EmailMultiAlternatives(
            subject=subject,
            body=strip_tags(html),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
            reply_to=reply_to or None,
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Email '%s' to %s failed", template, to)
        return False


def send_inquiry_to_landlord(inquiry):
    """
    reply_to is the prospective tenant, so the landlord just hits Reply and
    is talking to them directly — no in-app relay to build, no message the
    tenant can't see. The inquiry is a lead, not a chat thread.
    """
    showcase = getattr(inquiry.landlord, "showcase", None)
    to = showcase.inquiry_email if showcase else inquiry.landlord.user.email
    return send(
        "inquiry_received",
        to=[to],
        subject=f"New inquiry — {inquiry.property.name}",
        context={"inquiry": inquiry, "property": inquiry.property},
        reply_to=[inquiry.email],
    )


def send_tenant_invite(lease_tenant):
    """The invite link that was previously never actually emailed."""
    if not lease_tenant.invited_email or lease_tenant.tenant_id:
        return False
    url = lease_tenant.get_invite_url(settings.FRONTEND_URL)
    if not url:
        return False
    lease = lease_tenant.lease
    return send(
        "tenant_invite",
        to=[lease_tenant.invited_email],
        subject=f"{lease.landlord.user.name} invited you to sign a lease",
        context={
            "name": lease_tenant.invited_name or "",
            "landlord_name": lease.landlord.user.name,
            "property_label": (
                lease.property.name
                if lease.property_id
                else (lease.group.name if lease.group_id else lease.lease_number)
            ),
            "rent": lease.total_rent,
            "start_date": lease.start_date,
            "invite_url": url,
        },
    )
