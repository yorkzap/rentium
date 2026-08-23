"""Redacted real-transcript evals and executable certification gates."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

from django.db import DatabaseError
from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)",
)
_LEASE = re.compile(r"\bRMT[A-Z0-9-]+\b", re.IGNORECASE)
_MIN_REDACTION_VALUE_LENGTH = 3
_MIN_CERTIFICATION_ATTEMPTS = 5
_MIN_CERTIFICATION_SCORE = 0.98

logger = logging.getLogger(__name__)


def redact_text(text: str, *, known_values: Iterable[str] = ()) -> str:
    """Remove direct identifiers while retaining the shape of a failure."""
    redacted = str(text or "")
    replacements = sorted(
        {
            str(value).strip()
            for value in known_values
            if len(str(value).strip()) >= _MIN_REDACTION_VALUE_LENGTH
        },
        key=len,
        reverse=True,
    )
    for index, value in enumerate(replacements, start=1):
        redacted = re.sub(re.escape(value), f"[ENTITY_{index}]", redacted, flags=re.I)
    redacted = _EMAIL.sub("[EMAIL]", redacted)
    redacted = _PHONE.sub("[PHONE]", redacted)
    return _LEASE.sub("[LEASE_ID]", redacted)


def input_fingerprint(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def evidence_summary(packet: dict) -> dict:
    facts = packet.get("facts") or []
    sources = {
        (
            str((fact.get("source") or {}).get("model") or ""),
            str((fact.get("source") or {}).get("field") or ""),
        )
        for fact in facts
    }
    return {
        "facet": packet.get("facet"),
        "coverage": packet.get("coverage"),
        "fact_count": len(facts),
        "source_fields": [
            {"model": model, "field": field} for model, field in sorted(sources)
        ],
        "missing": packet.get("missing") or [],
        "unavailable": packet.get("unavailable") or [],
        "conflict_count": len(packet.get("conflicts") or []),
        "allowed_negative_conclusions": packet.get("allowed_negative_conclusions")
        or [],
    }


def record_evidence_trace(  # noqa: PLR0913
    *,
    landlord,
    conversation_id,
    message_id,
    engine_mode: str,
    answer_mode: str,
    coverage: str,
    provider: str,
    model: str,
    intent: str,
    subject: dict,
    packet: dict,
    raw_input: str,
    known_values: Iterable[str] = (),
    violations: list | None = None,
):
    from .models import RamaEvidenceTrace

    # Diagnostic persistence must never make a factual answer unavailable. The
    # nested atomic block creates a savepoint, so even a missing trace table
    # cannot poison an enclosing request/test transaction.
    try:
        with transaction.atomic():
            return RamaEvidenceTrace.objects.create(
                landlord=landlord,
                conversation_id=conversation_id,
                message_id=message_id,
                engine_mode=engine_mode,
                answer_mode=answer_mode,
                coverage=coverage,
                provider=provider,
                model=model,
                intent=intent,
                subject={
                    "type": subject.get("type", ""),
                    "id_hash": input_fingerprint(subject.get("id", ""))[:16]
                    if subject.get("id")
                    else "",
                },
                evidence_summary=evidence_summary(packet),
                violations=violations or [],
                input_hash=input_fingerprint(raw_input),
                redacted_input=redact_text(raw_input, known_values=known_values),
            )
    except DatabaseError:
        logger.exception("RAMA evidence trace could not be persisted")
        return None


def grade_case(case, result: dict) -> dict:
    """Code-grade factual/control claims; style is intentionally not authority."""
    expected = case.expected or {}
    text = str(result.get("reply") or "")
    folded = text.casefold()
    failures = [
        f"missing required claim: {claim}"
        for claim in expected.get("required_claims") or []
        if str(claim).casefold() not in folded
    ]
    prohibited = list(case.prohibited_claims or []) + list(
        expected.get("prohibited_claims") or [],
    )
    failures.extend(
        f"unsupported/prohibited claim: {claim}"
        for claim in prohibited
        if str(claim).casefold() in folded
    )
    for key in ("answer_mode", "coverage"):
        wanted = expected.get(key)
        if wanted and result.get(key) != wanted:
            failures.append(f"{key} expected {wanted}, received {result.get(key)}")
    if expected.get("must_not_mutate") and result.get("effects"):
        failures.append("read-only case produced effects")
    if expected.get("confirmation_required") and not result.get("pending_plan"):
        failures.append("write did not produce a persisted confirmation plan")
    return {
        "passed": not failures,
        "failures": failures,
        "scores": {
            "facts": 1.0 if not failures else 0.0,
            "critical": 1.0 if not failures and case.severity == "critical" else 0.0,
        },
    }


def certification_metrics(run) -> dict:
    rows = list(run.results.select_related("case"))
    total = len(rows)
    critical = [row for row in rows if row.case.severity == "critical"]
    passed = sum(row.passed for row in rows)
    critical_passed = sum(row.passed for row in critical)
    attempts_complete = bool(rows) and all(
        sum(1 for row in rows if row.case_id == case_id) >= run.attempts_per_case
        for case_id in {row.case_id for row in rows}
    )
    unsupported = sum(
        any("unsupported/prohibited" in failure for failure in row.failures)
        for row in rows
    )
    return {
        "attempts_complete": attempts_complete,
        "attempts_per_case": run.attempts_per_case,
        "total_attempts": total,
        "overall_accuracy": (passed / total if total else 0.0),
        "critical_accuracy": (critical_passed / len(critical) if critical else 0.0),
        "unsupported_claims": unsupported,
        "retrieval_recall_at_5": float(
            (run.metrics or {}).get("retrieval_recall_at_5", 0.0),
        ),
    }


def passes_certification_gates(metrics: dict) -> bool:
    return bool(
        metrics.get("attempts_complete")
        and metrics.get("attempts_per_case", 0) >= _MIN_CERTIFICATION_ATTEMPTS
        and metrics.get("critical_accuracy") == 1.0
        and metrics.get("unsupported_claims") == 0
        and metrics.get("overall_accuracy", 0.0) >= _MIN_CERTIFICATION_SCORE
        and metrics.get("retrieval_recall_at_5", 0.0) >= _MIN_CERTIFICATION_SCORE,
    )


@transaction.atomic
def finalize_and_certify(run, *, model_version: str = ""):
    from .models import RamaEvalRun
    from .models import RamaModelCertification

    metrics = certification_metrics(run)
    passed = passes_certification_gates(metrics)
    run.metrics = {**(run.metrics or {}), **metrics}
    run.status = RamaEvalRun.Status.PASSED if passed else RamaEvalRun.Status.FAILED
    run.finished_at = timezone.now()
    run.save(update_fields=["metrics", "status", "finished_at"])
    certification, _ = RamaModelCertification.objects.update_or_create(
        provider=run.provider,
        model=run.model,
        model_version=model_version or run.model_version,
        domain_lane=run.domain_lane,
        eval_suite_version=run.suite_version,
        defaults={
            "status": (
                RamaModelCertification.Status.CERTIFIED
                if passed
                else RamaModelCertification.Status.CANDIDATE
            ),
            "metrics": metrics,
            "eval_run_id": run.pk,
            "certified_at": timezone.now() if passed else None,
        },
    )
    return certification
