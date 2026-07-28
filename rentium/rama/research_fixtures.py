"""
Canned research results for evals and CI.

Selected with `RAMA_RESEARCH_BACKEND=fake`. Two reasons this exists rather
than mocking at the test boundary: evals drive the real HTTP surface and must
be reproducible across runs and across models, and CI should not depend on a
third-party search API being up or on someone's quota.

The figures here are fixtures, NOT current program amounts. Nothing in the
product reads them outside `fake` mode.
"""

from __future__ import annotations

FIXTURES: dict[str, list[dict]] = {
    "bc_heat_pump_rebate": [
        {
            "url": "https://betterhomesbc.ca/rebate/heat-pump/",
            "title": "Heat pump rebates (fixture)",
            "status": 200,
            "text": (
                "Fixture page for tests. Eligible households may receive up to "
                "$5,000 toward a qualifying air-source heat pump. Income "
                "qualified households may receive up to $9,500. Installation "
                "must be completed by a registered contractor."
            ),
        }
    ],
    "bc_window_rebate": [
        {
            "url": "https://betterhomesbc.ca/rebate/windows/",
            "title": "Window rebates (fixture)",
            "status": 200,
            "text": (
                "Fixture page for tests. Rebates of $80 per window are "
                "available for qualifying ENERGY STAR replacements, to a "
                "maximum of $2,000 per home."
            ),
        }
    ],
    "bc_insulation_rebate": [
        {
            "url": "https://betterhomesbc.ca/rebate/insulation/",
            "title": "Insulation rebates (fixture)",
            "status": 200,
            "text": (
                "Fixture page for tests. Attic insulation upgrades may qualify "
                "for up to $1,800 depending on the R-value achieved."
            ),
        }
    ],
    "cleanbc_income_qualified": [
        {
            "url": "https://betterhomesbc.ca/income-qualified/",
            "title": "Income qualified program (fixture)",
            "status": 200,
            "text": (
                "Fixture page for tests. Households under the income threshold "
                "may receive up to $9,500 with no contractor co-payment."
            ),
        }
    ],
    "bc_mortgage_rates": [
        {
            "url": "https://www.bankofcanada.ca/rates/",
            "title": "Posted rates (fixture)",
            "status": 200,
            "text": "Fixture page for tests. Five-year conventional rate: 5.79 percent.",
        }
    ],
    # A page whose text does NOT contain the figure a model might claim from
    # it — used to prove the verbatim gate actually excludes the option.
    "unverifiable_topic": [
        {
            "url": "https://betterhomesbc.ca/rebate/heat-pump/",
            "title": "Page without the number (fixture)",
            "status": 200,
            "text": "Fixture page for tests. Rebates vary by program and region.",
        }
    ],
}
