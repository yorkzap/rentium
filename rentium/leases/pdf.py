"""
PDF rendering of a lease.

Renders the SAME Document that the screen renders (leases/documents.py). There
is no separate PDF layout, no second set of field lookups, and therefore no way
for the downloaded file to disagree with what the tenant signed — which is
exactly what the old build_lease_pdf() allowed, because it was an entirely
independent implementation that happened to read some of the same fields.

The old function also carried a disclaimer that it was only "a summary of the
lease terms on file and does not itself replace a fully executed legal
agreement." That's gone, because the document is now the agreement.

Requires reportlab (pip install reportlab) — its absence is what produced the
"pdflib not installed" 501 you were seeing.
"""

from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable
from reportlab.platypus import KeepTogether
from reportlab.platypus import PageBreak
from reportlab.platypus import Paragraph
from reportlab.platypus import SimpleDocTemplate
from reportlab.platypus import Spacer
from reportlab.platypus import Table
from reportlab.platypus import TableStyle

from .documents import render_lease

INK = colors.HexColor("#1c1c1a")
MUTED = colors.HexColor("#6b6862")
RULE = colors.HexColor("#e6e3de")
ACCENT = colors.HexColor("#0f766e")
WARN_BG = colors.HexColor("#fef3c7")
WARN_INK = colors.HexColor("#92400e")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "T",
            parent=base["Title"],
            fontSize=19,
            leading=23,
            textColor=INK,
            alignment=0,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "S", parent=base["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=2
        ),
        "section": ParagraphStyle(
            "H",
            parent=base["Heading2"],
            fontSize=11.5,
            leading=14,
            textColor=INK,
            spaceBefore=16,
            spaceAfter=5,
        ),
        "note": ParagraphStyle(
            "N", parent=base["Normal"], fontSize=8.5, textColor=MUTED, spaceAfter=6
        ),
        "clause": ParagraphStyle(
            "C",
            parent=base["Normal"],
            fontSize=9.5,
            leading=13.5,
            textColor=INK,
            alignment=TA_JUSTIFY,
            spaceAfter=6,
        ),
        "block": ParagraphStyle(
            "B", parent=base["Normal"], fontSize=9.5, leading=13, textColor=INK
        ),
        "small": ParagraphStyle(
            "SM", parent=base["Normal"], fontSize=8, textColor=MUTED
        ),
        "warn": ParagraphStyle(
            "W", parent=base["Normal"], fontSize=9, leading=12.5, textColor=WARN_INK
        ),
    }


def _footer(canvas, doc, lease):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.85 * inch, 0.55 * inch, f"{lease.lease_number}  ·  Rentium")
    canvas.drawRightString(
        letter[0] - 0.85 * inch, 0.55 * inch, f"Page {canvas.getPageNumber()}"
    )
    canvas.restoreState()


def build_lease_pdf(lease) -> bytes:
    doc_model = render_lease(lease)
    st = _styles()

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.8 * inch,
        bottomMargin=0.85 * inch,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        title=f"{doc_model.name} — {lease.lease_number}",
        author="Rentium",
    )

    story = []
    story.append(Paragraph(doc_model.name, st["title"]))
    if doc_model.subtitle:
        story.append(Paragraph(doc_model.subtitle, st["subtitle"]))
    story.append(
        Paragraph(
            f"Agreement no. {lease.lease_number} · Status: {lease.get_status_display()}",
            st["small"],
        )
    )
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.8, color=RULE))

    # The guard: if the official clause text hasn't been pasted into clauses.py
    # yet, SAY SO, loudly, on the document itself. Never let a paraphrase
    # masquerade as a government form.
    if not doc_model.official_text_loaded:
        banner = Table(
            [
                [
                    Paragraph(
                        "<b>Draft standing terms.</b> The numbered clauses in this document are a "
                        "plain-language statement of the standard terms, not the official government "
                        "form's wording. Where they differ, the Act and the official form prevail.",
                        st["warn"],
                    )
                ]
            ],
            colWidths=[6.3 * inch],
        )
        banner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(Spacer(1, 10))
        story.append(banner)

    for section in doc_model.sections:
        flow = [Paragraph(section.title, st["section"])]
        if section.note:
            flow.append(Paragraph(section.note, st["note"]))

        # Label/value rows as a clean two-column table; block rows get their own
        # full-width paragraph so long prose doesn't get squeezed into a column.
        inline = [r for r in section.rows if not r.block]
        if inline:
            data = [
                [Paragraph(r.label, st["small"]), Paragraph(r.value, st["block"])]
                for r in inline
            ]
            table = Table(data, colWidths=[1.9 * inch, 4.4 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        (
                            "LINEBELOW",
                            (0, 0),
                            (-1, -2),
                            0.4,
                            colors.HexColor("#f2f0ec"),
                        ),
                    ]
                )
            )
            flow.append(table)

        for row in [r for r in section.rows if r.block]:
            flow.append(Spacer(1, 5))
            flow.append(Paragraph(f"<b>{row.label}</b>", st["small"]))
            flow.append(Paragraph(row.value, st["block"]))

        if section.clauses:
            flow.append(Spacer(1, 6))
            for clause in section.clauses:
                flow.append(Paragraph(clause, st["clause"]))

        # Never split a section header away from its first rows.
        story.append(KeepTogether(flow[:2]) if len(flow) > 1 else flow[0])
        story.extend(flow[2:])

    if doc_model.legal_note:
        story.append(Spacer(1, 18))
        story.append(HRFlowable(width="100%", thickness=0.8, color=RULE))
        story.append(Spacer(1, 8))
        story.append(Paragraph(doc_model.legal_note, st["small"]))

    pdf.build(
        story,
        onFirstPage=lambda c, d: _footer(c, d, lease),
        onLaterPages=lambda c, d: _footer(c, d, lease),
    )
    buf.seek(0)
    return buf.read()
