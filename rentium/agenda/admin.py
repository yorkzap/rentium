from django.contrib import admin

from .models import AgendaEvent


@admin.register(AgendaEvent)
class AgendaEventAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "start_date", "end_date", "owner", "property")
    list_filter = ("kind",)
