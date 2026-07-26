# views.py

from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation

from django.db.models import Count
from django.db.models import Q
from django.db.models import Sum
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser
from rest_framework.parsers import JSONParser
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rentium.leases.models import Lease

from .. import services
from ..billing import lease_is_joint
from ..billing import split_utility_bill
from ..models import CHARGE_TYPES
from ..models import INCOME_CHARGE_TYPES
from ..models import ChargeStatus
from ..models import EntryType
from ..models import LedgerAttachment
from ..models import LedgerEntry


# --------------------------------------------------------------- serializers
class LedgerAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerAttachment
        fields = ["id", "file", "label", "created_at"]
        read_only_fields = ["id", "created_at"]


class LedgerEntrySerializer(serializers.ModelSerializer):
    entry_type_display = serializers.CharField(
        source="get_entry_type_display", read_only=True
    )
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    property_name = serializers.CharField(
        source="property.name", read_only=True, allow_null=True
    )
    holding_name = serializers.CharField(
        source="holding.name", read_only=True, allow_null=True
    )

    # Context for the Financial UI's expandable rows: which lease a charge
    # belongs to (human-readable number) and what kind of space it's for
    # ("ROOM" vs "COMPLETE_UNIT") without a second fetch per row.
    lease_number = serializers.CharField(
        source="lease.lease_number", read_only=True, allow_null=True
    )
    property_category = serializers.CharField(
        source="property.property_category", read_only=True, allow_null=True
    )

    tenant_name = serializers.SerializerMethodField()

    # True for household charges on joint (roommate) leases — everyone on
    # the lease owes it together, and any tenant's payment settles it.
    is_joint = serializers.SerializerMethodField()

    # From with_settlement() annotations (charges only; null otherwise).
    settled_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, required=False
    )
    outstanding = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, required=False
    )
    charge_status = serializers.SerializerMethodField()
    voided = serializers.SerializerMethodField()

    # Expenses only: has this actually left the landlord's bank account?
    # `paid_on` is the one column on this model that can change after posting
    # (see LedgerEntry.save()); `bank_status` is the derived label the UI reads
    # so it never has to reimplement the null-check.
    bank_status = serializers.CharField(read_only=True)

    attachments = LedgerAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "entry_type",
            "entry_type_display",
            "amount",
            "due_date",
            "effective_date",
            "description",
            "property",
            "property_name",
            "holding",
            "holding_name",
            "property_category",
            "lease",
            "lease_number",
            "tenant",
            "tenant_name",
            "is_joint",
            "settles",
            "reverses",
            "work_order",
            "payment_method",
            "reference_number",
            "category",
            "category_display",
            "vendor",
            "paid_on",
            "bank_status",
            "settled_amount",
            "outstanding",
            "charge_status",
            "voided",
            "metadata",
            "attachments",
            "created_at",
        ]
        # Writes go through the service-backed actions only. There is no PATCH on
        # a ledger entry, by design — the one field that can move (paid_on) moves
        # through its own explicit action, so "update an entry" never becomes a
        # generally available verb that someone later widens.
        read_only_fields = fields

    def get_tenant_name(self, obj):
        try:
            return obj.tenant.user.name if obj.tenant else None
        except AttributeError:
            return str(obj.tenant) if obj.tenant else None

    def get_is_joint(self, obj):
        return (
            obj.entry_type in CHARGE_TYPES
            and obj.tenant_id is None
            and obj.lease_id is not None
        )

    def get_voided(self, obj):
        annotated = getattr(obj, "is_voided", None)
        return annotated if annotated is not None else obj.voided

    def get_charge_status(self, obj):
        if obj.entry_type not in CHARGE_TYPES:
            return None
        if self.get_voided(obj):
            return ChargeStatus.VOIDED

        settled = getattr(obj, "settled_amount", None)
        if settled is not None:
            today = date.today()
            if settled >= obj.amount:
                return ChargeStatus.PAID
            if settled > 0:
                return ChargeStatus.PARTIALLY_PAID
            if obj.due_date and obj.due_date < today:
                return ChargeStatus.OVERDUE
            if obj.due_date == today:
                return ChargeStatus.DUE
            return ChargeStatus.SCHEDULED

        return obj.charge_status()


# ------------------------------------------------------------------ helpers
def _landlord(request):
    if not hasattr(request.user, "landlord_profile"):
        raise PermissionDenied("Landlords only.")
    return request.user.landlord_profile


def _decimal(value, field):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "A valid amount is required."})
    if d <= 0:
        raise ValidationError({field: "Amount must be positive."})
    return d


def _date(value, field, required=True):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "A date (YYYY-MM-DD) is required."})
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError({field: "Use YYYY-MM-DD."})


def _payer_from_request(request, charge):
    """
    Optional `tenant` in a record_payment body = who actually paid.

    Required semantics for joint charges (anyone in the household can pay,
    and we keep who-paid-what on the record); for split charges it defaults
    to the charge's own tenant. Validated against the charge's lease.
    """
    tenant_id = request.data.get("tenant")
    if not tenant_id:
        return None  # services.record_payment falls back to charge.tenant
    if not charge.lease_id:
        raise ValidationError(
            {"tenant": "This charge has no lease to match a payer against."}
        )
    lt = charge.lease.lease_tenants.filter(tenant__pk=tenant_id).first()
    if not lt or not lt.tenant:
        raise ValidationError({"tenant": "That tenant is not on this charge's lease."})
    return lt.tenant


# ------------------------------------------------------------------ viewset
class LedgerEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    /api/ledger/entries/  — the property ledger.

    Read-only by design: entries are immutable, so there is no PUT/PATCH/
    DELETE. All writes are explicit business actions below, each routed
    through the service layer (idempotency + events in one place).
    """

    serializer_class = LedgerEntrySerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = {
        "property": ["exact"],
        "lease": ["exact"],
        "tenant": ["exact"],
        "entry_type": ["exact", "in"],
        "category": ["exact"],
        "due_date": ["gte", "lte"],
        "effective_date": ["gte", "lte"],
        # ?paid_on__isnull=true -> "what have I recorded but not actually paid?"
        # That difference is a real number a landlord needs and the app had no
        # way to express before.
        "paid_on": ["exact", "isnull", "gte", "lte"],
    }
    search_fields = ["description", "vendor", "reference_number"]
    ordering_fields = ["effective_date", "due_date", "amount", "created_at", "paid_on"]
    ordering = ["-effective_date", "-created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = (
            LedgerEntry.objects.select_related("property", "tenant", "lease")
            .prefetch_related("attachments")
            .with_settlement()
        )

        if hasattr(user, "landlord_profile"):
            from rentium.users.access import scope_q

            return qs.filter(
                scope_q(user, property_field="property", lease_field="lease")
            ).distinct()

        if hasattr(user, "tenant_profile"):
            tp = user.tenant_profile
            # A tenant sees:
            #  1. entries in their own name (their split charges, their payments),
            #  2. JOINT household entries (tenant=NULL) on any lease they're on —
            #     the whole household owes those together,
            #  3. settlements OF those joint charges regardless of which
            #     roommate paid — so when a roommate's $400 lands, everyone
            #     sees the household balance clear.
            #
            # `lease__in=<queryset>` (a subquery) is used instead of joining
            # lease__lease_tenants directly: a join would multiply rows on
            # multi-tenant leases and corrupt the Sum() in with_settlement().
            my_leases = Lease.objects.filter(lease_tenants__tenant=tp)
            return qs.filter(
                Q(tenant=tp)
                | (Q(tenant__isnull=True) & Q(lease__in=my_leases))
                | (Q(settles__tenant__isnull=True) & Q(settles__lease__in=my_leases))
            )

        return LedgerEntry.objects.none()

    @action(detail=False, methods=["get"])
    def charges(self, request):
        """Charges only, with computed status; ?status=OVERDUE filter."""
        qs = self.filter_queryset(self.get_queryset()).charges()
        wanted = request.query_params.get("status")

        page = self.paginate_queryset(qs)
        items = page if page is not None else qs
        data = self.get_serializer(items, many=True).data

        if wanted:
            data = [d for d in data if d["charge_status"] == wanted]
        if page is not None and not wanted:
            return self.get_paginated_response(data)
        return Response(data)

    # ----------------------------------------------------------- writes
    @action(detail=True, methods=["post"])
    def record_payment(self, request, pk=None):
        """
        Landlord confirms an e-transfer/cash arrived for this charge.

        Body: {amount, payment_method, payment_date?, reference_number?,
               notes?, tenant?, idempotency_key}

        `tenant` = who paid — pass it on JOINT household charges so the
        record shows which roommate's money it was (two $400 e-transfers
        from different roommates are two payments settling the same $800
        charge). Send a client-generated UUID as the idempotency_key; a
        retry returns the original entry, never a double-post.
        """
        _landlord(request)
        charge = self.get_object()
        try:
            entry, created = services.record_payment(
                charge=charge,
                amount=_decimal(request.data.get("amount"), "amount"),
                payment_method=request.data.get("payment_method", "ETRANSFER"),
                payment_date=_date(
                    request.data.get("payment_date"), "payment_date", required=False
                ),
                reference_number=request.data.get("reference_number", ""),
                notes=request.data.get("notes", ""),
                paid_by=_payer_from_request(request, charge),
                idempotency_key=request.data.get("idempotency_key") or None,
                created_by=request.user,
            )
        except services.LedgerError as exc:
            raise ValidationError({"detail": str(exc)})

        return Response(
            LedgerEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def credit(self, request, pk=None):
        """One-off discount/goodwill credit against this charge."""
        _landlord(request)
        charge = self.get_object()
        try:
            entry, created = services.post_credit(
                charge=charge,
                amount=_decimal(request.data.get("amount"), "amount"),
                reason=request.data.get("reason", "Credit"),
                idempotency_key=request.data.get("idempotency_key") or None,
                created_by=request.user,
            )
        except services.LedgerError as exc:
            raise ValidationError({"detail": str(exc)})

        return Response(
            LedgerEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        """Void this entry with a reversal. Body: {reason}."""
        _landlord(request)
        entry = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise ValidationError(
                {"reason": "A reason is required — it goes on the audit trail."}
            )
        try:
            reversal = services.void_entry(
                entry, reason=reason, created_by=request.user
            )
        except services.LedgerError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(
            LedgerEntrySerializer(reversal).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """
        The 'edit' button: void + repost with changed fields in one
        transaction. Body: any of {amount, description, due_date,
        effective_date, category, vendor, reference_number, reason}.

        `paid_on` is deliberately NOT correctable here — it is carried across to
        the replacement by services.correct_entry(), and changed (if at all)
        through mark_paid below. Fixing a typo in a bill's description must not
        silently un-pay it.
        """
        _landlord(request)
        entry = self.get_object()

        changes = {}
        if "amount" in request.data:
            changes["amount"] = _decimal(request.data["amount"], "amount")
        for f in ("description", "category", "vendor", "reference_number"):
            if f in request.data:
                changes[f] = request.data[f]
        if "due_date" in request.data:
            changes["due_date"] = _date(
                request.data["due_date"], "due_date", required=False
            )
        if "effective_date" in request.data:
            changes["effective_date"] = _date(
                request.data["effective_date"], "effective_date"
            )

        if not changes:
            raise ValidationError({"detail": "Nothing to change."})

        try:
            replacement = services.correct_entry(
                entry,
                created_by=request.user,
                reason=request.data.get("reason", "Correction"),
                **changes,
            )
        except services.LedgerError as exc:
            raise ValidationError({"detail": str(exc)})

        return Response(
            LedgerEntrySerializer(replacement).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def mark_paid(self, request, pk=None):
        """
        POST /api/ledger/entries/<id>/mark_paid/  {"paid_on": "2026-07-16"}
        POST /api/ledger/entries/<id>/mark_paid/  {"paid_on": null}  -> reset

        The ONLY endpoint in this app that updates a ledger row rather than
        posting a new one. It moves exactly one whitelisted column, and the MODEL
        refuses anything else (see LedgerEntry.save() and MUTABLE_AFTER_POST) —
        so the append-only guarantee is enforced at the persistence layer, not by
        this view's good manners. If someone later adds a careless .save() call
        somewhere, it still raises.

        Why this exists at all: "the money has left my account" is genuinely
        unknown at the moment you record a bill. Providers auto-debit on their own
        schedule. Voiding and reposting the entry every time a bill clears would
        fill the audit trail with reversals recording nothing anybody cares about,
        which makes the audit trail worse, not better.
        """
        _landlord(request)
        entry = self.get_object()

        raw = request.data.get("paid_on", "__unset__")
        try:
            if raw in (None, ""):
                updated = services.unmark_expense_paid(entry)
            else:
                when = (
                    None
                    if raw == "__unset__"
                    else _date(raw, "paid_on", required=False)
                )
                updated = services.mark_expense_paid(
                    entry, paid_on=when, created_by=request.user
                )
        except services.LedgerError as exc:
            raise ValidationError({"detail": str(exc)})

        return Response(LedgerEntrySerializer(updated).data)

    @action(detail=False, methods=["post"])
    def expense(self, request):
        """
        Record money out. Body: {amount, category, description,
        incurred_date?, paid_on?, property?, vendor?, idempotency_key}.

        `incurred_date` and `paid_on` are two DIFFERENT dates and the distinction
        matters: the first is when the cost was incurred (the work was done, the
        bill was issued); the second is when the money actually left the bank.
        Conflating them is how a landlord ends up unable to reconcile against a
        bank statement. `paid_on` is optional — omit it and the expense reads as
        "not yet taken from bank" until you say otherwise.
        """
        landlord = _landlord(request)

        prop = None
        if request.data.get("property"):
            from rentium.properties.models import Property

            prop = Property.objects.filter(
                pk=request.data["property"], landlord=landlord
            ).first()
            if not prop:
                raise ValidationError({"property": "Not your property."})

        paid_on = _date(request.data.get("paid_on"), "paid_on", required=False)
        if paid_on and paid_on > date.today():
            raise ValidationError(
                {"paid_on": "Money can't have left your account in the future."}
            )

        entry, created = services.post_expense(
            landlord=landlord,
            property=prop,
            amount=_decimal(request.data.get("amount"), "amount"),
            category=request.data.get("category", "OTHER"),
            description=request.data.get("description", "").strip() or "Expense",
            incurred_date=_date(
                request.data.get("incurred_date"), "incurred_date", required=False
            ),
            vendor=request.data.get("vendor", ""),
            paid_on=paid_on,
            idempotency_key=request.data.get("idempotency_key") or None,
            created_by=request.user,
        )
        return Response(
            LedgerEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"])
    def charge(self, request):
        """
        Manual one-off charge (e.g. damage, late fee). Body: {lease,
        amount, due_date, description, entry_type?, tenant?}.

        `tenant` is optional on JOINT (roommate) leases — omit it to bill
        the whole household jointly; required on split-billing leases.
        """
        landlord = _landlord(request)

        lease = Lease.objects.filter(
            pk=request.data.get("lease"), landlord=landlord
        ).first()
        if not lease:
            raise ValidationError({"lease": "Not your lease."})

        tenant = None
        tenant_id = request.data.get("tenant")
        if tenant_id:
            lt = lease.lease_tenants.filter(tenant__pk=tenant_id).first()
            if not lt or not lt.tenant:
                raise ValidationError({"tenant": "That tenant is not on this lease."})
            tenant = lt.tenant
        elif not lease_is_joint(lease):
            raise ValidationError(
                {"tenant": "A tenant is required on this lease (it bills per-tenant)."}
            )

        etype = request.data.get("entry_type", EntryType.OTHER_CHARGE)
        if etype not in CHARGE_TYPES:
            raise ValidationError({"entry_type": "Must be a charge type."})

        entry, created = services.post_charge(
            landlord=landlord,
            tenant=tenant,
            lease=lease,
            amount=_decimal(request.data.get("amount"), "amount"),
            due_date=_date(request.data.get("due_date"), "due_date"),
            entry_type=etype,
            description=request.data.get("description", "").strip() or "Charge",
            idempotency_key=request.data.get("idempotency_key") or None,
            created_by=request.user,
        )
        return Response(
            LedgerEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def add_attachment(self, request, pk=None):
        _landlord(request)
        entry = self.get_object()
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"file": "A file is required."})
        att = LedgerAttachment.objects.create(
            entry=entry,
            file=file,
            label=request.data.get("label", ""),
            uploaded_by=request.user,
        )
        return Response(
            LedgerAttachmentSerializer(att).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        """
        GET /api/ledger/entries/<id>/receipt/ -> printable PDF receipt.

        Only PAYMENT entries have receipts. Visibility rides on the
        viewset's own queryset scoping — the landlord and every tenant on
        the lease can pull it (roommates share one household ledger, and a
        receipt for "who paid what" is exactly the record they need).
        """
        entry = self.get_object()
        if entry.entry_type != EntryType.PAYMENT:
            raise ValidationError(
                {"detail": "Receipts are only available for payments."}
            )

        from io import BytesIO

        from django.http import HttpResponse
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas as pdfcanvas

        charge = entry.settles
        lease = entry.lease or (charge.lease if charge else None)
        prop = entry.property or (charge.property if charge else None)

        try:
            payer = entry.tenant.user.name if entry.tenant else None
        except AttributeError:
            payer = str(entry.tenant) if entry.tenant else None
        try:
            landlord_name = entry.landlord.user.name
        except AttributeError:
            landlord_name = str(entry.landlord) if entry.landlord_id else ""

        buf = BytesIO()
        c = pdfcanvas.Canvas(buf, pagesize=LETTER)
        width, height = LETTER
        y = height - 1 * inch

        c.setFont("Helvetica-Bold", 18)
        c.drawString(1 * inch, y, "Payment Receipt")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawRightString(width - 1 * inch, y, f"Receipt {str(entry.pk)[:8].upper()}")
        c.setFillColorRGB(0, 0, 0)

        y -= 0.2 * inch
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.line(1 * inch, y, width - 1 * inch, y)
        y -= 0.4 * inch

        def row(label, value):
            nonlocal y
            if value in (None, ""):
                return
            c.setFont("Helvetica", 10)
            c.setFillColorRGB(0.45, 0.45, 0.45)
            c.drawString(1 * inch, y, label)
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(2.7 * inch, y, str(value)[:80])
            y -= 0.28 * inch

        row("Amount received", f"${entry.amount}")
        row("Date received", entry.effective_date)
        row("Paid by", payer or "Tenant")
        row("Received by", landlord_name)
        row("Method", entry.payment_method or "")
        row("Reference", entry.reference_number or "")
        if charge:
            row("Applied to", charge.description)
            row("Charge due date", charge.due_date)
        if lease is not None:
            row("Lease", getattr(lease, "lease_number", ""))
        if prop is not None:
            row("Property", getattr(prop, "name", ""))
            row("Address", getattr(prop, "address", ""))

        y -= 0.15 * inch
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.line(1 * inch, y, width - 1 * inch, y)
        y -= 0.3 * inch
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(
            1 * inch,
            y,
            "Recorded in Rentium. This receipt confirms the payment above was "
            "recorded on the lease ledger by the landlord.",
        )

        c.showPage()
        c.save()
        pdf = buf.getvalue()
        buf.close()

        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="receipt_{str(entry.pk)[:8]}.pdf"'
        )
        return response


# ------------------------------------------------------------- utility bill
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def utility_bill_view(request):
    """
    POST /api/ledger/utility-bills/

    {lease, total_amount, period_start, period_end, description,
     bill_key?, due_date?, record_landlord_expense?, vendor?}

    `bill_key` names one of the lease's configured bills_included entries
    (e.g. "electricity") — the tenants are then charged only their share
    per the lease terms (included -> $0, percentage, fixed, full). Omitted
    or unconfigured -> the full amount is billed (a one-off).

    JOINT leases get ONE household charge for the tenant portion; split
    leases fan the tenant portion into per-tenant UTILITY_CHARGE entries
    weighted by days actually occupied in the period (occupancy log).

    Optionally also books the landlord's own EXPENSE (always the FULL bill
    — what the landlord actually paid the provider) in the same call. The
    expense comes back "not yet taken from bank"; mark it paid via
    /entries/<id>/mark_paid/ when it clears.
    """
    landlord = _landlord(request)

    lease = Lease.objects.filter(
        pk=request.data.get("lease"), landlord=landlord
    ).first()
    if not lease:
        raise ValidationError({"lease": "Not your lease."})

    bill_key = (request.data.get("bill_key") or "").strip() or None
    if bill_key and bill_key not in (lease.bills_included or {}):
        raise ValidationError(
            {"bill_key": f"'{bill_key}' isn't configured on this lease."}
        )

    try:
        entries = split_utility_bill(
            lease=lease,
            total_amount=_decimal(request.data.get("total_amount"), "total_amount"),
            period_start=_date(request.data.get("period_start"), "period_start"),
            period_end=_date(request.data.get("period_end"), "period_end"),
            description=request.data.get("description", "").strip() or "Utility bill",
            due_date=_date(request.data.get("due_date"), "due_date", required=False),
            record_landlord_expense=bool(request.data.get("record_landlord_expense")),
            expense_vendor=request.data.get("vendor", ""),
            bill_key=bill_key,
            created_by=request.user,
        )
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)})

    return Response(
        LedgerEntrySerializer(entries, many=True).data, status=status.HTTP_201_CREATED
    )


# ----------------------------------------------------------------- summary
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def summary_view(request):
    """
    GET /api/ledger/summary/?months=6[&property=<id>]

    Per-month expected vs collected income, expenses, net; plus outstanding/
    overdue, deposits held, and expenses recorded-but-not-yet-cleared — all
    computed from the one ledger. Deposits are excluded from income
    (refundable liabilities).
    """
    landlord = _landlord(request)

    try:
        months_back = min(max(int(request.query_params.get("months", 6)), 1), 24)
    except ValueError:
        months_back = 6

    entries = LedgerEntry.objects.filter(landlord=landlord)
    if request.query_params.get("property"):
        entries = entries.filter(property_id=request.query_params["property"])
    live = entries.not_voided()
    today = date.today()

    def month_starts(n):
        y, m, out = today.year, today.month, []
        for _ in range(n):
            out.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        return list(reversed(out))

    def next_month(d):
        return date(d.year + (d.month // 12), (d.month % 12) + 1, 1)

    monthly = []
    for start in month_starts(months_back):
        end = next_month(start)

        expected = live.filter(
            entry_type__in=INCOME_CHARGE_TYPES, due_date__gte=start, due_date__lt=end
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        collected = live.filter(
            entry_type=EntryType.PAYMENT,
            settles__entry_type__in=INCOME_CHARGE_TYPES,
            effective_date__gte=start,
            effective_date__lt=end,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        spent = live.filter(
            entry_type=EntryType.EXPENSE,
            effective_date__gte=start,
            effective_date__lt=end,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        # Deposits stay out of income (refundable liability) but the UI must
        # be able to say what actually hit the bank this month.
        deposits_in = services.deposits_collected_between(
            landlord, start, end,
            property_id=request.query_params.get("property") or None,
        )

        monthly.append(
            {
                "month": start.strftime("%Y-%m"),
                "label": start.strftime("%b %Y"),
                "expected_income": str(expected),
                "collected_income": str(collected),
                "expenses": str(spent),
                "net": str(collected - spent),
                "deposits_collected": str(deposits_in),
            }
        )

    open_charges = entries.with_settlement().filter(
        entry_type__in=INCOME_CHARGE_TYPES,
        reversed_by__isnull=True,
        due_date__lte=today,
        outstanding__gt=0,
    )
    agg = open_charges.aggregate(total=Sum("outstanding"), count=Count("id"))
    overdue_count = open_charges.filter(due_date__lt=today).count()

    # Recorded, but the money hasn't actually gone yet. Not the same as "spent",
    # and a landlord reconciling against a bank statement needs both numbers.
    unsettled = live.filter(
        entry_type=EntryType.EXPENSE, paid_on__isnull=True
    ).aggregate(total=Sum("amount"), count=Count("id"))

    # Income + deposit payments received in the current calendar month —
    # "what hit the bank", regardless of how the accounting classifies it.
    current = monthly[-1]
    collected_this_month_total = (
        Decimal(current["collected_income"]) + Decimal(current["deposits_collected"])
    )

    return Response(
        {
            "monthly": monthly,
            "outstanding_total": str(agg["total"] or Decimal("0.00")),
            "outstanding_count": agg["count"] or 0,
            "overdue_count": overdue_count,
            "deposits_held": str(services.deposits_held(landlord)),
            "expenses_not_yet_paid": str(unsettled["total"] or Decimal("0.00")),
            "expenses_not_yet_paid_count": unsettled["count"] or 0,
            "collected_this_month_total": str(collected_this_month_total),
            "next_charge": services.next_upcoming_charge(
                landlord,
                property_id=request.query_params.get("property") or None,
            ),
        }
    )
