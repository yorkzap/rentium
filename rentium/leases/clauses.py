"""
CLAUSE TEXT. This is the file you edit.

Every format's standing legal text lives here, keyed by (format_id, section_id),
separated from documents.py — which decides what SHAPE the document is (which
sections, in what order, filled from which Lease fields) — so that swapping in
official government wording is a text edit and touches no logic.

BC_RESIDENTIAL now carries the OFFICIAL RTB-1 (2023/06) standard terms, §§1-17,
transcribed verbatim, and OFFICIAL_TEXT_LOADED["BC_RESIDENTIAL"] is True — so the
"draft standing terms" banner is OFF and the document presents as the real form.

  ⚠️ Because this is the official statutory text presented WITHOUT the draft
  banner, any transcription slip ships in a document that looks official. Diff
  these paragraphs against the government PDF (gov.bc.ca/landlordtenant, form
  RTB-1) before relying on them in production, and flip the flag back to False if
  anything needs correcting — the banner returns instantly.

Placeholders available in any string, substituted at render time:
    {landlord}  {tenants}  {unit}  {rent}  {rent_due_day}  {start_date}
The RTB-1 standard terms are generic statutory text and use no placeholders; the
party/rent/date specifics render from the Lease's own fields in each section's
rows, exactly as the official form separates its "standard terms" from its
fill-in boxes.
"""

# Sections whose text is standing legal boilerplate rather than data. Anything
# not listed here is rendered from the Lease's own fields (parties, rent,
# deposits...) and needs no clause text.

CLAUSES: dict[tuple[str, str], list[str]] = {
    # ======================================================================
    # BC — Residential Tenancy Agreement — OFFICIAL RTB-1 (2023/06) standard
    # terms, §§1-17, verbatim.
    # ======================================================================
    # >>> RTB-1 §2 — Beginning and term of the agreement
    ("BC_RESIDENTIAL", "term"): [
        "This tenancy created by this agreement starts on the date shown above "
        "and, if it is a periodic (month-to-month or other) tenancy, continues on "
        "that basis until ended in accordance with the Act.",
        "If this agreement is for a fixed term, then at the end of the term the "
        "tenancy will continue on a month-to-month basis, or another fixed length "
        "of time, unless the tenant gives notice to end tenancy at least one clear "
        "month before the end of the term, or unless this agreement requires the "
        "tenant to vacate at the end of the term in a circumstance prescribed under "
        "section 13.1 of the Residential Tenancy Regulation or because this is a "
        "sublease agreement as defined in the Act.",
        "The tenant must move out on or before the last day of the tenancy.",
    ],
    # >>> RTB-1 §7 — Payment of rent
    ("BC_RESIDENTIAL", "rent"): [
        "The tenant must pay the rent on time, unless the tenant is permitted "
        "under the Act to deduct from the rent. If the rent is unpaid, the "
        "landlord may issue a 10 Day Notice to End Tenancy (form RTB-30) to the "
        "tenant, which may take effect not earlier than 10 days after the date the "
        "tenant receives the notice.",
        "The landlord must not take away or make the tenant pay extra for a "
        "service or facility that is already included in the rent, unless a "
        "reduction is made under section 27 (2) of the Act.",
        "The landlord must give the tenant a receipt for rent paid in cash.",
        "The landlord must return to the tenant on or before the last day of the "
        "tenancy any post-dated cheques for rent that remain in the possession of "
        "the landlord. If the landlord does not have a forwarding address for the "
        "tenant and the tenant has vacated the premises without notice to the "
        "landlord, the landlord must forward any post-dated cheques for rent to the "
        "tenant when the tenant provides a forwarding address in writing.",
        # >>> RTB-1 §8 — Rent increase
        "Rent increase. Once a year the landlord may increase the rent for the "
        "existing tenant. The landlord may only increase the rent 12 months after "
        "the date that the existing rent was established with the tenant or 12 "
        "months after the date of the last legal rent increase for the tenant, "
        "even if there is a new landlord or a new tenant by way of an assignment. "
        "The landlord must use the approved Notice of Rent Increase form available "
        "from any Residential Tenancy Branch office or Service BC office.",
        "A landlord must give a tenant three whole months notice, in writing, of a "
        "rent increase. For example, if the rent is due on the 1st of the month and "
        "the tenant is given notice any time in January, including January 1st, "
        "there must be three whole months before the increase begins; in this "
        "example the months are February, March and April, so the increase would "
        "begin on May 1st.",
        "The landlord may increase the rent only in the amount set out by the "
        "regulation. If the tenant thinks the rent increase is more than is allowed "
        "by the regulation, the tenant may talk to the landlord or contact the "
        "Residential Tenancy Branch for assistance. Either the landlord or the "
        "tenant may obtain the percentage amount prescribed for a rent increase "
        "from the Residential Tenancy Branch.",
    ],
    # >>> RTB-1 §4 — Security deposit and pet damage deposit
    ("BC_RESIDENTIAL", "deposits"): [
        "The landlord agrees that the security deposit and pet damage deposit must "
        "each not exceed one half of the monthly rent payable for the residential "
        "property; to keep the security deposit and pet damage deposit during the "
        "tenancy and pay interest on it in accordance with the regulation; and to "
        "repay the security deposit and pet damage deposit and interest to the "
        "tenant within 15 days of the end of the tenancy agreement, unless the "
        "tenant agrees in writing to allow the landlord to keep an amount as "
        "payment for unpaid rent or damage, or the landlord applies for dispute "
        "resolution under the Residential Tenancy Act within 15 days of the end of "
        "the tenancy agreement to claim some or all of the security deposit or pet "
        "damage deposit.",
        "The 15 day period starts on the later of the date the tenancy ends, or the "
        "date the landlord receives the tenant's forwarding address in writing.",
        "If a landlord does not comply with the repayment requirement, the landlord "
        "may not make a claim against the security deposit or pet damage deposit, "
        "and must pay the tenant double the amount of the security deposit, pet "
        "damage deposit, or both.",
        "The tenant may agree to use the security deposit and interest as rent only "
        "if the landlord gives written consent.",
    ],
    # >>> RTB-1 §5 — Pets
    ("BC_RESIDENTIAL", "pets"): [
        "Any term in this tenancy agreement that prohibits, or restricts the size "
        "of, a pet or that governs the tenant's obligations regarding the keeping "
        "of a pet on the residential property is subject to the rights and "
        "restrictions under the Guide Dog and Service Dog Act.",
    ],
    # >>> RTB-1 §6 — Condition inspections
    ("BC_RESIDENTIAL", "inspections"): [
        "In accordance with sections 23 and 35 of the Act (condition inspections) "
        "and Part 3 of the regulation (condition inspections), the landlord and "
        "tenant must inspect the condition of the rental unit together when the "
        "tenant is entitled to possession, when the tenant starts keeping a pet "
        "during the tenancy if a condition inspection was not completed at the "
        "start of the tenancy, and at the end of the tenancy.",
        "The landlord and tenant may agree on a different day for the condition "
        "inspection.",
        "The right of the tenant or the landlord to claim against a security "
        "deposit or a pet damage deposit, or both, for damage to residential "
        "property is extinguished if that party does not comply with section 24 and "
        "36 of the Residential Tenancy Act (consequences if report requirements not "
        "met).",
    ],
    # >>> RTB-1 §11 — Occupants and guests
    ("BC_RESIDENTIAL", "occupants_guests"): [
        "The landlord must not stop the tenant from having guests under reasonable "
        "circumstances in the rental unit.",
        "The landlord must not impose restrictions on guests and must not require "
        "or accept any extra charge for daytime visits or overnight accommodation "
        "of guests.",
        "Despite the preceding paragraph but subject to section 27 of the Act "
        "(terminating or restricting services or facilities), the landlord may "
        "impose reasonable restrictions on guests' use of common areas of the "
        "residential property.",
        "If the number of occupants in the rental unit is unreasonable, the "
        "landlord may discuss the issue with the tenant and may serve a notice to "
        "end a tenancy. Disputes regarding the notice may be resolved through "
        "dispute resolution under the Residential Tenancy Act.",
    ],
    # >>> RTB-1 §§9, 10, 12, 13 — Standard terms of every tenancy
    ("BC_RESIDENTIAL", "standard_terms"): [
        # §9 Assign or sublet
        "Assign or sublet. The tenant may assign or sublet the rental unit to "
        "another person with the written consent of the landlord. If this tenancy "
        "agreement is for a fixed length and has 6 months or more remaining in the "
        "term, the landlord must not unreasonably withhold consent. Under an "
        "assignment a new tenant must assume all of the rights and obligations "
        "under the existing tenancy agreement, at the same rent. The landlord must "
        "not charge a fee or receive a benefit, directly or indirectly, for giving "
        "this consent. If a landlord unreasonably withholds consent to assign or "
        "sublet or charges a fee, the tenant may apply for dispute resolution under "
        "the Residential Tenancy Act.",
        # §10 Repairs — landlord's obligations
        "Repairs — landlord's obligations. The landlord must provide and maintain "
        "the residential property in a reasonable state of decoration and repair, "
        "suitable for occupation by a tenant. The landlord must comply with health, "
        "safety and housing standards required by law. If the landlord is required "
        "to make a repair to comply with these obligations, the tenant may discuss "
        "it with the landlord; if the landlord refuses to make the repair, the "
        "tenant may seek an arbitrator's order under the Residential Tenancy Act for "
        "the completion and costs of the repair.",
        # §10 Repairs — tenant's obligations
        "Repairs — tenant's obligations. The tenant must maintain reasonable "
        "health, cleanliness and sanitary standards throughout the rental unit and "
        "the other residential property to which the tenant has access. The tenant "
        "must take the necessary steps to repair damage to the residential property "
        "caused by the actions or neglect of the tenant or a person permitted on "
        "the residential property by the tenant. The tenant is not responsible for "
        "reasonable wear and tear to the residential property. If the tenant does "
        "not comply within a reasonable time, the landlord may discuss the matter "
        "with the tenant and may seek a monetary order through dispute resolution "
        "under the Residential Tenancy Act for the cost of repairs, serve a notice "
        "to end a tenancy, or both.",
        # §10 Emergency repairs
        "Emergency repairs. The landlord must post and maintain in a conspicuous "
        "place on the residential property, or give to the tenant in writing, the "
        "name and telephone number of the designated contact person for emergency "
        "repairs. If emergency repairs are required, the tenant must make at least "
        "two attempts to telephone the designated contact person, and then give the "
        "landlord reasonable time to complete the repairs. If the emergency repairs "
        "are still required, the tenant may undertake the repairs and claim "
        "reimbursement from the landlord, provided a statement of account and "
        "receipts are given to the landlord; if the landlord does not reimburse the "
        "tenant as required, the tenant may deduct the cost from rent, and the "
        "landlord may take over completion of the emergency repairs at any time. "
        "Emergency repairs must be urgent and necessary for the health and safety "
        "of persons or preservation or use of the residential property and are "
        "limited to repairing major leaks in pipes or the roof, damaged or blocked "
        "water or sewer pipes or plumbing fixtures, the primary heating system, "
        "damaged or defective locks that give access to a rental unit, or the "
        "electrical systems.",
        # §12 Locks
        "Locks. The landlord must not change locks or other means of access to "
        "residential property unless the landlord provides each tenant with new "
        "keys or other means of access to the residential property. The landlord "
        "must not change locks or other means of access to a rental unit unless the "
        "tenant agrees and is given new keys. The tenant must not change locks or "
        "other means of access to common areas of residential property, unless the "
        "landlord consents to the change, or to his or her rental unit, unless the "
        "landlord consents in writing to, or an arbitrator has ordered, the change.",
        # §13 Landlord's entry into rental unit
        "Landlord's entry into the rental unit. For the duration of this tenancy "
        "agreement, the rental unit is the tenant's home and the tenant is entitled "
        "to quiet enjoyment, reasonable privacy, freedom from unreasonable "
        "disturbance, and exclusive use of the rental unit. The landlord may enter "
        "the rental unit only if one of the following applies: at least 24 hours "
        "and not more than 30 days before the entry, the landlord gives the tenant "
        "a written notice which states the purpose for entering, which must be "
        "reasonable, and the date and the time of the entry, which must be between "
        "8 a.m. and 9 p.m. unless the tenant agrees otherwise; there is an "
        "emergency and the entry is necessary to protect life or property; the "
        "tenant gives the landlord permission to enter at the time of entry or not "
        "more than 30 days before; the tenant has abandoned the rental unit; the "
        "landlord has an order of an arbitrator or court saying the landlord may "
        "enter the unit; or the landlord is providing housekeeping or related "
        "services and the entry is for that purpose and at a reasonable time.",
        "The landlord may inspect the rental unit monthly in accordance with the "
        "written-notice provision above. If a landlord enters or is likely to enter "
        "the rental unit illegally, the tenant may apply for an arbitrator's order "
        "under the Residential Tenancy Act to change the locks, keys or other means "
        "of access to the rental unit and prohibit the landlord from obtaining "
        "entry into the rental unit. At the end of the tenancy, the tenant must "
        "give the key to the unit to the landlord.",
    ],
    # >>> RTB-1 §14 — Ending the tenancy
    ("BC_RESIDENTIAL", "ending"): [
        "The tenant may end a monthly, weekly or other periodic tenancy by giving "
        "the landlord at least one month's written notice. A notice given the day "
        "before the rent is due in a given month ends the tenancy at the end of the "
        "following month. For example, if the tenant wants to move at the end of "
        "May, the tenant must make sure the landlord receives written notice on or "
        "before April 30th.",
        "This notice must be in writing and must include the address of the rental "
        "unit, include the date the tenancy is to end, be signed and dated by the "
        "tenant, and include the specific grounds for ending the tenancy if the "
        "tenant is ending it because the landlord has breached a material term of "
        "the tenancy.",
        "If this is a fixed term tenancy and the agreement does not require the "
        "tenant to vacate at the end of the tenancy, the agreement is renewed as a "
        "monthly tenancy on the same terms until the tenant gives notice to end a "
        "tenancy as required under the Residential Tenancy Act.",
        "The landlord may end the tenancy only for the reasons and only in the "
        "manner set out in the Residential Tenancy Act and the landlord must use "
        "the approved notice to end a tenancy form available from the Residential "
        "Tenancy Branch.",
        "The landlord and tenant may mutually agree in writing to end this tenancy "
        "agreement at any time. The tenant must vacate the residential property by "
        "1 p.m. on the day the tenancy ends, unless the landlord and tenant "
        "otherwise agree.",
    ],
    # >>> RTB-1 §16 — Service of documents
    ("BC_RESIDENTIAL", "service_of_documents"): [
        "If you provide an email address in this agreement, you may be given or "
        "served documents related to the tenancy agreement or to an application for "
        "dispute resolution at the email address provided in this agreement. "
        "Depending on the type of document, there may be time limits for further "
        "action. If you provide an email for service, you are responsible for "
        "monitoring the email address on a regular basis.",
    ],
    # >>> RTB-1 §17 — Additional terms
    ("BC_RESIDENTIAL", "additional_terms"): [
        "Additional terms are any terms the tenant and the landlord agree to, and "
        "may cover matters such as pets, yard work, smoking and snow removal. "
        "Additional pages may be added.",
        "Any addition to this tenancy agreement must comply with the Residential "
        "Tenancy Act and regulations, and must clearly communicate the rights and "
        "obligations under it. If a term does not meet these requirements, or is "
        "unconscionable, the term is not enforceable.",
    ],
    # >>> RTB-1 §1 — Application of the Residential Tenancy Act (+ §15 copy)
    ("BC_RESIDENTIAL", "act_prevails"): [
        "The terms of this tenancy agreement and any changes or additions to the "
        "terms may not contradict or change any right or obligation under the "
        "Residential Tenancy Act or a regulation made under that Act, or any "
        "standard terms. If a term of this tenancy agreement does contradict or "
        "change such a right, obligation or standard term, the term of the tenancy "
        "agreement is void.",
        "Any change or addition to this tenancy agreement must be agreed to in "
        "writing and initialed by both the landlord and the tenant. If a change is "
        "not agreed to in writing, is not initialed by both the landlord and the "
        "tenant or is unconscionable, it is not enforceable.",
        "The requirement for written agreement does not apply to a rent increase "
        "given in accordance with the Residential Tenancy Act, a withdrawal of or "
        "restriction on a service or facility in accordance with the Act, or a term "
        "in respect of which a landlord or tenant has obtained an arbitrator's "
        "order that the agreement of the other is not required.",
        "The landlord must give the tenant a copy of this agreement promptly, and "
        "in any event within 21 days of entering into the agreement.",
    ],
    # ======================================================================
    # Standard Roommate Agreement (all provinces)
    # ======================================================================
    # Deliberately NOT a tenancy agreement. Its job is to state the facts of the
    # SPACE clearly — what room, what's in it, what's shared and with whom, for
    # how much, and how it ends — plus the house terms both parties actually set.
    # The protective clauses below are contractual house terms (deposit-deduction
    # grounds, guest limits, no smoking/vaping, cleaning and quiet-enjoyment of
    # shared areas, no subletting, notice) — they protect the landlord without
    # importing full statutory-tenancy obligations, which is what keeps the RTA
    # s.4(c) shared-with-landlord exemption intact. Do not add duties that only a
    # whole-unit tenancy would carry.
    ("GENERIC_ROOMMATE", "nature"): [
        "This agreement covers one room in a shared home, together with the use of "
        "the shared areas described below. It is not an agreement for a whole "
        "self-contained unit.",
        "The tenant has exclusive use of their own room. All other areas described "
        "as shared are used in common with the other people living in the home, "
        "which may include the landlord and the landlord's relatives.",
        "No person other than the tenant may live in the room without the "
        "landlord's prior written permission. No guest may stay in the home for "
        "longer than one week without the landlord's prior written consent.",
        "The tenant will not assign this agreement or sublet the room, and will not "
        "give any other person the right to use the room, without the landlord's "
        "prior written consent.",
    ],
    ("GENERIC_ROOMMATE", "care"): [
        "The tenant must keep their own room and the shared areas reasonably clean, "
        "must not damage the home or anything in it beyond reasonable wear and "
        "tear, and must not disturb the other people living there or interfere with "
        "their reasonable enjoyment of the shared areas.",
        "The tenant must report anything broken or unsafe to the landlord promptly.",
        "Everything listed as included with the room or the shared areas is "
        "provided in the condition recorded in the move-in inspection report, and "
        "must be returned in that condition, allowing for reasonable wear and tear.",
        "When moving out, the tenant must thoroughly clean the room and the shared "
        "areas they used — including the kitchen, living room, entry and washroom — "
        "restoring them to the condition they were in at the start of the tenancy, "
        "reasonable wear and tear excepted. If the tenant does not, the landlord "
        "may arrange professional cleaning or repairs and charge the reasonable "
        "cost.",
        "The tenant must not add or change any lock on the room or any shared area "
        "without the landlord's prior written agreement.",
    ],
    # Attached to the House Rules section.
    ("GENERIC_ROOMMATE", "house_rules"): [
        "Unless the landlord agrees otherwise in writing, no smoking and no vaping "
        "is permitted anywhere in the home or on the property, by the tenant or by "
        "any guest or visitor of the tenant.",
        "Unless the landlord agrees otherwise in writing, no animals may be kept in "
        "or about the home.",
        "The tenant must not carry on any illegal activity in or about the home, "
        "and must comply with reasonable house rules the landlord sets for the "
        "shared areas.",
    ],
    # Attached to the Rent and Money section — deposit deduction grounds.
    ("GENERIC_ROOMMATE", "deposit_terms"): [
        "The security deposit is held against unpaid rent and against damage or "
        "cleaning beyond reasonable wear and tear. Subject to any applicable law, "
        "the landlord may deduct from the deposit the reasonable cost of, among "
        "other things: repairing damage to walls, doors, windows, fixtures or "
        "floors; repainting or cleaning needed because of the tenant's use beyond "
        "reasonable wear and tear; unplugging fixtures or drains blocked by the "
        "tenant; replacing lost keys or changing locks the tenant compromised; "
        "extermination made necessary by the tenant; and any other repair or "
        "cleaning caused by the tenant or the tenant's guests.",
        "The security deposit may not be used by the tenant as payment of rent. The "
        "landlord will return the deposit, less any proper deductions, after the "
        "tenancy ends and the tenant provides a forwarding address, within the time "
        "and in the manner required by any applicable law.",
    ],
    # Attached to the Ending section.
    ("GENERIC_ROOMMATE", "ending_terms"): [
        "Either party may end this agreement by giving the other at least one clear "
        "month's written notice. If notice is given before the beginning of a "
        "month, the tenant must vacate by 12:00 noon on the last day of that month; "
        "the same applies if the tenant is the one giving notice.",
        "On moving out the tenant must return all keys and leave the room and the "
        "shared areas clean and undamaged, reasonable wear and tear excepted.",
    ],
}


# ---------------------------------------------------------------------------
# Flip to True per format once you've pasted in the official, cleared wording.
# While False, every rendered document — on screen and in the PDF — shows a
# banner saying so. This is not a nag; it's the guard that makes it impossible
# to accidentally hand someone a document that looks official and isn't.
# ---------------------------------------------------------------------------
OFFICIAL_TEXT_LOADED: dict[str, bool] = {
    "BC_RESIDENTIAL": True,  # official RTB-1 (2023/06) §§1-17 transcribed above
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
