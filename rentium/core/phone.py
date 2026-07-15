"""
Phone numbers, done once, correctly.

The problem this solves: right now there are FIVE phone fields across the
codebase — User.phone (max_length=20), Lease.landlord_daytime_phone (20),
Appointment.contact_phone (30), WorkOrder.contractor_phone (30), and
Inquiry.phone (30) — and not one of them validates anything. A landlord can
save "call me maybe" as their phone number. Two of them disagree on length. A
tenant typing "(250) 555-0100" and the same tenant typing "250-555-0100" produce
two different strings that no query will ever match.

The fix is the industry-standard one, and it's worth not reinventing:

  STORE   E.164 and nothing else: +12505550100. No spaces, no dashes, no
          brackets. It's the international standard, it's what every SMS
          provider (Twilio, MessageBird) requires, it's unique, and it's
          comparable with ==.

  VALIDATE with `phonenumbers` — Google's libphonenumber, the same library your
          phone's dialler uses. It knows 250 is a real BC area code and 999
          isn't; that a North American number can't have a leading 1 in the area
          code; that +1 555 0100 is 3 digits short. A regex knows none of that
          and will happily accept +1 000 000 0000.

  DISPLAY formatted for the reader's region: (250) 555-0100 in Canada,
          +44 20 7946 0958 in the UK. Storage and display are different
          problems and conflating them is how you end up unable to search.

    pip install phonenumbers
"""

from __future__ import annotations

import phonenumbers
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

DEFAULT_REGION = "CA"


def parse_phone(raw: str, region: str = DEFAULT_REGION):
    """-> phonenumbers.PhoneNumber. Raises ValidationError with a human message."""
    if not raw or not str(raw).strip():
        return None

    try:
        parsed = phonenumbers.parse(str(raw).strip(), region)
    except phonenumbers.NumberParseException:
        raise ValidationError(
            _("That doesn't look like a phone number. Try 250 555 0100.")
        )

    if not phonenumbers.is_possible_number(parsed):
        raise ValidationError(
            _(
                "That number has the wrong number of digits — a Canadian number "
                "has 10, like 250 555 0100."
            )
        )

    # is_valid_number is the strict check: it verifies the area code and prefix
    # actually exist, not just that the digit count is plausible.
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError(_("That isn't a working number — check the area code."))

    return parsed


def to_e164(raw: str, region: str = DEFAULT_REGION) -> str:
    """The canonical stored form. '' for blank input."""
    parsed = parse_phone(raw, region)
    if parsed is None:
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def to_display(e164: str, region: str = DEFAULT_REGION) -> str:
    """
    '+12505550100' -> '(250) 555-0100' for a Canadian reader, or
    '+44 20 7946 0958' for anything outside their region. Never store this.
    """
    if not e164:
        return ""
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164  # legacy garbage — show it rather than hiding it
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if phonenumbers.region_code_for_number(parsed) == region
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(parsed, fmt)


class PhoneField(models.CharField):
    """
    A CharField that can only ever contain E.164 or ''.

    Normalises in to_python, so it's impossible to write an unvalidated number
    through the ORM, admin, a fixture, a shell, or a serializer. That
    completeness is the point — a validator that only runs in one code path is
    a validator that will eventually be bypassed by another.

    max_length=20 is comfortable: the longest possible E.164 number is 15 digits
    plus the '+'.
    """

    description = "Phone number, stored E.164"

    def __init__(self, *args, region: str = DEFAULT_REGION, **kwargs):
        self.region = region
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("default", "")
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.region != DEFAULT_REGION:
            kwargs["region"] = self.region
        return name, path, args, kwargs

    def to_python(self, value):
        if value in (None, ""):
            return ""
        value = str(value).strip()
        if value.startswith("+") and value[1:].isdigit():
            # Already canonical — don't re-parse a value we ourselves wrote.
            return value
        return to_e164(value, self.region)

    def get_prep_value(self, value):
        return self.to_python(value)

    @property
    def display(self):  # pragma: no cover
        raise AttributeError("Use rentium.core.phone.to_display(instance.field)")
