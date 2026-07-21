"""Stage 3 — inquiry↔messaging threading, with the security invariants that make
an accountless prospect a safe participant.
"""

from __future__ import annotations

import uuid

import pytest
from django.core import mail
from django.db import IntegrityError
from rest_framework.test import APIClient

from rentium.messaging.models import Conversation, Message
from rentium.messaging.services import (
    get_or_create_prospect_thread,
    post_prospect_message,
    send_message,
)


@pytest.fixture
def prospect_thread(landlord, bc_property):
    convo = get_or_create_prospect_thread(
        landlord, bc_property, "prospect@example.com", "Pat Prospect"
    )
    post_prospect_message(convo, "Hi, is this place still available?")
    return convo


# --------------------------------------------------------- data model
@pytest.mark.django_db
def test_tenant_xor_prospect_constraint(landlord, tenant, bc_property):
    # Both set → rejected.
    with pytest.raises(IntegrityError):
        Conversation.objects.create(
            landlord=landlord, tenant=tenant, prospect_email="x@example.com"
        )


@pytest.mark.django_db
def test_prospect_thread_is_deduped(landlord, bc_property):
    a = get_or_create_prospect_thread(landlord, bc_property, "p@example.com", "P")
    b = get_or_create_prospect_thread(landlord, bc_property, "P@EXAMPLE.COM", "P")
    assert a.pk == b.pk  # case-insensitive, one thread per landlord+email+listing


# --------------------------------------------------------- public chat endpoint
@pytest.mark.django_db
def test_public_chat_read_is_pii_minimized(prospect_thread, bc_property):
    client = APIClient()
    res = client.get(f"/api/public/chat/{prospect_thread.access_token}/")
    assert res.status_code == 200
    data = res.json()

    # Present: listing name, landlord name, the messages.
    assert data["listing"] == bc_property.name
    assert data["landlord_name"]
    assert data["messages"][0]["body"].startswith("Hi, is this place")
    assert data["messages"][0]["from_landlord"] is False

    # Absent: the street address and any other PII must never be in the payload.
    blob = res.content.decode().lower()
    assert bc_property.address.lower() not in blob
    assert "source_ip" not in blob
    assert "1234 oak" not in blob


@pytest.mark.django_db
def test_public_chat_send_appends_prospect_message(prospect_thread):
    client = APIClient()
    res = client.post(
        f"/api/public/chat/{prospect_thread.access_token}/send/",
        {"body": "Yes I'm interested, can I view it Saturday?"},
        format="json",
    )
    assert res.status_code == 200
    msg = prospect_thread.messages.order_by("created_at").last()
    assert msg.body.startswith("Yes I'm interested")
    assert msg.sender_id is None  # prospect messages are unauthenticated


@pytest.mark.django_db
def test_public_chat_unknown_token_404(prospect_thread):
    client = APIClient()
    assert client.get(f"/api/public/chat/{uuid.uuid4()}/").status_code == 404


@pytest.mark.django_db
def test_public_chat_cannot_reach_a_tenant_thread(landlord, tenant):
    """The token surface is prospect-only — a registered-tenant thread's id must
    never be readable through the public endpoint."""
    convo = Conversation.objects.create(landlord=landlord, tenant=tenant)
    client = APIClient()
    # access_token exists on every row, but the endpoint filters tenant__isnull.
    assert client.get(f"/api/public/chat/{convo.access_token}/").status_code == 404


# --------------------------------------------------------- notifications
@pytest.mark.django_db
def test_landlord_reply_emails_the_prospect(prospect_thread, landlord, settings):
    """The message.created handler emails a prospect (who has no in-app inbox)
    the landlord's reply with a link back to their tokenized chat."""
    from types import SimpleNamespace

    from rentium.events.handlers import on_message_created_email_prospect
    from rentium.messaging.services import chat_url

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    # The payload send_message() publishes for a landlord→prospect message.
    event = SimpleNamespace(
        payload={
            "to_prospect": True,
            "prospect_email": prospect_thread.prospect_email,
            "prospect_name": prospect_thread.prospect_name,
            "landlord_name": landlord.user.name,
            "preview": "Yes! Want to come see it?",
            "chat_url": chat_url(prospect_thread),
        }
    )
    on_message_created_email_prospect(event)

    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert email.to == ["prospect@example.com"]
    assert str(prospect_thread.access_token) in email.body  # the chat link


@pytest.mark.django_db
def test_message_handler_ignores_non_prospect_events():
    """A tenant/landlord in-app message must NOT trigger a prospect email."""
    from types import SimpleNamespace

    from rentium.events.handlers import on_message_created_email_prospect

    mail.outbox = []
    on_message_created_email_prospect(SimpleNamespace(payload={"to_prospect": False}))
    assert mail.outbox == []


# --------------------------------------------------------- inbox integration
@pytest.mark.django_db
def test_prospect_thread_shows_in_landlord_inbox(prospect_thread, landlord):
    client = APIClient()
    client.force_authenticate(user=landlord.user)
    res = client.get("/api/messaging/conversations/")
    assert res.status_code == 200
    rows = res.json()
    row = next(r for r in rows if r["id"] == str(prospect_thread.id))
    assert row["is_lead"] is True
    assert row["other_party"] == "Pat Prospect"
