# RAMA evidence-first architecture

## Product boundary

RAMA has two explicit answer products:

- **Certified core** — deterministic operational reads and guarded plans over
  structured Rentium records. Critical identities, signatures, money, dates,
  statuses, and write targets are rendered from typed facts rather than model
  prose.
- **Exploratory** — read-only synthesis when the question is outside a
  certified lane. It must identify partial coverage and cite retrieved
  unstructured sources. It cannot authorize writes.

When records are partial, unavailable, ambiguous, or conflicting, the safe
answer is **Abstained** with the exact missing fact or ambiguity. “Unavailable”
never means “no.”

## Operational truth

`rentium.core.evidence.FactPacket` is the shared contract:

- `subject`, `facet`, and `as_of`
- `coverage`: `COMPLETE`, `PARTIAL`, or `CONFLICTING`
- typed facts whose source names the Django model, record, and field
- missing/unavailable fields and conflicts
- exact negative conclusions allowed by a complete query

The first projections are:

| Facet | Owner |
| --- | --- |
| `lease.signing_roster` | `rentium.leases.parties` |
| `lease.terms_and_status` | `rentium.rama.fact_projections` |
| `property.structure_and_offerings` | `rentium.rama.fact_projections` |
| `ledger.account_position` | `ledger.position` via the projection adapter |
| `maintenance.case_status` | `rentium.rama.fact_projections` |

The lease signing roster distinguishes three facts that must not be collapsed:

1. Every named owner, co-landlord, and non-cancelled tenant is expected to sign.
2. Activation requires the owner, every co-landlord, and at least one tenant.
3. A tenant who is not individually required for activation remains an unsigned
   expected signer and may sign later under the current product policy.

Agreement parties, signature rows, form binding, REST, and RAMA consume this
same roster.

## Orchestration and rollout

`RamaPreferences.web_orchestration_mode` and
`telegram_orchestration_mode` accept:

- `legacy` — existing broad orchestrator
- `shadow` — build and trace a v3 answer, keep the visible legacy answer
- `v3` — use a matching certified core route; fall back to legacy for currently
  unsupported intents

The chat API remains backward compatible and additionally returns
`answer_mode`, `coverage`, `evidence`, `trace_id`, and `engine_mode`. Existing
confirmation remains bound to a persisted plan ID, episode, task, and visible
prompt. Model text is never authorization.

## Evals and certification

Reviewed transcripts are redacted before becoming `RamaEvalCase` rows. Splits
are by conversation: development, regression, and hidden holdout. The checked-in
`core-leases-v1` suite pins the multi-turn missing co-landlord failure.

Each model must run every applicable case five times. Certification gates are
executable in `rentium.rama.evals`:

- 100% critical identity/signature/money/date/legal/status/write-target cases
- zero unsupported names, numbers, dates, or negative claims
- 100% confirmation, authorization, effect-set, and idempotency invariants
- at least 98% overall held-out accuracy
- at least 98% retrieval recall at five for retrieval lanes

`RamaModelCertification` is keyed by provider, model, model version, domain
lane, and eval suite. Catalogue membership alone does not certify a v3 model.

Load checked-in cases with:

```console
python manage.py rama_load_eval_cases
```

## Unstructured retrieval

`RamaKnowledgeSource` and `RamaKnowledgeChunk` live in the same PostgreSQL
database. Official sources are global and must pass an authority-domain
allowlist; private sources are landlord-scoped. Each source records URL,
authority, jurisdiction, effective dates, version, and checksum.

Retrieval metadata-prefilters jurisdiction and effective date, combines
PostgreSQL full-text and pgvector cosine rankings with reciprocal-rank fusion,
and returns citation metadata. Retrieval coverage is always partial and never
permits an absence claim. Live operational records are deliberately excluded.

The production Postgres image is the multi-architecture pgvector PostgreSQL 16
image, so the Oracle ARM VM and a future x86 VPS use the same Compose stack and
database backup/restore process.
