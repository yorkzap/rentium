"""Versioned runtime policy text for RAMA's model-facing execution contract.

The Markdown is guidance, not authority. Tool schemas, domain validation,
pending plans, confirmations, and receipts remain the executable contract.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def execution_policy() -> str:
    path = Path(__file__).with_name("policies") / "EXECUTION_CONTRACT.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        # Deployments should package the policy file, but losing prompt guidance
        # must never disable the server-side confirmation and validation guards.
        return ""
