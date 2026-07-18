#!/usr/bin/env python
"""Seed demo records so RAMA non-empty paths can be smoke-tested.

Docker:
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_seed_demo.py

Idempotent-ish: skips creating WO/inquiry if a demo marker already exists.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from io import BytesIO

import django

APP = "/app" if os.path.isdir("/app") else os.path.dirname(os.path.dirname(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)
os.chdir(APP)
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgres://debug:debug@postgres:5432/rentium"),
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.core.files.base import ContentFile
from django.utils import timezone

from rentium.users.models import User


EMAIL = os.environ.get("RAMA_EMAIL", "rajgurshersingh@gmail.com")
MARKER = "[RAMA-DEMO]"


def main() -> int:
    user = User.objects.filter(email=EMAIL).first()
    if not user or not getattr(user, "landlord_profile", None):
        print(f"No landlord for {EMAIL}")
        return 1
    landlord = user.landlord_profile
    from rentium.properties.models import Property
    from rentium.leases.models import Lease, LeaseDocument, LeaseTenant
    from rentium.maintenance.models import WorkOrder
    from rentium.showcase.models import Inquiry
    from rentium.messaging.models import Conversation, Message
    from rentium.messaging.services import send_message

    props = list(Property.objects.filter(landlord=landlord).order_by("name"))
    if not props:
        print("No properties")
        return 1
    room_e = next((p for p in props if "Room E" in p.name), props[0])
    garden = next((p for p in props if "Garden" in p.name), props[-1])
    print("Using", room_e.name, garden.name)

    # --- Work order ---
    wo, created = WorkOrder.objects.get_or_create(
        property=room_e,
        title=f"{MARKER} Fix bathroom fan",
        defaults={
            "reported_by": user,
            "description": f"{MARKER} Fan rattles on high. Demo work order for RAMA.",
            "category": WorkOrder.Category.APPLIANCE,
            "priority": WorkOrder.Priority.HIGH,
            "status": WorkOrder.Status.NEW,
            "origin": WorkOrder.Origin.LANDLORD,
        },
    )
    print("work_order", "created" if created else "exists", wo.pk)

    # --- Inquiry ---
    inq, created = Inquiry.objects.get_or_create(
        landlord=landlord,
        property=garden,
        email="demo.lead@example.com",
        defaults={
            "name": "Demo Lead",
            "phone": "+1 250-555-0199",
            "message": f"{MARKER} Interested in the garden suite for September.",
            "move_in_target": date.today() + timedelta(days=45),
            "status": Inquiry.Status.NEW,
        },
    )
    print("inquiry", "created" if created else "exists", inq.pk)

    # --- Active lease / tenant messaging ---
    lease = (
        Lease.objects.filter(landlord=landlord, status=Lease.LeaseStatus.ACTIVE)
        .select_related("property")
        .order_by("-start_date")
        .first()
    )
    lt = None
    if lease:
        lt = (
            LeaseTenant.objects.filter(lease=lease)
            .select_related("tenant__user")
            .order_by("-is_primary_tenant")
            .first()
        )

    if lease and lt and lt.tenant_id:
        conv, _ = Conversation.objects.get_or_create(
            landlord=landlord,
            tenant=lt.tenant,
            lease=lease,
            defaults={"subject": f"{MARKER} Move-in questions"},
        )
        # Tenant → landlord unread message
        if not Message.objects.filter(
            conversation=conv, body__contains=MARKER
        ).exists():
            tenant_user = lt.tenant.user
            send_message(
                conv,
                tenant_user,
                f"{MARKER} Hi — what time should I arrive on move-in day?",
            )
            print("message seeded from tenant", tenant_user.email)
        else:
            print("message exists")
    else:
        print("skip message — no linked tenant user on active lease")

    # --- Agreement PDF + attachment ---
    if lease:
        if not lease.document_file:
            pdf_bytes = (
                b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
                + MARKER.encode()
            )
            lease.document_file.save(
                f"rama_demo_agreement_{lease.pk}.pdf",
                ContentFile(pdf_bytes),
                save=True,
            )
            print("lease document_file saved")
        else:
            print("lease document_file already set")

        if not LeaseDocument.objects.filter(
            lease=lease, title__contains=MARKER
        ).exists():
            doc = LeaseDocument(
                lease=lease,
                title=f"{MARKER} House rules PDF",
                description="Demo attachment for RAMA list_documents",
                is_signed=False,
            )
            doc.document.save(
                "rama_demo_rules.pdf",
                ContentFile(b"%PDF-1.4 demo house rules " + MARKER.encode()),
                save=False,
            )
            doc.save()
            print("LeaseDocument created", doc.pk)
        else:
            print("LeaseDocument exists")

    # --- Condition inspection with checklist (if template seeded) ---
    try:
        from rentium.leases.inspection_services import build_inspection, InspectionError
        from rentium.leases.inspections import ConditionInspection, ConditionCode

        if lease and not ConditionInspection.objects.filter(lease=lease).exists():
            try:
                # Room lease may need lease_tenant
                insp = build_inspection(
                    lease=lease,
                    lease_tenant=lt if lt else None,
                    created_by=user,
                )
            except InspectionError as exc:
                # Try without lease_tenant for complete units
                try:
                    insp = build_inspection(lease=lease, created_by=user)
                except InspectionError as exc2:
                    print("inspection skip:", exc, exc2)
                    insp = None
            if insp:
                # Prefill a few checklist lines for richer smoke
                items = list(insp.items.all()[:8])
                for i, item in enumerate(items):
                    item.move_in_condition_code = (
                        ConditionCode.GOOD if i % 3 else ConditionCode.FAIR
                    )
                    item.move_in_cleanliness_code = ""
                    if i == 0:
                        item.needs_attention = True
                        item.move_in_comment = f"{MARKER} scuff near door"
                    item.save()
                print("inspection created", insp.pk, "items", insp.items.count())
        else:
            print("inspection exists or no lease")
    except Exception as exc:  # noqa: BLE001
        print("inspection error:", exc)

    print("Done seed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
