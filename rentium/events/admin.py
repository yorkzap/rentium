from django.contrib import admin

from .models import DomainEvent, Notification


@admin.register(DomainEvent)
class DomainEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "created_at", "processed_at", "property_id", "lease_id")
    list_filter = ("event_type",)
    search_fields = ("event_type",)
    readonly_fields = [f.name for f in DomainEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "recipient", "category", "read_at", "created_at")
    list_filter = ("category",)
    search_fields = ("title", "body")
