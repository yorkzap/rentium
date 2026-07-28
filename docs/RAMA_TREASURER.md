# RAMA Treasurer — the finance head

The Treasurer's job is to net you more money: spend less, earn more, borrow
better, pay less tax. It is a **side role** — Rentium works fine without it —
but it is the one that is supposed to make the portfolio grow.

It is **read-only over the domain**, and that is structural rather than a
promise: no tool it can reach takes a `confirm` argument, so `pending_specs` is
provably always empty and no plan can originate from this role. Everything it
recommends comes back to you as a normal confirmed plan **from the General**.

---

## 1. How to reach it

| Route | When |
|---|---|
| Settings → the **Treasurer** chip in the RAMA panel | You want to ask it something directly. |
| The General's `ask_treasurer` | You asked the Chief a money question and it delegated. |
| Monday 07:30 beat | It looks at the highest-spend holding on its own, one topic a week. |
| Your morning briefing | Anything it needs from you appears as *"Treasurer needs from you:"*. |

Its default provider is **Gemini** (cheap and capable enough). Change it in
Settings → Account & RAMA → Roles. Switching to Claude, Grok, Mistral or OpenAI
changes the **prose only** — see §4.

---

## 2. What it reasons over

Four sources, deliberately kept apart, because mixing them is how a confident
wrong number gets made:

| Source | What it is | Rule |
|---|---|---|
| **Ledger** | Recorded, append-only, authoritative. | Counted. |
| **Asserted facts** (`TreasurerFact`) | Something you told it that the books don't have. | Counted *unless* the ledger already holds ≥80% of it. |
| **Provisional history** (`ImportBatch` DRAFT rows) | Uploaded, not committed, still editable by you. | Described, **never** added to a ledger total. |
| **Research** (`TreasurerSource`) | A rebate or rate fetched from an allowlisted page. | Usable only if the figure appears **verbatim** in the cached page text. |

### Correcting it

> *"You're missing that we took $2,000 a month in rent from the upstairs tenant
> from April 2024 to March 2025."*

That records a `TreasurerFact`, scoped and dated, which is then **reconciled
against the ledger over the same scope and period**. If the ledger already
holds most of that money the fact is marked `double_count_risk`, shown to the
analysis as a note, and **excluded from every total** — because a
double-counted income figure is worse than a missing one. It looks like good
news.

Corrections **supersede**; nothing is deleted. The superseded fact stays, the
old insight is dismissed with *"Revised after your correction."*, and the
re-run reuses the previous GATHER artifacts (the research is still valid) and
restarts at SCORE.

`TreasurerFact` is a separate store from `RamaMemory` on purpose: memory
refuses money, dates and counts, because a memory is injected into every prompt
forever with no as-of date. A financial fact is the opposite — scoped, dated,
reconciled, and injected into exactly one deliberation.

---

## 3. How it thinks deeply on a cheap model

Depth comes from **structure, not prompting**. Nine stages; only three cost
anything.

```
FRAME → SCOPE → ENUMERATE → GATHER → SCORE → COMPARE → CHALLENGE → RECOMMEND → PUBLISH
  $0     $0        $0         LLM      $0      $0         LLM         LLM        $0
```

The model never decides what to think about, what the options are, what the
criteria are, what the arithmetic is, or what the ranking is. It fills one
narrow slot per call, in its own conversation, and it cannot see the other
options while it does.

- **The option slate is a declared catalogue** (`interventions.py`), not model
  imagination. Nine interventions across envelope / mechanical / operating /
  revenue / financing / tax. Adding one is a frozen dataclass.
- **"Maybe windows before the heat pump"** is a `precedes` edge on
  `WINDOW_REPLACEMENT`, emitted by `compare()`. The model contributes nothing
  to that sentence's existence — it only narrates it.
- **Self-questioning is a `for` loop.** `sensitivity()` perturbs each assumed
  figure and reports which rankings flip; CHALLENGE narrates flips Python
  already found and cannot invent one.
- **Every number is computed by Python.** The model emits `{{f7}}` tokens and
  `render.substitute()` fills in the value *with its provenance parenthetical*.
  A weak model physically cannot drop the caveat, because it never typed the
  number. An unresolved token is a contract violation.
- **Bounded**: ≤5 options, ≤9 model calls, ≤12 research fetches, 2 per landlord
  per day. Overflow publishes the partial result as **provisional** rather than
  dropping it.

### When it needs something from you

An unfilled required slot becomes a `TreasurerRequest`, created by **Python**
(never by the model), with `why_it_matters` generated from the sensitivity
result so it always states the real consequence. The General relays it
**verbatim**, prefixed `Treasurer request:`, and is instructed never to answer
one, guess the figure, or soften it into a suggestion. Max 3 open, deduped on
`fact_key`, 14-day expiry — after which the analysis resumes **provisionally**,
naming the assumption it could not confirm.

---

## 4. Switching models

The claim is: **switch provider and the advice does not change, only the
wording.** That holds because enumeration, scoring, ranking and every figure
are Python. The model's output contract is line-oriented
(`FACT unit_cost 18000 CAD ONE_TIME src=WEB url=… conf=RESEARCHED`) for exactly
this reason — prose around it is noise.

`test_the_ranking_does_not_depend_on_the_model` runs the same facts in three
dialects (terse / chatty / fenced) and asserts byte-identical rankings, and
`test_a_model_that_editorialises_cannot_change_the_order` has the model lobby
in prose for the option the arithmetic rejects. **These are CI-gate tests.**

---

## 5. Web research

Mirrors `comms/whatsapp.py:_provider_send`: `_provider_search` and
`_provider_fetch` are the only two functions that name Firecrawl, so swapping
to Tavily/Exa/Brave is a two-function change.

- The model **names a topic key** from `RESEARCH_TOPICS`; it never composes a
  query or picks a domain.
- `scrub()` refuses any query carrying an email, phone, postal code, tenant
  name, or portfolio address — and **fails closed** if it cannot prove the query
  is clean. PII never leaves the system.
- `verify_in_source()` is the important one: a figure that is not present
  verbatim in the cached page is excluded from SCORE. A confidently wrong
  rebate amount reaching someone about to spend $18,000 is the worst thing this
  feature could do.

Settings: `FIRECRAWL_API_KEY`, `RAMA_RESEARCH_BACKEND=firecrawl|fake|none`.
Unconfigured is a safe no-op. `fake` serves fixtures so CI needs no network.

---

## 6. Tax

Planning estimates only, and it **never guesses a year it does not have**:
`tax.marginal_rate_estimate` returns `None` for a missing `TaxRateTable`, logs
a capability gap, and every tax dollar figure is omitted rather than
approximated from the prior year. Tables are loaded by a human with
`manage.py rama_load_tax_table` — an ongoing operational job, not a one-time
build. Every tax figure is labelled *"ESTIMATE — planning only, not tax
advice"*, rendered by Python.

---

## 7. Background watchers

Three deterministic, $0, idempotent sergeants alongside the existing five:

| Watcher | Fires when |
|---|---|
| `check_mortgage_renewals` | A term ends within 120 days — a rate hold has to be arranged *before* the date. |
| `check_valuation_staleness` | The newest valuation is over 730 days old — equity off a stale number is a confident wrong number. |
| `check_spend_drift` | A category costs ≥1.30× what it did the year before. Distinct from the monthly anomaly check: this catches slow creep. |

Each dedupes on a key, so a daily beat notifies once, not every morning.

---

## 8. Running the evals

```bash
docker compose -f docker-compose.local.yml exec -T django \
  env RAMA_RESEARCH_BACKEND=fake RAMA_EVAL_ONLY=treasurer \
  python /app/scripts/rama_eval.py
```

`RAMA_EVAL_DRY=1` builds and tears down fixtures without spending a token —
run that first after touching a fixture.

> **The eval harness clears landlord-scoped RAMA state** — memories, auto-action
> grants, and now Treasurer facts, requests, sources and deliberations. It is a
> DEV-only tool and it will wipe real Treasurer state on the landlord it runs
> as. That has always been true of memories; it is now true of facts too.

The two rows worth gating CI on are **"writes nothing to the domain"** and
**"provider-neutral"**; both also exist as unit tests in
`rama/test_deliberation.py` so they run without a live model.

---

## 9. Where things live

| File | What |
|---|---|
| `rama/deliberation.py` | The nine stages, the budget, the request generator. |
| `rama/interventions.py` | The catalogue and its `precedes` edges. |
| `rama/treasurer_facts.py` | Assertions, reconciliation, the double-count guard. |
| `rama/render.py` | `Figure` / `Provenance` / `substitute()`. |
| `rama/mortgage.py` | Canadian semi-annual compounding, equity, renewal horizon. |
| `rama/tax.py` | Marginal-rate estimates and the refusal to guess. |
| `rama/research.py` | The Firecrawl seam, the allowlist, the verbatim gate. |
| `ledger/models.py` | `HoldingFinancials`, `HoldingValuation`, `HoldingMortgage`. |
