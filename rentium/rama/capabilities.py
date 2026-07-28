"""Deterministic recognition of already-supported RAMA requests."""

from __future__ import annotations

import re


def supported_tool_for_request(request: str) -> str | None:
    text = " ".join((request or "").casefold().split())
    if not text:
        return None
    tool = None
    # A daily briefing already ships (Celery beat, 07:00, per-channel opt-in).
    # Without this, "how come no morning updates?" was answered by denying the
    # feature and offering to log a gap for it.
    if re.search(
        r"\b(morning|daily|every ?day|scheduled|recurring)\b.*"
        r"\b(update|updates|briefing|brief|digest|summary|message|report)\b",
        text,
    ) or re.search(r"\bmorning (brief|briefing|update)\b", text):
        tool = "get_notification_channels"
    # A described fault is a work order RAMA can raise itself, rather than
    # pointing the landlord at the maintenance dashboard to do it by hand.
    elif re.search(
        r"\b(leak|leaking|broken|not working|doesn'?t work|won'?t work|"
        r"clogged|blocked|cracked|damaged|faulty|jammed|stuck|no hot water|"
        r"no heat|flooding|dripping)\b",
        text,
    ):
        tool = "create_work_order"
    # Money already on the books, booked in the wrong place. This one is here
    # for the opposite reason to the rest: the others stop RAMA denying a
    # feature it has, this stops it IMPROVISING one it didn't. Asked to move a
    # shared-space repair off a single room, RAMA had create_expense but no
    # reallocation tool, so it composed a fresh expense with an out-of-band
    # void — three unlinked ledger rows for one $19.78 repair, and no recorded
    # reason. A correction to posted money is one named operation or it is a
    # gap; it is never assembled out of parts.
    elif re.search(
        r"\b(expense|cost|bill|charge|repair|invoice)\b.*"
        r"\b(wrong|mis-?scoped|misfiled|shouldn'?t be|should not be|belongs?)\b"
        r"|\b(move|reallocate|re-?assign|rebook|re-?book|shift|transfer)\b.*"
        r"\b(expense|cost|bill|repair|invoice)\b"
        r"|\b(expense|cost|bill|repair)\b.*\b(to the (address|house|building|property)|"
        r"off (of )?(the )?room)\b",
        text,
    ):
        tool = "reallocate_expense"
    elif re.search(r"\brename\b.+\bto\b", text):
        tool = "update_property"
    elif (
        re.search(r"\b(link|open|view|show|go to|take me to)\b", text)
        and re.search(
            r"\b(dashboard|properties|property groups|documents|leases|"
            r"finances?|financial|maintenance|settings)\b",
            text,
        )
    ):
        tool = "link"
    elif re.search(
        r"\b(show|list|view)\b.*\b(all|every|my)\b.*\brooms?\b",
        text,
    ):
        tool = "list_properties"
    elif (
        re.search(r"\b(create|add|make)\b", text)
        and re.search(r"\b(house|holding|building)\b", text)
        and re.search(r"\b(property )?groups?\b", text)
        and re.search(r"\brooms?\b", text)
    ):
        tool = "create_house_layout"
    elif (
        re.search(r"\b(create|add|make)\b", text)
        and re.search(r"\broom\b", text)
        and re.search(r"\b(property )?group\b", text)
    ):
        tool = "create_group_room"
    elif re.search(r"\b(create|add)\b.*\bproperty group\b", text):
        tool = "create_property_group"
    elif re.search(r"\b(move|assign|add|remove)\b.*\broom\b.*\bgroup\b", text):
        tool = "assign_property_to_group"
    return tool
