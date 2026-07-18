from django.contrib import admin

from .models import RamaAudit, RamaPreferences


@admin.register(RamaPreferences)
class RamaPreferencesAdmin(admin.ModelAdmin):
    """Support view — landlords edit these in the dashboard settings UI."""

    list_display = ("landlord", "enabled", "provider", "model", "updated_at")
    list_filter = ("enabled", "provider")
    search_fields = ("landlord__user__email", "landlord__user__name")
    readonly_fields = ("updated_at",)


@admin.register(RamaAudit)
class RamaAuditAdmin(admin.ModelAdmin):
    """Read-only: the audit trail is append-only by design."""

    list_display = (
        "created_at",
        "landlord",
        "kind",
        "provider",
        "model",
        "conversation_id",
    )
    list_filter = ("kind", "provider", "model")
    search_fields = ("conversation_id", "landlord__user__email")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
