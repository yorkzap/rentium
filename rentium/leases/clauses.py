"""
CLAUSE TEXT. This is the file you edit.

Every format's standing legal text lives here, keyed by (format_id, section_id),
separated from documents.py — which decides what SHAPE the document is (which
sections, in what order, filled from which Lease fields) — so that swapping in
official government wording is a text edit and touches no logic.

You said you've had the official RTB-1 text cleared. Here's how to drop it in:

    1. Find the block below marked  # >>> RTB-1 §N
    2. Replace the list of strings with the official paragraphs, one string per
       paragraph. Keep the key.
    3. Set OFFICIAL_TEXT_LOADED["BC_RESIDENTIAL"] = True at the bottom.

Until that flag is True, every rendered document (screen AND pdf) carries a
visible banner saying the standing terms are a plain-language paraphrase and
the Act prevails. That banner disappears the moment you flip the flag. It is
deliberately not possible to quietly ship a document that *looks* official but
isn't — that's the exact failure mode worth engineering against.

Placeholders available in any string, substituted at render time:
    {landlord}  {tenants}  {unit}  {rent}  {rent_due_day}  {start_date}
"""

# Sections whose text is standing legal boilerplate rather than data. Anything
# not listed here is rendered from the Lease's own fields (parties, rent,
# deposits...) and needs no clause text.

CLAUSES: dict[tuple[str, str], list[str]] = {
    # ======================================================================
    # BC — Residential Tenancy Agreement (modelled on RTB-1)
    # ======================================================================
    # >>> RTB-1 §2 — Beginning and length of the tenancy
    ("BC_RESIDENTIAL", "term"): [
        "This tenancy begins on the start date shown above.",
        "If this agreement is for a fixed term, the tenancy does not automatically "
        "end on the end date. Unless the landlord and tenant agree otherwise, or "
        "unless this agreement requires the tenant to vacate on that date under a "
        "circumstance prescribed by the Residential Tenancy Regulation, the tenancy "
        "continues on a month-to-month basis on the same terms after the end date.",
    ],
    # >>> RTB-1 §3 — Rent
    ("BC_RESIDENTIAL", "rent"): [
        "The tenant must pay the rent shown above on or before the due date, on "
        "time and in full.",
        "The landlord must not increase the rent except in accordance with the "
        "Residential Tenancy Act — once every twelve months, by no more than the "
        "amount allowed under the Act, and only after giving the tenant at least "
        "three whole months' written notice on the approved form.",
        "If the tenant does not pay the rent when it is due, the landlord may give "
        "the tenant a notice to end the tenancy which takes effect ten days after "
        "the tenant receives it, unless the tenant pays the overdue rent within "
        "five days of receiving that notice.",
    ],
    # >>> RTB-1 §4 — Security deposit and pet damage deposit
    ("BC_RESIDENTIAL", "deposits"): [
        "Neither the security deposit nor the pet damage deposit may exceed one "
        "half of one month's rent.",
        "The landlord holds these deposits in trust for the tenant. Within fifteen "
        "days of the later of the end of the tenancy and the date the landlord "
        "receives the tenant's forwarding address in writing, the landlord must "
        "either repay the deposits with interest, or apply for dispute resolution "
        "to claim against them.",
        "The landlord may only keep any part of a deposit if the tenant agrees to "
        "it in writing at the end of the tenancy, or if an arbitrator orders it.",
    ],
    # >>> RTB-1 §6 — Condition inspections
    ("BC_RESIDENTIAL", "inspections"): [
        "The landlord and the tenant must inspect the condition of the rental unit "
        "together on the day the tenant is entitled to possession, or on another "
        "day agreed to by both. They must inspect it together again on or after the "
        "day the tenant stops occupying it.",
        "The landlord must complete a condition inspection report and give the "
        "tenant a copy: within seven days of the move-in inspection, and within "
        "fifteen days of the later of the move-out inspection and the day the "
        "landlord receives the tenant's forwarding address in writing.",
        "A landlord who does not offer the tenant two opportunities to inspect, or "
        "who does not complete and deliver the report, loses the right to claim "
        "against the deposits for damage. A tenant who does not attend either "
        "opportunity, or who does not sign the report, loses the right to dispute "
        "what it records.",
    ],
    # >>> RTB-1 §§7-13 — Standard terms of every tenancy
    ("BC_RESIDENTIAL", "standard_terms"): [
        "Assignment and subletting. The tenant must not assign this agreement or "
        "sublet the rental unit without the landlord's written consent. If this is "
        "a fixed-term tenancy of six months or more, the landlord must not "
        "unreasonably withhold that consent.",
        "Repairs. The landlord must provide and maintain the rental unit and "
        "property in a state that complies with health, safety and housing "
        "standards required by law, and that, having regard to its age, character "
        "and location, makes it suitable for occupation. The tenant must maintain "
        "reasonable health, cleanliness and sanitary standards, and must repair "
        "damage caused by their own actions or neglect, or those of anyone they "
        "permit on the property. The tenant is not responsible for reasonable wear "
        "and tear.",
        "Emergency repairs. If an emergency repair is needed — a major leak, "
        "damaged or blocked water or sewer pipes, the primary heating system, "
        "damaged or defective locks that give access to the rental unit, or the "
        "electrical systems — the tenant must make at least two attempts to "
        "telephone the person the landlord has named for emergency repairs, and "
        "then give the landlord reasonable time to make the repair. The tenant may "
        "then arrange the repair themselves and claim reimbursement with receipts.",
        "Locks. The landlord must not change the locks to the rental unit unless "
        "the tenant agrees and is given a new key. The tenant must not change the "
        "locks without the landlord's written consent.",
        "Entry by the landlord. The landlord may enter the rental unit only with "
        "the tenant's agreement given at the time, or after giving the tenant "
        "written notice at least twenty-four hours and no more than thirty days "
        "beforehand, stating the purpose — which must be reasonable — and the date "
        "and time, which must be between 8 a.m. and 9 p.m. unless the tenant "
        "agrees otherwise. The landlord may also enter in an emergency where the "
        "health or safety of a person or the property is at risk.",
        "Quiet enjoyment. The tenant is entitled to quiet enjoyment of the rental "
        "unit, including reasonable privacy, freedom from unreasonable disturbance, "
        "and exclusive possession of the rental unit subject only to the landlord's "
        "right of entry set out above.",
    ],
    # >>> RTB-1 §14 — Ending the tenancy
    ("BC_RESIDENTIAL", "ending"): [
        "The tenant may end a month-to-month tenancy by giving the landlord at "
        "least one clear month's written notice. Notice given in one month ends the "
        "tenancy on the last day of the following month.",
        "The landlord may end the tenancy only for the reasons and with the notice "
        "period set out in the Residential Tenancy Act, using the approved form.",
        "Both parties may agree in writing to end the tenancy on any date, using "
        "the mutual agreement form. Neither party is obliged to sign one.",
        "The tenant must vacate the rental unit by one o'clock in the afternoon on "
        "the day the tenancy ends, leaving it reasonably clean and undamaged except "
        "for reasonable wear and tear, and must return all keys.",
    ],
    # >>> RTB-1 §17 — Additional terms
    ("BC_RESIDENTIAL", "additional_terms"): [
        "An additional term cannot contradict or change any right or duty under "
        "the Residential Tenancy Act or its regulations, and cannot change a "
        "standard term. Any term that does is unenforceable.",
    ],
    ("BC_RESIDENTIAL", "act_prevails"): [
        "The terms of this tenancy agreement may not contradict or change any right "
        "or obligation under the Residential Tenancy Act or the Residential Tenancy "
        "Regulation. Any term that does is void and unenforceable.",
        "Any change or addition to this agreement must be agreed to in writing and "
        "signed by both the landlord and the tenant. If a change is not agreed to "
        "in writing, is unconscionable, or is prohibited under the Act, it is not "
        "enforceable.",
        "The landlord must give the tenant a copy of this agreement within twenty-"
        "one days of entering into it.",
    ],
    # ======================================================================
    # Standard Roommate Agreement (all provinces)
    # ======================================================================
    # Deliberately short. This document's job is to state THE FACTS OF THE
    # SPACE clearly — what room, what's in it, what's shared, with whom, for
    # how much, and how it ends — and to add nothing that could be read as the
    # landlord taking on an obligation they didn't intend. That restraint is
    # the point of it, and every extra clause erodes it.
    ("GENERIC_ROOMMATE", "nature"): [
        "This agreement covers one room in a shared home, together with the use of "
        "the shared areas described below. It is not an agreement for a whole "
        "self-contained unit.",
        "The tenant has exclusive use of their own room. All other areas described "
        "as shared are used in common with the other people living in the home.",
    ],
    ("GENERIC_ROOMMATE", "care"): [
        "The tenant must keep their own room and the shared areas reasonably clean, "
        "must not damage the home or anything in it beyond reasonable wear and tear, "
        "and must not disturb the other people living there.",
        "The tenant must report anything broken or unsafe to the landlord promptly.",
        "Everything listed as included with the room or the shared areas is provided "
        "in the condition recorded in the move-in inspection report, and must be "
        "returned in that condition, allowing for reasonable wear and tear.",
    ],
}


# ---------------------------------------------------------------------------
# Flip to True per format once you've pasted in the official, cleared wording.
# While False, every rendered document — on screen and in the PDF — shows a
# banner saying so. This is not a nag; it's the guard that makes it impossible
# to accidentally hand someone a document that looks official and isn't.
# ---------------------------------------------------------------------------
OFFICIAL_TEXT_LOADED: dict[str, bool] = {
    "BC_RESIDENTIAL": False,
    "SK_RESIDENTIAL": False,
    "GENERIC_RESIDENTIAL": False,
    "GENERIC_ROOMMATE": True,  # this one is ours; there's no official form to match
}


def clauses_for(format_id: str, section_id: str, context: dict) -> list[str]:
    raw = CLAUSES.get((format_id, section_id), [])
    out = []
    for paragraph in raw:
        try:
            out.append(paragraph.format(**context))
        except (KeyError, IndexError):
            out.append(paragraph)  # a stray brace shouldn't blank a legal clause
    return out
