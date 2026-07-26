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
