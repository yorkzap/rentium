"""
The PDF engine, tested against the real RTB-8 that ships in the catalogue.

Deliberately not against a synthetic fixture. The whole point of the coordinate
maths is that it lands on a government form's actual boxes, and a PDF we
generated ourselves would agree with our own assumptions by construction.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader

from rentium.leases import form_intel
from rentium.leases import form_render
from rentium.leases.lease_forms import LeaseFormPlacement

RTB8 = Path(__file__).resolve().parent / "form_templates" / "bc" / "rtb8.pdf"

# Read out of the file itself with pypdf, not assumed:
#   Signature6 /Sig  [14.0, 164.8, 165.7, 196.8]  on a 612x792 page
SIG_RECT = (14.0, 164.8, 165.7, 196.8)


@pytest.fixture(scope="module")
def rtb8_bytes() -> bytes:
    return RTB8.read_bytes()


@pytest.fixture(scope="module")
def rtb8_info(rtb8_bytes) -> dict:
    return form_render.inspect_pdf(rtb8_bytes)


def test_inspect_reads_geometry_and_widgets(rtb8_info):
    assert rtb8_info["page_count"] == 1
    assert rtb8_info["page_sizes"] == [{"width": 612.0, "height": 792.0}]
    assert rtb8_info["rotated"] is False

    signatures = [f for f in rtb8_info["acroform_fields"] if f["type"] == "/Sig"]
    assert len(signatures) == 3, "RTB-8 has three signature widgets"


def test_placements_round_trip_back_to_the_same_points(rtb8_info):
    """A placed box must map back onto the widget it was derived from.

    This is the test that catches a flipped y-axis, and a flipped y-axis is
    silent: the signature simply appears in the wrong box on a legal form.
    """
    placements = form_render.placements_from_acroform(rtb8_info)
    by_key = {p["key"]: p for p in placements}
    assert "signature6" in by_key

    signature = by_key["signature6"]
    assert signature["kind"] == LeaseFormPlacement.Kind.SIGNATURE

    box = form_render.PageBox(left=0.0, bottom=0.0, width=612.0, height=792.0)
    left, bottom, width, height = form_render._box_to_points(signature, box)

    x0, y0, x1, y1 = SIG_RECT
    assert left == pytest.approx(x0, abs=0.5)
    assert bottom == pytest.approx(y0, abs=0.5)
    assert width == pytest.approx(x1 - x0, abs=0.5)
    assert height == pytest.approx(y1 - y0, abs=0.5)


def test_off_page_widgets_are_dropped(rtb8_info):
    """RTB-8 parks a script-holding field at +/-32768. It is not signable."""
    placements = form_render.placements_from_acroform(rtb8_info)
    assert len(rtb8_info["acroform_fields"]) == 26
    assert len(placements) == 25
    assert all(0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0 for p in placements)


def test_stamp_writes_values_and_flattens(rtb8_bytes, rtb8_info):
    placements = form_render.placements_from_acroform(rtb8_info)
    values = {
        "signature6": "Raj Singh",
        "date": "31/08/2026",
        "time": "1:00 PM",
        "tenant_first_and_middle": "Sarah",
    }
    out = form_render.stamp(rtb8_bytes, placements, values)
    reader = PdfReader(io.BytesIO(out))

    # An executed document must not still be a fillable form: a surviving
    # /AcroForm means the file can be re-typed after everyone has signed it.
    assert "/AcroForm" not in reader.trailer["/Root"]
    assert "/Annots" not in reader.pages[0]

    assert len(reader.pages) == 1
    assert float(reader.pages[0].mediabox.width) == 612.0

    text = reader.pages[0].extract_text()
    for value in values.values():
        assert value in text


def test_stamp_ignores_placements_with_nothing_in_them(rtb8_bytes, rtb8_info):
    placements = form_render.placements_from_acroform(rtb8_info)
    out = form_render.stamp(rtb8_bytes, placements, {})
    text = PdfReader(io.BytesIO(out)).pages[0].extract_text()
    assert "Mutual Agreement to End a Tenancy" in text


def test_render_page_png_is_one_pixel_per_point_at_72dpi(rtb8_bytes):
    from PIL import Image

    png = form_render.render_page_png(rtb8_bytes, 0, dpi=72)
    assert Image.open(io.BytesIO(png)).size == (612, 792)


def test_render_page_png_refuses_a_page_that_is_not_there(rtb8_bytes):
    with pytest.raises(form_render.FormRenderError):
        form_render.render_page_png(rtb8_bytes, 7)


def test_unreadable_input_fails_with_a_sentence(rtb8_bytes):
    with pytest.raises(form_render.FormRenderError):
        form_render.inspect_pdf(b"not a pdf at all")


def test_normalise_leaves_an_unrotated_pdf_byte_identical(rtb8_bytes):
    assert form_render.normalise_pdf(rtb8_bytes) is rtb8_bytes


def test_signature_font_is_registered_or_falls_back():
    """Typed signatures must render even if the calligraphic face is missing."""
    from reportlab.pdfbase import pdfmetrics

    name = form_render.signature_font()
    assert pdfmetrics.stringWidth("Raj Singh", name, 12) > 0


# ---------------------------------------------------------------------------
# form_intel
# ---------------------------------------------------------------------------


def test_rtb8_is_recognised_as_an_end_of_tenancy_form(rtb8_bytes, rtb8_info):
    text = PdfReader(io.BytesIO(rtb8_bytes)).pages[0].extract_text()
    names = [f["name"] for f in rtb8_info["acroform_fields"]]

    suggestion = form_intel.suggest_form_purpose(text, names, "rtb8.pdf")

    assert suggestion.stage == "MOVE_OUT"
    assert suggestion.confidence == "high"
    assert suggestion.is_actionable
    assert any(signal["label"] == "RTB-8" for signal in suggestion.signals)
    # Suggestion payloads land in a JSONField and are read by RAMA and the UI.
    assert all(isinstance(key, str) for key in suggestion.scores)


def test_an_unreadable_scan_suggests_nothing_rather_than_guessing():
    suggestion = form_intel.suggest_form_purpose("", [], "scan_0042.pdf")
    assert suggestion.stage == ""
    assert suggestion.confidence == "none"
    assert not suggestion.is_actionable


def test_a_document_that_says_both_things_is_not_confident():
    """Evidence for two stages should read as uncertainty, not a coin flip."""
    suggestion = form_intel.suggest_form_purpose(
        "Pet Agreement Addendum. Notice to end tenancy if breached.", [], ""
    )
    assert suggestion.confidence in {"low", "medium"}
    if suggestion.confidence == "low":
        assert not suggestion.is_actionable
