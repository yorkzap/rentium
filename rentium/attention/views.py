from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .service import compute_attention


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def attention_view(request):
    """
    GET /api/attention/

    The Action Center: everything that currently needs this landlord's
    attention, computed on read. Ordered urgent -> soon -> info, then by
    due date. Thin view — all logic lives in service.compute_attention.
    """
    if not hasattr(request.user, "landlord_profile"):
        raise PermissionDenied("Landlords only.")
    items = compute_attention(request.user.landlord_profile)
    return Response({"items": [i.as_dict() for i in items]})
