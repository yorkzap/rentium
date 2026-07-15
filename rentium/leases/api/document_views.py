"""
The lease AS A DOCUMENT.

    GET /api/leases/<id>/document/   -> the rendered document as JSON
    GET /api/leases/<id>/pdf/        -> the same document as a PDF

Both call leases/documents.py:render_lease(). The tenant's sign gate renders
the JSON; the download button fetches the PDF. They cannot disagree, because
they are the same object rendered twice.

Visible to the landlord and to any tenant on the lease, at any status — a draft
is exactly what you want to read *before* you sign it.
"""

from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases.documents import render_lease
from rentium.leases.models import Lease


def _lease_for(request, lease_id) -> Lease:
    lease = (
        Lease.objects.filter(pk=lease_id)
        .select_related("landlord__user", "property", "group")
        .prefetch_related("lease_tenants__tenant__user", "lease_tenants__room")
        .first()
    )
    if not lease:
        raise NotFound("Lease not found.")

    user = request.user
    if hasattr(user, "landlord_profile"):
        if lease.landlord_id == user.landlord_profile.pk:
            return lease
    if hasattr(user, "tenant_profile"):
        on_lease = lease.lease_tenants.filter(tenant=user.tenant_profile).exists()
        if on_lease:
            return lease
    # An invited-but-not-yet-linked tenant is still a party to this agreement
    # and must be able to read it before deciding whether to sign.
    if lease.lease_tenants.filter(
        tenant__isnull=True, invited_email__iexact=user.email
    ).exists():
        return lease

    raise PermissionDenied("This isn't your lease.")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_document(request, lease_id):
    lease = _lease_for(request, lease_id)
    return Response(render_lease(lease).as_dict())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_pdf(request, lease_id):
    lease = _lease_for(request, lease_id)

    # reportlab is now a hard requirement (requirements/base.txt). If it's
    # genuinely missing the import blows up loudly at startup rather than
    # limping along and 501-ing at the one moment a tenant tries to download
    # the thing they're being asked to sign — which is what used to happen.
    from rentium.leases.pdf import build_lease_pdf

    response = HttpResponse(build_lease_pdf(lease), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="lease_{lease.lease_number}.pdf"'
    )
    return response
