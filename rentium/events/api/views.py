from django.utils import timezone
from rest_framework import mixins
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    /api/notifications/            list the current user's notifications
    /api/notifications/unread/     unread only
    /api/notifications/unread_count/
    /api/notifications/{id}/read/  mark one read
    /api/notifications/read_all/   mark all read
    """

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user)

    @action(detail=False, methods=["get"])
    def unread(self, request):
        qs = self.get_queryset().filter(read_at__isnull=True)
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        return Response(
            {"count": self.get_queryset().filter(read_at__isnull=True).count()}
        )

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        n = self.get_object()
        n.mark_read()
        return Response(self.get_serializer(n).data)

    @action(detail=False, methods=["post"])
    def read_all(self, request):
        updated = (
            self.get_queryset()
            .filter(read_at__isnull=True)
            .update(read_at=timezone.now())
        )
        return Response({"marked_read": updated})
