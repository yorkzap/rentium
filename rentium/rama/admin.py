from django.contrib import admin

from .models import RamaAudit


@admin.register(RamaAudit)
class RamaAuditAdmin(admin.ModelAdmin):
    """Read-only: the audit trail is append-only by design."""

    list_display = ("created_at", "landlord", "kind", "provider", "model", "conversation_id")
    list_filter = ("kind", "provider", "model")
    search_fields = ("conversation_id",)
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
