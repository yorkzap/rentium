"""Hybrid retrieval for versioned official and private unstructured knowledge.

Operational state never enters this index.  Lease/signature/payment/status facts
come from domain projections.  This store is for official law/reference text and
the landlord's own policies or documents, always with source metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.postgres.search import SearchQuery
from django.contrib.postgres.search import SearchRank
from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pgvector.django import CosineDistance

if TYPE_CHECKING:
    from datetime import date

DEFAULT_OFFICIAL_DOMAINS = frozenset(
    {
        "canada.ca",
        "www.canada.ca",
        "gov.bc.ca",
        "www2.gov.bc.ca",
        "bclaws.gov.bc.ca",
        "ontario.ca",
        "www.ontario.ca",
        "tribunalsontario.ca",
        "publications.saskatchewan.ca",
    },
)
_UNAPPROVED_AUTHORITY = "is not an approved official authority"
_PRIVATE_SCOPE_REQUIRED = "Private knowledge must be scoped to one landlord."
_INVALID_KIND = "kind must be OFFICIAL or PRIVATE."


def official_domains() -> frozenset[str]:
    configured = getattr(settings, "RAMA_OFFICIAL_SOURCE_DOMAINS", ()) or ()
    return (
        frozenset(str(value).casefold() for value in configured)
        or DEFAULT_OFFICIAL_DOMAINS
    )


def validate_official_url(url: str) -> None:
    host = (urlparse(url or "").hostname or "").casefold()
    if not host or host not in official_domains():
        authority = host or "That URL"
        message = f"{authority} {_UNAPPROVED_AUTHORITY}."
        raise ValueError(message)


@transaction.atomic
def store_source(  # noqa: PLR0913
    *,
    kind: str,
    title: str,
    sha256: str,
    chunks: list[dict],
    landlord=None,
    authority: str = "",
    canonical_url: str = "",
    jurisdiction: str = "",
    effective_from=None,
    effective_to=None,
    version: str = "",
    metadata: dict | None = None,
):
    """Store one immutable source version after authority/scope validation."""
    from .models import RamaKnowledgeSource

    normalized_kind = str(kind or "").upper()
    if normalized_kind == RamaKnowledgeSource.Kind.OFFICIAL:
        validate_official_url(canonical_url)
        landlord = None
    elif normalized_kind == RamaKnowledgeSource.Kind.PRIVATE:
        if landlord is None:
            raise ValueError(_PRIVATE_SCOPE_REQUIRED)
    else:
        raise ValueError(_INVALID_KIND)
    source, created = RamaKnowledgeSource.objects.get_or_create(
        kind=normalized_kind,
        landlord=landlord,
        sha256=sha256,
        defaults={
            "title": title,
            "authority": authority,
            "canonical_url": canonical_url,
            "jurisdiction": jurisdiction,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "version": version,
            "metadata": metadata or {},
        },
    )
    if created:
        replace_chunks(source, chunks)
    return source, created


@transaction.atomic
def replace_chunks(source, chunks: list[dict]) -> None:
    """Replace one source version's contextual chunks and FTS vectors."""
    from .models import RamaKnowledgeChunk

    source.chunks.all().delete()
    rows = [
        RamaKnowledgeChunk(
            source=source,
            ordinal=index,
            heading=str(chunk.get("heading") or "")[:300],
            context=str(chunk.get("context") or ""),
            content=str(chunk.get("content") or ""),
            citation_label=str(chunk.get("citation_label") or source.title)[:300],
            embedding=chunk.get("embedding"),
            embedding_model=str(chunk.get("embedding_model") or "")[:120],
            token_count=int(chunk.get("token_count") or 0),
            metadata=dict(chunk.get("metadata") or {}),
        )
        for index, chunk in enumerate(chunks)
        if str(chunk.get("content") or "").strip()
    ]
    RamaKnowledgeChunk.objects.bulk_create(rows)
    source.chunks.update(
        search_vector=(
            SearchVector("heading", weight="A", config="english")
            + SearchVector("context", weight="A", config="english")
            + SearchVector("content", weight="B", config="english")
        ),
    )


def retrieve(  # noqa: PLR0913
    *,
    landlord,
    query: str,
    jurisdiction: str = "",
    as_of: date | None = None,
    official_only: bool = False,
    query_embedding: list[float] | None = None,
    limit: int = 5,
) -> dict:
    """Metadata-prefiltered FTS + vector retrieval combined with RRF."""
    from .models import RamaKnowledgeChunk
    from .models import RamaKnowledgeSource

    as_of = as_of or timezone.localdate()
    source_filter = (
        Q(source__active=True)
        & (
            Q(source__effective_from__isnull=True)
            | Q(source__effective_from__lte=as_of)
        )
        & (Q(source__effective_to__isnull=True) | Q(source__effective_to__gte=as_of))
    )
    if official_only:
        source_filter &= Q(source__kind=RamaKnowledgeSource.Kind.OFFICIAL)
    else:
        source_filter &= Q(source__kind=RamaKnowledgeSource.Kind.OFFICIAL) | Q(
            source__kind=RamaKnowledgeSource.Kind.PRIVATE,
            source__landlord=landlord,
        )
    if jurisdiction:
        source_filter &= Q(source__jurisdiction__iexact=jurisdiction) | Q(
            source__jurisdiction="",
        )
    base = RamaKnowledgeChunk.objects.filter(source_filter).select_related("source")
    candidate_limit = max(limit * 4, 20)
    text_query = SearchQuery(query, config="english", search_type="websearch")
    lexical = list(
        base.annotate(rank=SearchRank("search_vector", text_query))
        .filter(rank__gt=0)
        .order_by("-rank")[:candidate_limit],
    )
    vector = []
    if query_embedding:
        vector = list(
            base.exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance("embedding", query_embedding))
            .order_by("distance")[:candidate_limit],
        )

    scores: dict[int, float] = {}
    objects = {}
    for ranking in (lexical, vector):
        for rank, chunk in enumerate(ranking, start=1):
            objects[chunk.pk] = chunk
            scores[chunk.pk] = scores.get(chunk.pk, 0.0) + 1.0 / (60 + rank)
    ordered = sorted(scores, key=scores.get, reverse=True)[: max(1, min(limit, 20))]
    rows = []
    for chunk_id in ordered:
        chunk = objects[chunk_id]
        rows.append(
            {
                "chunk_id": chunk.pk,
                "title": chunk.source.title,
                "authority": chunk.source.authority,
                "url": chunk.source.canonical_url,
                "jurisdiction": chunk.source.jurisdiction,
                "effective_from": (
                    chunk.source.effective_from.isoformat()
                    if chunk.source.effective_from
                    else None
                ),
                "effective_to": (
                    chunk.source.effective_to.isoformat()
                    if chunk.source.effective_to
                    else None
                ),
                "version": chunk.source.version,
                "heading": chunk.heading,
                "context": chunk.context,
                "content": chunk.content,
                "citation_label": chunk.citation_label or chunk.source.title,
                "rrf_score": scores[chunk_id],
            },
        )
    return {
        "query": query,
        "coverage": "PARTIAL",
        "answer_mode": "EXPLORATORY",
        "results": rows,
        "citations_required": True,
        "negative_conclusions_allowed": False,
    }
