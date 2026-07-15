from django.contrib import admin

from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        "kind",
        "status",
        "property",
        "starts_at",
        "ends_at",
        "contact_name",
        "landlord",
    )
    list_filter = ("kind", "status")
    search_fields = (
        "contact_name",
        "contact_email",
        "contact_phone",
        "property__name",
        "notes",
    )
    date_hierarchy = "starts_at"
    autocomplete_fields = ["landlord", "property", "lease", "work_order"]
    readonly_fields = ("created_at", "updated_at")
    actions = ["cancel_selected"]

    @admin.action(description="Cancel selected appointments")
    def cancel_selected(self, request, queryset):
        count = queryset.exclude(status=Appointment.Status.CANCELLED).update(
            status=Appointment.Status.CANCELLED
        )
        self.message_user(request, f"Cancelled {count} appointment(s).")
