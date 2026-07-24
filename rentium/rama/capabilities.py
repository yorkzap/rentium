"""Deterministic recognition of already-supported RAMA requests."""

from __future__ import annotations

import re


def supported_tool_for_request(request: str) -> str | None:
    text = " ".join((request or "").casefold().split())
    if not text:
        return None
    tool = None
    if re.search(r"\brename\b.+\bto\b", text):
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
