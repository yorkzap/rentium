# RAMA — Constitution & Model Layers (landlord guide)

Two things landlords ask about: the **Constitution** tab, and how the
**smarter/layered models** work. Here's how both actually behave today.

---

## 1. Your Constitution

**What it is.** The Constitution is *your* written policy that RAMA follows and
the background "watchers" enforce. It has four sections — **Balances**,
**Vendors**, **Tenant policies**, **Workflows** — each a list of rules you write
(e.g. "keep at least $2,000 in the McKenzie account", "use Al's Plumbing for
leaks", "never waive a late fee without asking me").

**Does the system write it over time? No.** RAMA does **not** auto-populate or
silently rewrite your Constitution. It's empty until you fill it in. There are
exactly two ways it changes, both under your control:

1. **You edit it** in the Settings → Constitution tab (the "Write" buttons).
2. **You ask the General to** ("add a rule that…") — and it **always shows you
   the change and asks before saving**. Nothing is written without your yes.

**Every save keeps the old version in history** — you can see what changed and
when. So it's a living policy document you own, not something the AI mutates on
its own.

**What it's for.** The more you write here, the more the weak model behaves the
way *you* want without you repeating yourself — because the rules are injected
into RAMA's context and the watchers check against them. Empty sections just
mean RAMA falls back to its built-in safe defaults (ask before risky actions,
never delete ledger entries, etc.).

---

## 2. The model layers — and configuring smarter models

RAMA is deliberately built to run on **cheap, weak models** — the intelligence
comes from deterministic Python scaffolding (planning, grounding, confirmation),
not a big model. But it has a **command structure** of three roles, and each can
run a *different* model, so you can put a smarter model where it helps most:

| Role | What it does | Model it uses |
|---|---|---|
| **Corporal** | The ops agent — does the actual CRUD (create/duplicate/invite/…). | Your **main** RAMA model (Settings → Account & RAMA: provider + model + key). |
| **General** | Your "chief of staff" — routing, the Constitution, delegating to the Corporal. This is the natural home for a **smarter** model. | `general_*` config → falls back to your main model. |
| **FSA** | Reasons over facts (the Insights/analysis layer). | `fsa_*` config → falls back to your main model. |

**How the model for a role is resolved** (`rama/runtime.py:get_role_config`):

> your per-role preference → the platform's `RAMA_<ROLE>_*` setting → your main
> chat provider with that role's default tier.

**BYOK keys:** the key you enter in Settings applies to any role that uses the
**same provider** as your main one. If a role uses a *different* provider, RAMA
uses the platform's key for that provider.

### Are the smarter models "working"? What you're seeing

Today the **Settings UI only exposes your main model** (the Mistral Small +
API-key box you saw). That model drives the **Corporal**. The **General and
FSA currently fall back to that same main model** unless their per-role config
is set — and there is **no UI yet** to set the per-role (`general_model`,
`fsa_model`) values. So out of the box, everything runs on your one model; the
layered architecture is wired, but the per-role knobs aren't surfaced.

**To actually run a smarter model for the General** you currently need to set
its per-role fields on your `RamaPreferences` (e.g. via the API/admin):
`general_provider`, `general_model` (and optionally a key if it's a different
provider). A settings UI for this — a "smarter model for the General" picker —
is a small, planned addition; ask and it'll be built so you can do it from the
screen.

**Bottom line:** your Mistral Small key *is* working (it runs the Corporal). A
smarter decision-layer model is supported by the architecture but not yet
switch-on-able from the UI.
# Document intelligence

RAMA can ingest receipts, invoices, tax notices, mortgage correspondence, and
other business records from the chat attachment control or the dashboard
Documents page. See `RAMA_DOCUMENT_INTELLIGENCE.md` for the OCR/PDF-A pipeline,
property-holding hierarchy, filing convention, review boundary, and ledger
integration.

## Property vocabulary and corrections

RAMA uses three distinct levels:

- **Holding / physical property** — a house or building, such as 950 McKenzie.
- **Property group / household unit** — room listings sharing common spaces.
- **Rental listing** — one rentable room or one self-contained complete unit.

When “property” is ambiguous, RAMA reports both physical holdings and rental
listings. Type and layout corrections use structured fields
(`property_category`, `unit_type`, bedrooms, bathrooms, and internal areas);
RAMA never rewrites a listing description to simulate those facts. Questions
remain read-only, and missing layout facts are reported as not recorded.

## Routine property operations

High-confidence renames, grouped room listings, dashboard navigation, and room
creation inside an existing property group are routed deterministically. They
do not depend on the planner guessing the operation, and supported requests
cannot be logged as capability gaps.

The General may call routine property tools directly. Mutations still follow
the universal safety boundary: RAMA shows one complete preview, waits for an
explicit confirmation, then reports the concrete result (for example,
“Renamed McKenzie B to Room A”).

Compound routine instructions use the same boundary. If one turn previews
several creations, renames, or group assignments, RAMA persists every preview
in dependency order as one confirmation batch; it never keeps only the final
tool call. Chained operations resolve future names to stable listing IDs, so
“rename Room 5 to Room 3, then assign Room 3” does not fail a pre-rename
existence check. Only the pending-plan runner can add `confirm=yes`; a model
cannot approve its own write. Duplicate confirmations are idempotent, and
already-correct names/group memberships are reported as no-ops.

Confirmation language is backed by state, not prose. RAMA suppresses any
model-authored “reply yes” request when no executable pending plan was saved and
instead returns the actual `needs_input` question or validation error. Ordinary
side questions keep a pending plan intact. A correction such as “No, make it
like this…” rejects the old steps, retains structured address/city/province
defaults, validates the replacement tool calls, and returns one newly persisted
preview.

`create_group_room` derives address, city, province, holding, and related
property data only when all existing group rooms agree. Private inventory and
group common areas appear together in the preview. If RAMA would create or
change a common area's landlord-use classification, it first asks whether the
landlord or an immediate relative uses that area. Confirmation creates the
room, inventory, group membership, and area associations in one database
transaction. An empty group does not require a disposable “first room” as a
workaround: the operation can bootstrap directly from one exact existing
holding/address plus any missing city/province, all shown in the preview.

### Units: bedrooms are layout, not listings

`create_property_structure`, `update_unit_layout` and `set_unit_rental_mode`
replace `create_house_layout` for anything new. The rule they encode is the one
the old tool got wrong: **a bedroom described inside a floor is internal layout,
not something on the market.** `create_house_layout` turned every described
bedroom into a rentable ROOM listing and every floor name into a PropertyGroup,
so "McCaughey Main Floor is one complete unit, and inside it there are these
rooms" produced three separate room listings and no way to say they were one
home. It stays registered for existing room-by-room houses, with a docstring
that says plainly when not to use it.

What is *offered* is a separate decision — the unit's `rental_mode`. When the
landlord has not made it clear, `_rental_mode_from_text` returns `None` and the
tool asks exactly one question rather than defaulting. This is deliberate:
guessing is what produced three listings for one home, and the landlord could
not see that it had guessed. Missing facts produce a usable unit flagged
`layout_complete=False` with a note, never an invented bathroom count.

`set_unit_rental_mode` is `own_confirm=True` — it reshapes what is on the
market, so it pauses for its own confirmation inside a multi-step plan even
though it deletes nothing. It refuses outright while any lease is live in the
unit.

### Playbooks

`plan_operation` covers listing-scoped work (`delete_listings`,
`terminate_and_delete`, `retire_listings`, `update_status`, `set_visibility`)
and unit-scoped work (`switch_rental_mode`). Unit scope matters because "rent
the Wascana floors room by room" is one intent over three physical spaces, each
of which may be blocked for its own reason.

Registered tools are thin wrappers in `tools.py`, and **the registry builds each
tool's JSON schema from the wrapper's signature**, silently dropping any
argument the model sends that is not in it. A parameter added to an
implementation but not its wrapper is therefore unreachable — the model passes
it, it vanishes, and the tool behaves as if it were never sent. This bit twice
(`plan_move_tenant` told the model to pass `pick`, then discarded it).
`test_wrapper_exposes_every_argument_the_implementation_accepts` now fails on
that drift.

### Damage, claims and deposits

`attribute_work_order` records WHO caused a repair and whether they are being
charged. `deposit_position` reports what is held on a lease versus what is
claimed against it.

What these deliberately do NOT do is deduct. Under the BC RTA a landlord may
keep deposit money only with the tenant's WRITTEN agreement, or by applying to
the RTB within 15 days of the later of the tenancy ending and receiving the
forwarding address — and getting it wrong loses the claim AND makes double the
deposit payable. So damage raises a CLAIM the tenant owes; the deposit stays a
separate liability; nothing nets one off the other. Every deposit_position
response carries `lawful_routes` and the double-penalty warning, and the
"what's left" field is named `returnable_if_all_claims_agreed` so it cannot be
read as permission.

Two chain details worth keeping:

- Blame is usually assigned AFTER the job closes ("fixed it, closed it, later
  worked out who broke it"). COMPLETED is terminal, so attribution raises the
  claim itself rather than relying on completion. Idempotent per work order.
- A shared-space job carries no lease, so the claim falls back to the
  responsible tenant's live lease — otherwise it never appears in the deposit
  position and the gap surfaces at move-out.

### Capability gaps

`log_capability_gap` dedupes restatements: identical text first, then a
word-set comparison, because consecutive rewordings differ by an inserted email
address or trailing clause — barely a change in meaning but a long way in
character similarity. A restatement keeps the fuller detail and can raise
priority but never lowers it. A gap already BUILT or DISMISSED that comes back
is new information and opens a fresh row. `triage_capability_gap` and
`GET/PATCH /api/rama/capability-gaps/` make the backlog workable from outside a
chat; neither builds anything.

### Legacy

`create_house_layout` is the composite boundary for a landlord describing a
whole hierarchy at once: physical house, property groups, rooms, private areas,
and exact shared-area access. RAMA keeps the understood hierarchy as a draft,
asks one focused question for missing city/province and landlord-sharing
classification, then saves one preview. Confirmation creates or idempotently
reuses the holding and groups, creates the rooms, and records private, subset-
shared, and group-wide areas inside one transaction. An unspecified group may
remain empty until the landlord describes its rooms.

RAMA emits dashboard links only from the registered link map and canonical
frontend origin. “Show my dashboard properties” therefore resolves to
`https://www.rentium.ca/dashboard/properties`; legacy `app.rentium.ca` links
are never generated.
