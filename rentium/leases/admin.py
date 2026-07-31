from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from .inspections import AreaConditionState
from .inspections import ConditionInspection
from .inspections import InspectionItem
from .inspections import InspectionKeyRow
from .inspections import InspectionTemplate
from .inspections import InspectionTemplateItem
from .models import Lease
from .models import LeaseDocument
from .models import LeaseInviteEvent
from .models import LeaseTenant
from .models import MoveOutRequest
from .models import Occupancy
from .models import Payment
from .models import PaymentReminder
from .models import RentAdjustment


class LeaseTenantInline(admin.TabularInline):
    model = LeaseTenant
    extra = 0
    fields = (
        "tenant",
        "invited_email",
        "invited_name",
        "room",
        "rent_amount",
        "is_primary_tenant",
        "has_signed",
        "declined",
    )
    readonly_fields = ("has_signed", "declined")
    show_change_link = True
    autocomplete_fields = ["tenant", "room"]


@admin.register(LeaseInviteEvent)
class LeaseInviteEventAdmin(admin.ModelAdmin):
    """Read-only evidence of invite delivery, link access, linking, and signing."""

    list_display = ("created_at", "lease_tenant", "kind", "actor")
    list_filter = ("kind",)
    search_fields = (
        "lease_tenant__lease__lease_number",
        "lease_tenant__invited_email",
        "lease_tenant__invited_name",
    )
    readonly_fields = ("lease_tenant", "kind", "actor", "metadata", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LeaseDocumentInline(admin.TabularInline):
    model = LeaseDocument
    extra = 0
    show_change_link = True


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = (
        "tenant",
        "payment_type",
        "amount_due",
        "amount_paid",
        "due_date",
        "status",
    )
    readonly_fields = ("status",)
    show_change_link = True
    can_delete = False


class RentAdjustmentInline(admin.TabularInline):
    model = RentAdjustment
    extra = 0
    show_change_link = True


class MoveOutRequestInline(admin.TabularInline):
    model = MoveOutRequest
    extra = 0
    fields = (
        "kind",
        "status",
        "requested_end_date",
        "effective_end_date",
        "initiated_by",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(Lease)
class LeaseAdmin(admin.ModelAdmin):
    list_display = (
        "lease_number",
        "lease_type",
        "status_badge",
        "property_or_group",
        "landlord",
        "start_date",
        "end_date",
        "is_month_to_month",
        "total_rent",
        "is_locked_display",
    )
    list_filter = ("status", "lease_type", "is_month_to_month")
    search_fields = (
        "lease_number",
        "property__name",
        "property__address",
        "group__name",
        "landlord__user__name",
        "landlord__user__email",
    )
    date_hierarchy = "start_date"
    autocomplete_fields = ["property", "group", "landlord", "previous_lease"]
    readonly_fields = ("lease_number", "created_at", "updated_at", "is_locked_display")
    inlines = [
        LeaseTenantInline,
        PaymentInline,
        LeaseDocumentInline,
        MoveOutRequestInline,
    ]
    actions = ["terminate_selected_leases", "mark_expired"]

    fieldsets = (
        (
            "Identification",
            {
                "fields": ("lease_number", "lease_type", "status", "is_locked_display"),
            },
        ),
        (
            "Linked to",
            {
                "fields": ("property", "group", "landlord", "previous_lease"),
            },
        ),
        (
            "Term",
            {
                "fields": (
                    "start_date",
                    "end_date",
                    "is_month_to_month",
                    "move_in_date",
                    "move_out_date",
                ),
            },
        ),
        (
            "Financials",
            {
                "fields": (
                    "total_rent",
                    "security_deposit",
                    "pet_deposit",
                    "cleaning_fee",
                    "bills_included",
                ),
            },
        ),
        (
            "Terms & Clauses",
            {
                "fields": (
                    "pets_allowed",
                    "smoking_allowed",
                    "special_terms",
                    "common_space_shared_with",
                    "custom_tenant_notice_months",
                    "fixed_term_end_reason",
                    "fixed_term_end_regulation_section",
                ),
            },
        ),
        (
            "Landlord contact / payment routing",
            {
                "classes": ("collapse",),
                "fields": (
                    "landlord_service_address",
                    "landlord_daytime_phone",
                    "landlord_other_phone",
                    "landlord_fax",
                    "landlord_service_email",
                    "etransfer_email",
                ),
            },
        ),
        (
            "Signature",
            {
                "fields": ("landlord_signed", "landlord_signed_date"),
            },
        ),
        (
            "Document",
            {
                "fields": ("document_file",),
            },
        ),
        (
            "Metadata",
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "DRAFT": "#94a3b8",
            "PENDING": "#f59e0b",
            "ACTIVE": "#22c55e",
            "EXPIRED": "#64748b",
            "TERMINATED": "#ef4444",
            "RENEWED": "#3b82f6",
        }
        color = colors.get(obj.status, "#64748b")
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;'
            'border-radius:10px;font-size:11px;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Property/Group")
    def property_or_group(self, obj):
        if obj.property:
            return obj.property.name
        return obj.group.name if obj.group else "—"

    @admin.display(description="Locked?", boolean=True)
    def is_locked_display(self, obj):
        return obj.is_locked()

    @admin.action(description="Terminate selected leases (effective today)")
    def terminate_selected_leases(self, request, queryset):
        today = timezone.now().date()
        updated, skipped = 0, 0
        final_states = (
            Lease.LeaseStatus.TERMINATED,
            Lease.LeaseStatus.EXPIRED,
            Lease.LeaseStatus.RENEWED,
        )
        for lease in queryset:
            if lease.status in final_states:
                skipped += 1
                continue
            lease.status = Lease.LeaseStatus.TERMINATED
            lease.move_out_date = today
            if not lease.end_date or lease.end_date > today:
                lease.end_date = today
            note = f"[Admin] Terminated by {request.user} on {today}."
            lease.special_terms = (
                f"{lease.special_terms}\n\n{note}".strip()
                if lease.special_terms
                else note
            )
            lease.save()
            updated += 1
        self.message_user(
            request,
            f"Terminated {updated} lease(s). Skipped {skipped} already-final lease(s).",
        )

    @admin.action(description="Mark selected leases as expired")
    def mark_expired(self, request, queryset):
        final_states = (
            Lease.LeaseStatus.TERMINATED,
            Lease.LeaseStatus.EXPIRED,
            Lease.LeaseStatus.RENEWED,
        )
        count = queryset.exclude(status__in=final_states).update(
            status=Lease.LeaseStatus.EXPIRED
        )
        self.message_user(request, f"Marked {count} lease(s) as expired.")


@admin.register(LeaseTenant)
class LeaseTenantAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "lease",
        "room",
        "rent_amount",
        "is_primary_tenant",
        "has_signed",
        "declined",
    )
    list_filter = ("has_signed", "declined", "is_primary_tenant")
    search_fields = (
        "invited_email",
        "invited_name",
        "tenant__user__name",
        "tenant__user__email",
        "lease__lease_number",
    )
    autocomplete_fields = ["lease", "tenant", "room"]
    readonly_fields = (
        "invite_token",
        "invite_sent_at",
        "invite_accepted_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Tenant")
    def display_name(self, obj):
        return obj.display_name


@admin.register(RentAdjustment)
class RentAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "lease_tenant",
        "adjustment_type",
        "calculation_method",
        "amount",
        "effective_date",
        "end_date",
        "is_recurring",
    )
    list_filter = ("adjustment_type", "calculation_method", "is_recurring")
    search_fields = ("lease_tenant__lease__lease_number",)
    autocomplete_fields = ["lease_tenant", "created_by"]


@admin.register(LeaseDocument)
class LeaseDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "lease", "is_signed", "uploaded_at")
    list_filter = ("is_signed",)
    search_fields = ("title", "lease__lease_number")
    autocomplete_fields = ["lease"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "lease",
        "tenant",
        "payment_type",
        "amount_due",
        "amount_paid",
        "due_date",
        "status",
    )
    list_filter = ("payment_type", "status", "payment_method")
    search_fields = (
        "lease__lease_number",
        "tenant__user__name",
        "reference_number",
        "utility_provider",
    )
    date_hierarchy = "due_date"
    autocomplete_fields = ["lease", "tenant", "rent_adjustment"]


@admin.register(PaymentReminder)
class PaymentReminderAdmin(admin.ModelAdmin):
    list_display = ("payment", "reminder_date", "is_sent", "send_method")
    list_filter = ("is_sent", "send_method")
    autocomplete_fields = ["payment"]


@admin.register(MoveOutRequest)
class MoveOutRequestAdmin(admin.ModelAdmin):
    list_display = (
        "lease",
        "kind",
        "status",
        "initiated_by",
        "requested_end_date",
        "effective_end_date",
    )
    list_filter = ("kind", "status", "initiated_by")
    search_fields = ("lease__lease_number",)
    autocomplete_fields = ["lease", "lease_tenant"]


@admin.register(Occupancy)
class OccupancyAdmin(admin.ModelAdmin):
    list_display = ("room", "tenant", "lease", "move_in", "move_out")
    list_filter = ("move_out",)
    search_fields = ("room__name", "tenant__user__name", "lease__lease_number")
    autocomplete_fields = ["room", "tenant", "lease"]


@admin.register(InspectionTemplate)
class InspectionTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "province", "version", "is_active")
    list_filter = ("province", "is_active")
    search_fields = ("name",)


@admin.register(InspectionTemplateItem)
class InspectionTemplateItemAdmin(admin.ModelAdmin):
    list_display = ("template", "section", "label", "sort_order")
    list_filter = ("template__province",)
    search_fields = ("section", "label")


@admin.register(ConditionInspection)
class ConditionInspectionAdmin(admin.ModelAdmin):
    list_display = (
        "lease",
        "lease_tenant",
        "status",
        "possession_date",
        "move_out_date",
    )
    list_filter = ("status",)
    search_fields = ("lease__lease_number",)
    autocomplete_fields = ["lease", "lease_tenant", "template", "created_by"]


@admin.register(InspectionItem)
class InspectionItemAdmin(admin.ModelAdmin):
    list_display = (
        "inspection",
        "section",
        "label",
        "move_in_condition_code",
        "move_out_condition_code",
        "needs_attention",
        "suggestion_status",
    )
    list_filter = ("suggestion_status", "needs_attention")
    search_fields = ("section", "label", "inspection__lease__lease_number")


@admin.register(InspectionKeyRow)
class InspectionKeyRowAdmin(admin.ModelAdmin):
    list_display = ("inspection", "key_type", "issued_count", "returned_count")


@admin.register(AreaConditionState)
class AreaConditionStateAdmin(admin.ModelAdmin):
    list_display = ("area", "condition", "updated_at")
    list_filter = ("condition",)
