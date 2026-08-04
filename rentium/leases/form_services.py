"""
The one place lease forms are created, sent, signed, and executed.

REST, RAMA, the public signing link and the move-out workflow are all adapters
over these functions — the shared-application-service boundary the architecture
notes describe for `create_lease_record` and `schedule_viewing`. Four callers
with four copies of "is this form finished?" is how a lease ends up active with
an unsigned addendum on it.

## The three questions this module answers

**Who signs?** Placements are made against a ROLE and an INDEX (TENANT/0,
LANDLORD/0) because a landlord places boxes long before they know who is
moving in. `send_form` is where a slot becomes a person: from the lease roster
if there is one, or from a name and email the landlord types if there is not.
That ordering is the whole reason a landlord can prepare a form pack on an empty
draft lease.

**What does the signer see?** The blank PDF plus every value already known —
their name, the address, the vacate date. Prefill comes only from AUTO_SOURCES,
a whitelist. An unrecognised `auto_source` raises rather than rendering blank:
a form with a silently empty name field is a form someone signs without noticing
it is wrong.

**When is it done?** Every required signer has signed. At that moment the form
is stamped once, hashed, and stored. It is never re-rendered — the same
freeze-at-execution rule `capture_signed_document` applies to the lease.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import form_render
from .form_intel import suggest_form_purpose
from .lease_forms import FormStage
from .lease_forms import LeaseForm
from .lease_forms import LeaseFormEvent
from .lease_forms import LeaseFormPlacement
from .lease_forms import LeaseFormSignature
from .lease_forms import LeaseFormSigner
from .lease_forms import LeaseFormTemplate
from .lease_forms import SignerRole

logger = logging.getLogger(__name__)

MAX_TEMPLATE_BYTES = 25 * 1024 * 1024
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
SIGN_TOKEN_TTL_DAYS = 30

FILE_REQUIRED = _("A PDF file is required.")
FILE_TOO_LARGE = _("That form is too large (maximum 25 MB).")
NOT_A_PDF = _("Lease forms must be PDFs. Convert the file and try again.")


class FormError(ValidationError):
    """Anything a landlord or signer did that we can explain in one sentence."""


# ---------------------------------------------------------------------------
# Prefill
# ---------------------------------------------------------------------------


def _tenant_phone(lease_tenant) -> str:
    if lease_tenant.tenant_id:
        phone = getattr(lease_tenant.tenant.user, "phone", "") or ""
        if phone:
            return phone
    return lease_tenant.invited_phone or ""


def _tenant_email(lease_tenant) -> str:
    if lease_tenant.tenant_id:
        return lease_tenant.tenant.user.email or ""
    return lease_tenant.invited_email or ""


def _property_of(lease):
    return getattr(lease, "property", None)


def _landlord_user(lease):
    landlord = getattr(lease, "landlord", None)
    return getattr(landlord, "user", None) if landlord else None


@dataclass(frozen=True)
class Party:
    """Whoever a given box belongs to — a tenant, a co-landlord, a guarantor."""

    name: str = ""
    email: str = ""
    phone: str = ""


#: Whitelisted prefill keys. A placement's `auto_source` must be one of these
#: or attaching the form fails loudly — see the module docstring.
#:
#: Each entry takes (lease, party) and returns a string. `party` is whoever the
#: BOX belongs to, resolved from the lease roster by the placement's own role
#: and index — not from whoever happens to be signing. Keying it on the signer
#: meant nothing party-scoped resolved until send_form created signer rows, so
#: RTB-8's tenant block sat empty on a lease whose tenant was right there in the
#: roster. The landlord was then asked to type it, into a box labelled
#: identically to their own, and their name went into the tenant's block.
AUTO_SOURCES: dict[str, object] = {
    "tenant.display_name": lambda lease, party: party.name,
    # For forms that split a party across two boxes. Naive split — see _surname.
    "tenant.first_name": lambda lease, party: _given_names(party.name),
    "tenant.last_name": lambda lease, party: _surname(party.name),
    "tenant.email": lambda lease, party: party.email,
    "tenant.phone": lambda lease, party: party.phone,
    "signer.name": lambda lease, party: party.name,
    "signer.first_name": lambda lease, party: _given_names(party.name),
    "signer.last_name": lambda lease, party: _surname(party.name),
    "signer.email": lambda lease, party: party.email,
    "signer.phone": lambda lease, party: party.phone,
    "landlord.display_name": lambda lease, party: (
        getattr(_landlord_user(lease), "name", "") or ""
    ),
    "landlord.first_name": lambda lease, party: _given_names(
        getattr(_landlord_user(lease), "name", "") or ""
    ),
    "landlord.last_name": lambda lease, party: _surname(
        getattr(_landlord_user(lease), "name", "") or ""
    ),
    "landlord.email": lambda lease, party: (
        getattr(_landlord_user(lease), "email", "") or ""
    ),
    "landlord.phone": lambda lease, party: (
        getattr(_landlord_user(lease), "phone", "") or ""
    ),
    "property.address": lambda lease, party: (
        getattr(_property_of(lease), "address", "") or ""
    ),
    "property.city": lambda lease, party: (
        getattr(_property_of(lease), "city", "") or ""
    ),
    # Stored lowercase ("bc") for slugs and lookups; a government form prints
    # the province code.
    "property.province": lambda lease, party: (
        getattr(_property_of(lease), "province", "") or ""
    ).upper(),
    "property.postal_code": lambda lease, party: (
        getattr(_property_of(lease), "postal_code", "") or ""
    ),
    "lease.number": lambda lease, party: lease.lease_number or "",
    "lease.start_date": lambda lease, party: _date(lease.start_date),
    "lease.end_date": lambda lease, party: _date(lease.end_date),
    "lease.rent": lambda lease, party: (
        f"{lease.total_rent:.2f}" if lease.total_rent else ""
    ),
    "moveout.end_date": lambda lease, party: "",  # filled by bind_moveout_values
    "today": lambda lease, party: _date(timezone.localdate()),
}


def _date(value) -> str:
    """DD/MM/YYYY — what BC RTB forms print above their date boxes."""
    return value.strftime("%d/%m/%Y") if value else ""


def _given_names(full_name: str) -> str:
    """Everything before the last word. See _surname for the caveat."""
    parts = str(full_name or "").split()
    return " ".join(parts[:-1]) if len(parts) > 1 else ""


def _surname(full_name: str) -> str:
    """The last word of a name.

    Naive, and knowingly so. "Maria de la Cruz" comes out as "Cruz", and there is
    no rule that gets that right for every name on earth. It is used only where a
    form splits a party across "first and middle name(s)" and "Last name(s)" —
    RTB-8 does — and the split lands in `values`, which the landlord reviews and
    can correct before sending. A proposal they can see and fix beats either a
    blank required box or the same full name printed twice.
    """
    parts = str(full_name or "").split()
    return parts[-1] if parts else ""


def party_for(form: LeaseForm, row: dict) -> Party:
    """Who a given box belongs to, from the lease itself.

    Read off the placement's own role and index — TENANT/0 is "the first tenant
    on this lease" — so a form knows the tenant's name the moment it is
    attached, long before anybody is sent a link. A bound signer wins when one
    exists, because that is the person who was actually invited.
    """
    role = str(row.get("signer_role") or "")
    index = int(row.get("signer_index") or 0)

    signer = next(
        (s for s in form.signers.all() if s.role == role and s.order == index), None
    )
    if signer is not None:
        phone = (
            _tenant_phone(signer.lease_tenant) if signer.lease_tenant_id else ""
        ) or (getattr(signer.user, "phone", "") or "")
        return Party(name=signer.display_name, email=signer.email, phone=phone)

    details = roster_candidates(form.lease).get((role, index))
    if not details:
        return Party()
    return Party(
        name=details.get("name") or "",
        email=details.get("email") or "",
        phone=details.get("phone") or "",
    )


def resolve_auto_values(form: LeaseForm, signer: LeaseFormSigner | None = None) -> dict:
    """Every placement value we can derive right now, keyed by placement key.

    `signer` is accepted for callers that have one to hand; the party is worked
    out per box regardless, so passing it is an optimisation rather than the
    thing that makes tenant details appear.
    """
    resolved: dict[str, str] = {}
    for row in form.placements_snapshot:
        source = (row.get("auto_source") or "").strip()
        if not source:
            continue
        if source not in AUTO_SOURCES:
            raise FormError(
                _("This form asks to prefill from '%(source)s', which isn't a field "
                  "Rentium knows. Fix the field on the form template.")
                % {"source": source}
            )
        value = AUTO_SOURCES[source](form.lease, party_for(form, row))
        if value:
            resolved[row["key"]] = str(value)
    return resolved


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def record_form_event(
    form: LeaseForm,
    kind: str,
    *,
    signer: LeaseFormSigner | None = None,
    actor=None,
    metadata: dict | None = None,
    debounce_seconds: int = 0,
) -> LeaseFormEvent | None:
    """Append one immutable lifecycle row.

    `debounce_seconds` exists for LINK_OPENED: a signing page re-fetches on
    every mount, and an audit trail that says "opened 40 times" is noise
    pretending to be evidence. Same reasoning as record_invite_event.
    """
    if debounce_seconds:
        since = timezone.now() - timedelta(seconds=debounce_seconds)
        recent = form.events.filter(kind=kind, created_at__gte=since)
        if signer is not None:
            recent = recent.filter(signer=signer)
        if recent.exists():
            return None
    return LeaseFormEvent.objects.create(
        form=form,
        signer=signer,
        kind=kind,
        actor=actor if getattr(actor, "pk", None) else None,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _read_upload(upload) -> bytes:
    if not upload:
        raise FormError(FILE_REQUIRED)
    upload.seek(0)
    data = upload.read(MAX_TEMPLATE_BYTES + 1)
    upload.seek(0)
    if not data:
        raise FormError(_("That file is empty."))
    if len(data) > MAX_TEMPLATE_BYTES:
        raise FormError(FILE_TOO_LARGE)
    # Trust the bytes, not the browser's content-type header: a PDF renamed by
    # a scanner or forwarded through Telegram often arrives as
    # application/octet-stream.
    if data[:5] != b"%PDF-":
        raise FormError(NOT_A_PDF)
    return data


@transaction.atomic
def upload_template(
    landlord,
    upload,
    *,
    name: str = "",
    purpose: str = "",
    stage: str = "",
    created_by=None,
    source_label: str = "",
) -> tuple[LeaseFormTemplate, bool]:
    """Store a landlord's own blank form. Returns (template, created).

    Content-addressed and idempotent per landlord, the same way
    `rama.document_services.ingest_document` is: re-uploading the identical file
    returns the existing template rather than making a second one with the same
    placements to maintain.
    """
    data = _read_upload(upload)
    data = form_render.normalise_pdf(data)
    digest = hashlib.sha256(data).hexdigest()

    existing = LeaseFormTemplate.objects.filter(
        landlord=landlord, sha256=digest
    ).first()
    if existing:
        return existing, False

    info = form_render.inspect_pdf(data)
    filename = Path(getattr(upload, "name", "") or "form.pdf").name[:255]
    display_name = (name or "").strip() or Path(filename).stem.replace("_", " ").title()

    template = LeaseFormTemplate(
        landlord=landlord,
        name=display_name[:200],
        purpose=(purpose or "").strip(),
        source=LeaseFormTemplate.Source.CUSTOM,
        stage=stage or FormStage.UNCLASSIFIED,
        availability=LeaseFormTemplate.Availability.AVAILABLE,
        original_filename=filename,
        sha256=digest,
        byte_size=len(data),
        page_count=info["page_count"],
        page_sizes=info["page_sizes"],
        # Born-digital forms carry their own text, so the cheap read is enough
        # to work out what this is. Scans fall through to OCR below.
        ocr_text=form_render.extract_text(data),
        created_by=created_by if getattr(created_by, "pk", None) else None,
    )
    template.file.save(filename, ContentFile(data), save=False)
    template.full_clean(exclude=["file"])
    template.save()

    _create_placements(template, form_render.placements_from_acroform(info))
    classify_template(template, field_names=[f["name"] for f in info["acroform_fields"]])

    if len(template.ocr_text) < form_render.TEXT_LAYER_FLOOR:
        _queue_ocr(template)
    return template, True


def _queue_ocr(template: LeaseFormTemplate) -> None:
    """Hand a scanned form to the OCR pipeline, or do it inline in tests.

    A scan says nothing until it has been read, and a form we cannot describe
    is a form RAMA has to ask a vague question about. Queued rather than run
    inline because OCRmyPDF takes seconds to minutes and an upload must return
    immediately.
    """
    from .tasks import ocr_lease_form_template

    try:
        ocr_lease_form_template.delay(str(template.pk))
    except Exception:  # noqa: BLE001 - no broker in tests / dev shells
        logger.warning("OCR could not be queued for form template %s", template.pk)


def _create_placements(template: LeaseFormTemplate, rows: list[dict]) -> int:
    LeaseFormPlacement.objects.filter(template=template).delete()
    created = []
    for order, row in enumerate(rows):
        placement = LeaseFormPlacement(
            template=template,
            key=row["key"],
            label=row.get("label", "")[:200],
            page=row.get("page", 0),
            x=row["x"],
            y=row["y"],
            width=row["width"],
            height=row["height"],
            kind=row["kind"],
            signer_role=row.get("signer_role") or SignerRole.TENANT,
            signer_index=row.get("signer_index") or 0,
            auto_source=row.get("auto_source", ""),
            required=bool(row.get("required", True)),
            font_size=row.get("font_size") or 10.0,
            order=row.get("order", order),
        )
        placement.full_clean()
        created.append(placement)
    LeaseFormPlacement.objects.bulk_create(created)
    return len(created)


@transaction.atomic
def set_placements(template: LeaseFormTemplate, rows: list[dict]) -> int:
    """Replace a template's whole placement set (the placement editor's save).

    Whole-set replacement rather than per-row edits because the editor is a
    canvas: the landlord's mental model is "this is where the boxes are now",
    and diffing individual boxes would let a half-applied save leave a form with
    two signature boxes for the same person.
    """
    for row in rows:
        source = (row.get("auto_source") or "").strip()
        if source and source not in AUTO_SOURCES:
            raise FormError(
                _("'%(source)s' isn't a field Rentium can prefill.")
                % {"source": source}
            )
        if row.get("page", 0) >= max(template.page_count, 1):
            raise FormError(
                _("This form only has %(count)s page(s).")
                % {"count": template.page_count}
            )
    count = _create_placements(template, rows)
    template.save(update_fields=["updated_at"])
    return count


def classify_template(
    template: LeaseFormTemplate, *, field_names: list[str] | None = None
) -> LeaseFormTemplate:
    """Store a purpose SUGGESTION. Never touches `stage` — see form_intel."""
    suggestion = suggest_form_purpose(
        template.ocr_text,
        field_names or list(template.placements.values_list("label", flat=True)),
        template.original_filename,
    )
    template.suggested_stage = suggestion.stage or ""
    template.suggested_purpose = suggestion.purpose or ""
    template.suggestion_signals = suggestion.as_dict()
    template.save(
        update_fields=[
            "suggested_stage",
            "suggested_purpose",
            "suggestion_signals",
            "updated_at",
        ]
    )
    return template


def catalog_for(landlord, *, jurisdiction: str = "") -> list[LeaseFormTemplate]:
    """Every form this landlord may see: system entries plus their own uploads.

    COMING_SOON rows are included deliberately. A landlord who cannot find
    RTB-26 needs to know we know it exists and have not shipped it, which is a
    different message from an empty list.
    """
    from django.db.models import Q

    query = Q(landlord__isnull=True) | Q(landlord=landlord)
    rows = LeaseFormTemplate.objects.filter(query, is_active=True)
    if jurisdiction:
        rows = rows.filter(Q(jurisdiction="") | Q(jurisdiction__iexact=jurisdiction))
    return list(rows.order_by("availability", "jurisdiction", "name"))


# ---------------------------------------------------------------------------
# Attaching
# ---------------------------------------------------------------------------


def _lease_is_live(lease) -> bool:
    from .models import Lease

    return lease.status == Lease.LeaseStatus.ACTIVE


@transaction.atomic
def attach_form(
    lease,
    template: LeaseFormTemplate,
    *,
    actor=None,
    title: str = "",
    required: bool = True,
    moveout_request=None,
    created_via: str = LeaseForm.CreatedVia.WEB,
    source_attachment_id: str = "",
) -> LeaseForm:
    """Bind a blank template to one lease, freezing its placements.

    Placements are snapshotted rather than referenced. Editing a template later
    is a normal thing to do — a landlord fixes a misplaced box — and it must not
    reach back into a form somebody has already been asked to sign.
    """
    if not template.is_selectable:
        raise FormError(
            _("%(name)s isn't available to attach yet.") % {"name": template.name}
        )
    if template.landlord_id and template.landlord_id != lease.landlord_id:
        raise FormError(_("That form belongs to a different landlord."))
    if template.stage == FormStage.UNCLASSIFIED:
        raise FormError(
            _("Tell Rentium what %(name)s is for before attaching it: signed with "
              "the lease, any time during the tenancy, or to end the tenancy.")
            % {"name": template.name}
        )

    placements = [row.as_dict() for row in template.placements.all()]
    if not placements:
        raise FormError(
            _("%(name)s has no fields on it yet — place at least one signature "
              "box before sending it to anyone.") % {"name": template.name}
        )

    # A WITH_LEASE form only holds up a lease that has not started yet. Attaching
    # one to a live tenancy records an obligation and raises an attention item;
    # it must never drag an ACTIVE lease backwards, because rent is already
    # being charged against it and occupancy is already open.
    blocks = (
        required
        and template.stage == FormStage.WITH_LEASE
        and not _lease_is_live(lease)
    )

    form = LeaseForm(
        lease=lease,
        template=template,
        moveout_request=moveout_request,
        title=(title or template.name)[:255],
        required=required,
        blocks_activation=blocks,
        placements_snapshot=placements,
        created_via=created_via,
        source_attachment_id=str(source_attachment_id or "")[:64],
        created_by=actor if getattr(actor, "pk", None) else None,
    )
    form.save()

    form.values = resolve_auto_values(form)
    if moveout_request is not None:
        form.values.update(_moveout_values(form, moveout_request))
    form.save(update_fields=["values", "updated_at"])

    record_form_event(
        form,
        LeaseFormEvent.Kind.CREATED,
        actor=actor,
        metadata={
            "template": str(template.pk),
            "stage": str(template.stage),
            "blocks_activation": blocks,
            "via": created_via,
        },
    )
    return form


def _moveout_values(form: LeaseForm, moveout) -> dict:
    """Vacate date/time boxes filled from the move-out request.

    Matched by placement KIND and key rather than by a fixed field name, so this
    works for RTB-8 and for a landlord's own end-of-tenancy form without either
    of them having to use BC's field names.
    """
    values: dict[str, str] = {}
    end = moveout.effective_end_date or moveout.requested_end_date
    if not end:
        return values
    for row in form.placements_snapshot:
        key = (row.get("key") or "").casefold()
        label = (row.get("label") or "").casefold()
        if row.get("kind") == LeaseFormPlacement.Kind.DATE and (
            "dd" in label or key in {"date", "end_date", "vacate_date"}
        ):
            values[row["key"]] = _date(end)
        elif "time" in key or "time" in label:
            values.setdefault(row["key"], "1:00 PM")
    return values


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def required_roles(form: LeaseForm) -> list[tuple[str, int]]:
    """Every (role, index) slot that has at least one required box on it."""
    slots: list[tuple[str, int]] = []
    for row in form.placements_snapshot:
        if not row.get("required"):
            continue
        if row.get("kind") not in {
            LeaseFormPlacement.Kind.SIGNATURE,
            LeaseFormPlacement.Kind.INITIALS,
        }:
            continue
        slot = (row.get("signer_role"), int(row.get("signer_index") or 0))
        if slot not in slots:
            slots.append(slot)
    return slots


#: The prefill key that marks a date box as "the date this was signed", as
#: opposed to a date that is part of the agreement's content.
DATE_SIGNED_SOURCE = "today"


def is_signed_at_signing(row: dict) -> bool:
    """Whether this box is filled BY the act of signing.

    Not every DATE box is a date-signed box. RTB-8 has two: "Date signed", and
    the DD/MM/YYYY the tenant agrees to vacate. Treating them the same is how a
    mutual agreement to end a tenancy ends up dated today rather than on the day
    the parties actually agreed to — a wrong date on a legal form, arrived at by
    the system rather than by anyone. `auto_source=today` is what distinguishes
    them.
    """
    kind = row.get("kind")
    if kind in {LeaseFormPlacement.Kind.SIGNATURE, LeaseFormPlacement.Kind.INITIALS}:
        return True
    return (
        kind == LeaseFormPlacement.Kind.DATE
        and (row.get("auto_source") or "") == DATE_SIGNED_SOURCE
    )


def unfilled_required_rows(form: LeaseForm) -> list[dict]:
    """Required boxes that nobody fills at signing time and that are still blank.

    Signature, initials and date-signed boxes are excluded: those are filled BY
    the act of signing. What is left is content — a vacate date, a name, an
    amount — and if it is still empty when the form goes out, it goes out blank.
    """
    values = form.values or {}
    return [
        row
        for row in form.placements_snapshot
        if row.get("required")
        and not is_signed_at_signing(row)
        and not str(values.get(row["key"]) or "").strip()
    ]


_ROLE_POSSESSIVE = {
    SignerRole.LANDLORD: "Landlord's",
    SignerRole.CO_LANDLORD: "Co-landlord's",
    SignerRole.TENANT: "Tenant's",
}


def unfilled_required_fields(form: LeaseForm) -> list[str]:
    """The same boxes, named the way a landlord would recognise them.

    A form's labels are not unique — RTB-8 prints "first and middle name(s)"
    over both the landlord block and the tenant block — so a repeated label is
    qualified by whose block it is in. Without that, the message tells someone to
    fill in a field they are looking straight at with text already in it.

    Labels that appear once are left alone: "Landlord's time" would be a worse
    name for the hour the tenant vacates than "time" is.
    """
    seen: dict[str, int] = {}
    for row in form.placements_snapshot:
        label = row.get("label") or row["key"]
        seen[label] = seen.get(label, 0) + 1

    names: list[str] = []
    for row in unfilled_required_rows(form):
        label = row.get("label") or row["key"]
        if seen.get(label, 0) > 1:
            who = _ROLE_POSSESSIVE.get(str(row.get("signer_role") or ""), "")
            label = f"{who} {label}".strip()
        names.append(label)
    return names


def roster_candidates(lease) -> dict[tuple[str, int], dict]:
    """The lease's own parties, keyed by the slot they would fill."""
    candidates: dict[tuple[str, int], dict] = {}

    landlord_user = _landlord_user(lease)
    candidates[(SignerRole.LANDLORD, 0)] = {
        "user": landlord_user,
        "name": getattr(landlord_user, "name", "") or "",
        "email": getattr(landlord_user, "email", "") or "",
        "phone": getattr(landlord_user, "phone", "") or "",
    }
    for index, signatory in enumerate(lease.landlord_signatories.order_by("created_at")):
        candidates[(SignerRole.CO_LANDLORD, index)] = {
            "landlord_signatory": signatory,
            "user": signatory.member,
            "name": signatory.name,
            "email": signatory.email,
            "phone": signatory.phone or "",
        }
    tenants = lease.lease_tenants.filter(declined=False).order_by("created_at")
    for index, lease_tenant in enumerate(tenants):
        candidates[(SignerRole.TENANT, index)] = {
            "lease_tenant": lease_tenant,
            "user": lease_tenant.tenant.user if lease_tenant.tenant_id else None,
            "name": lease_tenant.display_name,
            "email": _tenant_email(lease_tenant),
            "phone": _tenant_phone(lease_tenant),
        }
    return candidates


@transaction.atomic
def send_form(
    form: LeaseForm,
    *,
    actor=None,
    manual_signers: dict | None = None,
    notify: bool = True,
) -> LeaseForm:
    """Bind every required slot to a person, mint tokens, and email them.

    `manual_signers` maps "ROLE:index" to {"name", "email"} and is how a landlord
    sends a form to somebody the lease does not know about yet — the case where
    a unit has no invitee. Roster people always win over a typed name for the
    same slot: if the lease knows who Tenant 1 is, that is who signs.
    """
    if form.status == LeaseForm.Status.VOID:
        raise FormError(_("That form was voided. Attach a fresh copy to send one."))
    if form.status == LeaseForm.Status.COMPLETED:
        raise FormError(_("That form is already fully signed."))

    slots = required_roles(form)
    if not slots:
        raise FormError(
            _("There are no signature boxes on this form, so there is nobody to "
              "send it to.")
        )

    roster = roster_candidates(form.lease)
    manual = manual_signers or {}
    expires = timezone.now() + timedelta(days=SIGN_TOKEN_TTL_DAYS)
    signers: list[LeaseFormSigner] = []

    for role, index in slots:
        existing = form.signers.filter(role=role, order=index).first()
        if existing and existing.has_signed:
            signers.append(existing)
            continue

        details = dict(roster.get((role, index)) or {})
        typed = manual.get(f"{role}:{index}") or manual.get(f"{role}:{index}".lower())
        if typed:
            details.setdefault("name", "")
            details.setdefault("email", "")
            if not details.get("name"):
                details["name"] = str(typed.get("name") or "").strip()
            if not details.get("email"):
                details["email"] = str(typed.get("email") or "").strip()

        if not (details.get("email") or details.get("user")):
            raise FormError(
                _("Nobody is assigned to the %(role)s %(n)s signature yet. Add "
                  "them to the lease, or give a name and email to send the "
                  "signing link to.")
                % {"role": str(role).replace("_", " ").lower(), "n": index + 1}
            )

        signer = existing or LeaseFormSigner(form=form, role=role, order=index)
        signer.lease_tenant = details.get("lease_tenant")
        signer.landlord_signatory = details.get("landlord_signatory")
        signer.user = details.get("user")
        signer.name = (details.get("name") or "")[:200]
        signer.email = (details.get("email") or "")[:254]
        signer.token_expires_at = expires
        signer.sent_at = timezone.now()
        signer.save()
        signers.append(signer)

    # Prefill again now that slots have people in them — "tenant.display_name"
    # could not resolve at attach time and can now.
    values = dict(form.values or {})
    for signer in signers:
        values.update(resolve_auto_values(form, signer))
    form.values = values

    missing = unfilled_required_fields(form)
    if missing:
        # A mutual agreement to end a tenancy with no end date on it is not a
        # document anyone should be asked to sign. Nothing fills these in
        # behind the landlord's back, so the only safe move is to refuse and
        # name them.
        raise FormError(
            _("Fill in %(fields)s before sending this — it can't go out blank.")
            % {"fields": ", ".join(missing)}
        )

    if form.status == LeaseForm.Status.DRAFT:
        form.status = LeaseForm.Status.SENT
    form.save(update_fields=["values", "status", "updated_at"])

    record_form_event(
        form,
        LeaseFormEvent.Kind.SENT,
        actor=actor,
        metadata={"signers": [str(s.pk) for s in signers]},
    )
    if notify:
        for signer in signers:
            if not signer.has_signed:
                _notify_signer(form, signer)
    return form


def _notify_signer(form: LeaseForm, signer: LeaseFormSigner) -> None:
    """Email one signer their link. Never raises — a bounced email is not a
    reason to lose the form that was already created."""
    from rentium.showcase.emails import send_lease_form_signature_request

    try:
        send_lease_form_signature_request(form, signer)
    except Exception:  # noqa: BLE001
        logger.exception("could not email signing link for form %s", form.pk)


def remind_outstanding(form: LeaseForm, *, actor=None) -> int:
    """Nudge everyone who still owes a signature. Returns how many were emailed.

    The case this exists for: a tenant signs the lease, and only afterwards does
    the landlord attach a form that the lease now waits on. From the tenant's
    side nothing visibly happened, so they need telling.
    """
    sent = 0
    for signer in form.signers.filter(signed_at__isnull=True, declined_at__isnull=True):
        _notify_signer(form, signer)
        record_form_event(form, LeaseFormEvent.Kind.REMINDED, signer=signer, actor=actor)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

_DATA_URL = re.compile(r"^data:image/(png|jpeg);base64,", re.IGNORECASE)


def decode_signature_png(raw: str) -> bytes | None:
    """Turn the canvas's data URL into PNG bytes, or refuse it."""
    if not raw:
        return None
    payload = _DATA_URL.sub("", str(raw).strip())
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FormError(_("That signature image could not be read.")) from exc
    if len(data) > MAX_SIGNATURE_BYTES:
        raise FormError(_("That signature image is too large."))
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FormError(_("Signature images must be PNG."))
    return data


def values_digest(values: dict) -> str:
    import json

    canonical = json.dumps(values or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@transaction.atomic
def sign_form(
    signer: LeaseFormSigner,
    *,
    typed_name: str,
    method: str = LeaseFormSignature.Method.TYPED,
    signature_png: bytes | None = None,
    ip_address: str = "",
    user_agent: str = "",
    actor=None,
) -> LeaseFormSignature:
    """Record one signature and, if that was the last one, execute the form."""
    form = signer.form
    typed_name = (typed_name or "").strip()

    if not typed_name:
        raise FormError({"typed_name": _("Type your full legal name to sign.")})
    if signer.has_signed:
        raise FormError(_("You have already signed this form."))
    if signer.declined_at:
        raise FormError(_("You declined this form. Ask the landlord to re-issue it."))
    if form.status == LeaseForm.Status.VOID:
        raise FormError(_("This form was withdrawn and can no longer be signed."))
    if form.status == LeaseForm.Status.COMPLETED:
        raise FormError(_("This form is already fully signed."))
    if method == LeaseFormSignature.Method.DRAWN and not signature_png:
        raise FormError(_("Draw your signature, or switch to typing your name."))

    signature = LeaseFormSignature(
        form=form,
        signer=signer,
        typed_name=typed_name[:200],
        method=method,
        signed_at=timezone.now(),
        ip_address=ip_address or None,
        user_agent=(user_agent or "")[:300],
        template_sha256=form.template.sha256,
        values_sha256=values_digest(form.values),
    )
    if signature_png:
        signature.signature_png.save(
            f"{signer.pk}.png", ContentFile(signature_png), save=False
        )
    signature.save()

    signer.signed_at = signature.signed_at
    signer.save(update_fields=["signed_at", "updated_at"])

    record_form_event(
        form,
        LeaseFormEvent.Kind.SIGNED,
        signer=signer,
        actor=actor,
        metadata={"method": method, "name": typed_name[:200]},
    )

    if form.status in {LeaseForm.Status.DRAFT, LeaseForm.Status.SENT}:
        form.status = LeaseForm.Status.PARTIALLY_SIGNED
        form.save(update_fields=["status", "updated_at"])

    complete_form_if_ready(form)
    return signature


@transaction.atomic
def decline_form(
    signer: LeaseFormSigner, *, reason: str = "", actor=None
) -> LeaseFormSigner:
    if signer.has_signed:
        raise FormError(_("You have already signed this form."))
    signer.declined_at = timezone.now()
    signer.decline_reason = (reason or "")[:2000]
    signer.save(update_fields=["declined_at", "decline_reason", "updated_at"])
    record_form_event(
        signer.form,
        LeaseFormEvent.Kind.DECLINED,
        signer=signer,
        actor=actor,
        metadata={"reason": signer.decline_reason},
    )
    return signer


def signer_for_user(form: LeaseForm, user) -> LeaseFormSigner | None:
    """Which signature slot belongs to this logged-in person, if any.

    Matched by linked account first, then by lease-tenant, then by the landlord
    owning the lease, then by email — so somebody who signed up after the link
    was sent still lands on their own slot rather than being told the form is
    not theirs. Returns None rather than raising, because "can this person sign"
    is a question the UI asks about every form it lists.
    """
    if not getattr(user, "pk", None):
        return None
    signers = list(form.signers.all())

    for signer in signers:
        if signer.user_id and signer.user_id == user.pk:
            return signer

    tenant_profile = getattr(user, "tenant_profile", None)
    if tenant_profile:
        for signer in signers:
            if (
                signer.lease_tenant_id
                and signer.lease_tenant.tenant_id == tenant_profile.pk
            ):
                return signer

    landlord_profile = getattr(user, "landlord_profile", None)
    if landlord_profile and form.lease.landlord_id == landlord_profile.pk:
        owner_slot = next(
            (s for s in signers if s.role == SignerRole.LANDLORD), None
        )
        if owner_slot is not None:
            return owner_slot

    email = (getattr(user, "email", "") or "").casefold()
    if email:
        for signer in signers:
            if signer.email and signer.email.casefold() == email:
                return signer
    return None


def signature_images_for(form: LeaseForm) -> dict[str, bytes]:
    """Drawn-signature PNGs, keyed by the placement they belong in."""
    images: dict[str, bytes] = {}
    for signature in form.signatures.select_related("signer"):
        if not signature.signature_png:
            continue
        try:
            signature.signature_png.open("rb")
            payload = signature.signature_png.read()
        finally:
            signature.signature_png.close()
        for row in form.placements_snapshot:
            if row.get("kind") not in {
                LeaseFormPlacement.Kind.SIGNATURE,
                LeaseFormPlacement.Kind.INITIALS,
            }:
                continue
            if row.get("signer_role") == signature.signer.role and int(
                row.get("signer_index") or 0
            ) == signature.signer.order:
                images[row["key"]] = payload
    return images


def rendered_values(form: LeaseForm) -> dict:
    """Prefilled values plus exactly what the signatures themselves contribute.

    Signatures contribute two things and no more: the name in the signature box,
    and the date in a box explicitly marked "date signed".

    They deliberately do NOT fill empty NAME boxes. RTB-8 splits a party across
    "first and middle name(s)" and "Last name(s)"; a signature is one string, and
    spreading it across both puts "Raj Singh" in the surname box of a government
    form. A name box is filled from a prefill source or typed by the landlord, or
    it stays blank — an empty box is obviously incomplete, whereas a
    confidently-wrong one is not.
    """
    values = dict(form.values or {})
    for signature in form.signatures.select_related("signer"):
        signer = signature.signer
        for row in form.placements_snapshot:
            if row.get("signer_role") != signer.role:
                continue
            if int(row.get("signer_index") or 0) != signer.order:
                continue
            kind = row.get("kind")
            if kind in {
                LeaseFormPlacement.Kind.SIGNATURE,
                LeaseFormPlacement.Kind.INITIALS,
            }:
                values[row["key"]] = signature.typed_name
            elif is_signed_at_signing(row):
                # The real signing date wins over the placeholder that
                # resolve_auto_values wrote when the form was attached.
                values[row["key"]] = _date(timezone.localdate(signature.signed_at))
    return values


def render_form_pdf(form: LeaseForm) -> bytes:
    """The document as it stands right now.

    A COMPLETED form returns its stored bytes, never a fresh render — those
    bytes are what people signed, and re-rendering could quietly disagree with
    them if a placement or a prefill source changed in between.
    """
    if form.executed_file:
        form.executed_file.open("rb")
        try:
            return form.executed_file.read()
        finally:
            form.executed_file.close()

    template = form.template
    template.file.open("rb")
    try:
        blank = template.file.read()
    finally:
        template.file.close()
    return form_render.stamp(
        blank,
        form.placements_snapshot,
        rendered_values(form),
        signature_images_for(form),
    )


@transaction.atomic
def complete_form_if_ready(form: LeaseForm) -> bool:
    """Execute the form once every required signer has signed. Idempotent."""
    if form.status == LeaseForm.Status.COMPLETED or form.executed_sha256:
        return False
    outstanding = form.signers.filter(required=True, signed_at__isnull=True)
    if outstanding.exists() or not form.signers.exists():
        return False

    data = render_form_pdf(form)
    digest = hashlib.sha256(data).hexdigest()
    filename = f"{form.template.code or 'form'}-{form.pk.hex[:8]}.pdf"

    form.executed_file.save(filename, ContentFile(data), save=False)
    form.executed_sha256 = digest
    form.status = LeaseForm.Status.COMPLETED
    form.completed_at = timezone.now()
    form.blocks_activation = False
    form.save(
        update_fields=[
            "executed_file",
            "executed_sha256",
            "status",
            "completed_at",
            "blocks_activation",
            "updated_at",
        ]
    )
    record_form_event(
        form, LeaseFormEvent.Kind.COMPLETED, metadata={"sha256": digest}
    )

    _run_stage_hooks(form)
    return True


def _run_stage_hooks(form: LeaseForm) -> None:
    """What a completed form actually DOES, by stage.

    Deferred imports and a broad catch on the publish: a completed signature is
    a fact the moment it is stored, and a downstream side effect failing must
    not roll back the evidence that somebody signed.
    """
    from rentium.events.registry import publish

    try:
        publish(
            "lease.form_completed",
            {
                "form_id": str(form.pk),
                "lease_id": str(form.lease_id),
                "template": form.template.code or form.template.name,
                "stage": str(form.template.stage),
            },
            property_id=form.lease.property_id,
            lease_id=form.lease_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not publish lease.form_completed for %s", form.pk)

    if form.template.stage == FormStage.WITH_LEASE:
        # The signature that unblocks a lease should activate it in the same
        # breath, exactly as LeaseTenant.sign() does.
        form.lease.check_and_activate()
    elif form.template.binds_to == "moveout" and form.moveout_request_id:
        apply_moveout_signatures(form)


@transaction.atomic
def apply_moveout_signatures(form: LeaseForm) -> None:
    """Push a completed end-of-tenancy form onto its MoveOutRequest.

    The RTB-8 workflow already existed as state — kind, form_type, two signature
    booleans, accept() — with no document behind it. This is the join: the form
    is the paper, MoveOutRequest is the consequence.
    """
    from .moveout import MoveOutRequest

    moveout = form.moveout_request
    if moveout is None or moveout.status != MoveOutRequest.Status.PENDING:
        return

    for signer in form.signers.filter(signed_at__isnull=False):
        moveout.sign(as_landlord=signer.role != SignerRole.TENANT)
    moveout.save(
        update_fields=[
            "tenant_signed",
            "tenant_signed_at",
            "landlord_signed",
            "landlord_signed_at",
            "updated_at",
        ]
    )
    if moveout.tenant_signed and moveout.landlord_signed:
        moveout.accept()


def moveout_form_template(lease) -> LeaseFormTemplate | None:
    """The system end-of-tenancy form for this tenancy's jurisdiction, if any.

    Matched on `binds_to` and jurisdiction rather than on the literal string
    "RTB-8" so a second province drops in by seeding a row, not by editing this.
    """
    from django.db.models import Q

    from .tenancy_rules import rules_for_lease

    try:
        jurisdiction = rules_for_lease(lease).jurisdiction
    except Exception:  # noqa: BLE001 - a missing rules row must not block a move-out
        logger.exception("could not read tenancy rules for lease %s", lease.pk)
        return None

    return (
        LeaseFormTemplate.objects.filter(
            Q(jurisdiction__iexact=jurisdiction) | Q(jurisdiction=""),
            landlord__isnull=True,
            binds_to="moveout",
            is_active=True,
            availability=LeaseFormTemplate.Availability.AVAILABLE,
        )
        .exclude(file="")
        .order_by("jurisdiction")  # "" sorts first, so a province match wins
        .last()
    )


def ensure_mutual_agreement_form(moveout, *, actor=None) -> LeaseForm | None:
    """Attach the province's mutual-agreement form to a pending move-out.

    Called wherever a MUTUAL_AGREEMENT request is created. Returns None — not an
    error — when the province has no shipped form: the mutual-agreement workflow
    predates this feature and has to keep working on a plain written agreement.
    """
    from .moveout import MoveOutRequest

    if moveout.kind != MoveOutRequest.Kind.MUTUAL_AGREEMENT:
        return None
    existing = moveout.lease_forms.exclude(status=LeaseForm.Status.VOID).first()
    if existing:
        return existing

    template = moveout_form_template(moveout.lease)
    if template is None:
        return None
    try:
        return attach_form(
            moveout.lease,
            template,
            actor=actor,
            moveout_request=moveout,
            created_via=LeaseForm.CreatedVia.SYSTEM,
        )
    except ValidationError:
        # A form we cannot attach (no placements yet, say) must not stop the
        # landlord ending a tenancy. It is paperwork, not the decision.
        logger.exception("could not attach the mutual-agreement form to %s", moveout.pk)
        return None


def sync_moveout_date(form: LeaseForm, end_date) -> None:
    """Keep an unsigned end-of-tenancy form and its request in step.

    Direction of travel flips at the first signature. Before it, the form is a
    draft of the request and follows it. After it, the form is a signed document
    and the request follows the form — which is why changing the date later
    means voiding and re-issuing, not editing.
    """
    if form.signatures.exists():
        raise FormError(
            _("This form has already been signed, so its date can't be changed. "
              "Void it and issue a new one if the date has moved.")
        )
    # Move the request first, then re-derive the paper from it — the other
    # order silently re-stamps the OLD date onto the form.
    if form.moveout_request_id and end_date:
        form.moveout_request.requested_end_date = end_date
        form.moveout_request.save(update_fields=["requested_end_date", "updated_at"])
    form.values.update(_moveout_values(form, form.moveout_request))
    form.save(update_fields=["values", "updated_at"])


@transaction.atomic
def void_form(form: LeaseForm, *, reason: str = "", actor=None) -> LeaseForm:
    """Withdraw a form. Allowed after signatures — the evidence is kept."""
    if form.status == LeaseForm.Status.COMPLETED:
        raise FormError(
            _("A fully signed form can't be voided — it's executed. Attach a "
              "replacement form instead.")
        )
    form.status = LeaseForm.Status.VOID
    form.blocks_activation = False
    form.save(update_fields=["status", "blocks_activation", "updated_at"])
    record_form_event(
        form,
        LeaseFormEvent.Kind.VOIDED,
        actor=actor,
        metadata={"reason": (reason or "")[:500]},
    )
    # Voiding may have been the last thing holding the lease back.
    form.lease.check_and_activate()
    return form


# ---------------------------------------------------------------------------
# Queries the rest of the system shares
# ---------------------------------------------------------------------------


def outstanding_forms(lease, *, stage: str = ""):
    """Attached forms that still need somebody's signature."""
    rows = lease.lease_forms.exclude(
        status__in=[LeaseForm.Status.COMPLETED, LeaseForm.Status.VOID]
    ).select_related("template")
    if stage:
        rows = rows.filter(template__stage=stage)
    return rows


def blocking_forms(lease):
    """Forms that must be signed before this lease can activate.

    The single source of truth for that question — read by
    Lease.check_and_activate, the lease API, the attention feed and RAMA, so
    they cannot drift apart about why a lease is stuck.
    """
    return lease.lease_forms.filter(
        required=True,
        blocks_activation=True,
    ).exclude(status__in=[LeaseForm.Status.COMPLETED, LeaseForm.Status.VOID])


def activation_blockers(lease) -> list[str]:
    """Human-readable reasons this lease has not activated, forms included."""
    from .models import Lease

    if lease.status != Lease.LeaseStatus.PENDING_SIGNATURES:
        return []
    reasons: list[str] = []
    if not lease.landlord_signed:
        reasons.append(_("The landlord hasn't signed yet."))
    if lease.landlord_signatories.filter(has_signed=False).exists():
        reasons.append(_("A co-landlord hasn't signed yet."))
    if not lease.lease_tenants.filter(has_signed=True).exists():
        reasons.append(_("No tenant has signed yet."))
    for form in blocking_forms(lease).select_related("template"):
        reasons.append(
            _("%(title)s still needs signing.") % {"title": form.title}
        )
    return reasons


def frontend_base_url() -> str:
    return (
        getattr(settings, "FRONTEND_URL", "")
        or getattr(settings, "CANONICAL_FRONTEND_ORIGIN", "")
        or "https://www.rentium.ca"
    ).rstrip("/")
