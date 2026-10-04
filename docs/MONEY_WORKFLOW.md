# Money workflow — October 2026

## Intended use

Open **Dashboard → Financial / Money**. Choose a month and an address, physical
unit, listing, or the whole portfolio. The overview shows expected income,
income received, overdue income, unpaid bills, deposits held, and recorded cash
movement. Follow any record to its exact ledger entry. Existing ledger tools
and historical imports remain available.

Upload a **CAD CSV or XLSX bank export** in **Review statements**, using the same
account name on future uploads. Select the worksheet, columns and date format.
Rentium proposes matches only when amount and descriptive evidence agree on a
single record. Review unresolved rows, allocate partial or split incoming
payments to existing charges, match paid bills, or describe a new expense.
Transfers, personal spending and duplicates require an explicit exclusion.
Unallocated incoming money stays in review; imports never create tenancies.

Upload receipt PDFs/photos in the existing Documents inbox. OCR remains a Celery
job. Once extracted facts are reviewable, a bank decision can attach that receipt
to the same expense. A receipt and its bank payment do not become two expenses.
The approval preview includes the receipt, cost, scope, dates, and allocations.

Select ready rows, inspect the **complete preview**, and approve once. Unselected
rows remain saved for later. Approval posts all selected effects atomically and
returns verified record links. Changes to balances, decisions, or target records
invalidate the preview. Preview authority expires after 30 minutes; drafts do
not expire with RAMA's chat episode.

The previous 2024 workbook informed the workflow. This change does **not** import
its history or mutate existing records. No production migration or deployment
has been performed as part of the implementation.

## Financial meaning and evidence

- `ledger.money.period_money` is shared by the overview, legacy monthly summary,
  and RAMA's monthly financial reader. `ledger.money.overview` supplies both the
  overview API and RAMA's `money_overview` tool.
- Income is payments against income charges. Deposits are liabilities, included
  in cash received but excluded from income. Deposit returns reduce cash and
  deposits held. Mortgage expenses are identified separately within cash out.
- An expense's `effective_date` records when it was incurred. `paid_on` records
  when it cleared. An unpaid expense affects incurred expenses, not cash out.
- Legacy `expenses` and `net` retain their original meanings: incurred expenses
  and received income less incurred expenses. The UI labels that distinction.
- Cash is **recorded activity, not a bank balance**. No opening bank balance,
  completeness claim, live bank feed or estimated property valuation is implied.
- Payment and credit attribution follows their settled charge. Holding scope
  includes both holding expenses and the holding's listing records; unit scope
  includes its listings. Unassigned holding costs are not silently allocated
  across units. Overdue income and unpaid bills are current, across periods;
  monthly receipts and expenses use the selected period `[start, end)`.

## Service and API boundaries

`ledger.statement_files` parses bounded source files without evaluating formulas.
`ledger.reconciliation` owns upload, mapping, saved decisions, exact effects,
preview, validation, deduplication, posting and receipts. Views and RAMA call
these services; neither implements a second money writer.

Authenticated endpoints under `/api/ledger/`:

| Route | Purpose |
| --- | --- |
| `money-overview/?start=&end=&holding=&unit=&property=` | Scoped projection and evidence |
| `review-options/?batch=` | Owned scopes, receipt summaries, candidate entries |
| `statements/` | Saved reviews; multipart upload with `file`, `account`, optional `worksheet` |
| `statements/<batch UUID>/` | Read review; POST `columns`, `mapping`, `decision`, `preview`, `confirm`, `cancel` |

`BankStatement` extends `ImportBatch`; `BankTransaction` preserves raw source rows,
parse errors, draft decisions and approved effects. Source bytes are retained in
the existing media store. Historical `StagedLedgerEntry` imports remain a separate
workflow and cannot commit bank drafts through the old endpoint.

Up to 100 source rows prepare synchronously; larger statements use
`ledger.tasks.prepare_bank_statement`. Preparation is atomic, bounded by worker
timeouts, and **never writes money**. The UI polls durable queued/ready/failed
state. A lost job may be requeued after five minutes; obsolete job IDs are ignored.
Failures retain the source and prior draft. Approval always runs in the guarded
service, regardless of whether RAMA or the UI prepared the preview.

RAMA tools: `money_overview`, `review_bank_statements`,
`reconcile_bank_statement`. The last produces a persisted normal RAMA pending plan
whose resolved arguments freeze the bank preview ID. Only explicit control-plane
confirmation authorizes it. It is never an autonomous background write.
The frontend emits `rentium:finance-changed` after successful writes/confirmations
so the overview, manual tools and ledger feed reload their authoritative data.

## Integrity and limits

- Owner-scoped lookups and owner/entry locks protect preview/commit and concurrent
  approvals. Existing settlement writers use compatible entry locks.
- File hashes deduplicate repeat uploads for the same account and worksheet.
  Account/date/amount/description/reference fingerprints identify overlapping
  approved rows. Same-value transactions need explicit separate-transaction
  approval when flagged. These are detection rules, not a claim that all bank
  formats provide stable transaction IDs.
- Allocations must exactly consume the bank amount and cannot exceed approved
  open balances, individually or in aggregate. Existing historical overpayment
  behavior elsewhere in the ledger is preserved.
- Retry of an approved preview returns its saved receipt, including after a
  later partial approval. Each posted entry has a source-derived idempotency key.
- Bounds: 10 MB file, 50 MB expanded workbook, 5,000 rows, 64 columns. CSV is UTF-8;
  XLSX is tabular with headers in the first row. Formulas and nonfinite amounts
  are rejected. Dates require an explicit format; typed Excel dates are supported.
- Candidate selection shows up to 1,000 entries, prioritizing open charges and
  unpaid bills. Receipt choices show up to 250 recent reviewable documents. A
  missing candidate remains an exception; it is never converted into a new lease
  or guessed payment. Exact-ID service validation does not depend on those caps.
- CAD only in this workflow. Bank PDF parsing, connected accounts, exchange
  conversion, automatic legal/tenancy changes and automatic fund transfers are
  outside this implementation.

## Deployment and rollback

1. Back up PostgreSQL **and the active media store** using the existing operations
   runbook. Check out the matching backend/frontend changes; both were implemented
   against their `main` checkouts, not the historical `claude/phase-b` branch.
2. Install backend requirements (adds `openpyxl`) and apply ledger migrations
   `0011_bank_reconciliation` and `0012_bank_staging_job`. Both are additive; no
   legacy history is rewritten. Restart Django and Celery with the same revision.
3. Deploy the frontend only after the new API and worker are healthy. Start with
   one actual statement, reconcile its preview against source evidence, then
   approve and compare its receipt to the monthly view.
4. Roll back application code if needed while **retaining the new tables and
   media**. Never reverse the schema after reviews contain evidence. Correct
   approved money using existing ledger adjustments/reversals, never deletion.

## Validation and disposable browser demo

Backend: run `pytest rentium/ledger rentium/rama/test_financial_agreement.py
rentium/rama/test_deposits_received.py rentium/rama/test_capability_foundation.py
rentium/rama/test_prior_year.py rentium/rama/test_receipt_becomes_expense.py` against
PostgreSQL with pgvector. New workflow tests cover split and partial settlement,
stale/expired approval, rollback, repeat and overlapping files, concurrent
approval, owner isolation, cleared versus incurred dates, receipt deduplication,
RAMА/API agreement, workbook dates, background preparation and retry receipts.

`scripts/seed_money_demo.py` requires a **separate** database named with the
`rentium_money` prefix, Django settings, and `MONEY_DEMO_OUTPUT` pointing to a
temporary JSON file. It creates synthetic users, rent and a bill, never deletes
history, and writes the disposable token with mode 0600. Do not use a real account.
Start a backend against that database and a frontend configured to that API.

From the frontend repo, set `MONEY_DEMO_OUTPUT` and run `npm run test:money`.
Defaults are frontend `127.0.0.1:3109`, API `127.0.0.1:8019/api`, installed Chrome;
override with `MONEY_E2E_URL` and `MONEY_E2E_API`. Seed a fresh account for each
run. The browser suite verifies the complete approval journey, exact evidence
navigation, failed-data behavior and mobile width. Synthetic local success is
not a substitute for the first real-statement pilot after deployment.
