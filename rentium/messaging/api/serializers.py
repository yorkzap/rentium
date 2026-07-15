from rest_framework import serializers

from ..models import Conversation, Message


class MessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "body", "sender_name", "is_mine", "read_at", "created_at"]
        read_only_fields = fields

    def get_sender_name(self, obj):
        return getattr(obj.sender, "name", None) or getattr(obj.sender, "email", "—") if obj.sender else "—"

    def get_is_mine(self, obj):
        request = self.context.get("request")
        return bool(request and obj.sender_id == request.user.id)


class ConversationSerializer(serializers.ModelSerializer):
    other_party = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "subject", "lease", "other_party", "last_message", "unread_count", "updated_at"]
        read_only_fields = fields

    def _is_landlord(self):
        u = self.context["request"].user
        return hasattr(u, "landlord_profile")

    def get_other_party(self, obj):
        if self._is_landlord():
            u = getattr(obj.tenant, "user", None)
        else:
            u = getattr(obj.landlord, "user", None)
        return (getattr(u, "name", None) or getattr(u, "email", "—")) if u else "—"

    def get_last_message(self, obj):
        m = obj.messages.last()
        return {"body": m.body[:120], "created_at": m.created_at} if m else None

    def get_unread_count(self, obj):
        me = self.context["request"].user
        return obj.messages.filter(read_at__isnull=True).exclude(sender=me).count()
