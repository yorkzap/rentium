"""
Authenticated API for lease form packs.

Two things about this module differ from the rest of leases/api and both are
deliberate:

**`LeaseNotLocked` is not applied.** An RTB-8 is attached to a lease that is
ACTIVE, by definition — it is the document that ends a live tenancy. The lock
exists to stop a signed lease's TERMS being edited; attaching paperwork to a
running tenancy is normal, and gating it behind the lock would make the whole
move-out flow impossible.

**No file field is ever serialised.** Production runs with
`AWS_QUERYSTRING_AUTH = False`, so a `FileField.url` is a permanent public link.
Blank forms, page images and executed documents all come through the download
views below, which check who is asking — the same approach
`rama/views.py:document_download_view` already takes.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases import form_render
from rentium.leases import form_services as svc
from rentium.leases.lease_forms import LeaseForm
from rentium.leases.lease_forms import LeaseFormSignature
from rentium.leases.lease_forms import LeaseFormTemplate
from rentium.leases.models import Lease

from .form_serializers import EventSerializer
from .form_serializers import LeaseFormSerializer
from .form_serializers import PlacementSerializer
from .form_serializers import PlacementWriteSerializer
from .form_serializers import TemplateSerializer

PAGE_CACHE_SECONDS = 60 * 60 * 24 * 7


# ---------------------------------------------------------------------------
# Scoping helpers
# ---------------------------------------------------------------------------


def _landlord(request):
    profile = getattr(request.user, "landlord_profile", None)
    if profile is None:
        raise PermissionDenied("Only a landlord can manage lease forms.")
    return profile


def _lease_for_landlord(request, lease_id) -> Lease:
    from rentium.users.access import accessible_leases

    lease = accessible_leases(request.user).filter(pk=lease_id).first()
    if lease is None:
        raise NotFound("Lease not found.")
    return lease


def _readable_lease(request, lease_id) -> Lease:
    """A lease the caller may READ forms on — landlord side or tenant side."""
    from rentium.users.access import accessible_leases

    lease = Lease.objects.filter(pk=lease_id).first()
    if lease is None:
        raise NotFound("Lease not found.")
    if accessible_leases(request.user).filter(pk=lease.pk).exists():
        return lease
    user = request.user
    if hasattr(user, "tenant_profile") and lease.lease_tenants.filter(
        tenant=user.tenant_profile
    ).exists():
        return lease
    if lease.lease_tenants.filter(
        tenant__isnull=True, invited_email__iexact=user.email
    ).exists():
        return lease
    raise PermissionDenied("This isn't your lease.")


def _template_for(request, pk) -> LeaseFormTemplate:
    """System forms are readable by everyone; uploads only by their owner."""
    from django.db.models import Q

    landlord = _landlord(request)
    template = LeaseFormTemplate.objects.filter(
        Q(landlord__isnull=True) | Q(landlord=landlord), pk=pk
    ).first()
    if template is None:
        raise NotFound("Form not found.")
    return template


def _form_for(request, pk, *, write: bool = False) -> LeaseForm:
    form = LeaseForm.objects.filter(pk=pk).select_related("lease", "template").first()
    if form is None:
        raise NotFound("Form not found.")
    if write:
        _lease_for_landlord(request, form.lease_id)
    else:
        _readable_lease(request, form.lease_id)
    return form


def _translate(exc: DjangoValidationError):
    """Domain errors are already written for a human — pass them straight out."""
    if hasattr(exc, "message_dict"):
        return ValidationError(exc.message_dict)
    return ValidationError({"detail": exc.messages if hasattr(exc, "messages") else str(exc)})


# ---------------------------------------------------------------------------
# Catalogue + templates
# ---------------------------------------------------------------------------


class LeaseFormTemplateViewSet(viewsets.ViewSet):
    """The form catalogue: system forms plus this landlord's own uploads."""

    permission_classes = [IsAuthenticated]

    def list(self, request):
        landlord = _landlord(request)
        rows = svc.catalog_for(
            landlord, jurisdiction=request.query_params.get("jurisdiction", "")
        )
        return Response(TemplateSerializer(rows, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(TemplateSerializer(_template_for(request, pk)).data)

    def create(self, request):
        """Upload a custom blank form (multipart: `file`, `name`, `stage`)."""
        landlord = _landlord(request)
        try:
            template, created = svc.upload_template(
                landlord,
                request.FILES.get("file"),
                name=request.data.get("name", ""),
                purpose=request.data.get("purpose", ""),
                stage=request.data.get("stage", ""),
                created_by=request.user,
            )
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        return Response(
            TemplateSerializer(template).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def partial_update(self, request, pk=None):
        """Confirm a suggested stage, rename, or describe a form.

        Only a landlord's own upload is editable — a system form's stage is what
        makes RTB-8 behave like an end-of-tenancy document everywhere, and one
        account must not be able to change that for every other account.
        """
        template = _template_for(request, pk)
        if template.landlord_id is None:
            raise PermissionDenied("System forms can't be edited.")

        for field in ("name", "purpose", "stage"):
            if field in request.data:
                setattr(template, field, request.data[field])
        try:
            template.full_clean(exclude=["file"])
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        template.save()
        return Response(TemplateSerializer(template).data)

    def destroy(self, request, pk=None):
        template = _template_for(request, pk)
        if template.landlord_id is None:
            raise PermissionDenied("System forms can't be deleted.")
        if template.instances.exists():
            # Deactivate rather than delete: an attached form's placements are
            # snapshotted, but the blank it came from is still the provenance
            # of a signed document.
            template.is_active = False
            template.save(update_fields=["is_active", "updated_at"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "put"])
    def placements(self, request, pk=None):
        template = _template_for(request, pk)
        if request.method == "GET":
            return Response(
                PlacementSerializer(template.placements.all(), many=True).data
            )
        if template.landlord_id is None:
            raise PermissionDenied("System form fields can't be moved.")

        serializer = PlacementWriteSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        try:
            count = svc.set_placements(template, serializer.validated_data)
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        return Response({"placements": count})

    @action(detail=True, methods=["get"], url_path=r"page/(?P<page>\d+)")
    def page(self, request, pk=None, page="0"):
        """A blank page as a PNG, for the field-placement canvas."""
        template = _template_for(request, pk)
        if not template.file:
            raise NotFound("This form has no file yet.")
        template.file.open("rb")
        try:
            data = template.file.read()
        finally:
            template.file.close()
        return _png_response(data, int(page), request, etag=template.sha256)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        template = _template_for(request, pk)
        if not template.file:
            raise NotFound("This form has no file yet.")
        template.file.open("rb")
        try:
            data = template.file.read()
        finally:
            template.file.close()
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'inline; filename="{template.original_filename or "form.pdf"}"'
        )
        return response

    @action(detail=False, methods=["get"])
    def prefill_sources(self, request):
        """The whitelist the placement editor offers in its 'prefill from' menu."""
        return Response(sorted(svc.AUTO_SOURCES))


def _png_response(pdf_bytes: bytes, page: int, request, *, etag: str = ""):
    try:
        dpi = int(request.query_params.get("dpi") or form_render.DEFAULT_RASTER_DPI)
    except (TypeError, ValueError):
        dpi = form_render.DEFAULT_RASTER_DPI
    try:
        png = form_render.render_page_png(pdf_bytes, page, dpi=dpi)
    except DjangoValidationError as exc:
        raise _translate(exc) from exc
    response = HttpResponse(png, content_type="image/png")
    # Page images are a pure function of (bytes, page, dpi), so they can be
    # cached hard — but privately: this is a tenancy document, not a public asset.
    response["Cache-Control"] = f"private, max-age={PAGE_CACHE_SECONDS}"
    if etag:
        response["ETag"] = f'"{etag}-{page}-{dpi}"'
    return response


# ---------------------------------------------------------------------------
# Forms attached to a lease
# ---------------------------------------------------------------------------


class LeaseFormViewSet(viewsets.ViewSet):
    """Forms attached to leases. Every mutation routes through form_services."""

    permission_classes = [IsAuthenticated]

    def list(self, request):
        lease_id = request.query_params.get("lease")
        if not lease_id:
            raise ValidationError({"lease": "A lease id is required."})
        lease = _readable_lease(request, lease_id)
        rows = (
            lease.lease_forms.select_related("template")
            .prefetch_related("signers")
            .order_by("-created_at")
        )
        return Response(LeaseFormSerializer(rows, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(LeaseFormSerializer(_form_for(request, pk)).data)

    def create(self, request):
        lease = _lease_for_landlord(request, request.data.get("lease"))
        template = _template_for(request, request.data.get("template"))
        try:
            form = svc.attach_form(
                lease,
                template,
                actor=request.user,
                title=request.data.get("title", ""),
                required=request.data.get("required", True) in (True, "true", "True", 1),
            )
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        return Response(
            LeaseFormSerializer(form).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Bind signers to slots, mint links, email them.

        Returns each signer's link so the landlord can hand it over in person or
        paste it into a chat — the same affordance the tenant invite already has.
        """
        form = _form_for(request, pk, write=True)
        try:
            svc.send_form(
                form,
                actor=request.user,
                manual_signers=request.data.get("signers") or {},
                notify=request.data.get("notify", True) is not False,
            )
        except DjangoValidationError as exc:
            raise _translate(exc) from exc

        base = svc.frontend_base_url()
        return Response(
            {
                "form": LeaseFormSerializer(form).data,
                "links": {
                    f"{signer.role}:{signer.order}": signer.sign_url(base)
                    for signer in form.signers.all()
                    if signer.token_is_live
                },
            }
        )

    @action(detail=True, methods=["post"])
    def remind(self, request, pk=None):
        form = _form_for(request, pk, write=True)
        return Response({"reminded": svc.remind_outstanding(form, actor=request.user)})

    @action(detail=True, methods=["post"])
    def sign(self, request, pk=None):
        """In-app signing, for a party who is logged in.

        The public token route (public_form_views) is the same operation for
        somebody without an account. Both call sign_form, so the evidence row
        looks identical whichever door the signer came through.
        """
        form = _form_for(request, pk)
        signer = _signer_for_user(form, request.user)
        try:
            svc.sign_form(
                signer,
                typed_name=request.data.get("typed_name", ""),
                method=request.data.get("method", LeaseFormSignature.Method.TYPED),
                signature_png=svc.decode_signature_png(
                    request.data.get("signature_png", "")
                ),
                ip_address=_client_ip(request),
                user_agent=request.headers.get("User-Agent", ""),
                actor=request.user,
            )
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        form.refresh_from_db()
        return Response(LeaseFormSerializer(form).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        form = _form_for(request, pk, write=True)
        try:
            svc.void_form(
                form, reason=request.data.get("reason", ""), actor=request.user
            )
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        return Response(LeaseFormSerializer(form).data)

    @action(detail=True, methods=["patch"], url_path="values")
    def set_values(self, request, pk=None):
        """Type into the non-signature boxes before sending.

        Refused once anybody has signed: the values are part of what they signed.
        """
        form = _form_for(request, pk, write=True)
        if form.signatures.exists():
            raise ValidationError(
                {
                    "detail": (
                        "Someone has already signed this form, so its contents "
                        "can't change. Void it and issue a new one instead."
                    )
                }
            )
        updates = request.data.get("values") or {}
        if not isinstance(updates, dict):
            raise ValidationError({"values": "Expected an object of key -> value."})
        known = {row["key"] for row in form.placements_snapshot}
        unknown = set(updates) - known
        if unknown:
            raise ValidationError(
                {"values": f"No such field(s) on this form: {', '.join(sorted(unknown))}"}
            )
        form.values.update({key: str(value) for key, value in updates.items()})
        form.save(update_fields=["values", "updated_at"])
        return Response(LeaseFormSerializer(form).data)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        form = _form_for(request, pk)
        return Response(
            EventSerializer(form.events.select_related("actor", "signer"), many=True).data
        )

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        form = _form_for(request, pk)
        try:
            data = svc.render_form_pdf(form)
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        response = HttpResponse(data, content_type="application/pdf")
        disposition = "attachment" if form.is_executed else "inline"
        response["Content-Disposition"] = (
            f'{disposition}; filename="{form.title[:60]}.pdf"'
        )
        return response

    @action(detail=True, methods=["get"], url_path=r"page/(?P<page>\d+)")
    def page(self, request, pk=None, page="0"):
        """The form as it stands now, values included, as a PNG."""
        form = _form_for(request, pk)
        try:
            data = svc.render_form_pdf(form)
        except DjangoValidationError as exc:
            raise _translate(exc) from exc
        return _png_response(data, int(page), request)


def _signer_for_user(form: LeaseForm, user):
    """Which signature slot belongs to the logged-in caller.

    Matched by linked account first and email second, so a tenant who signs up
    after the link was sent still lands on their own slot rather than being told
    the form is not theirs.
    """
    signers = form.signers.all()
    for signer in signers:
        if signer.user_id and signer.user_id == user.pk:
            return signer
    tenant_profile = getattr(user, "tenant_profile", None)
    if tenant_profile:
        for signer in signers:
            if (
                signer.lease_tenant_id
                and signer.lease_tenant.tenant_id == tenant_profile.pk
            ):
                return signer
    landlord_profile = getattr(user, "landlord_profile", None)
    if landlord_profile and form.lease.landlord_id == landlord_profile.pk:
        landlord_slot = form.signers.filter(role="LANDLORD").first()
        if landlord_slot:
            return landlord_slot
    for signer in signers:
        if signer.email and signer.email.casefold() == (user.email or "").casefold():
            return signer
    raise PermissionDenied("This form isn't waiting on your signature.")


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


# ---------------------------------------------------------------------------
# Why isn't this lease active yet?
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_activation_status(request, lease_id):
    """Everything still standing between this lease and ACTIVE.

    Exists because the failure it describes is otherwise invisible: a landlord
    signs, a tenant signs, and the lease just... stays PENDING, with nothing on
    screen saying an addendum is what it is waiting for.
    """
    lease = _readable_lease(request, lease_id)
    blockers = svc.activation_blockers(lease)
    return Response(
        {
            "status": lease.status,
            "can_activate": not blockers
            and lease.status == Lease.LeaseStatus.PENDING_SIGNATURES,
            "blockers": [str(reason) for reason in blockers],
            "blocking_forms": LeaseFormSerializer(
                svc.blocking_forms(lease).select_related("template"), many=True
            ).data,
        }
    )
