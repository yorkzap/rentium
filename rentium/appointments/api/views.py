from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Appointment
from .serializers import AppointmentSerializer


class AppointmentViewSet(viewsets.ModelViewSet):
    """
    Access model:
      LANDLORD  full CRUD + confirm/decline on their own appointments,
                including REQUESTED leads from the public booking page.
      TENANT    read-only, scoped to their leases/property; REQUESTED
                leads are landlord business and are hidden from tenants.
      PUBLIC    creates REQUESTED viewing leads via public_views.py only.

    Events: appointment.requested / appointment.scheduled /
    appointment.cancelled — map to notifications in the events handler.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Appointment.objects.select_related("property", "lease", "work_order")
        if hasattr(user, "landlord_profile"):
            qs = qs.filter(landlord=user.landlord_profile)
        elif hasattr(user, "tenant_profile"):
            tenant = user.tenant_profile
            from django.db.models import Q

            from rentium.leases.models import Lease

            my_leases = Lease.objects.filter(
                lease_tenants__tenant=tenant,
                status=Lease.LeaseStatus.ACTIVE,
            )
            my_property_ids = list(
                my_leases.exclude(property__isnull=True).values_list(
                    "property_id", flat=True
                )
            )
            qs = (
                qs.filter(Q(lease__in=my_leases) | Q(property_id__in=my_property_ids))
                .exclude(
                    status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.REQUESTED,
                    ]
                )
                .distinct()
            )
        else:
            return qs.none()
        lease_id = self.request.query_params.get("lease")
        if lease_id:
            qs = qs.filter(lease_id=lease_id)
        prop = self.request.query_params.get("property")
        if prop:
            qs = qs.filter(property_id=prop)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        upcoming = self.request.query_params.get("upcoming")
        if upcoming:
            from django.utils import timezone

            qs = qs.filter(starts_at__gte=timezone.now())
        return qs

    def _landlord(self):
        if not hasattr(self.request.user, "landlord_profile"):
            raise PermissionDenied("Only landlords can manage appointments.")
        return self.request.user.landlord_profile

    def perform_create(self, serializer):
        landlord = self._landlord()
        prop = serializer.validated_data.get("property")
        if prop.landlord != landlord:
            raise ValidationError({"property": "Not your property."})
        lease = serializer.validated_data.get("lease")
        if lease and lease.landlord != landlord:
            raise ValidationError({"lease": "Not your lease."})
        appt = serializer.save(landlord=landlord, status=Appointment.Status.SCHEDULED)
        appt.publish_event("appointment.scheduled")

    def perform_update(self, serializer):
        self._landlord()
        serializer.save()

    def perform_destroy(self, instance):
        self._landlord()
        instance.status = Appointment.Status.CANCELLED
        instance.save(update_fields=["status", "updated_at"])
        instance.publish_event("appointment.cancelled")

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Landlord confirms a public viewing REQUEST -> SCHEDULED."""
        self._landlord()
        appt = self.get_object()
        if appt.status != Appointment.Status.REQUESTED:
            raise ValidationError(
                {"detail": "Only requested viewings can be confirmed."}
            )
        new_time = request.data.get("starts_at")
        if new_time:
            from django.utils.dateparse import parse_datetime

            parsed = parse_datetime(new_time)
            if not parsed:
                raise ValidationError({"starts_at": "Invalid datetime."})
            appt.starts_at = parsed
        appt.status = Appointment.Status.SCHEDULED
        appt.save()
        appt.publish_event("appointment.scheduled")
        return Response(self.get_serializer(appt).data)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        """Landlord declines a public viewing REQUEST -> CANCELLED."""
        self._landlord()
        appt = self.get_object()
        if appt.status != Appointment.Status.REQUESTED:
            raise ValidationError(
                {"detail": "Only requested viewings can be declined."}
            )
        appt.status = Appointment.Status.CANCELLED
        if request.data.get("reason"):
            appt.notes = f"{appt.notes}\n[Declined] {request.data['reason']}".strip()
        appt.save()
        appt.publish_event("appointment.cancelled")
        return Response(self.get_serializer(appt).data)
