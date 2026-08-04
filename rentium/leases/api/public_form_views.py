"""
Signing without an account.

The lease itself cannot be signed this way: `LeaseTenant.sign` requires a logged
in user, and the invite link's job is to *create* that account first. That is the
right trade for the tenancy agreement — it is the document the tenant will need
ongoing access to, so an account is worth the friction.

A form pack is different. The landlord may need a signature from someone the
lease will never know about — a guarantor, an outgoing roommate, a co-owner —
and making them create a Rentium account to put their name on one page is a
reason the form does not get signed at all.

So `sign_token` is a capability: whoever holds the link may sign that one slot on
that one form, once. It follows the pattern `Appointment.public_token` already
established here — no session, no account, and a token that stops working the
moment it has been used or has expired.

Evidence is not weaker for it. Every signature stores the typed legal name, the
timestamp, the IP, the user agent, and the checksums of both the blank form and
the values shown at the time. That is more than the authenticated lease-signing
path records today.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from rentium.leases import form_render
from rentium.leases import form_services as svc
from rentium.leases.lease_forms import LeaseFormEvent
from rentium.leases.lease_forms import LeaseFormSignature
from rentium.leases.lease_forms import LeaseFormSigner

LINK_OPEN_DEBOUNCE_SECONDS = 120


def _signer(token) -> LeaseFormSigner:
    signer = (
        LeaseFormSigner.objects.filter(sign_token=token)
        .select_related("form", "form__template", "form__lease", "form__lease__landlord__user")
        .first()
    )
    if signer is None:
        # Deliberately the same message a live-but-finished link gets. A
        # different 404 would let someone probe which tokens exist.
        raise NotFound("This signing link is no longer valid.")
    return signer


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _payload(signer: LeaseFormSigner) -> dict:
    form = signer.form
    lease = form.lease
    mine = form.placements_for(signer)
    return {
        "form_id": str(form.pk),
        "title": form.title,
        "purpose": form.template.purpose or form.template.suggested_purpose,
        "stage": str(form.template.stage),
        "status": form.status,
        "page_count": form.template.page_count,
        "page_sizes": form.template.page_sizes,
        "landlord_name": lease.landlord.user.name,
        "property_label": (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else lease.lease_number)
        ),
        "signer": {
            "name": signer.display_name,
            "role": signer.role,
            "email": signer.email,
            "has_signed": signer.has_signed,
            "declined": signer.declined_at is not None,
        },
        # Only this signer's boxes. The others' positions are not secret, but
        # showing them invites someone to sign in a slot that is not theirs.
        "my_fields": mine,
        "values": form.values,
        "blocks_activation": form.blocks_activation,
        "expires_at": signer.token_expires_at,
        "already_complete": form.is_executed,
    }


@api_view(["GET"])
@permission_classes([AllowAny])
def public_form_detail(request, token):
    """What the signing page loads. Records that the link was opened."""
    signer = _signer(token)
    if not signer.opened_at:
        signer.opened_at = timezone.now()
        signer.save(update_fields=["opened_at", "updated_at"])
    svc.record_form_event(
        signer.form,
        LeaseFormEvent.Kind.LINK_OPENED,
        signer=signer,
        metadata={"user_agent": str(request.headers.get("User-Agent") or "")[:300]},
        # The page re-fetches on mount; "opened 40 times" is noise, not evidence.
        debounce_seconds=LINK_OPEN_DEBOUNCE_SECONDS,
    )
    return Response(_payload(signer))


@api_view(["GET"])
@permission_classes([AllowAny])
def public_form_page(request, token, page):
    """One page of the document, rendered with everything filled in so far."""
    signer = _signer(token)
    try:
        data = svc.render_form_pdf(signer.form)
        dpi = int(request.query_params.get("dpi") or form_render.DEFAULT_RASTER_DPI)
        png = form_render.render_page_png(data, int(page), dpi=dpi)
    except DjangoValidationError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    response = HttpResponse(png, content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def public_form_pdf(request, token):
    """The whole document, so a signer can read it properly before signing."""
    signer = _signer(token)
    try:
        data = svc.render_form_pdf(signer.form)
    except DjangoValidationError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{signer.form.title[:60]}.pdf"'
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def public_form_sign(request, token):
    signer = _signer(token)
    if not signer.token_is_live:
        raise PermissionDenied(
            "This signing link has already been used or has expired."
        )
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
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
        raise ValidationError(detail) from exc

    signer.refresh_from_db()
    signer.form.refresh_from_db()
    return Response(_payload(signer))


@api_view(["POST"])
@permission_classes([AllowAny])
def public_form_decline(request, token):
    signer = _signer(token)
    if not signer.token_is_live:
        raise PermissionDenied("This signing link is no longer active.")
    try:
        svc.decline_form(signer, reason=request.data.get("reason", ""))
    except DjangoValidationError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    signer.refresh_from_db()
    return Response(_payload(signer))
