# RAMA — Why We Keep Building Tools, and How to Make It Truly Smart

> **What this document is.** A diagnosis of a recurring problem with RAMA, and a
> proposed architecture to fix it. It is written so you can either hand the
> **"Hand-off prompt"** at the bottom to an agent to produce an implementation
> plan, or act on the plan already sketched here. Read the diagnosis first — the
> prompt at the end only makes sense once the pattern is clear.

---

## STATUS: Phases 1–4 shipped and certified on the weak-model bar

### August 2026 visible-state and orchestration addendum

RAMA now separates the visible conversation from its diagnostic audit trail.
`RamaConversation`, `RamaEpisode`, and `RamaMessage` record exactly what the
landlord saw, including external domain notifications. Episodes split after 30
minutes of visible inactivity. Attachments, pronouns, and confirmations cannot
cross that boundary implicitly.

Lease follow-ups retain exact landlord-authored lease numbers inside the active
episode. When two open leases share a listing, property-only mutation lookup
fails closed instead of silently choosing the newest. First-month rent targets
use a deterministic composite that prices the household adjustment from live
tenant allocations, persists the resolved lease and expected total, and bounds
one-time adjustments to one month. Confirmations execute only a matching
persisted plan; provider prose can never act as a substitute plan. Document
recovery has no global-inbox fallback, preventing an old receipt or OCR amount
from becoming the subject of a later lease conversation.

Single and parallel period-rent instructions are effect-compiled, not left to a
model tool loop. Requests such as “make Aug rent $400 for A and $1,900 for B”
and “Aug rent for A and B can be $400” resolve each participant to one
landlord-owned lease, bind the named billing period and final household total,
preview live arithmetic for every item, and persist one version-2 contract whose
effects must each appear exactly once. This path does not call the configured AI
provider. One confirmation runs the complete approved batch; rent rows do not
ask for another confirmation per step. Named periods are bounded by the legal lease term rather than the
default move-in schedule. An explicit August target therefore remains August
when move-in changes to September, creates that August charge on activation,
and overrides existing proration before the preview is priced. Automatic
proration still follows possession-date edits; landlord-authored period targets
do not.

Visible turns are serialized per conversation with a non-blocking PostgreSQL
advisory lock (cache fallback outside PostgreSQL). A “?” arriving while another
turn is running gets an immediate deterministic busy response instead of
starting an overlapping model loop. Generic provider work is also bounded by a
45-second loop budget, eight rounds, and a configurable 25-second per-request
timeout; exhaustion produces an explicit no-change response rather than an
inbound message with no outbound result.

The dashboard has one AUTO conversation with optional Ops and Treasurer targets;
switching a specialist no longer destroys the thread. Confirm/cancel controls
address a specific persisted plan and prompt instead of sending magic chat
words. Strategic Treasurer requests invoke the existing structured deliberation
engine on demand. Capability parity reporting now publishes behavioural
certification instead of equating a registered function name with a working
capability.

General architecture Markdown remains documentation for people and development
agents, not runtime memory or portfolio truth. The narrow exception is
`rama/policies/EXECUTION_CONTRACT.md`: versioned, generic model guidance loaded
at runtime. It never contains portfolio facts and has no authority of its own.
Runtime knowledge and enforcement still come from generated capability schemas,
scoped resolvers, live database facts, visible messages, validated plans, and
verified receipts.

Certified models also have a generic plan-compilation recovery path. If a model
understands a requested change but writes “reply confirm” without calling a
tool, the engine feeds that proposal back once with a strict instruction to
emit real preview tool calls. Capability retrieval is rerun over the original
message plus the proposal, so terse follow-ups such as “the first one” can still
reach the relevant operation. Valid previews become the ordinary persisted
confirmation batch; failed compilation becomes an honest blocker. A “yes” to a
previous orphan prompt compiles a preview but cannot execute it, because the
landlord has not yet approved the validated contract.

The proposal below has been **built**. Summary of what now exists in `rama/`:

| Phase | What shipped | Files |
|---|---|---|
| 1 | Domain Capability Manifest + generic scope-safe `read` | `manifest.py`, `domain_read.py` |
| — | Read manifest broadened to **11 entities** | `manifest.py` |
| 2 | Generic `link`/artifacts + Telegram file delivery | `domain_read.py`, `comms/` |
| 3 | Generic `update` (writes): default-deny + FSM guard + preview/confirm | `domain_write.py` |
| — | Editable manifest broadened (leases, work orders, inquiries, inventory) | `manifest.py` |
| 4 | `capability_digest()` — capabilities come from **data**, persona keeps behaviour | `manifest.py`, `service.py` |

### July 30, 2026 command-engine addendum

The fixed-tool surface is now fronted by selective capability retrieval
(`rama/capabilities.py`), so each turn receives a small relevant schema set and
can explicitly search the registered catalogue. Confirmed work is durable
workflow state (`RamaTask`) and produces immutable `RamaActionReceipt` evidence;
provider prose cannot stand in for completion.

The high-cost orchestration failures from real conversations now have
application-owned composite/service boundaries: exact chat attachment batches,
listing media manifests/removal, shared lease creation, shared viewing
scheduling, invite lifecycle evidence, partial ledger settlement, and atomic
whole-suite-to-room-offering conversion. These remain bespoke because they are
real workflows, not simple field access.

**Certification.** New Mistral-Small eval scenarios (composed `read`, a field
`update_lease` lacks via `update`, a deep `link`) all **PASS** on the weak-model
bar (`scripts/rama_eval.py`, `RAMA_EVAL_MODELS=mistral:mistral-small-latest`).
The pre-existing deterministic scenarios still pass; 2–3 multi-step plan/
disambiguation scenarios show **weak-model variance** (each passes reliably in
isolation; ~1–2 rotate as failures under a full sequential run) — inherent to
weak models on long multi-turn plans, not a regression from this work.

**Net effect:** a new read, filter, deep-link, or simple-field edit is now a
one-line manifest declaration the model discovers on its own — no bespoke tool,
no deploy, no `log_capability_gap`. The remaining open items (broaden writes to
more entities, per-field custom previews, retrieval if the digest outgrows the
prompt) are polish, not treadmill.

---

## 1. The symptom: the gap → build → deploy treadmill

Every time a landlord asks RAMA to do something it wasn't explicitly built for,
the same loop plays out:

1. RAMA can't do it, so it either **fails**, **hallucinates** ("feature coming
   August 11"), or **logs a capability gap** ("noted for the team to build").
2. A human (so far, me) reads the gap, writes a new **tool wrapper** + **domain
   impl** + **tool_meta risk entry** + **persona lines** + **tests**, wires it
   into the registry, and deploys.
3. The landlord can now do that _one specific thing_. The next slightly-different
   ask starts the loop over.

This has happened for, among others:

| Ask from you | What we had to hand-build |
|---|---|
| "rename this listing" | schema param-docs + rename doc + disambiguation bridge |
| "attach these photos" | `attach_photo_to_listing` (then again for _multiple_/all) |
| "set the rent" (it made a $0 lease) | a deterministic "ask for missing rent" gate |
| "invite an existing account" | `_resolve_existing_tenant` linking logic |
| "add a co-landlord / co-host" | `add_co_landlord`, `add_co_host_to_lease`, scoping, co-signing |
| "shared with landlord & roommates" | `shared_with` param + enum mapping |
| "add inventory to the room" | `bulk_add_inventory`, `create_inventory_item` |
| "per-date viewing hours" | `specific_date` on availability |
| "give me a link to the lease/property" | `open_lease`, `open_property` |
| "send me the PDF / metadata / photos" | still partly gap-logged |
| "edit the bills" | `bills_included` editing + property default |

The through-line: **almost every gap was a field, relation, or link that already
existed in the data model but had no tool exposing it to RAMA.** The database
knew about `bills_included`, `common_space_shared_with`, `co_hosts`, the lease
PDF URL, the property photos — RAMA just had no way to _reach_ them until someone
wrote a bespoke tool.

## 2. Root cause: capability is **enumerated**, not **generative**

RAMA today is a **fixed-tool router**. A deliberately weak model selects from a
hand-curated list of ~60 tools; each tool is a bespoke Python function with its
own wrapper docstring, per-parameter docs, risk metadata, persona instructions,
and tests. This design was a good and correct choice for **safety** — it makes
every write previewable, confirmable, permission-scoped, FSM-guarded, and
auditable. But it has a hard ceiling:

> **RAMA's capability is exactly the set of tools someone remembered to build.
> The model cannot reach any field, relation, action, or artifact that lacks a
> hand-written tool — no matter how obvious it is from the data model.**

Three structural consequences:

- **Read/write asymmetry with the domain.** The domain has hundreds of
  fields/relations; RAMA exposes a few dozen. Every unexposed one is a latent
  gap waiting to be discovered by a landlord.
- **High marginal cost per capability.** Shipping one new ability touches ~5
  files (wrapper, impl, meta, persona, tests) and a deploy. That cost is why it
  _feels_ like "we have to note it and then build it" — because we literally do.
- **The persona carries knowledge that should be data.** Rules like "co-host is
  a name on the doc, not a login" live as prose in the system prompt. Every new
  concept adds more prose, which a weak model then has to parse correctly.

The `log_capability_gap` tool is a symptom, not a solution: it's a polite way of
saying "the enumerated surface didn't cover this." A truly smart system would
rarely need it, because the surface would be **complete by construction**.

## 3. What "truly smart" actually requires here

You've been explicit that the answer is **not** "use a bigger model" — smartness
must live in deterministic scaffolding (the weak-model-first principle). That
principle is right. The problem is that today's scaffolding is _enumerated_. To
be truly smart under the same principle, the scaffolding must become
**complete, self-describing, and safe-by-construction** so the model's fixed
intelligence can reach _any_ part of the domain without a human pre-building the
path.

In other words: move the smartness from **"which of these 60 tools"** to
**"which slice of a fully-addressable domain"** — where the domain map, its
permissions, its guards, and its previews are all _derived from one declaration_
instead of hand-written per capability.

## 4. Proposed architecture: a Domain Capability Manifest + generic safe primitives

The core idea is a single source of truth — a **Domain Capability Manifest
(DCM)** — and a small set of **generic, safe primitives** that operate over it,
plus retrieval so the model can navigate a large surface without prompt bloat.

### 4.1 The Domain Capability Manifest (one declaration → everything)

A declarative registry describing the domain the way RAMA needs to reason about
it. For each **entity** (Property, Lease, LeaseTenant, LedgerEntry, WorkOrder,
Inspection, …):

- **Fields**: type, human label, whether readable/editable-by-RAMA, the FSM
  states in which they're editable (e.g. `bills_included` editable only when the
  lease isn't locked), value domain / enum mapping (so "roommates & landlord" →
  `["ROOMMATES","LANDLORD"]` is data, not a hand-coded `if`), and a **risk tier**
  (low/medium/high) that drives preview/confirm behaviour.
- **Relations**: co-hosts, signatories, inventory items, images — with the same
  read/write/guard metadata.
- **Named actions**: the genuinely stateful transitions (sign, terminate,
  move-out) that _aren't_ just field edits.
- **Links/artifacts**: how to produce a deep link or a downloadable artifact for
  this entity (lease PDF, property page, photo set).
- **Scope rule**: the `scope_q`/`accessible_*` predicate that already exists —
  referenced here so every generic operation inherits multi-tenant isolation and
  co-landlord access for free.

Everything downstream is **derived** from the DCM: the tool schemas, the
per-parameter docs, the preview/confirm text, the persona's capability
descriptions, the risk metadata, and the retrieval index. **Adding a capability
becomes adding a manifest entry, not editing five files.**

### 4.2 Two generic primitives instead of dozens of bespoke tools

- **`read(entity, filter, fields)`** — a constrained, scope-safe query interface
  over the DCM. RAMA answers _any_ read question by composing a query against
  declared, permission-checked fields — no `charge_status` / `list_inquiries` /
  `lease_state` explosion. (Bespoke read tools can remain as convenient
  shortcuts, but they stop being the _only_ way to reach data.)
- **`update(entity, id, changes)`** — a schema-driven mutate that can set any
  field the DCM marks editable-in-this-state, running the declared validation,
  FSM guard, permission check, immutability rule, preview, confirm, and audit —
  all generic, all derived from the manifest. "Set bills", "set shared areas",
  "add a co-host", "rename" all collapse into this one gated operation.

Because the guards are declared per field/state in the DCM, the generic
`update` is **not** a foot-gun: it can't touch an immutable ledger entry or a
locked lease, because the manifest says those fields aren't editable in that
state. Safety stays exactly where it is today — it just stops being copy-pasted
into every new tool.

### 4.3 Keep bespoke tools for genuine orchestration

Some operations are legitimately multi-step and benefit from hand-tuned
orchestration: room-tenancy setup, move-out, the plan/confirm machine. **Keep
those.** The DCM + primitives handle the _long tail of simple field/relation/
link reads and writes_ (where ~90% of the gaps came from); bespoke tools handle
the _short head of complex flows_. This hybrid is the pragmatic sweet spot.

### 4.4 Retrieval so a large surface doesn't bloat the prompt

A weak model can't hold a 300-field manifest in its context. Add a lightweight
**intent → capability retrieval** step: given the landlord's message, retrieve
the handful of relevant entities/fields/actions/links from a DCM index and put
_only those_ in front of the model to plan over. This is what lets the surface
grow to "the whole domain" while the prompt stays small — and it's what makes it
_feel_ like RAMA "just knows," because the knowledge is indexed and complete
rather than curated and partial.

### 4.5 What happens to the gap loop

With the DCM in place, `log_capability_gap` fires only for genuine _new
mechanics_ (a new stateful workflow), not for "there's a field with no tool."
And even then, the loop tightens: a gap can auto-scaffold a **manifest stub**
for a human to confirm, rather than requiring five hand-written files.

## 5. Trade-offs and risks (be honest about these)

- **Generic mutate is powerful; the manifest must be trustworthy.** The safety
  now lives in the DCM's per-field/per-state guards. That registry must be
  reviewed as carefully as the ledger rules are today. Mitigation: default-deny
  (a field is not editable unless the manifest explicitly says so, in an
  explicit state), plus keep the existing preview/confirm/audit envelope.
- **Preview quality.** Bespoke tools can write beautiful, specific previews
  ("rebalances unsigned shares"). Generic previews are more mechanical.
  Mitigation: allow the manifest to attach a custom preview renderer per field
  where it matters; fall back to a generic one elsewhere.
- **Retrieval can mis-route.** If the intent→capability step retrieves the wrong
  slice, the model plans over the wrong thing. Mitigation: retrieval returns
  candidates, and the existing disambiguation machinery asks when unsure.
- **Migration risk.** This is a substantial refactor of RAMA's core. It must be
  incremental and behind the existing eval harness (Mistral-Small pass bar), not
  a big-bang rewrite.

## 6. A phased plan (concrete, testable, incremental)

Each phase ships independently and is gated by the existing RAMA eval suite.

- **Phase 0 — Inventory the surface.** Enumerate every model field/relation/
  action/link a landlord could plausibly want, and mark which have a tool today.
  This quantifies the gap and becomes the seed of the manifest. _(Deliverable: a
  coverage table; no behaviour change.)_
- **Phase 1 — Manifest for reads.** Introduce the DCM for read-only fields +
  a generic `read` primitive + retrieval. Prove RAMA can answer questions that
  previously required a bespoke read tool, on weak models. Keep existing read
  tools as shortcuts. _(Lowest risk — reads can't corrupt anything.)_
- **Phase 2 — Manifest for links/artifacts.** Declare deep links + downloadable
  artifacts per entity; a generic `link`/`artifact` resolver. Kills the whole
  "give me a link / send the PDF / send the photos" class of gaps.
- **Phase 3 — Manifest for simple writes.** Add editability + FSM guards + risk
  to the manifest and a generic, previewed, confirmed, audited `update`. Migrate
  the simple field-setters (bills, shared areas, house rules, rename, pets/
  smoking, co-host) onto it. Prove immutability/FSM/permission guards hold with
  tests + the eval bar.
- **Phase 4 — Retrofit the persona.** Move concept knowledge out of the prompt
  prose into manifest descriptions; the persona shrinks to _behaviour_ (ask when
  unsure, confirm before writing) while _capabilities_ come from the DCM.
- **Phase 5 — Tighten the gap loop.** `log_capability_gap` auto-scaffolds a
  manifest stub; remaining true gaps are only new mechanics.

## 7. Open questions for you (decisions that shape the plan)

1. **Ambition vs. stability.** Full DCM + generic primitives (bigger payoff,
   bigger refactor), or a lighter "declarative field registry for writes only"
   first (smaller, faster, still kills most gaps)?
2. **Retrieval dependency.** Are you OK adding a small retrieval/index component
   (still deterministic, no model upgrade), or must everything stay in a single
   prompt with no retrieval step?
3. **Generic mutate risk appetite.** Comfortable with a default-deny generic
   `update` gated by a reviewed manifest, or do you want writes to stay bespoke
   and only reads/links to go generic (safer, leaves some write-gaps)?
4. **Where the manifest lives.** Derive it from the Django models/serializers
   (less duplication, always in sync) vs. a standalone declaration (more control,
   can diverge)?

---

## 8. Hand-off prompt (paste this to an agent to get an implementation plan)

> **Context:** RAMA is a landlord-facing agent in a Django + Next.js property
> platform. Its design is "weak-model-first": a deliberately weak LLM routes over
> ~60 hand-built, safety-gated tools (preview/confirm/FSM/immutable-ledger/
> multi-tenant scope/audit). We will **not** fix smartness by upgrading the
> model. The recurring problem: capability is _enumerated_ — any field, relation,
> action, or link without a bespoke tool is invisible to RAMA, so we constantly
> discover gaps, log them, and hand-build one tool at a time (~5 files each).
> Almost every gap was data that already existed in the model but had no tool.
>
> **Read** `docs/RAMA_SMARTNESS_ARCHITECTURE.md` (this file), and the RAMA code
> under `rentium/rama/` (`registry.py`, `tools.py`, `domain_crud.py`,
> `domain_actions.py`, `tool_meta.py`, `roles.py`, `runtime.py`, `service.py`,
> `resolve.py`, `playbooks.py`) plus `users/access.py` (the `scope_q` /
> `accessible_*` multi-tenant + co-landlord scoping already used everywhere).
>
> **Produce an implementation plan** for making RAMA "smart by construction" —
> able to reach the whole domain without a human pre-building each capability —
> while preserving every existing safety property (previews, confirms, FSM
> guards, immutable ledger, per-landlord scope, audit) and the weak-model pass
> bar (the eval suite in `scripts/rama_eval*.py` on Mistral-Small).
>
> Specifically:
> 1. Assess the "Domain Capability Manifest + generic `read`/`update`/`link`
>    primitives + retrieval + keep-bespoke-for-orchestration" proposal in §4.
>    Agree, refine, or counter-propose — with reasons.
> 2. Design the manifest schema (entities, fields with type/label/editability/
>    FSM-guard/enum-mapping/risk, relations, actions, links, scope predicate) and
>    say whether to derive it from Django models/serializers or declare it
>    standalone.
> 3. Specify the generic primitives and exactly how each inherits the existing
>    guards (default-deny editability, `scope_q`, preview/confirm, audit,
>    immutability). Show how a currently-bespoke ability (e.g. "edit bills",
>    "add co-host", "give me a link") reduces to a manifest entry.
> 4. Give the phased, independently-shippable migration (mirror §6), each phase
>    gated by the eval suite, with the tests that prove safety is preserved.
> 5. Call out risks (generic-mutate foot-guns, preview quality, retrieval
>    mis-routing, migration blast radius) and mitigations.
> 6. Answer the open questions in §7 with a recommendation for each.
>
> Deliver the plan as a phased spec with file-level touch points and a testing
> strategy. Do not start coding until the plan is reviewed.
