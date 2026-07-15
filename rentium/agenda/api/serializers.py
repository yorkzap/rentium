from rest_framework import serializers

from ..models import AgendaEvent


class AgendaEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgendaEvent
        fields = ["id", "title", "notes", "kind", "start_date", "end_date", "property", "lease", "created_at"]
        read_only_fields = ["id", "created_at"]
