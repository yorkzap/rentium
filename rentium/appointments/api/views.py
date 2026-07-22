from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Appointment, AppointmentProposal, AvailabilityWindow
from .serializers import AppointmentSerializer, AvailabilityWindowSerializer


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
            from rentium.users.access import scope_q

            qs = qs.filter(
                scope_q(user, property_field="property", lease_field="lease")
            ).distinct()
        elif hasattr(user, "tenant_profile"):
            tenant = user.tenant_profile
            from django.db.models import Q

            from rentium.leases.models import Lease

            my_leases = Lease.objects.filter(
                lease_tenants__tenant=tenant,
                status=Lease.LeaseStatus.ACTIVE,
            )
            # A tenant's view is exactly their ENTRY NOTICE — nothing more:
            #   1. appointments explicitly attached to one of their leases
            #      (incl. pre-move-in inspections dated before the lease
            #      starts — those are about them);
            #   2. property-wide appointments (lease unset, e.g. confirmed
            #      public viewing requests) only from their lease's start
            #      date onward. Anything the landlord scheduled on the same
            #      property BEFORE this tenancy began — showings to other
            #      prospects while the unit was listed — is landlord
            #      business and must never leak to the incoming tenant.
            visible = Q(lease__in=my_leases)
            for lease in my_leases.exclude(property__isnull=True).only(
                "property_id", "start_date"
            ):
                visible |= Q(
                    lease__isnull=True,
                    property_id=lease.property_id,
                    starts_at__date__gte=lease.start_date,
                )
            # Hide the landlord's prospect pipeline (pending showings), EXCEPT a
            # showing at this tenant's own occupied unit that's asking for their
            # consent — that one stays visible while pending so they can respond.
            pending = [
                Appointment.Status.REQUESTED,
                Appointment.Status.AWAITING_REQUESTER,
            ]
            qs = (
                qs.filter(visible)
                .exclude(status=Appointment.Status.CANCELLED)
                # Hide only the landlord's VIEWING prospect pipeline while pending.
                # An INSPECTION walkthrough is about THIS tenant, so a pending one
                # on their lease stays visible so they can accept or counter it.
                .exclude(
                    Q(kind=Appointment.Kind.VIEWING)
                    & Q(status__in=pending)
                    & Q(tenant_consent=Appointment.TenantConsent.NOT_APPLICABLE)
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

    _PENDING = (Appointment.Status.REQUESTED, Appointment.Status.AWAITING_REQUESTER)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Landlord confirms a viewing -> SCHEDULED. Optionally at a new time
        (an override — the landlord may have agreed it by phone). Allowed from
        either pending state, and regardless of tenant_consent (the landlord can
        proceed over a tenant objection; it's recorded either way)."""
        from rentium.core.fsm import IllegalTransition

        self._landlord()
        appt = self.get_object()
        if appt.status not in self._PENDING:
            raise ValidationError({"detail": "Only pending viewings can be confirmed."})

        new_time = request.data.get("starts_at")
        if new_time:
            from django.utils.dateparse import parse_datetime

            parsed = parse_datetime(new_time)
            if not parsed:
                raise ValidationError({"starts_at": "Invalid datetime."})
            appt.starts_at = parsed
            appt.stamp_time_class()
            appt.record_proposal(
                by=AppointmentProposal.By.LANDLORD, starts_at=parsed
            )
        try:
            appt.transition_to(Appointment.Status.SCHEDULED)
        except IllegalTransition as exc:
            raise ValidationError({"detail": str(exc)})
        if new_time:
            appt.save(update_fields=["starts_at", "time_class"])
        appt.publish_event("appointment.scheduled")
        return Response(self.get_serializer(appt).data)

    @action(detail=True, methods=["post"])
    def counter(self, request, pk=None):
        """Landlord proposes a DIFFERENT time -> AWAITING_REQUESTER. The
        requester then accepts, counters again, or withdraws. Loops until it's
        scheduled or cancelled."""
        from django.utils.dateparse import parse_datetime

        from rentium.core.fsm import IllegalTransition

        self._landlord()
        appt = self.get_object()
        if appt.status not in self._PENDING:
            raise ValidationError({"detail": "Only pending viewings can be countered."})

        parsed = parse_datetime(str(request.data.get("starts_at") or ""))
        if not parsed:
            raise ValidationError({"starts_at": "Pick a date and time."})
        from django.utils import timezone as djtz

        if djtz.is_naive(parsed):
            parsed = djtz.make_aware(parsed)
        if parsed <= djtz.now():
            raise ValidationError({"starts_at": "Pick a time in the future."})

        appt.starts_at = parsed
        appt.stamp_time_class()
        try:
            appt.transition_to(Appointment.Status.AWAITING_REQUESTER)
        except IllegalTransition as exc:
            raise ValidationError({"detail": str(exc)})
        appt.save(update_fields=["starts_at", "time_class"])
        appt.record_proposal(
            by=AppointmentProposal.By.LANDLORD,
            starts_at=parsed,
            message=(request.data.get("message") or "").strip(),
        )
        appt.publish_event("appointment.countered", proposed_by="LANDLORD")
        return Response(self.get_serializer(appt).data)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        """Landlord declines a pending viewing -> CANCELLED."""
        from rentium.core.fsm import IllegalTransition

        self._landlord()
        appt = self.get_object()
        if appt.status not in self._PENDING:
            raise ValidationError({"detail": "Only pending viewings can be declined."})
        if request.data.get("reason"):
            appt.notes = f"{appt.notes}\n[Declined] {request.data['reason']}".strip()
            appt.save(update_fields=["notes"])
        try:
            appt.transition_to(Appointment.Status.CANCELLED)
        except IllegalTransition as exc:
            raise ValidationError({"detail": str(exc)})
        appt.publish_event("appointment.cancelled", cancelled_by="LANDLORD")
        return Response(self.get_serializer(appt).data)

    @action(detail=True, methods=["post"])
    def tenant_respond(self, request, pk=None):
        """Current tenant records consent for a showing at their unit:
        {consent: "OK" | "OBJECTED", notes?} or proposes an alternate time
        ({consent: "OBJECTED", starts_at?}). Advisory only — it never changes
        the appointment's status or cancels it; the landlord decides."""
        user = request.user
        tenant = getattr(user, "tenant_profile", None)
        if tenant is None:
            raise PermissionDenied("Only the current tenant can respond here.")
        appt = self.get_object()  # get_queryset scopes this to their lease
        if appt.tenant_consent == Appointment.TenantConsent.NOT_APPLICABLE:
            raise ValidationError({"detail": "This visit doesn't need your consent."})

        consent = str(request.data.get("consent") or "").strip().upper()
        if consent not in (
            Appointment.TenantConsent.OK,
            Appointment.TenantConsent.OBJECTED,
        ):
            raise ValidationError({"consent": "Use OK or OBJECTED."})
        appt.tenant_consent = consent
        notes = (request.data.get("notes") or "").strip()
        if notes:
            appt.tenant_consent_notes = notes[:1000]
        appt.save(update_fields=["tenant_consent", "tenant_consent_notes"])

        # A suggested alternate is recorded as a proposal for the landlord to
        # see, but does not itself move the negotiation — the landlord acts on it.
        alt = request.data.get("starts_at")
        if alt:
            from django.utils.dateparse import parse_datetime

            parsed = parse_datetime(str(alt))
            if parsed:
                appt.record_proposal(
                    by=AppointmentProposal.By.TENANT, starts_at=parsed, message=notes
                )
        appt.publish_event("appointment.tenant_responded", consent=consent)
        return Response(self.get_serializer(appt).data)

    @action(detail=False, methods=["post"])
    def propose_inspection(self, request):
        """Landlord proposes a move-in/move-out inspection walkthrough time,
        opening a negotiation with the tenant: {inspection, starts_at}."""
        from django.utils.dateparse import parse_datetime

        from rentium.leases.inspections import ConditionInspection

        from ..services import propose_inspection_time

        landlord = self._landlord()
        inspection = ConditionInspection.objects.filter(
            pk=request.data.get("inspection"), lease__landlord=landlord
        ).first()
        if inspection is None:
            raise ValidationError({"inspection": "Not found."})
        parsed = parse_datetime(str(request.data.get("starts_at") or ""))
        if not parsed:
            raise ValidationError({"starts_at": "Pick a date and time."})
        from django.utils import timezone as djtz

        if djtz.is_naive(parsed):
            parsed = djtz.make_aware(parsed)
        appt = propose_inspection_time(landlord, inspection, parsed)
        return Response(self.get_serializer(appt).data, status=201)

    @action(detail=True, methods=["post"])
    def schedule_respond(self, request, pk=None):
        """The tenant's half of an INSPECTION negotiation (they have an account,
        so no public token): {action: accept | counter, starts_at?}. accept →
        SCHEDULED; counter → back to the landlord with a new time."""
        from rentium.core.fsm import IllegalTransition

        tenant = getattr(request.user, "tenant_profile", None)
        if tenant is None:
            raise PermissionDenied("Only the tenant can respond here.")
        appt = self.get_object()  # scoped to their lease by get_queryset
        if appt.kind != Appointment.Kind.INSPECTION:
            raise ValidationError({"detail": "Only inspection times are negotiated here."})
        if appt.status != Appointment.Status.AWAITING_REQUESTER:
            raise ValidationError({"detail": "Nothing is awaiting your reply on this."})

        action_name = str(request.data.get("action") or "").strip().lower()
        try:
            if action_name == "accept":
                appt.transition_to(Appointment.Status.SCHEDULED)
                appt.publish_event("appointment.scheduled")
            elif action_name == "counter":
                from django.utils.dateparse import parse_datetime

                parsed = parse_datetime(str(request.data.get("starts_at") or ""))
                if not parsed:
                    raise ValidationError({"starts_at": "Pick a date and time."})
                from django.utils import timezone as djtz

                if djtz.is_naive(parsed):
                    parsed = djtz.make_aware(parsed)
                appt.starts_at = parsed
                appt.stamp_time_class()
                appt.transition_to(Appointment.Status.REQUESTED)
                appt.save(update_fields=["starts_at", "time_class"])
                appt.record_proposal(
                    by=AppointmentProposal.By.TENANT, starts_at=parsed
                )
                appt.publish_event("appointment.countered", proposed_by="REQUESTER")
            else:
                raise ValidationError({"action": "Use accept or counter."})
        except IllegalTransition as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(appt).data)


class AvailabilityWindowViewSet(viewsets.ModelViewSet):
    """A landlord's preferred viewing hours. Landlord-scoped CRUD; property=NULL
    rows are their default, property-set rows override it for one property."""

    permission_classes = [IsAuthenticated]
    serializer_class = AvailabilityWindowSerializer

    def _landlord(self):
        profile = getattr(self.request.user, "landlord_profile", None)
        if profile is None:
            raise PermissionDenied("Only landlords set viewing hours.")
        return profile

    def get_queryset(self):
        landlord = getattr(self.request.user, "landlord_profile", None)
        if landlord is None:
            return AvailabilityWindow.objects.none()
        qs = AvailabilityWindow.objects.filter(landlord=landlord)
        prop = self.request.query_params.get("property")
        if prop:
            qs = qs.filter(property_id=prop)
        return qs

    def perform_create(self, serializer):
        landlord = self._landlord()
        prop = serializer.validated_data.get("property")
        if prop and prop.landlord_id != landlord.pk:
            raise ValidationError({"property": "Not your property."})
        serializer.save(landlord=landlord)
