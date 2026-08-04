"""One searchable catalogue over RAMA's registered operations.

The registry remains the compatibility adapter for existing tools.  This
module adds the command metadata and selective retrieval layer so models see a
small relevant surface instead of all 100+ schemas on every turn.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .registry import REGISTRY
from .tool_meta import meta_for

_TOKEN = re.compile(r"[a-z0-9]+")

# Business-language aliases that do not naturally occur in Python function
# names.  Keep these focused: descriptions remain the general retrieval corpus.
CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "manage_lease_forms": (
        "attach a form",
        "RTB-8",
        "RTB8",
        "mutual agreement to end tenancy",
        "end of tenancy form",
        "addendum",
        "pet addendum",
        "lease addendum",
        "extra document to sign",
        "send for signature",
        "get this signed",
        "custom form",
        "sign this pdf",
    ),
    "record_payment": (
        "received money",
        "partial payment",
        "paid deposit",
        "e-transfer received",
        "cash received",
        "remaining balance",
    ),
    "attach_photo_to_listing": (
        "add pictures",
        "listing photos",
        "gallery images",
        "upload photos",
    ),
    "remove_photo_from_listing": (
        "remove wrong image",
        "delete listing photo",
        "mortgage image on listing",
        "remove basement photos",
    ),
    "remove_photos_from_listing": (
        "remove selected images",
        "delete multiple listing photos",
        "remove photo numbers",
        "delete specific pictures",
    ),
    "list_listing_media": (
        "which listing photos",
        "image handles",
        "show gallery",
    ),
    "create_lease": (
        "draft lease",
        "new tenancy",
        "room lease",
        "rental agreement",
    ),
    "adjust_lease": (
        "change start date",
        "move lease start",
        "make furnished",
        "semi furnished",
        "unfurnished lease",
        "edit pending lease",
        "change lease dates",
    ),
    "renew_lease": (
        "renew tenancy",
        "renew the lease",
        "extend lease another year",
        "new term same tenant",
        "roll over lease",
    ),
    "settle_moveout": (
        "end tenancy",
        "move out notice",
        "mutual agreement end",
        "settle deposit",
        "return security deposit",
        "landlord notice to end",
    ),
    "complete_inspection_package": (
        "condition inspection",
        "move in inspection",
        "move out inspection",
        "rtb inspection",
        "walkthrough report",
    ),
    "apply_rent_adjustment": (
        "rent discount",
        "rent increase",
        "reduce rent",
        "raise rent",
        "prorate rent",
    ),
    "record_utility_bill": (
        "utility bill",
        "hydro bill",
        "gas bill",
        "electricity bill",
        "split utility",
        "water bill",
    ),
    "convert_inquiry_to_viewing": (
        "inquiry to viewing",
        "book showing from inquiry",
        "turn lead into viewing",
        "schedule from inquiry",
    ),
    "void_ledger_entry": (
        "void charge",
        "void expense",
        "cancel ledger entry",
        "reverse entry",
    ),
    "mark_ledger_paid": (
        "expense paid",
        "marked paid",
        "cleared bank",
        "left my account",
    ),
    "correct_ledger_entry": (
        "fix expense",
        "correct charge",
        "wrong amount",
        "typo on ledger",
    ),
    "post_ledger_credit": (
        "give credit",
        "goodwill credit",
        "discount on charge",
    ),
    "post_one_off_charge": (
        "damage charge",
        "late fee",
        "one off charge",
        "charge tenant for",
    ),
    "cancel_viewing": (
        "cancel viewing",
        "cancel showing",
        "cancel appointment",
    ),
    "update_inspection_items": (
        "fill inspection",
        "condition codes",
        "inspection items",
    ),
    "mark_cleaning_deposit_paid": (
        "cleaning deposit paid",
        "paid cleaning deposit",
        "cleaning fee paid",
        "paid cleaning fee",
    ),
    "record_deposit_deduction": (
        "deduct from deposit",
        "deposit deduction",
        "charge for cleaning",
        "cleaning cost",
        "cleaning hours",
        "professional cleaners",
        "garbage removal",
        "dump run",
        "keep some of the deposit",
    ),
    "return_deposits": (
        "return the deposit",
        "return deposits",
        "give the deposit back",
        "refund the deposit",
        "pay back the deposit",
        "send the deposit back",
    ),
    "create_payment_reminder": (
        "payment reminder",
        "remind about rent",
        "rent reminder",
    ),
    "update_inquiry": (
        "archive inquiry",
        "inquiry notes",
    ),
    "commit_import_batch": (
        "commit import",
        "finalize import batch",
    ),
    "update_lease": (
        "edit lease terms",
        "change rent",
        "update pending lease",
        "change end date",
        # Setting a deposit on a lease that already exists. Without these the
        # ranker offered create_lease and mark_cleaning_deposit_paid for
        # "add a $200 cleaning deposit to lease RMT415536-0617" and never
        # offered the tool that does it.
        "add deposit",
        "set deposit",
        "change deposit",
        "edit deposit",
        "cleaning deposit amount",
        "security deposit amount",
        "add cleaning deposit to lease",
        "edit lease deposit",
        "lease deposit field",
    ),
    "invite_tenant_to_lease": (
        "send lease",
        "invite renter",
        "email agreement",
    ),
    "list_lease_roster": (
        "viewed invite",
        "opened lease",
        "created account",
        "signed up",
        "signed lease",
        "last seen lease",
        "when did tenant open",
        "has tenant seen",
        "seen the lease",
    ),
    "create_property_structure": (
        "create unit",
        "property hierarchy",
        "suite structure",
    ),
    "update_unit_layout": (
        "add bedrooms",
        "add rooms",
        "bonus room",
        "change layout",
    ),
    "set_unit_rental_mode": (
        "rent by room",
        "whole unit",
        "convert suite",
        "property group",
    ),
    "configure_unit_room_offerings": (
        "turn suite into rooms",
        "add rooms to complete unit",
        "add two rooms into the garden suite",
        "convert garden suite to property group",
        "change how this unit is rented",
        "rent by room",
        "bonus room",
        "rooms on the market",
        "divide suite into rooms",
        "mckenzie garden suite rooms",
    ),
    "viewing_invite_status": (
        "have they seen the link",
        "did they open the invite",
        "viewing link opened",
        "prospect opened viewing",
        "seen the viewing",
        "opened the status page",
    ),
    "tenant_lease_status": (
        "has signed the lease",
        "has seen the lease",
        "opened the lease invite",
        "when did they open the lease",
        "has the tenant signed",
    ),
    "schedule_viewing": (
        "book showing",
        "property tour",
        "appointment tomorrow",
    ),
    "reschedule_viewing": (
        "change viewing time",
        "move showing",
        "reschedule appointment",
        "change from july to august",
        "new time for viewing",
    ),
    "catalog_business_document": (
        "ocr",
        "scan receipt",
        "pdf receipt",
        "invoice document",
        "maintenance expense receipt",
        "file against holding",
        "business document",
        "scanned bill",
    ),
    "file_business_document": (
        "post invoice expense",
        "put invoice on ledger",
        "file receipt expense",
        "expense already paid",
        "record paid invoice",
    ),
    "rename_business_document": (
        "rename receipt",
        "rename invoice",
        "change document name",
        "fix receipt title",
        "edit document title",
    ),
    "manage_business_documents": (
        "tag receipts",
        "move documents",
        "restore receipt from trash",
        "trash invoice",
        "rerun ocr",
        "mark receipt expense paid",
        "bulk document edit",
    ),
    "reorder_listing_media": (
        "reorder listing photos",
        "make photo first",
        "sort gallery",
    ),
    "manage_property_group": (
        "edit property group",
        "edit shared inventory",
        "update common area",
        "move room into group",
    ),
    "update_lease_roster": (
        "edit tenant rent share",
        "change tenant email",
        "make primary tenant",
        "edit lease roster",
    ),
    "schedule_appointment": (
        "schedule inspection",
        "book contractor",
        "calendar appointment",
    ),
    "manage_viewing_availability": (
        "edit viewing hours",
        "remove availability",
        "replace showing window",
    ),
    "manage_agenda_event": (
        "calendar reminder",
        "edit calendar event",
        "archive agenda event",
    ),
    "update_condition_inspection": (
        "edit inspection header",
        "add inspection item",
        "update keys handed over",
    ),
    "manage_import_rows": (
        "fix imported row",
        "map bank statement columns",
        "exclude import row",
    ),
    "manage_showcase_settings": (
        "change public listing slug",
        "edit showcase text",
        "publish showcase",
    ),
    "manage_insight": (
        "acknowledge insight",
        "dismiss recommendation",
        "reopen insight",
    ),
    "manage_notification_channel": (
        "link telegram",
        "notification channel settings",
        "disable whatsapp notifications",
    ),
    "update_treasurer_settings": (
        "treasurer consent",
        "tax rate settings",
        "tax province",
    ),
    "save_last_workflow": (
        "save this workflow",
        "remember this chain",
        "make this a macro",
    ),
    "run_saved_workflow": (
        "run saved workflow",
        "use my macro",
        "repeat saved chain",
    ),
    "business_document_status": (
        "what did ocr find",
        "invoice amount",
        "document status",
    ),
    "create_expense": (
        "maintenance expense",
        "record expense",
        "log receipt cost",
        "post expense",
    ),
    "link": (
        "public link",
        "listing url",
        "dashboard link",
        "open page",
    ),
    "public_property_link": (
        "public link",
        "listing for applicants",
        "rental url",
        "send prospect the listing",
    ),
    "charge_status": (
        "outstanding",
        "amount left",
        "overdue",
        "charge balance",
    ),
    "month_money": (
        "charged total",
        "received this month",
        "money in out net",
    ),
}

CORE_CAPABILITIES = (
    "search_capabilities",
    "portfolio_snapshot",
    "data_catalogue",
    "read",
    "link",
)


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    aliases: tuple[str, ...]
    description: str
    input_schema: dict
    risk: str
    requires_confirmation: bool

    def schema(self) -> dict:
        return {
            "name": self.key,
            "description": self.description,
            "parameters": self.input_schema,
        }


def get_capability(key: str) -> CapabilitySpec | None:
    tool = REGISTRY.get(key)
    if tool is None:
        return None
    meta = meta_for(key)
    return CapabilitySpec(
        key=key,
        aliases=CAPABILITY_ALIASES.get(key, ()),
        description=tool.description,
        input_schema=tool.parameters,
        risk=meta.risk,
        requires_confirmation="confirm" in tool.parameters.get("properties", {}),
    )


def catalogue(keys: Iterable[str] | None = None) -> list[CapabilitySpec]:
    names = keys if keys is not None else REGISTRY
    return [spec for name in names if (spec := get_capability(name)) is not None]


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").casefold()))


def _score(message: str, spec: CapabilitySpec) -> tuple[int, int, str]:
    query = _tokens(message)
    name_tokens = _tokens(spec.key.replace("_", " "))
    alias_tokens = _tokens(" ".join(spec.aliases))
    description_tokens = _tokens(spec.description)
    exact_alias = max(
        (len(_tokens(alias)) for alias in spec.aliases if alias.casefold() in message.casefold()),
        default=0,
    )
    score = (
        12 * len(query & name_tokens)
        + 8 * len(query & alias_tokens)
        + 2 * len(query & description_tokens)
        + 20 * exact_alias
    )
    return score, exact_alias, spec.key


def search_capability_catalogue(
    query: str,
    *,
    allowed_names: Iterable[str] | None = None,
    limit: int = 8,
) -> list[dict]:
    specs = catalogue(allowed_names)
    ranked = [
        spec
        for spec in sorted(specs, key=lambda spec: _score(query, spec), reverse=True)
        if _score(query, spec)[0] > 0
    ]
    return [
        {
            "key": spec.key,
            "description": spec.description,
            "aliases": list(spec.aliases),
            "risk": spec.risk,
            "requires_confirmation": spec.requires_confirmation,
        }
        for spec in ranked[: max(1, limit)]
    ]


def supported_tool_for_request(request: str) -> str | None:
    """Fail capability-gap logging closed for operations already implemented.

    These high-confidence business phrases intentionally remain deterministic;
    catalogue retrieval is advisory, while gap creation must not turn a weak
    similarity score into a false claim that the product supports something.
    """
    text = " ".join((request or "").casefold().split())
    if not text:
        return None
    if re.search(
        r"\b(morning|daily|every ?day|scheduled|recurring)\b.*"
        r"\b(update|updates|briefing|brief|digest|summary|message|report)\b",
        text,
    ) or re.search(r"\bmorning (brief|briefing|update)\b", text):
        return "get_notification_channels"
    if re.search(
        r"\b(leak|leaking|broken|not working|doesn'?t work|won'?t work|"
        r"clogged|blocked|cracked|damaged|faulty|jammed|stuck|no hot water|"
        r"no heat|flooding|dripping)\b",
        text,
    ):
        return "create_work_order"
    if re.search(
        r"\b(expense|cost|bill|charge|repair|invoice)\b.*"
        r"\b(wrong|mis-?scoped|misfiled|shouldn'?t be|should not be|belongs?|"
        r"should (be|go)|put (it |that )?(on|against)|against (the )?(wrong|other))\b"
        r"|\b(move|reallocate|re-?assign|rebook|re-?book|shift|transfer|relocate|"
        r"re-?file|refile)\b.*"
        r"\b(expense|cost|bill|repair|invoice|ledger)\b"
        r"|\b(expense|cost|bill|repair)\b.*\b(to the (address|house|building|property|holding)|"
        r"off (of )?(the )?room|to (room|suite|unit)|from (room|suite|unit))\b"
        r"|\b(that|the) (expense|cost|bill|\$[\d.]+).*\b(should be|belongs) (on|at|to)\b"
        r"|\breallocate_expense\b",
        text,
    ):
        return "reallocate_expense"
    # Email / invite delivery — map to viewing_invite_status (has email + open fields).
    if re.search(
        r"\b(did|has).*\b(email|invite|invitation)\b.*(deliver|bounce|bounced|arrive|sent)"
        r"|\b(email|invite).*\b(deliver|bounce|bounced|opened|open)"
        r"|\b(have they|did they).*\b(get|receive|see).*\b(email|invite|link)\b"
        r"|\b(bounce|bounced|undeliverable)\b.*\b(email|invite)\b",
        text,
    ):
        return "viewing_invite_status"
    # Existing document metadata edits are not a new upload/catalog request.
    if re.search(
        r"\b(rename|retitle|change|fix|edit)\b.*\b"
        r"(receipt|invoice|document|notice|pdf)\b"
        r"|\b(receipt|invoice|document|notice|pdf)\b.*\b"
        r"(rename|retitle|change (?:the )?(?:name|title)|fix (?:the )?(?:name|title))\b",
        text,
    ):
        return "rename_business_document"
    if re.search(
        r"\b(tag|trash|restore|re-?ocr|move)\b.*\b"
        r"(receipt|invoice|document|pdf)\b"
        r"|\b(receipt|invoice|document|pdf)\b.*\b"
        r"(tag|trash|restore|re-?ocr|move)\b",
        text,
    ):
        return "manage_business_documents"
    if re.search(
        r"\b(save|remember|name)\b.*\b(workflow|macro|chain)\b",
        text,
    ):
        return "save_last_workflow"
    if re.search(
        r"\b(run|repeat|use)\b.*\b(saved workflow|macro|saved chain)\b",
        text,
    ):
        return "run_saved_workflow"
    # Receipts / PDFs / OCR — never allow a false "I can't scan" gap.
    if re.search(
        r"\b(ocr|scan|scanned|scanner)\b"
        r"|\b(receipt|invoice|pdf|statement|notice|paperwork)\b.+\b(expense|maintenance|holding|address|house|property)\b"
        r"|\b(maintenance expense|record (an? )?expense|log (an? )?(receipt|expense|bill))\b"
        r"|\b(file|catalog|archive)\b.+\b(document|receipt|invoice|pdf)\b",
        text,
    ):
        return "catalog_business_document"
    # Edit existing unlocked lease (before create — "change start date" is not create).
    if re.search(
        r"\b(change|update|edit|move|set)\b.+\b(start date|end date|lease start|lease end)\b"
        r"|\b(start date|end date)\b.+\b(from|to)\b.+\b\d{4}-\d{2}-\d{2}\b"
        r"|\b(make|mark|set)\b.+\b(furnished|semi[- ]?furnished|unfurnished)\b"
        r"|\b(furnished|semi[- ]?furnished|unfurnished)\b.+\b(lease|room|listing)\b"
        r"|\badjust_lease\b|\bchange the lease\b",
        text,
    ):
        return "adjust_lease"
    if re.search(
        r"\b(renew|renewal|roll over|rollover|extend)\b.+\b(lease|tenancy|term)\b"
        r"|\brenew (the |this )?lease\b|\banother (year|term) (on|for) (the )?lease\b",
        text,
    ):
        return "renew_lease"
    if re.search(
        r"\b(end|ending|terminate)\b.+\b(tenancy|move[- ]?out)\b"
        r"|\b(move[- ]?out|mutual agreement)\b.+\b(end|notice|deposit)\b"
        # "settle the deposit" is the compliance record (which of the three
        # lawful routes was taken). "return the deposit" is the money going
        # back, which is return_deposits — a different tool since deposits are
        # returned one by one.
        r"|\bsettle\b.+\b(deposit|security deposit)\b"
        r"|\blandlord notice\b.+\b(end|vacate)\b",
        text,
    ):
        return "settle_moveout"
    if re.search(
        r"\b(condition|move[- ]?in|move[- ]?out)\b.+\binspection\b"
        r"|\binspection (package|report|walkthrough)\b"
        r"|\brtb[- ]?27\b|\bcondition inspection\b",
        text,
    ):
        return "complete_inspection_package"
    if re.search(
        r"\b(rent )?(discount|increase|proration|adjustment)\b"
        r"|\b(reduce|raise|lower|increase)\b.+\brent\b"
        r"|\bapply_rent_adjustment\b",
        text,
    ):
        return "apply_rent_adjustment"
    if re.search(
        r"\b(utility|hydro|electricity|gas|water)\b.+\bbill\b"
        r"|\bbill\b.+\b(utility|hydro|electricity|gas|water)\b"
        r"|\bsplit (the )?utility\b|\brecord_utility_bill\b",
        text,
    ):
        return "record_utility_bill"
    if re.search(
        r"\b(inquiry|lead|enquiry)\b.+\b(viewing|showing|appointment|tour)\b"
        r"|\b(viewing|showing)\b.+\b(inquiry|lead|enquiry)\b"
        r"|\bconvert_inquiry\b|\bto_appointment\b",
        text,
    ):
        return "convert_inquiry_to_viewing"
    if re.search(
        r"\bvoid\b.+\b(charge|expense|entry|payment|\d)\b"
        r"|\breverse\b.+\b(ledger|charge|expense)\b"
        r"|\bvoid both\b|\bvoid the (wrong |two |\$)?",
        text,
    ):
        return "void_ledger_entry"
    if re.search(
        r"\b(mark|marked)\b.+\b(expense|bill|draino|invoice)\b.+\bpaid\b"
        r"|\b(expense|bill|draino|invoice)\b.+\b(mark|marked|needs?).+\bpaid\b"
        r"|\b(paid|cleared)\b.+\b(from )?(my )?(bank|account)\b"
        r"|\bexpense (is )?paid\b"
        r"|\bnot yet taken\b"
        r"|\bwhy does it say not yet\b"
        r"|\bneeds? to be (marked )?paid\b",
        text,
    ):
        return "mark_ledger_paid"
    if re.search(
        r"\b(correct|fix)\b.+\b(expense|charge|ledger|entry|amount)\b"
        r"|\bwrong (amount|description)\b.+\b(expense|charge)\b",
        text,
    ):
        return "correct_ledger_entry"
    if re.search(
        r"\b(damage|late fee|one[- ]?off)\b.+\bcharge\b"
        r"|\bcharge (the )?tenant\b.+\b(for|damage|fee)\b",
        text,
    ):
        return "post_one_off_charge"
    if re.search(
        r"\bcancel\b.+\b(viewing|showing|appointment)\b"
        r"|\b(viewing|showing)\b.+\bcancel",
        text,
    ):
        return "cancel_viewing"
    if re.search(
        r"\b(cleaning (?:deposit|fee))\b.+\bpaid\b"
        r"|\bpaid\b.+\bcleaning (?:deposit|fee)\b",
        text,
    ):
        return "mark_cleaning_deposit_paid"
    # Deposits out. Kept ahead of create_lease, whose "rent ... deposit" rule
    # would otherwise swallow "return their deposit" on a lease with rent in
    # the same sentence.
    if re.search(
        r"\b(return|refund|give|send|pay|hand)\w*\b.{0,30}\bdeposits?\b"
        r"|\bdeposits?\b.{0,30}\b(back|returned|refunded)\b",
        text,
    ):
        return "return_deposits"
    if re.search(
        r"\b(deduct\w*|withhold\w*|keep|retain\w*)\b.{0,30}\bdeposits?\b"
        r"|\bdeposits?\b.{0,30}\bdeduct"
        r"|\b(garbage|rubbish|junk)\b.{0,20}\b(removal|haul|run|fee)\b"
        r"|\bdump(ing)?\b.{0,10}\b(run|fee|charge)\b"
        r"|\b(cleaning|cleaners?)\b.{0,20}\b(cost|charge|bill|invoice|hours)\b",
        text,
    ):
        return "record_deposit_deduction"
    # Setting a deposit on a lease that ALREADY EXISTS. Asked to "add a $200
    # cleaning deposit to lease RMT415536-0617", retrieval offered create_lease,
    # adjust_lease and mark_cleaning_deposit_paid and never offered
    # update_lease — so the model reported, accurately for the menu it had,
    # that it could only set deposits while creating a lease. It then went on
    # to invent a reason ("already signed and active, so the deposit fields are
    # locked") for a PENDING lease that was editable the whole time. Pinning
    # the tool removes the opportunity: the tool's own guard answers the lock
    # question truthfully, and the model never has to guess it.
    #
    # Kept ahead of create_lease, whose "rent … deposit" rule would otherwise
    # swallow this, and behind the paid/deduction/return rules, which are more
    # specific things to do with a deposit.
    if not re.search(
        r"\b(create|draft|make|new)\b.{0,30}\b(lease|tenancy|agreement)\b", text
    ) and re.search(
        r"\b(add|set|change|update|edit|correct|fix|amend|put)\b"
        r"[^.?!]{0,60}\b(?:security |pet |cleaning )?deposits?\b"
        r"|\b(edit|update|change|amend)\b[^.?!]{0,25}\blease\b",
        text,
    ):
        return "update_lease"
    if re.search(
        r"\b(payment|rent)\b.+\breminder\b|\bremind\b.+\b(rent|payment)\b",
        text,
    ):
        return "create_payment_reminder"
    if re.search(
        r"\barchive\b.+\binquir"
        r"|\binquir(y|ies)\b.+\b(archive|notes)\b",
        text,
    ):
        return "update_inquiry"
    # Leases — the most common false "I can't create a lease" gap.
    # Deliberately exclude co-landlord / co-host / invite-only phrasings.
    if not re.search(
        r"\b(co-?landlord|co-?host|another landlord|property manager)\b",
        text,
    ) and re.search(
        r"\b(create|draft|make|start|new)\b.+\b(lease|tenancy|rental agreement)\b"
        r"|\b(draft|new)\b.+\blease\b"
        r"|\blease for (room|suite|unit)\b"
        r"|\blease term\b|\btotal monthly rent\b"
        r"|\b(monthly )?rent\b.+\b(security )?deposit\b",
        text,
    ):
        return "create_lease"
    if re.search(
        r"\b(invite|send)\b.+\b(lease|tenant|renter|sign)\b"
        r"|\bsend (the )?lease\b|\binvite .+@(?:gmail|yahoo|hotmail|outlook|icloud)",
        text,
    ):
        return "invite_tenant_to_lease"
    if re.search(
        r"\b(has|did|have)\b.+\b(signed|viewed|opened|clicked|seen)\b.+\b(lease|invite)\b"
        r"|\b(signed|viewed|opened|seen)\b.+\b(lease|invite)\b"
        r"|\blast (seen|opened|viewed)\b.+\b(lease|invite|tenant)\b"
        r"|\bwhen did .+\b(open|see|view)\b.+\b(lease|invite)\b"
        r"|\bcreated an? account\b|\bsigned up\b"
        r"|\bhas \w+ signed\b|\bhas \w+ seen\b",
        text,
    ):
        return "tenant_lease_status"
    if re.search(
        r"\b(reschedule|re-schedule|move|change)\b.+\b(viewing|showing|appointment)\b"
        r"|\b(viewing|showing)\b.+\b(from|to)\b.+\b(am|pm|\d{1,2}:\d{2}|august|july|june|may|september)\b"
        r"|\bchange (the )?(viewing|showing) (time|date|to)\b",
        text,
    ):
        return "reschedule_viewing"
    if re.search(
        r"\b(seen|opened|clicked|viewed)\b.+\b(viewing|invite|link|status)\b"
        r"|\b(viewing|invite|status)\b.+\b(seen|opened|clicked|viewed)\b"
        r"|\bhave they (seen|opened)\b"
        r"|\bdid they open\b",
        text,
    ):
        return "viewing_invite_status"
    if re.search(
        r"\b(schedule|book|set up|make|create|arrange)\b.+\b"
        r"(viewing|showing|tour|appointment)\b"
        r"|\bviewing\b.+\b(tomorrow|today|at \d|@|pm|am)\b"
        r"|\b(viewing|showing)\b.+\b(send|email|invite)\b",
        text,
    ):
        return "schedule_viewing"
    if re.search(
        r"\b(remove|delete|take off)\b.+\b(photo|photos|image|images|picture|pictures)\b"
        r"|\b(photo|photos|image|images)\b.+\b(remove|delete)\b",
        text,
    ):
        return "remove_photos_from_listing"
    if re.search(
        r"\b(add|attach|upload)\b.+\b(photo|photos|pic|pics|image|images|picture|pictures)\b"
        r"|\b(photo|photos|pics)\b.+\b(listing|property|suite|room)\b",
        text,
    ):
        return "attach_photo_to_listing"
    if re.search(r"\brename\b.+\bto\b", text):
        return "update_property"
    if (
        re.search(r"\b(link|open|view|show|go to|take me to|where|check)\b", text)
        and re.search(
            r"\b(dashboard|properties|property groups|documents|leases|"
            r"finances?|financial|maintenance|settings|calendar|appointments?|"
            r"viewings?|showings?|visits?)\b",
            text,
        )
    ):
        return "link"
    if re.search(
        r"\b(public link|listing url|listing for applicants|"
        r"send (me )?(the )?(public |applicant |rental )?link|"
        r"www\.rentium\.ca|rentium\.ca/[a-z]{2}/|"
        r"(shareable|prospect|tenant-facing|logged-?out) link)\b",
        text,
    ) or (
        re.search(r"\b(public|applicant|rental|prospect)\b.+\blink\b", text)
        and re.search(r"\b(listing|property|room|suite|garden)\b", text)
    ) or (
        re.search(r"\blink\b", text)
        and re.search(r"\b(like this|www\.|public page|public listing)\b", text)
    ):
        return "public_property_link"
    if re.search(r"\b(show|list|view)\b.*\b(all|every|my)\b.*\brooms?\b", text):
        return "list_properties"
    if (
        re.search(r"\b(create|add|make)\b", text)
        and re.search(r"\b(house|holding|building)\b", text)
        and re.search(r"\b(property )?groups?\b", text)
        and re.search(r"\brooms?\b", text)
    ):
        return "create_house_layout"
    if (
        (
            re.search(r"\b(create|add|make)\b", text)
            and re.search(r"\brooms?\b", text)
            and re.search(r"\b(suite|floor|unit)\b", text)
        )
        or re.search(
            r"\b(convert|turn|divide|split)\b.+\b(suite|unit|floor)\b.+\brooms?\b"
            r"|\bchange how\b.+\brented\b"
            r"|\brent\b.+\b(by room|room by room|room-by-room)\b",
            text,
        )
    ):
        return "configure_unit_room_offerings"
    if (
        re.search(r"\b(create|add|make)\b", text)
        and re.search(r"\broom\b", text)
        and re.search(r"\b(property )?group\b", text)
    ):
        return "create_group_room"
    if re.search(r"\b(create|add)\b.*\bproperty group\b", text):
        return "create_property_group"
    if re.search(r"\b(move|assign|add|remove)\b.*\broom\b.*\bgroup\b", text):
        return "assign_property_to_group"
    return None


def select_tool_schemas(
    message: str,
    schemas: list[dict],
    *,
    limit: int = 12,
) -> list[dict]:
    """Return a stable, relevant subset from an already role-filtered surface."""
    if len(schemas) <= limit:
        return schemas
    by_name = {schema["name"]: schema for schema in schemas}
    ranked = search_capability_catalogue(
        message,
        allowed_names=by_name,
        limit=max(limit * 2, limit),
    )
    selected: list[dict] = []
    for name in CORE_CAPABILITIES:
        if name in by_name and by_name[name] not in selected:
            selected.append(by_name[name])
    # Pin tools that the weak model otherwise invents gaps for — even when
    # retrieval ranks them below unrelated catalogue noise.
    forced = supported_tool_for_request(message)
    if forced and forced in by_name and by_name[forced] not in selected:
        selected.append(by_name[forced])
    # Lease drafts almost always need invite as the next step.
    if forced == "create_lease" and "invite_tenant_to_lease" in by_name:
        invite = by_name["invite_tenant_to_lease"]
        if invite not in selected:
            selected.append(invite)
    for row in ranked:
        schema = by_name.get(row["key"])
        if schema is not None and schema not in selected:
            selected.append(schema)
        if len(selected) >= limit:
            break
    # Delegation schemas are not registry capabilities. Score them locally and
    # include only when relevant, while preserving the same total limit.
    if len(selected) < limit:
        query = _tokens(message)
        extras = sorted(
            (schema for schema in schemas if schema["name"] not in REGISTRY),
            key=lambda schema: len(
                query
                & _tokens(
                    f"{schema['name']} {schema.get('description', '')}",
                ),
            ),
            reverse=True,
        )
        for schema in extras:
            if schema not in selected:
                selected.append(schema)
            if len(selected) >= limit:
                break
    return selected[:limit]
