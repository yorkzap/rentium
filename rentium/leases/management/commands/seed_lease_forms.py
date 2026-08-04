"""
Seed the system form catalogue.

Idempotent on `code`: safe to re-run on every deploy. Re-running refreshes
metadata and, when the shipped file's checksum has changed, re-imports the file
and re-derives placements — which is how a new revision of a government form
gets picked up. Existing LeaseForm rows are untouched: they carry their own
frozen placements_snapshot, so a template refresh can never alter a document
somebody has already signed.

## Why only one form has a file

A government form is a legal instrument with a revision date printed on it. It
is not something to reproduce from memory: shipping "RTB-26" with the wrong
title, the wrong revision, or the wrong field layout would be worse than not
shipping it, because the landlord would send it believing it was current.

So the catalogue ships exactly what has been verified against the actual
document: RTB-8 (2025/07), whose file lives beside this command and whose 25
form fields were read out of the PDF itself. Everything else is seeded as
COMING_SOON — named only where this codebase already attests to the number and
title (clauses.py cites RTB-1 and RTB-30; inspections.py is built on RTB-27) —
so a landlord can see what is planned without being handed a form we have not
checked. To promote one: drop its PDF in `form_templates/<province>/`, set
`file`, verify the placements it derives, and flip availability.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.leases import form_render
from rentium.leases.form_services import _create_placements
from rentium.leases.lease_forms import FormStage
from rentium.leases.lease_forms import LeaseFormPlacement
from rentium.leases.lease_forms import LeaseFormTemplate
from rentium.leases.lease_forms import SignerRole

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "form_templates"

Availability = LeaseFormTemplate.Availability

#: RTB-8's own AcroForm field names, mapped to who fills each box and where the
#: value comes from. Read out of the PDF, not guessed: the landlord block is
#: fields 1-8, the tenant block repeats them with a `_2` suffix, and the three
#: /Sig widgets sit left-to-right as landlord, tenant, tenant.
RTB8_FIELDS: dict[str, tuple[str, int, str]] = {
    # key:                        (signer_role,           index, auto_source)
    "first_and_middle_names": (SignerRole.LANDLORD, 0, "landlord.first_name"),
    "last_name_s": (SignerRole.LANDLORD, 0, "landlord.last_name"),
    # The landlord's ADDRESS FOR SERVICE, which is not the rental unit unless
    # they happen to live in it. Rentium holds no landlord mailing address, so
    # these are left blank for the landlord to type rather than asserting the
    # property as theirs — which printed the same address in both blocks and
    # read, correctly, as the tenant's details having been overwritten.
    "siteunit": (SignerRole.LANDLORD, 0, ""),
    "street__and_name": (SignerRole.LANDLORD, 0, ""),
    "city": (SignerRole.LANDLORD, 0, ""),
    "province": (SignerRole.LANDLORD, 0, ""),
    "postal_code": (SignerRole.LANDLORD, 0, ""),
    "main_phone": (SignerRole.LANDLORD, 0, "landlord.phone"),
    "other_phone": (SignerRole.LANDLORD, 0, ""),
    "tenant_first_and_middle": (SignerRole.TENANT, 0, "tenant.first_name"),
    "last_name_s_2": (SignerRole.TENANT, 0, "tenant.last_name"),
    "siteunit_2": (SignerRole.TENANT, 0, ""),
    "street__and_name_2": (SignerRole.TENANT, 0, "property.address"),
    "city_2": (SignerRole.TENANT, 0, "property.city"),
    "province_2": (SignerRole.TENANT, 0, "property.province"),
    "postal_code_2": (SignerRole.TENANT, 0, "property.postal_code"),
    "main_phone_2": (SignerRole.TENANT, 0, "tenant.phone"),
    "other_phone_2": (SignerRole.TENANT, 0, ""),
    "time": (SignerRole.LANDLORD, 0, ""),
    "date": (SignerRole.LANDLORD, 0, ""),
    "signature6": (SignerRole.LANDLORD, 0, ""),
    "signature4": (SignerRole.TENANT, 0, ""),
    "signature2": (SignerRole.TENANT, 1, ""),
    "text2": (SignerRole.LANDLORD, 0, "today"),
    "time_": (SignerRole.LANDLORD, 0, ""),
}

#: Boxes RTB-8 genuinely requires. Everything else is stated if known and left
#: blank if not — a mutual agreement is valid without an "other phone".
#:
#: `date` and `time` are the day and hour the tenant agrees to vacate. They are
#: the whole point of the form, and form_services.send_form refuses to send it
#: while either is blank rather than letting a tenancy be ended on no date.
RTB8_REQUIRED = {
    "signature6",
    "signature4",
    "time",
    "date",
    "first_and_middle_names",
    "last_name_s",
    "tenant_first_and_middle",
    "last_name_s_2",
}

#: A second tenant's signature box only becomes required when a second tenant is
#: actually on the lease; a one-tenant tenancy must not be stuck waiting for a
#: signature nobody owes.
RTB8_OPTIONAL_SIGNATURES = {"signature2"}


SYSTEM_FORMS: tuple[dict, ...] = (
    {
        "code": "BC_RTB8",
        "name": "Mutual Agreement to End a Tenancy (RTB-8)",
        "purpose": (
            "Ends a BC tenancy on a date both parties agree to, in writing. It is "
            "NOT a Notice to End Tenancy: nobody is obliged to sign it, and signing "
            "gives up any compensation a notice would have carried."
        ),
        "jurisdiction": "BC",
        "stage": FormStage.MOVE_OUT,
        "binds_to": "moveout",
        "availability": Availability.AVAILABLE,
        "path": TEMPLATE_DIR / "bc" / "rtb8.pdf",
    },
    # --- Known, not shipped. Titles taken from this codebase's own citations. ---
    {
        "code": "BC_RTB1",
        "name": "Residential Tenancy Agreement (RTB-1)",
        "purpose": (
            "BC's standard tenancy agreement. Rentium already generates this "
            "agreement itself (leases/documents.py), so the official form is only "
            "needed when a landlord wants the government's own paper."
        ),
        "jurisdiction": "BC",
        "stage": FormStage.WITH_LEASE,
        "availability": Availability.COMING_SOON,
    },
    {
        "code": "BC_RTB27",
        "name": "Condition Inspection Report (RTB-27)",
        "purpose": (
            "Move-in and move-out condition record. Rentium has a built-in "
            "inspection workflow modelled on this form (leases/inspections.py)."
        ),
        "jurisdiction": "BC",
        "stage": FormStage.ADDENDUM,
        "availability": Availability.COMING_SOON,
    },
    {
        "code": "BC_RTB30",
        "name": "10 Day Notice to End Tenancy for Unpaid Rent or Utilities (RTB-30)",
        "purpose": (
            "Served by a landlord when rent or utilities are unpaid. A notice, not "
            "an agreement — the tenant does not sign it."
        ),
        "jurisdiction": "BC",
        "stage": FormStage.MOVE_OUT,
        "availability": Availability.COMING_SOON,
    },
)


class Command(BaseCommand):
    help = "Create or refresh the system lease-form catalogue (BC RTB-8 and friends)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-import files and re-derive placements even if unchanged.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = skipped = 0
        for spec in SYSTEM_FORMS:
            outcome = self._seed(spec, force=options["force"])
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                skipped += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Lease form catalogue: {created} created, {updated} updated, "
                f"{skipped} unchanged."
            )
        )

    def _seed(self, spec: dict, *, force: bool) -> str:
        template = LeaseFormTemplate.objects.filter(
            landlord__isnull=True, code=spec["code"]
        ).first()
        is_new = template is None
        if is_new:
            template = LeaseFormTemplate(landlord=None, code=spec["code"])

        template.name = spec["name"]
        template.purpose = spec["purpose"]
        template.jurisdiction = spec.get("jurisdiction", "")
        template.stage = spec["stage"]
        template.binds_to = spec.get("binds_to", "")
        template.availability = spec["availability"]
        template.source = LeaseFormTemplate.Source.SYSTEM
        template.is_active = True

        path: Path | None = spec.get("path")
        file_changed = False
        if path is not None:
            if not path.exists():
                raise FileNotFoundError(f"Missing catalogue file: {path}")
            data = form_render.normalise_pdf(path.read_bytes())
            digest = hashlib.sha256(data).hexdigest()
            if force or digest != template.sha256:
                info = form_render.inspect_pdf(data)
                template.original_filename = path.name
                template.sha256 = digest
                template.byte_size = len(data)
                template.page_count = info["page_count"]
                template.page_sizes = info["page_sizes"]
                template.file.save(path.name, ContentFile(data), save=False)
                file_changed = True

        template.save()

        if file_changed:
            self._place_fields(template, data, spec)
        if is_new:
            return "created"
        return "updated" if file_changed else "unchanged"

    def _place_fields(self, template: LeaseFormTemplate, data: bytes, spec: dict):
        """Derive boxes from the PDF, then assign roles from the verified map."""
        rows = form_render.placements_from_acroform(form_render.inspect_pdf(data))
        overrides = RTB8_FIELDS if spec["code"] == "BC_RTB8" else {}

        for row in rows:
            role, index, source = overrides.get(
                row["key"], (row["signer_role"], row["signer_index"], "")
            )
            row["signer_role"] = role
            row["signer_index"] = index
            row["auto_source"] = source
            if overrides:
                row["required"] = row["key"] in RTB8_REQUIRED
            # A signature box for a party who may not exist is still a real box
            # someone can sign — it just must not hold the form hostage.
            if row["key"] in RTB8_OPTIONAL_SIGNATURES:
                row["required"] = False
                row["kind"] = LeaseFormPlacement.Kind.SIGNATURE

        count = _create_placements(template, rows)
        self.stdout.write(f"  {template.code}: {count} field(s) placed")
