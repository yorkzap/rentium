from django.contrib import admin

from .models import LedgerAttachment, LedgerEntry


class LedgerAttachmentInline(admin.TabularInline):
    model = LedgerAttachment
    extra = 0


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    """Read-only in admin too — the ledger has no back door."""

    list_display = ("entry_type", "amount", "description", "property", "tenant",
                    "due_date", "effective_date", "created_at")
    list_filter = ("entry_type", "category", "payment_method")
    search_fields = ("description", "vendor", "reference_number", "idempotency_key")
    inlines = [LedgerAttachmentInline]
    readonly_fields = [f.name for f in LedgerEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
