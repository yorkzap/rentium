"""
Event handlers for the showcase app. Registered by ShowcaseConfig.ready().

Why this is a handler and not a line inside the inquiry view:

The view's job is to accept the message and return 201. Sending an email is a
call to an external service that can be slow, can time out, and — on a bad day —
can fail entirely. If that happened inside the view, a prospective tenant would
get a 500 and assume their message didn't send, when in fact it's sitting in the
database perfectly intact. Worse, ATOMIC_REQUESTS is on, so a raised exception
would roll the inquiry back and the message would be genuinely lost.

So the write commits, the event goes to the outbox, and delivery happens
downstream where a failure is a retry rather than a lost lead.

The in-app Notification is NOT created here — that's ROUTES in events/notify.py,
which fans every event out to the right audience in one place. This handler owns
only the email.
"""

import logging

from rentium.events.registry import on

logger = logging.getLogger(__name__)


@on("inquiry.created")
def email_landlord_on_inquiry(event):
    """
    A stranger is interested in a property. Tell the landlord, by email, with the
    prospective tenant set as reply-to — so hitting Reply talks to that person
    directly. No in-app relay to build, no message the tenant can't see, and no
    thread for anyone to forget to check. An inquiry is a lead, not a chat.
    """
    from .emails import send_inquiry_to_landlord
    from .models import Inquiry

    inquiry_id = (event.payload or {}).get("inquiry_id")
    if not inquiry_id:
        return

    inquiry = (
        Inquiry.objects.filter(pk=inquiry_id)
        .select_related("property", "landlord__user", "landlord__showcase")
        .first()
    )
    if not inquiry:
        # The inquiry was deleted between publish and dispatch. Nothing to do,
        # and nothing worth shouting about.
        logger.info("inquiry.created for a missing inquiry (%s)", inquiry_id)
        return

    if not send_inquiry_to_landlord(inquiry):
        # send() never raises — it logs and returns False — so a dead SMTP server
        # can't take down the dispatcher and poison every other handler on the
        # event. Raising here would make the Celery task retry, which is what we
        # want: a transient mail failure should get another go.
        raise RuntimeError(f"Could not email the landlord about inquiry {inquiry.pk}")
