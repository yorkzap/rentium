"""
The prospect's tokenized chat surface. An accountless lead who inquired about a
listing continues the conversation here, reached ONLY by the access_token that
was emailed to their own address — the same bearer-credential pattern as the
public viewing-status page and lease invites.

Security, deliberately narrow:
  - The token grants access to EXACTLY ONE conversation. There is no listing of
    threads, no id enumeration — you either hold this conversation's token or
    you see nothing.
  - The payload is PII-MINIMIZED: the listing NAME, the landlord's display name,
    the subject, and the messages. Never the street address, never other
    tenants, never financials, never any other listing. So a forwarded link
    can't expose a portfolio the way an open URL would.
  - AllowAny is declared explicitly (the project default is IsAuthenticated),
    and both reads and sends are throttled.
"""

from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.decorators import throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from ..models import Conversation
from ..services import post_prospect_message

MAX_BODY = 4000


class ChatReadThrottle(ScopedRateThrottle):
    scope = "public_chat_read"


class ChatSendThrottle(ScopedRateThrottle):
    scope = "public_chat_send"


def _get_prospect_conversation(token) -> Conversation:
    convo = (
        Conversation.objects.select_related("landlord__user", "property")
        .filter(access_token=token, tenant__isnull=True)
        .first()
    )
    if convo is None:
        # Same 404 whether the token is malformed, unknown, or points at a
        # non-prospect thread — nothing to enumerate.
        raise NotFound("This conversation link is invalid or has expired.")
    return convo


def _serialize(convo: Conversation) -> dict:
    """PII-minimized. Explicit allowlist — never `.values()` the model."""
    return {
        "subject": convo.subject or "",
        "listing": convo.property.name if convo.property_id else "",
        "landlord_name": convo.landlord.user.name or "the landlord",
        "prospect_name": convo.prospect_name,
        "messages": [
            {
                "body": m.body,
                # From the prospect's side, their own messages have no sender.
                "from_landlord": m.sender_id is not None,
                "created_at": m.created_at,
            }
            for m in convo.messages.all().order_by("created_at")
        ],
    }


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ChatReadThrottle])
def public_thread(request, token):
    """GET /api/public/chat/<token>/ — the prospect reads their one thread."""
    convo = _get_prospect_conversation(token)
    return Response(_serialize(convo))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ChatSendThrottle])
def public_thread_send(request, token):
    """POST /api/public/chat/<token>/send/ {body} — the prospect replies."""
    convo = _get_prospect_conversation(token)
    body = (request.data.get("body") or "").strip()
    if not body:
        raise ValidationError({"body": "Message text is required."})
    if len(body) > MAX_BODY:
        raise ValidationError({"body": f"Message too long (max {MAX_BODY})."})
    post_prospect_message(convo, body)
    return Response(_serialize(convo))
