# RAMA Document Intelligence

## Purpose

RAMA accepts receipts, invoices, notices, and business correspondence as PDF,
JPEG, PNG, TIFF, WebP, HEIC, or HEIF. The pipeline retains the original,
creates a searchable PDF/A archival copy, extracts text, proposes a
classification and property scope, and waits for human confirmation before it
posts money.

## Property hierarchy

- `PropertyHolding` is one physical/legal address and the normal document and
  financial scope.
- `Property` is a rentable listing inside a holding: either a complete unit or
  a room.
- `PropertyGroup` describes shared space among room listings. It is not a
  legal-address or accounting boundary.
- A legally separately addressed apartment should have its own holding.

Thus a tax notice for 950 McKenzie Ave is filed once against its holding even
if the holding contains a garden suite and several room listings.

When the landlord identifies an address overall and no holding exists yet,
RAMA uses listings with that exact normalized legal address to propose the
physical holding. One confirmation creates it, links its child rooms and
complete units, and catalogs the document above those children. It never
forces a room/unit choice for an address-level record. Listings with distinct
legal addresses are not combined.

## Pipeline

1. `POST /api/rama/documents/` stores the byte-identical original and a SHA-256
   checksum. Duplicate submissions for one landlord are idempotent.
2. Celery runs `process_rama_document`.
3. OCRmyPDF rotates and deskews pages, runs English/French OCR, and emits PDF/A
   plus a text sidecar. Existing text pages are preserved.
4. Deterministic classification proposes type, amount, paid/unpaid state, and
   a holding match. Low-confidence or ambiguous results become `NEEDS_REVIEW`.
5. The landlord confirms or corrects the proposal in the Documents UI.
6. The file is stored at:

   `business_documents/<landlord>/<holding>/<year>/<category>/<canonical-name>.pdf`

7. Expense-like records post through `ledger.services.post_expense` with an
   idempotency key. A receipt may be marked paid; an invoice remains unpaid
   until its bank-clearing date is known.

## Naming

`YYYY-MM-DD_<holding>_<document-title>[-<reference>]_<document-id>.pdf`

The stable ID prevents collisions. Names are filesystem-safe and retain useful
meaning outside Rentium.

## Safety and audit

- Originals are never replaced by normalized files.
- Every upload, OCR, classification, clarification, filing, failure, and ledger
  link is recorded in an append-only `RamaDocumentEvent`.
- Documents and downloads are landlord-scoped.
- OCR never posts money by itself. Human review is the accounting boundary.
- Ledger entries remain immutable and use holding scope for property-wide costs.
- Unpaid expenses reduce committed cash planning but do not reduce estimated
  bank balance until `paid_on` is set.

## Operations

The Django images install Ghostscript and Tesseract English/French language
packs. Python dependencies include OCRmyPDF and HEIC support. OCR failures are
visible as `FAILED`; they are never silently filed.
