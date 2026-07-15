from django.contrib import admin

from .models import WorkOrder, WorkOrderComment, WorkOrderImage


class WorkOrderImageInline(admin.TabularInline):
    model = WorkOrderImage
    extra = 0


class WorkOrderCommentInline(admin.TabularInline):
    model = WorkOrderComment
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("title", "property", "area", "origin", "category", "priority",
                    "status", "sla_due_at", "scheduled_date", "cost", "created_at")
    list_filter = ("status", "priority", "category", "origin")
    search_fields = ("title", "description", "property__name", "contractor_name")
    inlines = [WorkOrderImageInline, WorkOrderCommentInline]
