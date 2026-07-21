"""Message send path — creates the row, bumps the thread, publishes an event.

Handles both participant kinds:
  - tenant thread   → the other party is a registered user; in-app notify them.
  - prospect thread → the other party is an accountless lead reached by email;
    when the landlord replies we email them a "continue the conversation" link
    carrying the conversation's access_token (handled by the message.created
    handler in events/handlers.py).
"""

from django.conf import settings
from django.db import transaction

from rentium.events.registry import publish

from .models import Conversation, Message


def chat_url(conversation: Conversation) -> str:
    """The prospect's tokenized, single-conversation chat page."""
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/chat/{conversation.access_token}"


def send_message(
    conversation: Conversation, sender, body: str, *, notify: bool = True
) -> Message:
    """Post a message. `sender` is the landlord/tenant User, or None when the
    accountless prospect posts through the public endpoint. `notify=False` seeds
    a message without firing the notification event — used for the very first
    inquiry message, which the inquiry.created event already announces (so the
    landlord doesn't get two rows for one lead)."""
    with transaction.atomic():
        msg = Message.objects.create(conversation=conversation, sender=sender, body=body)
        conversation.touch()

    if not notify:
        return msg

    landlord_user = getattr(conversation.landlord, "user", None)
    from_landlord = sender is not None and sender == landlord_user

    if conversation.is_prospect():
        # Prospect thread: landlord→prospect is delivered by email (the handler
        # reads to_prospect); prospect→landlord pings the landlord in-app.
        recipient_ids = [] if from_landlord else ([landlord_user.pk] if landlord_user else [])
        if sender is None:
            sender_name = conversation.prospect_name or conversation.prospect_email or "The enquirer"
        else:
            sender_name = getattr(sender, "name", None) or getattr(sender, "email", "Someone")
    else:
        tenant_user = getattr(conversation.tenant, "user", None)
        recipient = tenant_user if from_landlord else landlord_user
        recipient_ids = [recipient.pk] if recipient else []
        sender_name = getattr(sender, "name", None) or getattr(sender, "email", "Someone")

    publish(
        "message.created",
        {
            "conversation_id": str(conversation.id),
            "message_id": str(msg.id),
            "recipient_ids": recipient_ids,
            "title": f"New message from {sender_name}",
            "preview": body[:80],
            # For the prospect-email handler:
            "to_prospect": conversation.is_prospect() and from_landlord,
            "prospect_email": conversation.prospect_email,
            "prospect_name": conversation.prospect_name,
            "chat_url": chat_url(conversation) if conversation.is_prospect() else "",
            "landlord_name": getattr(landlord_user, "name", "") or "",
        },
        lease_id=conversation.lease_id,
    )
    return msg


def get_or_create_prospect_thread(landlord, property, email, name="") -> Conversation:
    """One prospect thread per landlord+email+listing; created on first contact."""
    conv = (
        Conversation.objects.filter(
            landlord=landlord,
            prospect_email__iexact=email,
            property=property,
            tenant__isnull=True,
        )
        .order_by("created_at")
        .first()
    )
    if conv is None:
        conv = Conversation.objects.create(
            landlord=landlord,
            prospect_email=email,
            prospect_name=name,
            property=property,
            subject=f"Enquiry — {property.name}" if property else "Enquiry",
        )
    elif name and not conv.prospect_name:
        conv.prospect_name = name
        conv.save(update_fields=["prospect_name"])
    return conv


def get_or_create_lead_thread(inquiry) -> Conversation:
    """Merge an inquiry into a continuing prospect thread."""
    return get_or_create_prospect_thread(
        inquiry.landlord, inquiry.property, inquiry.email, inquiry.name
    )


def post_prospect_message(
    conversation: Conversation, body: str, *, notify: bool = True
) -> Message:
    """A message from the accountless prospect (sender is None)."""
    return send_message(conversation, None, body, notify=notify)
