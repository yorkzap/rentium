"""Scenario definitions for scripts/rama_eval.py — RAMA smartness evals.

Each scenario is a dict:
  name      — label for the report
  setup(landlord) -> ctx        — creates [RAMA-EVAL] fixtures, returns dict
  turns     — [{say, expect}] run in ONE conversation, in order
  teardown(landlord, ctx)       — removes the fixtures (always runs)

expect keys (all optional, all deterministic — no LLM judge):
  tools_any      [names]  — at least one must appear in tools_used
  tools_none     [names]  — none may appear in tools_used
  reply_regex    [rx]     — every regex must match the reply (case-insens.)
  reply_not_regex[rx]     — no regex may match the reply
  pending_plan   bool     — response.pending_plan is (not) present
  awaiting_step  bool     — pending_plan.awaiting_own_confirm equals this
  db(landlord, ctx) -> (ok: bool, msg: str)   — direct DB assertion

The pass bar is WEAK models (Mistral Small / Gemini Flash) — smartness must
come from the deterministic scaffolding, not the model.
"""

from __future__ import annotations

from datetime import date, timedelta

MARKER = "[RAMA-EVAL]"

TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _room(landlord, name, **extra):
    from rentium.properties.models import Property

    return Property.objects.create(
        landlord=landlord,
        name=f"{MARKER} {name}",
        address="950 Eval Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
        asking_rent="900.00",
        **extra,
    )


def _lease(landlord, prop, status="ACTIVE", rent="900.00"):
    from rentium.leases.models import Lease

    return Lease.objects.create(
        landlord=landlord,
        property=prop,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=getattr(Lease.LeaseStatus, status),
        start_date=date.today() - timedelta(days=30),
        is_month_to_month=True,
        total_rent=rent,
    )


def _gallery(prop, name="eval.gif"):
    from django.core.files.base import ContentFile

    from rentium.properties.models import PropertyImage

    return PropertyImage.objects.create(
        property=prop, image=ContentFile(TINY_GIF, name=name)
    )


def _teardown(landlord, ctx):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    props = Property.objects.filter(landlord=landlord, name__contains=MARKER)
    for lease in Lease.objects.filter(property__in=props):
        try:
            lease.delete()
        except Exception:  # noqa: BLE001 — protected rows stay; report nothing
            pass
    for prop in list(props):
        try:
            prop.delete()
        except Exception:  # noqa: BLE001
            pass


def _setup_photo_portfolio(landlord) -> dict:
    """The failing-transcript portfolio: images, no-images±lease, an exclude."""
    from django.core.files.base import ContentFile

    hero = _room(landlord, "EvalRoom Hero")
    hero.primary_image.save("hero.gif", ContentFile(TINY_GIF), save=True)
    gallery = _room(landlord, "EvalRoom Gallery")
    _gallery(gallery)
    free = _room(landlord, "EvalRoom Free")
    blocked = _room(landlord, "EvalRoom Blocked")
    lease = _lease(landlord, blocked)
    nook = _room(landlord, "Eval Garden Nook")  # no images; user says keep
    return {
        "hero": hero,
        "gallery": gallery,
        "free": free,
        "blocked": blocked,
        "lease": lease,
        "nook": nook,
    }


# ------------------------------------------------------------ db checks
def _free_deleted_blocked_kept(landlord, ctx):
    from rentium.properties.models import Property

    if Property.objects.filter(pk=ctx["free"].pk).exists():
        return False, "EvalRoom Free (no images, no lease) was not deleted"
    for key in ("hero", "gallery", "blocked", "nook"):
        if not Property.objects.filter(pk=ctx[key].pk).exists():
            return False, f"{ctx[key].name} should NOT have been deleted"
    return True, ""


def _nothing_deleted(landlord, ctx):
    from rentium.properties.models import Property

    for key in ("hero", "gallery", "free", "blocked", "nook"):
        if not Property.objects.filter(pk=ctx[key].pk).exists():
            return False, f"{ctx[key].name} was deleted after a cancel"
    return True, ""


def _lease_still_active(landlord, ctx):
    ctx["lease"].refresh_from_db()
    if ctx["lease"].status != "ACTIVE":
        return False, f"lease ran before its own confirm ({ctx['lease'].status})"
    return True, ""


def _moved_out_and_re_leased(landlord, ctx):
    from rentium.leases.models import Lease

    ctx["lease"].refresh_from_db()
    if ctx["lease"].status != "TERMINATED":
        return False, f"old lease is {ctx['lease'].status}, expected TERMINATED"
    new = Lease.objects.filter(landlord=landlord, property=ctx["dst"]).first()
    if new is None:
        return False, "no new lease on the destination room"
    return True, ""


def _viewing_scheduled_with_email(landlord, ctx):
    from rentium.appointments.models import Appointment

    appt = Appointment.objects.filter(
        landlord=landlord, property=ctx["room"], kind="VIEWING"
    ).first()
    if appt is None:
        return False, "no viewing was created"
    if appt.status != "SCHEDULED":
        return False, f"viewing is {appt.status}, expected SCHEDULED"
    if "eval" not in (appt.contact_email or "").lower():
        return False, "viewer email was not recorded"
    return True, ""


def _no_rent_room(landlord) -> dict:
    """A room with NO asking_rent, so a lease needs the landlord to state rent."""
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name=f"{MARKER} EvalRoom NeedsRent",
        address="950 Eval Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    return {"room": room}


def _no_lease_yet(landlord, ctx):
    from rentium.leases.models import Lease

    if Lease.objects.filter(property=ctx["room"]).exists():
        return False, "a lease was created before the landlord gave the rent"
    return True, ""


def _renamed(landlord, ctx):
    from rentium.properties.models import Property

    prop = Property.objects.get(pk=ctx["room"].pk)
    if "Rightname" not in prop.name:
        return False, f"listing was not renamed (still {prop.name!r})"
    # A rename is an in-place edit — no second listing should have been made.
    twins = Property.objects.filter(landlord=landlord, name__contains="Rightname")
    if twins.count() != 1:
        return False, f"expected exactly one renamed listing, found {twins.count()}"
    return True, ""


def _not_yet_renamed(landlord, ctx):
    from rentium.properties.models import Property

    prop = Property.objects.get(pk=ctx["room"].pk)
    if "Wrongname" not in prop.name:
        return False, f"listing renamed before confirmation ({prop.name!r})"
    return True, ""


def _twins_untouched(landlord, ctx):
    from rentium.properties.models import Property

    for key in ("twin_old", "twin_new"):
        if not Property.objects.filter(pk=ctx[key].pk).exists():
            return False, f"{key} was deleted before confirmation"
    return True, ""


def _old_twin_deleted(landlord, ctx):
    from rentium.properties.models import Property

    if Property.objects.filter(pk=ctx["twin_old"].pk).exists():
        return False, "the OLD twin should have been deleted"
    if not Property.objects.filter(pk=ctx["twin_new"].pk).exists():
        return False, "the NEW twin should have been kept"
    return True, ""


def _blocked_terminated_listing_kept(landlord, ctx):
    from rentium.properties.models import Property

    ctx["lease"].refresh_from_db()
    if ctx["lease"].status != "TERMINATED":
        return False, f"lease not terminated ({ctx['lease'].status})"
    prop = Property.objects.get(pk=ctx["blocked"].pk)
    # No auto-retire: the listing stays exactly as it was, re-leasable.
    if prop.status != Property.PropertyStatus.AVAILABLE or not prop.is_publicly_visible:
        return False, "listing must stay as-is (no auto retire/hide)"
    return True, ""


# ------------------------------------------------------------- scenarios
SCENARIOS: list[dict] = [
    {
        # The exact failing transcript, as a regression scenario.
        "name": "bulk delete listings without images (transcript regression)",
        "setup": _setup_photo_portfolio,
        "teardown": _teardown,
        "turns": [
            {
                "say": (
                    "Delete all listings that don't have images except the "
                    "Eval Garden Nook"
                ),
                "expect": {
                    "tools_any": ["plan_operation", "find_listings"],
                    # The COMPLETE partition must be surfaced: deletable AND
                    # blocked, with the blocked question asked unprompted.
                    "reply_regex": [r"EvalRoom Free", r"EvalRoom Blocked"],
                    # Listings WITH images must never be called photo-less.
                    "reply_not_regex": [
                        r"EvalRoom Hero[^.]*no (image|photo)",
                        r"EvalRoom Gallery[^.]*no (image|photo)",
                    ],
                    "pending_plan": True,
                    "db": _nothing_deleted,
                },
            },
            {
                "say": "yes",
                "expect": {
                    "reply_regex": [r"EvalRoom Free"],
                    "db": _free_deleted_blocked_kept,
                },
            },
            {
                "say": "terminate the leases first and then delete those listings",
                "expect": {
                    "tools_any": ["plan_operation"],
                    "pending_plan": True,
                    "db": _lease_still_active,
                },
            },
            {
                # Single-step plan (just the termination — the listing can't be
                # deleted and is NOT auto-retired) → one yes runs it.
                "say": "yes",
                "expect": {
                    "pending_plan": False,
                    "db": _blocked_terminated_listing_kept,
                },
            },
        ],
    },
    {
        "name": "read-only set query is complete and grounded",
        "setup": _setup_photo_portfolio,
        "teardown": _teardown,
        "turns": [
            {
                "say": "Which of my listings have no photos at all?",
                "expect": {
                    "tools_any": ["find_listings"],
                    "reply_regex": [
                        r"EvalRoom Free",
                        r"EvalRoom Blocked",
                        r"Eval Garden Nook",
                    ],
                    "reply_not_regex": [
                        r"EvalRoom Hero[^.]*no (image|photo)",
                        r"EvalRoom Gallery[^.]*no (image|photo)",
                    ],
                    "pending_plan": False,
                },
            },
            {
                # Cross-turn memory: the fact came from last turn's tool call.
                "say": "And how many photos does EvalRoom Gallery have?",
                "expect": {"reply_regex": [r"\b(1|one)\b"]},
            },
        ],
    },
    {
        "name": "cancel path leaves everything untouched",
        "setup": _setup_photo_portfolio,
        "teardown": _teardown,
        "turns": [
            {
                "say": "Delete every listing that has no images except Eval Garden Nook",
                "expect": {"pending_plan": True, "db": _nothing_deleted},
            },
            {
                "say": "no",
                "expect": {"pending_plan": False, "db": _nothing_deleted},
            },
        ],
    },
    {
        "name": "move tenant chain (terminate → re-lease another room)",
        "setup": lambda landlord: {
            "src": (src := _room(landlord, "EvalRoom MoveFrom")),
            "dst": _room(landlord, "EvalRoom MoveTo"),
            "lease": _lease(landlord, src, rent="777.00"),
        },
        "teardown": _teardown,
        "turns": [
            {
                "say": (
                    f"Move the tenant from {MARKER} EvalRoom MoveFrom to "
                    f"{MARKER} EvalRoom MoveTo, their name is Sam Eval, email "
                    "sam.eval@example.com"
                ),
                "expect": {
                    "tools_any": ["plan_move_tenant"],
                    "pending_plan": True,
                },
            },
            {
                "say": "yes",
                "expect": {"pending_plan": True, "awaiting_step": True,
                           "db": _lease_still_active},
            },
            {
                "say": "yes",
                "expect": {"pending_plan": False, "db": _moved_out_and_re_leased},
            },
        ],
    },
    {
        # Rename transcript regression: the model must know update_property
        # renames — never claim it can't, never create-then-delete.
        "name": "rename a listing in place (transcript regression)",
        "setup": lambda landlord: {"room": _room(landlord, "EvalRoom Wrongname")},
        "teardown": _teardown,
        "turns": [
            {
                "say": f"Rename {MARKER} EvalRoom Wrongname to {MARKER} EvalRoom Rightname",
                "expect": {
                    "tools_any": ["update_property"],
                    "tools_none": ["create_property", "delete_property"],
                    # Must NOT voice the old wrong belief or the create+delete
                    # workaround.
                    "reply_not_regex": [
                        r"can'?t rename",
                        r"cannot rename",
                        r"create a new listing",
                        r"delete the old",
                    ],
                    "pending_plan": True,
                    "db": _not_yet_renamed,
                },
            },
            {
                "say": "yes",
                "expect": {"pending_plan": False, "db": _renamed},
            },
        ],
    },
    {
        # Duplicate-name disambiguation: "delete the old one" of two identically
        # named listings. Ambiguity must be a QUESTION, never a "blocked, skip?".
        "name": "delete the old duplicate (disambiguation regression)",
        "setup": lambda landlord: {
            "twin_old": _room(landlord, "EvalRoom Twin"),
            "twin_new": _room(landlord, "EvalRoom Twin"),
        },
        "teardown": _teardown,
        "turns": [
            {
                "say": f"Delete the {MARKER} EvalRoom Twin listing",
                "expect": {
                    # Ambiguity is surfaced as a choice, not a PROTECT block and
                    # not a plan that nukes both twins.
                    "reply_regex": [r"which|old one|new one|two|both|match"],
                    "reply_not_regex": [
                        r"is blocked",
                        r"skip (them|it)",
                        r"undeletable",
                    ],
                    "pending_plan": False,
                    "db": _twins_untouched,
                },
            },
            {
                "say": "the old one",
                "expect": {
                    "tools_any": ["plan_operation", "delete_property"],
                    "db": _twins_untouched,
                },
            },
            {
                "say": "yes",
                "expect": {"db": _old_twin_deleted},
            },
        ],
    },
    {
        # The "forgot the rent" transcript: RAMA must ASK for the rent instead
        # of silently creating a $0 lease.
        "name": "forgot the rent → RAMA asks (essential-field gate)",
        "setup": _no_rent_room,
        "teardown": _teardown,
        "turns": [
            {
                "say": (
                    f"Create a month-to-month lease on {MARKER} EvalRoom NeedsRent "
                    "starting 2026-09-01"
                ),
                "expect": {
                    "reply_regex": [r"rent"],
                    "reply_not_regex": [r"created", r"\$0\b", r"zero"],
                    "pending_plan": False,
                    "db": _no_lease_yet,
                },
            },
            {
                "say": "the rent is $850 per month",
                "expect": {
                    "tools_any": ["create_lease", "setup_room_tenancy"],
                    "pending_plan": True,
                },
            },
        ],
    },
    {
        # The "not-alive" transcript: RAMA schedules a viewing, then must be
        # able to say HOW the viewer was notified instead of shrugging.
        "name": "viewing notification is grounded (aliveness regression)",
        "setup": lambda landlord: {"room": _room(landlord, "EvalRoom Showing")},
        "teardown": _teardown,
        "turns": [
            {
                "say": (
                    f"Book a viewing at {MARKER} EvalRoom Showing on 2026-08-05 "
                    "at 2pm for Pat, email pat.eval@example.com"
                ),
                "expect": {
                    "tools_any": ["schedule_viewing"],
                    "pending_plan": True,
                },
            },
            {
                "say": "yes",
                "expect": {
                    "pending_plan": False,
                    "db": _viewing_scheduled_with_email,
                },
            },
            {
                "say": "how was the viewer notified?",
                "expect": {
                    # answerable from the receipt / list_viewing_requests —
                    # never "the tool result doesn't say"
                    "reply_regex": [r"email"],
                    "reply_not_regex": [r"does not (say|include)", r"can'?t tell"],
                },
            },
        ],
    },
]
