#!/usr/bin/env python
"""Seed demo records so RAMA non-empty paths can be smoke-tested.

Docker:
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_seed_demo.py

Reset (delete [RAMA-DEMO] entities + this landlord's RAMA chat memory —
DEV ONLY, refuses outside DEBUG):
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_seed_demo.py --reset

Idempotent-ish: skips creating WO/inquiry if a demo marker already exists.
Image fixtures: seeds listings in every photo state (primary-only,
gallery-only, none±lease) so "listings without images" answers are testable.
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

# Smallest valid GIF — enough for ImageField fixtures.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _demo_landlord():
    user = User.objects.filter(email=EMAIL).first()
    if not user or not getattr(user, "landlord_profile", None):
        print(f"No landlord for {EMAIL}")
        return None, None
    return user, user.landlord_profile


def reset() -> int:
    """Delete every [RAMA-DEMO]-marked record plus this landlord's RAMA chat
    memory. Test-fixture hygiene ONLY — the append-only audit principle holds
    for real landlords, so this refuses to run outside DEBUG."""
    from django.conf import settings

    if not settings.DEBUG:
        print("Refusing: --reset is dev-only (settings.DEBUG is off).")
        return 1
    user, landlord = _demo_landlord()
    if not landlord:
        return 1

    from rentium.leases.models import Lease, LeaseDocument
    from rentium.maintenance.models import WorkOrder
    from rentium.messaging.models import Conversation, Message
    from rentium.properties.models import Property
    from rentium.rama.models import RamaAudit, RamaPendingPlan
    from rentium.showcase.models import Inquiry

    n, _ = Message.objects.filter(
        conversation__landlord=landlord, body__contains=MARKER
    ).delete()
    print("messages deleted:", n)
    n, _ = Conversation.objects.filter(
        landlord=landlord, subject__contains=MARKER
    ).delete()
    print("conversations deleted:", n)
    n, _ = WorkOrder.objects.filter(
        property__landlord=landlord, title__contains=MARKER
    ).delete()
    print("work orders deleted:", n)
    n, _ = Inquiry.objects.filter(landlord=landlord, message__contains=MARKER).delete()
    print("inquiries deleted:", n)
    n, _ = LeaseDocument.objects.filter(
        lease__landlord=landlord, title__contains=MARKER
    ).delete()
    print("lease documents deleted:", n)

    # Demo listings: their leases first (drafts and demo leases delete
    # cleanly; anything with payment records raises PROTECT and is kept).
    demo_props = Property.objects.filter(landlord=landlord, name__contains=MARKER)
    for lease in Lease.objects.filter(property__in=demo_props):
        try:
            lease.delete()
        except Exception as exc:  # noqa: BLE001 — keep going, report at end
            print("lease kept (protected):", lease.lease_number, exc)
    deleted = 0
    for prop in list(demo_props):
        try:
            prop.delete()
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print("listing kept (protected):", prop.name, exc)
    print("demo listings deleted:", deleted)

    # RAMA conversation memory for the demo landlord (dev-only wipe).
    n, _ = RamaPendingPlan.objects.filter(landlord=landlord).delete()
    print("pending plans deleted:", n)
    n, _ = RamaAudit.objects.filter(landlord=landlord).delete()
    print("rama audit rows deleted:", n)
    print("Done reset.")
    return 0


def seed_image_fixtures(landlord) -> None:
    """Listings in every photo state, so set questions have known answers:

    - "Room PrimaryPix"  — hero image only
    - "Room GalleryPix"  — gallery image only (must still count as has_images)
    - "Room NoPix Free"  — no images, no lease  → trivially deletable
    - "Room NoPix Leased"— no images, ACTIVE lease → delete blocked (PROTECT)
    """
    from rentium.leases.models import Lease
    from rentium.properties.models import Property, PropertyImage

    def room(name, **extra):
        prop, created = Property.objects.get_or_create(
            landlord=landlord,
            name=f"{MARKER} {name}",
            defaults={
                "address": "950 Demo Ave",
                "city": "Victoria",
                "province": "bc",
                "property_category": Property.PropertyCategory.ROOM,
                "room_type": Property.RoomType.PRIVATE,
                "asking_rent": "900.00",
                **extra,
            },
        )
        print("listing", "created" if created else "exists", prop.name)
        return prop, created

    p1, created = room("Room PrimaryPix")
    if created or not p1.primary_image:
        p1.primary_image.save("demo_primary.gif", ContentFile(TINY_GIF), save=True)

    p2, _ = room("Room GalleryPix")
    if not p2.property_images.exists():
        PropertyImage.objects.create(
            property=p2,
            image=ContentFile(TINY_GIF, name="demo_gallery.gif"),
            caption=f"{MARKER} gallery only",
        )

    room("Room NoPix Free")

    p4, _ = room("Room NoPix Leased")
    if not Lease.objects.filter(property=p4).exists():
        Lease.objects.create(
            landlord=landlord,
            property=p4,
            lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
            status=Lease.LeaseStatus.ACTIVE,
            start_date=date.today() - timedelta(days=30),
            is_month_to_month=True,
            total_rent="900.00",
        )
        print("lease created on", p4.name)


def main() -> int:
    user, landlord = _demo_landlord()
    if not landlord:
        return 1
    seed_image_fixtures(landlord)
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
    raise SystemExit(reset() if "--reset" in sys.argv[1:] else main())
