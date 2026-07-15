"""
Backfill + repair. Safe to re-run; everything it writes is derived.

    python manage.py backfill_public_fields
    python manage.py backfill_public_fields --geocode   # slow, hits Geoapify

What it repairs, and why it matters: the province field used to be free text, so
your existing rows contain "BC", "British Columbia", "bc " and — the killer —
anything mistyped, which normalises to '' and makes the property permanently,
silently unpublishable. This command reports every one it can't resolve, by name,
so you can fix them by hand instead of discovering them six months from now when
a landlord asks why their listing never appeared.
"""

from django.core.management.base import BaseCommand

from rentium.core.phone import to_e164
from rentium.properties.furnishing import compute_is_furnished
from rentium.properties.models import Property
from rentium.properties.models import normalise_province
from rentium.showcase.models import Showcase
from rentium.showcase.tasks import geocode_property
from rentium.users.models import LandlordProfile
from rentium.users.models import User


class Command(BaseCommand):
    help = "Backfill showcases, province codes, slugs, furnishing, phones."

    def add_arguments(self, parser):
        parser.add_argument("--geocode", action="store_true")

    def handle(self, *args, **options):
        # 1. A Showcase row per landlord. PRIVATE by default — creating the row
        #    is bookkeeping, not consent. Nobody becomes visible here.
        made = sum(
            Showcase.objects.get_or_create(landlord=ll)[1]
            for ll in LandlordProfile.objects.all()
        )
        self.stdout.write(f"Showcases created (all private): {made}")

        # 2. Province: free text -> code.
        fixed, unresolved = 0, []
        for prop in Property.objects.all().iterator():
            if prop.province and len(prop.province) == 2:
                continue  # already a code
            code = normalise_province(prop.province)
            if code:
                Property.objects.filter(pk=prop.pk).update(
                    province=code, province_code=code
                )
                fixed += 1
            else:
                unresolved.append(prop)
        self.stdout.write(f"Provinces normalised: {fixed}")

        if unresolved:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(unresolved)} propert{'y' if len(unresolved) == 1 else 'ies'} "
                    "have an unreadable province and CANNOT BE PUBLISHED until it's "
                    "fixed. Re-enter their address in the app and pick from the "
                    "dropdown:"
                )
            )
            for p in unresolved:
                self.stdout.write(f"    #{p.pk}  {p.name}  (province: {p.province!r})")

        # 3. Slugs + city slugs (Property.save() derives them).
        for prop in Property.objects.all().iterator():
            prop.save()
        self.stdout.write("Slugs and city slugs rebuilt.")

        # 4. Furnishing, from inventory.
        furnished = 0
        for prop in Property.objects.all().iterator():
            value = compute_is_furnished(prop)
            if prop.is_furnished != value:
                Property.objects.filter(pk=prop.pk).update(is_furnished=value)
                furnished += int(value)
        self.stdout.write(f"Properties marked furnished: {furnished}")

        # 5. Phones -> E.164. Anything unparseable is BLANKED and reported —
        #    a phone field containing "call me maybe" is worse than an empty one,
        #    because a Twilio call against it fails at 3am instead of at import.
        bad_phones = []
        for user in User.objects.exclude(phone=""):
            try:
                canonical = to_e164(user.phone)
                if canonical != user.phone:
                    User.objects.filter(pk=user.pk).update(phone=canonical)
            except Exception:
                bad_phones.append((user.email, user.phone))
                User.objects.filter(pk=user.pk).update(phone="")
        self.stdout.write("Phone numbers normalised to E.164.")
        if bad_phones:
            self.stdout.write(
                self.style.WARNING(f"\n{len(bad_phones)} unreadable phone(s), cleared:")
            )
            for email, raw in bad_phones:
                self.stdout.write(f"    {email}: {raw!r}")

        # 6. Geocode.
        if options["geocode"]:
            pending = Property.objects.filter(latitude__isnull=True).exclude(address="")
            self.stdout.write(f"Geocoding {pending.count()} propert(ies)...")
            done = sum(1 for p in pending if geocode_property(p.pk))
            self.stdout.write(f"Geocoded: {done}")

        self.stdout.write(self.style.SUCCESS("\nDone. No landlord was made public."))
