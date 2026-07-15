"""Message send path — creates the row, bumps the thread, publishes an event."""

from django.db import transaction

from rentium.events.registry import publish

from .models import Conversation, Message


def send_message(conversation: Conversation, sender, body: str) -> Message:
    with transaction.atomic():
        msg = Message.objects.create(conversation=conversation, sender=sender, body=body)
        conversation.touch()

    # Recipient = the other participant's user.
    landlord_user = getattr(conversation.landlord, "user", None)
    tenant_user = getattr(conversation.tenant, "user", None)
    recipient = tenant_user if sender == landlord_user else landlord_user
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
        },
        lease_id=conversation.lease_id,
    )
    return msg
