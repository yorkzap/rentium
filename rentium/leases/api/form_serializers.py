"""Read shapes for lease form packs.

Deliberately hand-rolled rather than ModelSerializers for the write paths:
every mutation goes through `form_services`, so a serializer that could `.save()`
a LeaseForm would be a second way to create one with none of the rules — no
placement snapshot, no event, no activation recalculation.

One thing is conspicuously absent from every payload here: file URLs. Production
runs `AWS_QUERYSTRING_AUTH = False`, so anything a FileField serialises is a
permanently public, guessable link. A signed tenancy document must not be one,
so bytes are only ever served through the download views.
"""

from __future__ import annotations

from rest_framework import serializers

from rentium.leases.lease_forms import LeaseForm
from rentium.leases.lease_forms import LeaseFormEvent
from rentium.leases.lease_forms import LeaseFormPlacement
from rentium.leases.lease_forms import LeaseFormSigner
from rentium.leases.lease_forms import LeaseFormTemplate


class PlacementSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaseFormPlacement
        fields = [
            "id",
            "key",
            "label",
            "page",
            "x",
            "y",
            "width",
            "height",
            "kind",
            "signer_role",
            "signer_index",
            "auto_source",
            "required",
            "font_size",
            "order",
        ]


class PlacementWriteSerializer(serializers.Serializer):
    """One box as the placement editor sends it back."""

    key = serializers.CharField(max_length=120)
    label = serializers.CharField(max_length=200, required=False, allow_blank=True)
    page = serializers.IntegerField(min_value=0)
    x = serializers.FloatField(min_value=0.0, max_value=1.0)
    y = serializers.FloatField(min_value=0.0, max_value=1.0)
    width = serializers.FloatField(min_value=0.0, max_value=1.0)
    height = serializers.FloatField(min_value=0.0, max_value=1.0)
    kind = serializers.ChoiceField(choices=LeaseFormPlacement.Kind.choices)
    signer_role = serializers.CharField(max_length=15)
    signer_index = serializers.IntegerField(min_value=0, default=0)
    auto_source = serializers.CharField(
        max_length=60, required=False, allow_blank=True, default=""
    )
    required = serializers.BooleanField(default=True)
    font_size = serializers.FloatField(default=10.0, min_value=4.0, max_value=48.0)
    order = serializers.IntegerField(default=0)


class TemplateSerializer(serializers.ModelSerializer):
    available = serializers.BooleanField(source="is_selectable", read_only=True)
    is_system = serializers.SerializerMethodField()
    placement_count = serializers.SerializerMethodField()
    suggestion = serializers.SerializerMethodField()

    class Meta:
        model = LeaseFormTemplate
        fields = [
            "id",
            "code",
            "name",
            "purpose",
            "jurisdiction",
            "source",
            "stage",
            "availability",
            "available",
            "is_system",
            "binds_to",
            "page_count",
            "page_sizes",
            "placement_count",
            "suggestion",
            "original_filename",
            "created_at",
        ]

    def get_is_system(self, obj) -> bool:
        return obj.landlord_id is None

    def get_placement_count(self, obj) -> int:
        return obj.placements.count()

    def get_suggestion(self, obj) -> dict | None:
        """What OCR thinks this is — never what it IS.

        Surfaced separately from `stage` so the UI can render it as a proposal
        with an accept button rather than as a decided fact.
        """
        if not obj.suggested_stage:
            return None
        return {
            "stage": obj.suggested_stage,
            "purpose": obj.suggested_purpose,
            "confidence": (obj.suggestion_signals or {}).get("confidence", "low"),
            "signals": [
                signal.get("label")
                for signal in (obj.suggestion_signals or {}).get("signals", [])
            ],
        }


class SignerSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    has_signed = serializers.BooleanField(read_only=True)

    class Meta:
        model = LeaseFormSigner
        fields = [
            "id",
            "role",
            "order",
            "display_name",
            "email",
            "required",
            "has_signed",
            "sent_at",
            "opened_at",
            "signed_at",
            "declined_at",
            "decline_reason",
        ]
        # sign_token is absent on purpose: it is a bearer credential for a
        # signature. The landlord gets a copyable link from the `send` action,
        # not from every list payload their browser caches.


class EventSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.name", default="", read_only=True)
    signer_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaseFormEvent
        fields = [
            "id",
            "kind",
            "actor_name",
            "signer_name",
            "metadata",
            "created_at",
        ]

    def get_signer_name(self, obj) -> str:
        return obj.signer.display_name if obj.signer_id else ""


class LeaseFormSerializer(serializers.ModelSerializer):
    template = TemplateSerializer(read_only=True)
    signers = SignerSerializer(many=True, read_only=True)
    stage = serializers.CharField(read_only=True)
    outstanding = serializers.SerializerMethodField()
    needs_filling = serializers.SerializerMethodField()
    placements = serializers.JSONField(source="placements_snapshot", read_only=True)

    class Meta:
        model = LeaseForm
        fields = [
            "id",
            "lease",
            "template",
            "title",
            "stage",
            "status",
            "required",
            "blocks_activation",
            "moveout_request",
            "placements",
            "values",
            "signers",
            "outstanding",
            "needs_filling",
            "executed_sha256",
            "completed_at",
            "created_via",
            "created_at",
        ]

    def get_outstanding(self, obj) -> list[str]:
        return [
            signer.display_name
            for signer in obj.signers.all()
            if not signer.has_signed and not signer.declined_at
        ]

    def get_needs_filling(self, obj) -> list[str]:
        """Content the form insists on that is still blank.

        Surfaced on read so the UI can say "this needs a vacate date" up front,
        rather than the landlord finding out by pressing Send and getting an
        error back.
        """
        from rentium.leases.form_services import unfilled_required_fields

        if obj.status in {obj.Status.COMPLETED, obj.Status.VOID}:
            return []
        return unfilled_required_fields(obj)
