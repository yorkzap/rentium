from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases.models import Lease

from ..models import Conversation
from ..models import Message
from ..services import send_message
from .serializers import ConversationSerializer
from .serializers import MessageSerializer


class ConversationViewSet(viewsets.ModelViewSet):
    """
    GET  /api/messaging/conversations/            my threads
    POST /api/messaging/conversations/            {tenant?|landlord?, lease?, subject?} -> get-or-create
    GET  /api/messaging/conversations/{id}/messages/
    POST /api/messaging/conversations/{id}/send/  {body}
    POST /api/messaging/conversations/{id}/mark_read/
    """

    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        u = self.request.user
        qs = Conversation.objects.select_related(
            "landlord__user", "tenant__user"
        ).prefetch_related("messages")
        if hasattr(u, "landlord_profile"):
            from rentium.users.access import scope_q

            return qs.filter(
                scope_q(u, property_field="property", lease_field="lease")
            ).distinct()
        if hasattr(u, "tenant_profile"):
            return qs.filter(tenant=u.tenant_profile)
        return Conversation.objects.none()

    def create(self, request, *args, **kwargs):
        u = request.user
        lease_id = request.data.get("lease")
        lease = Lease.objects.filter(pk=lease_id).first() if lease_id else None

        if hasattr(u, "landlord_profile"):
            landlord = u.landlord_profile
            tenant_id = request.data.get("tenant")
            from rentium.users.models import TenantProfile

            tenant = TenantProfile.objects.filter(pk=tenant_id).first()
            if not tenant:
                raise ValidationError({"tenant": "Unknown tenant."})
        elif hasattr(u, "tenant_profile"):
            tenant = u.tenant_profile
            # tenant opens a thread with a landlord (usually via a lease)
            if lease:
                landlord = lease.landlord
            else:
                from rentium.users.models import LandlordProfile

                landlord = LandlordProfile.objects.filter(
                    pk=request.data.get("landlord")
                ).first()
            if not landlord:
                raise ValidationError({"landlord": "Unknown landlord."})
        else:
            raise PermissionDenied("Unknown user type.")

        convo, _ = Conversation.objects.get_or_create(
            landlord=landlord,
            tenant=tenant,
            lease=lease,
            defaults={"subject": request.data.get("subject", "")},
        )
        return Response(self.get_serializer(convo).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        convo = self.get_object()
        # opening a thread marks the other party's messages read
        convo.messages.filter(read_at__isnull=True).exclude(sender=request.user).update(
            read_at=timezone.now()
        )
        return Response(
            MessageSerializer(
                convo.messages.all(), many=True, context={"request": request}
            ).data
        )

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        convo = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            raise ValidationError({"body": "Message text is required."})
        msg = send_message(convo, request.user, body)
        return Response(
            MessageSerializer(msg, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        convo = self.get_object()
        n = (
            convo.messages.filter(read_at__isnull=True)
            .exclude(sender=request.user)
            .update(read_at=timezone.now())
        )
        return Response({"marked_read": n})
