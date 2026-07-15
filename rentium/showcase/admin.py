from django.contrib import admin

from .models import Inquiry
from .models import Showcase
from .models import ShowcaseSlugHistory


@admin.register(Showcase)
class ShowcaseAdmin(admin.ModelAdmin):
    list_display = ("slug", "public_name", "is_public", "first_published_at")
    list_filter = ("is_public",)
    search_fields = ("slug", "display_name", "landlord__user__email")


@admin.register(ShowcaseSlugHistory)
class SlugHistoryAdmin(admin.ModelAdmin):
    list_display = ("slug", "showcase", "retired_at")


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ("name", "property", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "email", "message")
    readonly_fields = ("source_ip", "user_agent", "created_at")
