from django.contrib import admin

from .models import (
    RamaActionReceipt,
    RamaAttachment,
    RamaAttachmentBatch,
    RamaAudit,
    RamaAutoAction,
    RamaCapabilityGap,
    RamaDocument,
    RamaDocumentEvent,
    RamaMemory,
    RamaPreferences,
    RamaTask,
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


@admin.register(RamaTask)
class RamaTaskAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "landlord",
        "capability_key",
        "status",
        "conversation_id",
    )
    list_filter = ("status", "capability_key")
    search_fields = ("conversation_id", "landlord__user__email", "idempotency_key")
    readonly_fields = (
        "landlord",
        "conversation_id",
        "capability_key",
        "status",
        "input",
        "context",
        "outcome",
        "idempotency_key",
        "error",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RamaActionReceipt)
class RamaActionReceiptAdmin(admin.ModelAdmin):
    list_display = ("created_at", "landlord", "capability_key", "task")
    list_filter = ("capability_key",)
    search_fields = ("landlord__user__email", "idempotency_key", "task__conversation_id")
    readonly_fields = (
        "landlord",
        "task",
        "capability_key",
        "idempotency_key",
        "inputs",
        "effects",
        "entity_refs",
        "verification",
        "links",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RamaAttachmentBatch)
class RamaAttachmentBatchAdmin(admin.ModelAdmin):
    list_display = ("created_at", "landlord", "conversation_id", "status")
    list_filter = ("status",)
    search_fields = ("conversation_id", "message_id", "landlord__user__email")
    readonly_fields = ("created_at", "sealed_at", "completed_at")


@admin.register(RamaAttachment)
class RamaAttachmentAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "original_filename",
        "batch",
        "classification",
        "status",
        "sequence",
    )
    list_filter = ("classification", "status")
    search_fields = ("original_filename", "sha256", "target_id")
    readonly_fields = ("sha256", "size", "created_at", "updated_at")


@admin.register(RamaAutoAction)
class RamaAutoActionAdmin(admin.ModelAdmin):
    """Read-only: the record of what RAMA did unattended.

    Editable receipts would defeat the point — this is the evidence a landlord
    (or we, in support) rely on to answer "why did that change?". Undo happens
    through the API, which records the reversal rather than rewriting history.
    """

    list_display = (
        "created_at",
        "landlord",
        "tool",
        "target_label",
        "status",
        "policy_rule_id",
    )
    list_filter = ("status", "tool")
    search_fields = ("landlord__user__email", "tool", "target_label", "conversation_id")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RamaMemory)
class RamaMemoryAdmin(admin.ModelAdmin):
    """Support view over durable landlord preferences.

    Deliberately not editable: corrections happen by supersession (memory.write)
    so the chain stays intact. Deleting IS allowed — an erasure request has to
    be able to actually remove the text.
    """

    list_display = (
        "created_at",
        "landlord",
        "key",
        "scope",
        "source",
        "status",
        "contains_personal_data",
        "use_count",
    )
    list_filter = ("status", "scope", "source", "contains_personal_data")
    search_fields = ("landlord__user__email", "key", "body", "entity_key")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
