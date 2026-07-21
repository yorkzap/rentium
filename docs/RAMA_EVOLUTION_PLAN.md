# RAMA — Safe Self-Evolution Plan ("learn now")

The goal: RAMA should cover the *range* of things a landlord does, and grow that
range over time — **mostly automatically, but always reviewed and tested before
anything new goes live.** Security is the hard constraint: RAMA runs a live
financial + legal + multi-tenant system, so we never let an LLM write and
activate executable code on its own.

This plan has **three parts**, in increasing ambition. Part 1 is DONE; Parts 2
and 3 are specced here to build fresh.

---

## Background: what `playbooks.py` is (why this is safe)

`rama/playbooks.py` is RAMA's **recipe layer**. A "playbook" is a Python function
that turns *intent + targets* into an **ordered list of steps, where every step
calls a tool that already exists and already enforces its own guardrails**
(PROTECT, FSM, immutable ledger, owner-scoping). The model never writes steps —
it picks a playbook; the deterministic runner executes it after your confirm.

The key consequence: **a new capability is very often just a new composition of
existing safe tools.** That is what makes assisted evolution safe — a generated
playbook can only ever chain vetted tools, never touch the database or run raw
code. All three parts below lean on this.

---

## Part 1 — Capability-gap capture ("learn now")  ✅ DONE

When RAMA can't do something, it **logs a structured gap instead of failing
silently**, turning real usage into a reviewable backlog.

**Built & shipped:**
- Model `RamaCapabilityGap` (`rama/models.py`): landlord, request, detail,
  `prioritised`, status (NEW/REVIEWED/BUILT/DISMISSED). Migration `0011`.
- Tools `log_capability_gap(request, detail, learn_now)` and
  `list_capability_gaps(status, limit)` (`rama/domain_actions.py`, wrapped in
  `tools.py`, registered, `tool_meta` risk=low so it's frictionless).
- Persona rule (`roles.py`): *"Can't do it? Don't just say 'I can't' — call
  log_capability_gap; if they say 'learn now', pass learn_now=yes."* De-dupes
  identical NEW gaps; "learn now" prioritises.
- Django admin (`RamaCapabilityGapAdmin`) to review the backlog and mark BUILT.
- Test: `test_log_capability_gap_records_and_prioritises`.

**How you use it today:**
1. Ask RAMA for something it can't do → it logs the gap and tells you.
2. Say **"learn now"** to prioritise it.
3. Review the backlog in **Django admin → Rama capability gaps** (or ask RAMA
   "what have you flagged to learn?").
4. For now, **the capability is built by a human** (you or a dev session with
   me): add a real tool, or — better — a **playbook** that composes existing
   tools. Then set the gap's status to BUILT. Part 3 automates the *drafting* of
   that playbook.

**Left (small, optional polish):**
- A landlord-facing "What RAMA is learning" list in the dashboard (the data +
  `list_capability_gaps` tool already exist; this is just a read screen).
- Auto-log on hard tool errors (currently RAMA logs when it *chooses* to; we
  could also auto-capture unhandled "no such tool" cases).

---

## Part 2 — Decision layer: a smarter General  ⬜ TO BUILD

The safe way "smarter models" help is **better planning/routing over the
existing vetted tools**, not codegen. RAMA already has the 3-role structure
(Corporal = ops, **General = router/chief-of-staff**, FSA = analysis), and each
role *can* run a different model (`RamaPreferences.general_provider/general_model`
resolved in `rama/runtime.py:get_role_config`). Today only the **main** model is
configurable from the UI, so the General falls back to it.

**To build — "smarter model for the General" picker in Settings:**
- Backend: extend the settings serializer/endpoint (`rama/views.py:settings_view`
  + `_settings_payload`) to read/write `general_provider`, `general_model` (and
  an optional separate key). The model fields already exist — no migration.
- Frontend: in `ProfileSettings.tsx` (Account & RAMA), add an optional
  "Smarter model for the General (decision layer)" section: provider + model +
  (optional) key, mirroring the existing main-model controls. Default off =
  General uses your main model.
- Guide: `docs/RAMA_GUIDE.md` §2 already documents the layers — link it there.
- Decision needed from you: **which provider/model** to offer for the General
  (e.g. a Claude model, a Grok reasoning model). Pick one and it gets wired.

**Effort:** small–medium (mostly one settings screen + serializer fields).

---

## Part 3 — LLM-drafted playbooks, human-reviewed  ⬜ TO BUILD (deliberate)

The "mostly automatic" part — but **never auto-activated.**

**Flow:**
1. A prioritised gap (Part 1) is picked up.
2. A **draft step** (via the Claude API, given a system pack: the tool catalog,
   `tool_meta`, the playbook contract) proposes a **new playbook = an ordered
   list of EXISTING tool calls** in the same JSON contract `plan_runner` already
   validates. It may NOT emit raw Python, SQL, or new tools — only compositions
   of vetted tools.
3. The draft is validated against the tool schema + `tool_meta` (unknown tool →
   rejected) and stored as **INACTIVE**, pending review.
4. **A human reviews it** and an **automated test** runs it against the isolated
   eval landlord (the `scripts/rama_eval.py` harness already exists). Only on
   pass + explicit approval does it flip to ACTIVE and become selectable.
5. Guardrails unchanged: every step still re-validates its own tool's guards at
   execution; risky steps (`own_confirm`) still pause for your yes.

**Hard rules (security):**
- No generated raw code, no direct DB access, no new low-level tools — only
  playbooks composed of existing tools.
- Nothing activates without passing tests **and** a human approval.
- The generating model is given least-privilege context; the eval landlord is
  isolated so a bad draft can never touch real data.

**Explicitly NOT doing:** an LLM that writes + auto-registers executable tool
code in production. That is arbitrary code execution and is off the table.

**Effort:** medium–large; do it as its own project after Parts 1–2 are in use.

---

## Status at a glance

| Part | What | Status |
|---|---|---|
| 1 | Capability-gap capture + "learn now" | ✅ Done (model, tools, persona, admin, test) |
| 1b | Landlord-facing "what RAMA is learning" screen | ⬜ Optional polish |
| 2 | Smarter-General model picker in Settings | ⬜ To build (fields exist; needs serializer + UI + your model choice) |
| 3 | LLM-drafted playbooks with human review + tests | ⬜ To build (deliberate, later) |

**Recommended order:** use Part 1 now → build Part 2 (quick win, makes the
decision layer real) → then Part 3 as a focused project.
