# Rentium Document Library (in-house, paperless-inspired)

**Decision (July 2026):** Stay in-house. Do **not** adopt paperless-ngx as
the system of record. Bring over the *product ideas* that matter for landlords
and wire them to holdings, leases, ledger, and RAMA.

---

## Why not paperless-ngx as core

| Need | paperless-ngx | Rentium in-house |
|---|---|---|
| Multi-tenant landlord isolation | Weak fit | Already `landlord=` scoped |
| Holding / listing / lease scope | Tags only | First-class FKs |
| Expense → immutable ledger | No | `file_business_document` + `post_expense` |
| Confirm-before-money | No | RAMA preview + `confirm=yes` |
| Chat / Telegram ingest | Separate | Already in pipeline |
| Full-text archive UX | Excellent | We build deliberately |

Optional later: export/import zip, or a read-only search UI inspired by
paperless — never dual-write production money docs.

---

## What we already have

- **Ingest + SHA-256** idempotency (`ingest_document`)
- **OCR → PDF/A** (`process_rama_document` / ocrmypdf)
- **Kinds, amount, payment_state, issuer, reference**
- **Holding-level scope** (not room-by-default)
- **Canonical path**:  
  `business_documents/<landlord>/<holding>/<year>/<category>/<canonical-name>.pdf`
- **Chat path**: prepare (hash+OCR) → scope → `file_business_document`
- **Duplicate hard-stop** on same content hash
- **Documents inbox UI** with pagination + delete (unlinked)
- **Ledger link** on expense post

---

## Paperless features to bring in (mapped to Rentium)

### Tier 1 — organize like a filing cabinet (next build)

| paperless idea | Rentium shape |
|---|---|
| **Correspondents** (vendor/issuer) | First-class `DocumentCorrespondent` (or reuse `issuer` + vendor table later) with autocomplete from past docs |
| **Document types** | Expand `RamaDocument.Kind` + optional free tags |
| **Tags** | `DocumentTag` M2M: e.g. `tax-2026`, `year-end`, `insurance`, `hvac` |
| **Full-text search** | Postgres `SearchVector` on `ocr_text` + title/issuer/ref (GIN index) |
| **Filters** | API/UI: holding, kind, year, tag, payment_state, status, has_expense |
| **Stable titles** | Always rename display to OCR/title + date; never surface `file_30.jpg` as the headline |
| **Bulk actions** | Multi-select: set holding, tags, delete (if unlinked), re-run OCR |

### Tier 2 — RAMA as filing clerk

| Capability | Behaviour |
|---|---|
| `search_business_documents` | Full-text + filters for chat |
| `organize_documents` | Propose tags/kind/title renames in one preview batch |
| `retag` / `move_to_holding` | Confirm-gated writes |
| Inbox triage | “What’s unfiled?” → list `NEEDS_REVIEW` / unscoped |
| Year-end pack | “All 2025 tax + insurance for McKenzie” → links + zip later |

### Tier 3 — scale & polish

| Feature | Notes |
|---|---|
| Saved views | “Unpaid invoices”, “McKenzie 2026 expenses” |
| Zip export | Holding + year for accountant |
| Similar-doc suggestions | Embedding optional; start with amount+date+issuer |
| Soft-delete / trash | 30-day recycle before hard delete |
| Storage quotas | Per-landlord caps |
| Bank statement path | Already non-expense; feed import batch, not single charge |

---

## Information architecture

```text
Portfolio
  └── Holding (950 McKenzie Ave)          ← default document scope
        ├── Documents/                    ← browsable by year/kind/tag
        │     ├── 2026/Maintenance/...
        │     └── 2026/Tax/...
        ├── Listings (rooms/suite)        ← optional property FK only when needed
        └── Ledger expenses               ← linked from document when filed
```

**Rules**

1. Physical address / holding is the default shelf.
2. Listing-level only when the paper is truly room-specific.
3. Money never posts without explicit landlord confirm.
4. Same file bytes → one document row (duplicate stop).
5. Original bytes immutable; archival PDF is derived.

---

## Naming (kill `file_30.jpg` as identity)

Display and storage use **semantic names**:

```text
2026-07-30_950-mckenzie-ave_maintenance-window-screens_INV-4412_<id8>.pdf
```

- `document_date` or OCR date  
- holding slug  
- title/kind slug  
- reference if present  
- short id for uniqueness  

UI primary line = **title**; secondary = canonical filename + original name.

---

## API surface (planned)

```
GET  /api/rama/documents/?page=&page_size=&q=&holding=&kind=&tag=&year=&status=
POST /api/rama/documents/                    # upload
GET  /api/rama/documents/<id>/
POST /api/rama/documents/<id>/               # file / correct
DELETE /api/rama/documents/<id>/             # if no ledger link
POST /api/rama/documents/<id>/reocr/
GET  /api/rama/document-tags/
POST /api/rama/documents/bulk/               # tag, move holding, delete
```

(Pagination + DELETE already landed; search/tags/bulk next.)

---

## UI surface (planned)

1. **Inbox** — needs review / unscoped (current page, improved)  
2. **Library** — filterable grid/list by holding · year · kind · tag  
3. **Document drawer** — OCR text, events, expense link, open in ledger  
4. **Holding tab** — “Documents for this property”  
5. **RAMA** — organize/search tools, never invent amounts  

---

## Implementation phases

### Phase A — Search & structure (high leverage)

- Postgres full-text on `ocr_text`, title, issuer, reference  
- Query params: `q`, `holding`, `kind`, `year`, `status`  
- Tags model + UI chips  
- Title always preferred over original filename in lists  

### Phase B — RAMA organize

- `search_business_documents`  
- `organize_document` (tags/title/kind/holding, confirm)  
- Persona: after OCR, propose tags + “file under McKenzie / Maintenance”  

### Phase C — Library UX

- Filters, bulk, saved views  
- Holding detail “Documents” section  
- Soft-delete trash  

### Phase D — Scale

- Async bulk OCR  
- Export zip  
- Quotas / retention policy  

---

## Explicit non-goals

- Replacing S3/media with paperless storage  
- Auto-posting expenses without confirm  
- Auto-filing to a room listing by default  
- Storing millions of rows without pagination (already required)  

---

## Success criteria

1. Landlord finds “window screens invoice McKenzie 2026” in &lt;5s via search.  
2. Re-uploading same PDF never creates a second filing conversation.  
3. RAMA can say: “You already have this under 950 McKenzie; expense linked / not linked.”  
4. No primary UI label is a camera dump name (`IMG_….jpg` / `file_30.jpg`).  
5. Documents list stays fast at 10k+ rows per landlord (pagination + indexes).  

---

## First implementation slice (when we start coding)

1. `SearchVector` + `q=` on documents list API  
2. `DocumentTag` + M2M + filter  
3. RAMA `search_business_documents`  
4. Library filter bar on frontend Documents page  

Everything else builds on that shelf.
