from django.contrib import admin

from .models import (
    RamaAudit,
    RamaCapabilityGap,
    RamaDocument,
    RamaDocumentEvent,
    RamaPreferences,
)


@admin.register(RamaDocument)
class RamaDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "title",
        "landlord",
        "holding",
        "kind",
        "status",
        "payment_state",
    )
    list_filter = ("status", "kind", "payment_state")
    search_fields = (
        "title",
        "issuer",
        "reference_number",
        "original_filename",
        "ocr_text",
    )
    readonly_fields = ("sha256", "created_at", "updated_at", "filed_at")


@admin.register(RamaDocumentEvent)
class RamaDocumentEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "document", "kind", "actor")
    readonly_fields = ("document", "kind", "actor", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RamaCapabilityGap)
class RamaCapabilityGapAdmin(admin.ModelAdmin):
    """The 'learn now' backlog — what RAMA couldn't do yet. Review here, build the
    capability (a tool or a playbook composition of existing tools), then set
    status=BUILT. Nothing here runs code."""

    list_display = ("created_at", "landlord", "status", "prioritised", "request")
    list_filter = ("status", "prioritised")
    search_fields = ("request", "detail", "landlord__user__email")
    list_editable = ("status",)
    ordering = ("-prioritised", "-created_at")


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
