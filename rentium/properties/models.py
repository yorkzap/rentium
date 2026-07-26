# rentium/properties/models.py
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from rentium.users.models import LandlordProfile

# --- Province normalisation -------------------------------------------------
# `province` is free text the landlord typed ("BC", "British Columbia", "b.c.").
# The public URL is /<province>/<city>/, so we need ONE canonical two-letter
# code per property, computed on save and indexed.
PROVINCE_CODES = {
    "ab": "ab",
    "alberta": "ab",
    "bc": "bc",
    "b c": "bc",
    "british columbia": "bc",
    "mb": "mb",
    "manitoba": "mb",
    "nb": "nb",
    "new brunswick": "nb",
    "nl": "nl",
    "newfoundland": "nl",
    "newfoundland and labrador": "nl",
    "ns": "ns",
    "nova scotia": "ns",
    "nt": "nt",
    "northwest territories": "nt",
    "nu": "nu",
    "nunavut": "nu",
    "on": "on",
    "ontario": "on",
    "pe": "pe",
    "pei": "pe",
    "prince edward island": "pe",
    "qc": "qc",
    "quebec": "qc",
    "québec": "qc",
    "sk": "sk",
    "saskatchewan": "sk",
    "yt": "yt",
    "yukon": "yt",
}

PROVINCE_NAMES = {
    "ab": "Alberta",
    "bc": "British Columbia",
    "mb": "Manitoba",
    "nb": "New Brunswick",
    "nl": "Newfoundland and Labrador",
    "ns": "Nova Scotia",
    "nt": "Northwest Territories",
    "nu": "Nunavut",
    "on": "Ontario",
    "pe": "Prince Edward Island",
    "qc": "Quebec",
    "sk": "Saskatchewan",
    "yt": "Yukon",
}


class Province(models.TextChoices):
    """
    THE closed set. Province was free text, which meant "BC", "British Columbia",
    "b.c." and "Britsh Columbia" were four different provinces — and the last one
    normalised to '', which silently made the property unpublishable with no
    error and no explanation anywhere. A dropdown of 13 options is not a
    limitation; it's the only sane way to model a set of 13 things.
    """

    AB = "ab", "Alberta"
    BC = "bc", "British Columbia"
    MB = "mb", "Manitoba"
    NB = "nb", "New Brunswick"
    NL = "nl", "Newfoundland and Labrador"
    NS = "ns", "Nova Scotia"
    NT = "nt", "Northwest Territories"
    NU = "nu", "Nunavut"
    ON = "on", "Ontario"
    PE = "pe", "Prince Edward Island"
    QC = "qc", "Quebec"
    SK = "sk", "Saskatchewan"
    YT = "yt", "Yukon"


class UnitType(models.TextChoices):
    """What kind of self-contained space this is.

    Module-level because both PropertyUnit (the physical space) and Property
    (the listing offered on it) need it, and PropertyUnit is defined first.
    Property.UnitType stays as an alias — it is referenced widely across the
    API, RAMA and the frontend serializers.
    """

    BASEMENT = "BASEMENT", _("Basement")
    GARDEN_SUITE = "GARDEN_SUITE", _("Garden Suite")
    MAIN_FLOOR = "MAIN_FLOOR", _("Main Floor")
    APARTMENT = "APARTMENT", _("Apartment")
    OTHER = "OTHER", _("Other")


# PropertyArea has a field literally named `property`, which shadows the
# builtin inside its class body — so its computed attributes need this alias.
_py_property = property


postal_code_validator = RegexValidator(
    regex=r"^[A-Za-z]\d[A-Za-z][ ]?\d[A-Za-z]\d$",
    message="Enter a Canadian postal code, like V8Z 3T7.",
)


def normalise_postal_code(raw: str | None) -> str:
    """'v8z3t7' -> 'V8Z 3T7'. One canonical form, so two identical codes match."""
    if not raw:
        return ""
    clean = "".join(str(raw).split()).upper()
    if len(clean) == 6:
        return f"{clean[:3]} {clean[3:]}"
    return clean


def normalise_province(raw: str | None) -> str:
    if not raw:
        return ""
    key = " ".join(str(raw).lower().replace(".", " ").split())
    return PROVINCE_CODES.get(key, "")


# --- PropertyGroup Model ---
class PropertyHolding(models.Model):
    """One physical address, one bank account, any mix of listings.

    Orthogonal to PropertyGroup (which stays room-only layout grouping, e.g.
    "McKenzie Side Unit"). A Holding is the financial/physical container a
    landlord's Constitution and bank-balance policy attach to — a house with
    a garden suite, a basement suite, and three rooms upstairs is ONE
    Holding containing a mix of ROOM and COMPLETE_UNIT listings (and may
    contain several PropertyGroups for room-level layout inside it).

    `kind` makes this the same concept at any scale: a multi-unit building is
    just a Holding with more listings — no separate hierarchy level needed
    unless a real distinction emerges later.
    """

    class Kind(models.TextChoices):
        HOUSE = "HOUSE", _("House")
        BUILDING = "BUILDING", _("Building")
        OTHER = "OTHER", _("Other")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="property_holdings"
    )
    name = models.CharField(
        _("Holding Name"), max_length=100, help_text=_("e.g., McKenzie House")
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.HOUSE)
    address = models.CharField(_("Address"), max_length=255, blank=True)
    city = models.CharField(_("City"), max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Holding")
        verbose_name_plural = _("Property Holdings")
        ordering = ["landlord", "name"]
        unique_together = ("landlord", "name")

    def __str__(self):
        return f"{self.name} (Landlord: {self.landlord.user.name})"


class PropertyUnit(models.Model):
    """One physical, self-contained space inside a Holding — a floor, a suite,
    a household. "McCaughey Main Floor" is ONE unit that happens to contain
    three bedrooms.

    This is the level the old model was missing, and its absence is what made
    the portfolio unreadable: a floor rented whole and a floor rented room-by-
    room were both stored as a PropertyGroup full of room listings, so nothing
    could tell "a 3-bedroom floor let to one family" from "3 rooms let to 3
    strangers".

    Three ideas that used to be one, now deliberately separate:

      PropertyUnit   the physical space. Knows its full internal layout (via
                     PropertyArea) no matter how it is currently rented.
      rental_mode    how it is OFFERED right now. Exactly one mode is live at
                     a time; switching is reversible and never destructive
                     (see properties/services.py).
      lease scope    what a given lease actually COVERS. This — not
                     rental_mode — decides the legal regime; see
                     leases/tenancy_rules.py. A BY_ROOM unit where one party
                     holds every room is a whole-unit tenancy in law.

    The listings a landlord actually rents stay Property rows pointing here:
    one COMPLETE_UNIT listing under WHOLE_UNIT, or N ROOM listings under
    BY_ROOM. Listings for the inactive mode are kept (is_active_offering=False)
    so switching back reuses them instead of recreating them.
    """

    # Same alias as Property.UnitType, so callers reach it from whichever
    # model they already hold.
    UnitType = UnitType

    class RentalMode(models.TextChoices):
        WHOLE_UNIT = "WHOLE_UNIT", _("Rented as one whole unit")
        BY_ROOM = "BY_ROOM", _("Rented room by room")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="property_units"
    )
    holding = models.ForeignKey(
        PropertyHolding,
        on_delete=models.CASCADE,
        related_name="units",
        verbose_name=_("Holding"),
        help_text=_("The address this unit is part of."),
    )
    name = models.CharField(
        _("Unit Name"),
        max_length=100,
        help_text=_("e.g., Main Floor, Basement, Upstairs, Garden Suite"),
    )
    unit_type = models.CharField(
        _("Unit Type"),
        max_length=20,
        choices=UnitType.choices,
        blank=True,
    )
    rental_mode = models.CharField(
        _("Rental Mode"),
        max_length=20,
        choices=RentalMode.choices,
        default=RentalMode.WHOLE_UNIT,
        db_index=True,
        help_text=_("Whether this unit is currently offered whole or room by room."),
    )
    # A unit whose layout we only partly know is still USABLE — it is created
    # and flagged, never blocked and never invented. RAMA sets this when a
    # landlord describes a floor without saying how many bathrooms it has.
    layout_complete = models.BooleanField(
        _("Layout Complete"),
        default=False,
        help_text=_("False when we know some rooms/bathrooms are still unrecorded."),
    )
    missing_layout_notes = models.TextField(
        _("Missing Layout Notes"),
        blank=True,
        help_text=_("What is known to be missing, in the landlord's own words."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Unit")
        verbose_name_plural = _("Property Units")
        ordering = ["holding", "name"]
        unique_together = ("holding", "name")

    def __str__(self):
        return f"{self.name} ({self.holding.name})"

    @property
    def is_whole_unit(self) -> bool:
        return self.rental_mode == self.RentalMode.WHOLE_UNIT

    def active_offerings(self):
        """The listings currently on the market for this unit."""
        return self.offerings.filter(is_active_offering=True)


class PropertyGroup(models.Model):
    """Room-by-room tenancy for ONE unit: the shared-space membership that a
    set of room listings belong to.

    Since PropertyUnit exists, a group is no longer "whatever the landlord
    lumped together" — it expresses exactly one thing, that a unit is being
    let room by room. Hence the one-to-one back to its unit.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="property_groups"
    )
    # Nullable while the portfolio is migrated; the backfill attaches every
    # existing group to the unit it turned out to describe.
    unit = models.OneToOneField(
        PropertyUnit,
        on_delete=models.SET_NULL,
        related_name="room_group",
        null=True,
        blank=True,
        verbose_name=_("Unit"),
        help_text=_("The physical unit these rooms make up."),
    )
    name = models.CharField(
        _("Group Name"),
        max_length=100,
        help_text=_("e.g., Unit 5 Shared Spaces, Basement Suite Rooms"),
    )
    description = models.TextField(_("Description"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Group")
        verbose_name_plural = _("Property Groups")
        ordering = ["landlord", "name"]
        unique_together = ("landlord", "name")

    def __str__(self):
        return f"{self.name} (Landlord: {self.landlord.user.name})"

    @property
    def total_occupancy(self):
        """Total possible occupancy (number of rooms in group)."""
        return self.grouped_properties.count()

    @property
    def current_occupancy(self):
        """Current occupancy (rooms with active leases)."""
        from rentium.leases.models import Lease

        leased_rooms = set()
        for lease in self.group_leases.filter(status=Lease.LeaseStatus.ACTIVE):
            for lease_tenant in lease.lease_tenants.all():
                if lease_tenant.room:
                    leased_rooms.add(lease_tenant.room.id)
        return len(leased_rooms)

    @property
    def current_tenants(self):
        """List of current tenants across all rooms in the group."""
        from rentium.leases.models import Lease

        tenant_data = []
        for lease in self.group_leases.filter(status=Lease.LeaseStatus.ACTIVE):
            for lease_tenant in lease.lease_tenants.all():
                if not lease_tenant.tenant:
                    continue  # pending invite slots have no linked account yet
                tenant_data.append(
                    {
                        "name": lease_tenant.tenant.user.name,
                        "room": lease_tenant.room.name
                        if lease_tenant.room
                        else "Unassigned",
                        "move_in": lease.start_date,
                        "tenant_id": str(lease_tenant.tenant.id),
                        "lease_id": str(lease.id),
                    }
                )
        return tenant_data

    @property
    def occupancy_percentage(self):
        total = self.total_occupancy
        if total == 0:
            return 0
        return round((self.current_occupancy / total) * 100)


# --- Public visibility -------------------------------------------------------
class PropertyQuerySet(models.QuerySet):
    def public(self):
        """
        THE visibility rule. Nothing anywhere in the codebase is allowed to
        reimplement it — the public city pages, the landlord showcase pages,
        the property detail page, the sitemap, and the appointments teaser all
        go through here. Four conditions, ALL required:

          1. The landlord opted in         (Showcase.is_public — default FALSE)
          2. This property isn't hidden    (is_publicly_visible — default TRUE)
          3. The property is AVAILABLE     (status automation keeps this true;
                                            OCCUPIED / MAINTENANCE / NOT_AVAILABLE
                                            self-clean out of the public site)
          4. It's the unit's live offering (is_active_offering — the three
                                            rooms of a floor now rented whole
                                            must not still be advertised)

        A landlord who has never opened Settings is invisible. That's the point.
        """
        return self.filter(
            landlord__showcase__is_public=True,
            is_publicly_visible=True,
            status=Property.PropertyStatus.AVAILABLE,
            is_active_offering=True,
        )


# --- Property Model ---
class Property(models.Model):
    class PropertyCategory(models.TextChoices):
        COMPLETE_UNIT = "COMPLETE_UNIT", _("Complete Unit")
        ROOM = "ROOM", _("Room")

    # Alias of the module-level UnitType (hoisted so PropertyUnit can use it
    # too). Kept because Property.UnitType is referenced across the API, RAMA
    # and the serializers.
    UnitType = UnitType

    class RoomType(models.TextChoices):
        PRIVATE = "PRIVATE", _("Private Room")
        SHARED = "SHARED", _("Shared Room")
        OTHER = "OTHER", _("Other")

    class PropertyStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        OCCUPIED = "OCCUPIED", _("Occupied")
        MAINTENANCE = "MAINTENANCE", _("Under Maintenance")
        NOT_AVAILABLE = "NOT_AVAILABLE", _("Not Available")

    class BuildingAmenity(models.TextChoices):
        """
        Only meaningful for COMPLETE_UNITs. A unit is self-contained — the only
        things it can share are building facilities. (A ROOM's shared spaces
        are the suite's common areas, which are already modelled by
        PropertyGroup + Area/PropertyArea; don't duplicate them here.)
        """

        LAUNDRY = "LAUNDRY", _("Shared laundry room")
        PARKING = "PARKING", _("Shared parking")
        STORAGE = "STORAGE", _("Shared storage / locker")
        LOBBY = "LOBBY", _("Shared entry / lobby")
        YARD = "YARD", _("Shared yard / outdoor space")
        BIKE = "BIKE", _("Bike storage")

    # Common fields
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="properties"
    )
    name = models.CharField(_("Property Name"), max_length=255)
    description = models.TextField(_("Description"), blank=True)
    # --- Location ---
    # The landlord types ONE thing: the street address, into an autocomplete.
    # Everything below it — city, province, postal code, neighbourhood,
    # coordinates — is DERIVED from the address they picked (rentium/core/geo.py)
    # and is not free text any more. A field you don't ask for is a field nobody
    # can typo.
    address = models.CharField(
        _("Street Address"),
        max_length=255,
        help_text=_("Start typing and pick from the list — we'll fill in the rest."),
    )
    city = models.CharField(
        _("City"),
        max_length=100,
        help_text=_("Filled in from the address you picked."),
    )
    province = models.CharField(
        _("Province"),
        max_length=2,
        choices=Province.choices,
        blank=True,
        db_index=True,
    )
    postal_code = models.CharField(
        _("Postal Code"),
        max_length=7,
        blank=True,
        validators=[postal_code_validator],
    )
    country = models.CharField(_("Country"), max_length=100, default="Canada")

    # Set when the address came from the autocomplete (rather than being typed
    # blind or imported). An unverified address is the one thing that can quietly
    # make a property unpublishable, so we track it explicitly and SAY SO on the
    # property page instead of letting it vanish from the public site in silence.
    address_verified = models.BooleanField(default=False, editable=False)

    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=PropertyStatus.choices,
        default=PropertyStatus.AVAILABLE,
    )

    primary_image = models.ImageField(
        _("Primary Image"),
        upload_to="properties/primary/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Main property image shown in listings"),
    )

    property_category = models.CharField(
        _("Property Category"), max_length=20, choices=PropertyCategory.choices
    )

    # Fields for Complete Units
    unit_type = models.CharField(
        _("Unit Type"), max_length=20, choices=UnitType.choices, null=True, blank=True
    )
    bedrooms = models.IntegerField(_("Number of Bedrooms"), null=True, blank=True)
    bathrooms = models.DecimalField(
        _("Number of Bathrooms"), max_digits=3, decimal_places=1, null=True, blank=True
    )
    max_occupancy = models.IntegerField(_("Maximum Occupancy"), null=True, blank=True)
    square_footage = models.IntegerField(_("Square Footage"), null=True, blank=True)

    building_amenities = models.JSONField(
        _("Shared Building Amenities"),
        default=list,
        blank=True,
        help_text=_(
            "Complete units only. Building facilities this unit shares with other "
            "units (laundry, parking, storage...). A unit shares nothing else — "
            "a ROOM's shared spaces come from its property group's common areas."
        ),
    )
    default_bills_included = models.JSONField(
        _("Default Bills / Utilities"),
        default=dict,
        blank=True,
        help_text=_(
            "Bills/utilities configuration inherited by NEW leases created on this "
            "property (same shape as Lease.bills_included). Lets a landlord set "
            "'water included, hydro tenant-paid' once, so future leases start with "
            "it pre-filled instead of blank."
        ),
    )

    # Fields for Rooms
    room_type = models.CharField(
        _("Room Type"), max_length=20, choices=RoomType.choices, null=True, blank=True
    )

    # Relationship to Group (For Room Organization — layout only, rooms only)
    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.SET_NULL,
        related_name="grouped_properties",
        null=True,
        blank=True,
        verbose_name=_("Property Group"),
        help_text=_("Group this room belongs to (if sharing common areas)"),
    )
    # The physical/financial container (any listing category) — see
    # PropertyHolding. Nullable: existing/ungrouped properties keep working.
    holding = models.ForeignKey(
        PropertyHolding,
        on_delete=models.SET_NULL,
        related_name="listings",
        null=True,
        blank=True,
        verbose_name=_("Holding"),
        help_text=_("The house/building this listing belongs to (one bank account)."),
    )
    # The physical space this listing offers — see PropertyUnit. A listing is
    # an OFFER on a unit, not the unit itself: one COMPLETE_UNIT listing under
    # WHOLE_UNIT mode, or one per bedroom under BY_ROOM.
    unit = models.ForeignKey(
        PropertyUnit,
        on_delete=models.SET_NULL,
        related_name="offerings",
        null=True,
        blank=True,
        verbose_name=_("Unit"),
        help_text=_("The physical floor/suite this listing rents out."),
    )
    # False for listings belonging to the mode this unit is NOT currently in.
    # They are kept, not deleted, so switching rental mode back reuses the
    # original listing (with its photos, description and history) instead of
    # creating a duplicate. Maintained by properties/services.py — never edit
    # directly. Inactive offerings stay reachable via ?include_inactive=true.
    is_active_offering = models.BooleanField(
        _("Active Offering"),
        default=True,
        db_index=True,
        help_text=_("False for listings parked by a rental-mode switch."),
    )

    # ---------------------------------------------------------------- public
    # Everything below drives the logged-out, public-facing pages. None of it
    # is visible to anyone until the landlord opts in (see Showcase.is_public)
    # AND this property is individually visible AND its status is AVAILABLE.
    # See PropertyQuerySet.public() — the ONE place that rule lives.

    is_publicly_visible = models.BooleanField(
        _("Show this property publicly"),
        default=True,
        help_text=_(
            "Per-property override. Turning this off hides ONE property while "
            "leaving the rest of the landlord's public page intact. Has no effect "
            "unless the landlord has also opted in to public pages at all."
        ),
    )
    public_slug = models.SlugField(
        _("Public URL slug"),
        max_length=180,
        unique=True,
        null=True,
        blank=True,
        help_text=_("Auto-generated. Used in /<province>/<city>/<slug>/."),
    )

    asking_rent = models.DecimalField(
        _("Asking Rent (monthly)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "What you're advertising this space for. Distinct from Lease.total_rent, "
            "which is what an ACTUAL tenancy costs — a vacant unit has no lease, so "
            "there'd be nothing to show a prospective tenant without this."
        ),
    )
    available_from = models.DateField(
        _("Available From"),
        null=True,
        blank=True,
        help_text=_("Blank = available now."),
    )

    is_furnished = models.BooleanField(
        _("Furnished"),
        default=False,
        editable=False,
        help_text=_(
            "DERIVED, never entered by hand — computed from this property's "
            "inventory (see properties/furnishing.py) and refreshed by signal "
            "whenever inventory changes. Denormalised only so the public city "
            "page can filter on it in SQL."
        ),
    )

    # Location, coarsened. The public site NEVER renders `address` — it renders
    # `neighbourhood, City` and a deliberately jittered map marker. Exact
    # coordinates and street address are only ever revealed by the landlord,
    # after an inquiry, out of band.
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, editable=False
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, editable=False
    )
    geocoded_at = models.DateTimeField(null=True, blank=True, editable=False)
    neighbourhood = models.CharField(
        _("Neighbourhood"),
        max_length=120,
        blank=True,
        help_text=_(
            "Shown publicly INSTEAD of the street address. Auto-filled from "
            "geocoding; edit it if you'd rather show something else (or clear it "
            "to show only the city)."
        ),
    )

    # Canonical, indexed location keys for /<province>/<city>/ lookups.
    province_code = models.CharField(
        max_length=2, blank=True, db_index=True, editable=False
    )
    city_slug = models.SlugField(
        max_length=100, blank=True, db_index=True, editable=False
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PropertyQuerySet.as_manager()

    def __str__(self):
        group_info = f" (Group: {self.group.name})" if self.group else ""
        type_display = ""
        if self.property_category == self.PropertyCategory.COMPLETE_UNIT:
            type_display = self.get_unit_type_display() or "Unit"
        elif self.property_category == self.PropertyCategory.ROOM:
            type_display = self.get_room_type_display() or "Room"
        return f"{self.name} - {type_display} at {self.address}{group_info}"

    class Meta:
        verbose_name = _("Property")
        verbose_name_plural = _("Properties")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["province_code", "city_slug", "status"]),
            models.Index(fields=["is_publicly_visible", "status"]),
        ]

    # ------------------------------------------------------------------ save
    def save(self, *args, **kwargs):
        self.province_code = (self.province or "").lower()
        self.postal_code = normalise_postal_code(self.postal_code)
        self.city_slug = slugify(self.city or "")[:100]
        if not self.public_slug:
            self.public_slug = self._build_public_slug()
        super().save(*args, **kwargs)

    def _build_public_slug(self) -> str:
        """
        Stable, human, and address-free: "private-room-in-fernwood-a4f2".
        Deliberately does NOT include the street address — the slug ends up in
        the URL bar, in Google, and in shared links.
        """
        kind = (
            self.get_room_type_display()
            if self.property_category == self.PropertyCategory.ROOM
            else self.get_unit_type_display()
        ) or "rental"
        where = self.neighbourhood or self.city or ""
        base = slugify(f"{kind} in {where}")[:160] or "rental"
        suffix = uuid.uuid4().hex[:4]
        candidate = f"{base}-{suffix}"
        while Property.objects.filter(public_slug=candidate).exists():
            suffix = uuid.uuid4().hex[:4]
            candidate = f"{base}-{suffix}"
        return candidate

    def clean(self):
        super().clean()
        is_room_property = self.property_category == self.PropertyCategory.ROOM

        if not is_room_property:  # Complete Unit Validations
            if not self.unit_type:
                raise ValidationError(
                    {"unit_type": _("Unit type required for Complete Units.")}
                )
            if self.group:
                raise ValidationError(
                    {"group": _("Complete units cannot belong to a property group.")}
                )
            if self.pk:
                associated_areas = PropertyArea.objects.filter(
                    models.Q(property=self) | models.Q(shared_by=self)
                ).distinct()
                for area in associated_areas:
                    if area.shared_by.count() > 1 or (
                        area.shared_by.count() == 1 and area.shared_by.first() != self
                    ):
                        raise ValidationError(
                            _(
                                "Areas associated with a 'Complete Unit' cannot be shared "
                                "by other properties. Check area: %(area_name)s"
                            )
                            % {"area_name": area.get_area_type_display()}
                        )
        else:  # Room Validations
            if not self.room_type:
                raise ValidationError({"room_type": _("Room type required for Rooms.")})
            if self.building_amenities:
                raise ValidationError(
                    {
                        "building_amenities": _(
                            "Building amenities apply to complete units only. A room's "
                            "shared spaces come from its property group's common areas."
                        )
                    }
                )

        if self.building_amenities:
            valid = {c.value for c in Property.BuildingAmenity}
            invalid = set(self.building_amenities) - valid
            if invalid:
                raise ValidationError(
                    {"building_amenities": _(f"Invalid amenities: {invalid}")}
                )

        if self.group and self.group.landlord != self.landlord:
            raise ValidationError(
                {
                    "group": _(
                        "Cannot assign property to a group owned by a different landlord."
                    )
                }
            )

    # -------------------------------------------------------------- helpers
    @property
    def province_display(self) -> str:
        return PROVINCE_NAMES.get(self.province_code, self.province or "")

    @property
    def public_location(self) -> str:
        """The MOST precise location we will ever show a logged-out visitor."""
        if self.neighbourhood:
            return f"{self.neighbourhood}, {self.city}"
        return self.city or ""

    # ------------------------------------------------------- publishability
    def publish_blockers(self) -> list[str]:
        """
        Why this property cannot appear publicly — in words a landlord can act on.

        This exists because the old failure mode was SILENT. A typo'd province
        produced an empty province_code, the property fell out of every public
        query, and absolutely nothing anywhere told anyone why. The landlord's
        experience was "I listed it and it never showed up", which is the worst
        possible bug: invisible, unactionable, and blamed on us.

        Now the property page shows exactly this list.
        """
        blockers = []

        if not self.province:
            blockers.append(
                "We couldn't work out which province this is in. Re-enter the "
                "address and pick it from the dropdown."
            )
        if not self.city_slug:
            blockers.append("This property has no city.")
        if self.asking_rent is None:
            blockers.append(
                "Set an asking rent — a listing without a price gets ignored."
            )
        if not self.primary_image and not self.has_gallery_images:
            blockers.append("Add at least one photo. Nobody enquires about a grey box.")
        if self.latitude is None:
            blockers.append(
                "We couldn't place this address on a map. Re-enter it and pick "
                "from the dropdown so we can find it."
            )
        return blockers

    @property
    def can_be_published(self) -> bool:
        return not self.publish_blockers()

    # ------------------------------------------------------------ images
    @property
    def gallery_image_count(self) -> int:
        """Gallery (PropertyImage) count. Honours a `_gallery_count` queryset
        annotation when present so list views don't pay an N+1."""
        annotated = getattr(self, "_gallery_count", None)
        if annotated is not None:
            return int(annotated)
        # .all() uses the prefetch cache when property_images was prefetched.
        return len(self.property_images.all())

    @property
    def has_gallery_images(self) -> bool:
        return self.gallery_image_count > 0

    @property
    def image_count(self) -> int:
        """Total photos: primary (if set) + gallery."""
        return self.gallery_image_count + (1 if self.primary_image else 0)

    @property
    def display_image(self):
        """What a card should show: primary image, else first gallery image.

        A listing whose landlord uploaded gallery photos but never set a hero
        image must not render as a grey box — nor be told it has "no photos".
        """
        if self.primary_image:
            return self.primary_image
        first = list(self.property_images.all()[:1])
        return first[0].image if first else None

    @property
    def public_type_label(self) -> str:
        if self.property_category == self.PropertyCategory.ROOM:
            return {
                self.RoomType.PRIVATE: "Private room",
                self.RoomType.SHARED: "Shared room",
            }.get(self.room_type, "Room")
        return "Full suite"

    def furnishing_summary(self) -> dict:
        """{sleeping, furniture, appliances, other} — see furnishing.py."""
        from .furnishing import summarise_inventory

        return summarise_inventory(self.inventory_items.all())

    # --- @property accessors kept from before ---
    @property
    def additional_images(self):
        return self.property_images.all()

    @property
    def primary_areas(self):
        return self.primary_area_associations.all()

    @property
    def private_inventory_items(self):
        return self.inventory_items.all()

    @property
    def shared_inventory_items(self):
        if self.group:
            return self.group.group_shared_inventory.all()
        return SharedInventoryItem.objects.none()


# --- PropertyImage Model ---
class PropertyImage(models.Model):
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="property_images",
    )
    image = models.ImageField(_("Image"), upload_to="properties/additional/%Y/%m/")
    caption = models.CharField(_("Caption"), max_length=255, blank=True)
    order = models.PositiveIntegerField(_("Display Order"), default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Property Image")
        verbose_name_plural = _("Property Images")
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"Image for {self.property.name} ({self.id})"


# --- PropertyArea Model ---
class PropertyArea(models.Model):
    """A named space: the canonical model for what is physically inside a unit.

    This is the ONE area model. A second, newer `Area` model briefly existed in
    properties/areas.py with the maintenance and inspection foreign keys, but it
    was never seeded (its signals were dead) and held zero rows, while this
    model holds the real data AND the legally load-bearing fields —
    `shared_with_landlord` and `shared_by` are what
    leases/tenancy_rules.landlord_shares_common_areas() reads to decide whether
    the provincial tenancy act applies. The legal test lives here, so the merge
    went this way: PropertyArea absorbed Area's structural fields (name, kind,
    group, exclusive_to) and Area was deleted.

    An area hangs off exactly one parent:

      unit      internal layout of a physical unit — "Master Bedroom", the
                ensuite, the kitchen. Present regardless of rental mode, which
                is the point: a floor rented whole still knows it has 3
                bedrooms.
      group     shared space belonging to a room-by-room letting.
      property  a space belonging to one specific listing.
    """

    class AreaType(models.TextChoices):
        KITCHEN = "KITCHEN", _("Kitchen")
        BATHROOM = "BATHROOM", _("Bathroom")
        LIVING_ROOM = "LIVING_ROOM", _("Living Room")
        DINING_ROOM = "DINING_ROOM", _("Dining Room")
        BEDROOM = "BEDROOM", _("Bedroom")
        LAUNDRY = "LAUNDRY", _("Laundry Area")
        OFFICE = "OFFICE", _("Office/Den")
        BALCONY = "BALCONY", _("Balcony/Patio")
        HALLWAY = "HALLWAY", _("Hallway/Entryway")
        STORAGE = "STORAGE", _("Storage Area")
        GARAGE = "GARAGE", _("Garage")
        GARDEN = "GARDEN", _("Garden/Yard")
        # Building systems — landlord's concern, but tenants still see work
        # orders on the systems that serve their space (carried over from the
        # retired Area.Kind.SYSTEM).
        HEATING = "HEATING", _("Heating / Furnace")
        HOT_WATER = "HOT_WATER", _("Hot Water")
        ELECTRICAL = "ELECTRICAL", _("Electrical Panel")
        ROOF = "ROOF", _("Roof / Structure")
        OTHER = "OTHER", _("Other")

    class Kind(models.TextChoices):
        """WHO may use the space — orthogonal to AreaType, which is WHAT it is.

        A bathroom can be PRIVATE (the master ensuite), COMMON (shared by the
        household) or EXCLUSIVE (reserved to one room). Keeping the two axes
        separate is what lets a whole-unit floor record "3 bedrooms, one with
        an ensuite" without pretending any of them are rentable listings.
        """

        COMMON = "COMMON", _("Common / Shared")
        PRIVATE = "PRIVATE", _("Private to one space")
        EXCLUSIVE = "EXCLUSIVE", _("Exclusive to a Room")
        SYSTEM = "SYSTEM", _("Building System")

    # --- parent: exactly one of unit / group / property ---------------------
    unit = models.ForeignKey(
        "PropertyUnit",
        on_delete=models.CASCADE,
        related_name="areas",
        null=True,
        blank=True,
        help_text=_("The physical unit this space is part of."),
    )
    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.CASCADE,
        related_name="areas",
        null=True,
        blank=True,
        help_text=_("The room-rental group this shared space belongs to."),
    )
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="primary_area_associations",
        null=True,
        blank=True,
        help_text=_("The primary property this area belongs to."),
    )
    name = models.CharField(
        _("Name"),
        max_length=100,
        blank=True,
        help_text=_("e.g., Master Bedroom, Ensuite. Falls back to the area type."),
    )
    area_type = models.CharField(
        _("Area Type"), max_length=20, choices=AreaType.choices
    )
    kind = models.CharField(
        _("Kind"), max_length=10, choices=Kind.choices, default=Kind.COMMON
    )
    exclusive_to = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        related_name="exclusive_areas",
        null=True,
        blank=True,
        help_text=_("The room this area is reserved for (EXCLUSIVE kind only)."),
    )
    # Which bedrooms a shared space actually serves, when it isn't the whole
    # household — "the second bathroom serves Bedroom 2 and Bedroom 3". Points
    # at other areas because under WHOLE_UNIT the bedrooms are areas, not
    # listings, so shared_by (which points at listings) cannot express it.
    serves_areas = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="served_by_areas",
        verbose_name=_("Serves Areas"),
        help_text=_("The bedrooms/spaces this area serves, if not all of them."),
    )
    count = models.PositiveIntegerField(
        _("Count"), default=1, help_text=_("e.g., Number of identical areas")
    )
    description = models.TextField(
        _("Area Description"), blank=True, help_text=_("Optional details")
    )
    shared_by = models.ManyToManyField(
        Property,
        related_name="shared_areas",
        blank=True,
        verbose_name=_("Shared By Properties"),
        help_text=_("Select ROOM properties that share access to this area."),
    )
    shared_with_landlord = models.BooleanField(
        _("Shared with Landlord"),
        default=False,
        help_text=_(
            "The landlord or their immediate relatives also live here and "
            "use this area (kitchen/bathroom/common space). Affects whether "
            "the provincial tenancy act applies to leases on these rooms."
        ),
    )
    is_group_common = models.BooleanField(
        _("Group Common Area"),
        default=False,
        db_index=True,
        help_text=_(
            "This area belongs to the whole property group. Membership is "
            "synchronized automatically as rooms join, move, or leave."
        ),
    )
    # True for the generic starter set (Kitchen, Laundry, Roof...) created by
    # seed_default_areas so maintenance and inspections have something to
    # reference. These are SCAFFOLDING, not facts a landlord told us — RAMA
    # must not report them as the unit's recorded layout, or "we know nothing
    # about this floor" silently becomes "it has a garage and a laundry".
    is_seeded_default = models.BooleanField(
        _("Seeded Default"),
        default=False,
        db_index=True,
        help_text=_("Auto-created placeholder, not a recorded layout fact."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Property Area")
        verbose_name_plural = _("Property Areas")
        ordering = ["kind", "name", "area_type"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(unit__isnull=False)
                    | models.Q(group__isnull=False)
                    | models.Q(property__isnull=False)
                ),
                name="area_has_a_parent",
            ),
        ]

    @_py_property
    def label(self) -> str:
        """The name a landlord gave it, else the area type ("Master Bedroom"
        vs a bare "Bedroom")."""
        return self.name or self.get_area_type_display()

    @_py_property
    def parent(self):
        return self.unit or self.group or self.property

    def __str__(self):
        share_count = self.shared_by.count() if self.pk else 0
        if share_count == 1 and self.shared_by.first() == self.property:
            status = " (Private)"
        elif share_count > 0:
            status = f" (Shared by {share_count} properties)"
        else:
            status = " (Private - not explicitly shared)"
        if self.pk and self.shared_with_landlord:
            status += " [shared with landlord]"
        parent = self.parent
        where = parent.name if parent else "unattached"
        return f"{self.label} ({self.count}) in {where}{status}"

    def clean(self):
        super().clean()
        if not (self.unit_id or self.group_id or self.property_id):
            raise ValidationError(
                _("An area must belong to a unit, a group, or a property.")
            )
        if self.kind == self.Kind.EXCLUSIVE and not self.exclusive_to_id:
            raise ValidationError(
                {"exclusive_to": _("Exclusive areas must name their room.")}
            )
        if self.kind != self.Kind.EXCLUSIVE and self.exclusive_to_id:
            raise ValidationError(
                {
                    "exclusive_to": _(
                        "Only EXCLUSIVE areas can be reserved for a room."
                    )
                }
            )
        if self.exclusive_to_id and self.group_id:
            if self.exclusive_to.group_id != self.group_id:
                raise ValidationError(
                    {"exclusive_to": _("Room is not in this group.")}
                )
        if self.property_id and self.property:
            if (
                self.property.property_category
                == Property.PropertyCategory.COMPLETE_UNIT
            ):
                if (
                    self.pk
                    and self.shared_by.exists()
                    and (
                        self.shared_by.count() > 1
                        or self.shared_by.first() != self.property
                    )
                ):
                    raise ValidationError(
                        _(
                            "Areas primarily associated with a 'Complete Unit' cannot be "
                            "shared by other properties."
                        )
                    )


# --- InventoryItem Model (Private) ---
class InventoryItem(models.Model):
    class ItemCondition(models.TextChoices):
        NEW = "NEW", _("New")
        GOOD = "GOOD", _("Good")
        FAIR = "FAIR", _("Fair")
        POOR = "POOR", _("Poor")
        DAMAGED = "DAMAGED", _("Damaged")
        MISSING = "MISSING", _("Missing")

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="inventory_items",
    )
    name = models.CharField(
        _("Item Name"), max_length=200, help_text=_("e.g., Bedside Lamp")
    )
    description = models.TextField(_("Description/Notes"), blank=True)
    quantity = models.PositiveIntegerField(_("Quantity"), default=1)
    condition = models.CharField(
        _("Condition"),
        max_length=10,
        choices=ItemCondition.choices,
        blank=True,
        null=True,
    )
    location_description = models.CharField(
        _("Location Description"),
        max_length=255,
        blank=True,
        help_text=_("e.g., Bedroom Closet"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Private Inventory Item")
        verbose_name_plural = _("Private Inventory Items")
        ordering = ["property", "location_description", "name"]

    def __str__(self):
        return f"{self.name} (Qty: {self.quantity}) in {self.property.name}"


# --- SharedInventoryItem Model ---
class SharedInventoryItem(models.Model):
    class ItemCondition(models.TextChoices):
        NEW = "NEW", _("New")
        GOOD = "GOOD", _("Good")
        FAIR = "FAIR", _("Fair")
        POOR = "POOR", _("Poor")
        DAMAGED = "DAMAGED", _("Damaged")
        MISSING = "MISSING", _("Missing")

    group = models.ForeignKey(
        PropertyGroup,
        on_delete=models.CASCADE,
        related_name="group_shared_inventory",
    )
    name = models.CharField(
        _("Item Name"), max_length=200, help_text=_("e.g., Microwave Oven")
    )
    description = models.TextField(_("Description/Notes"), blank=True)
    quantity = models.PositiveIntegerField(_("Quantity"), default=1)
    condition = models.CharField(
        _("Condition"),
        max_length=10,
        choices=ItemCondition.choices,
        blank=True,
        null=True,
    )
    location_description = models.CharField(
        _("Location Description"),
        max_length=255,
        blank=True,
        help_text=_("e.g., Kitchen Counter"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shared Inventory Item")
        verbose_name_plural = _("Shared Inventory Items")
        ordering = ["group", "location_description", "name"]

    def __str__(self):
        return f"{self.name} (Qty: {self.quantity}) (Shared in {self.group.name})"


