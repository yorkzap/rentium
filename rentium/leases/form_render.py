"""
The PDF half of lease form packs: read a blank form, show it, stamp it, flatten it.

Pure functions over bytes. No models, no HTTP, no storage — which is what makes
the coordinate maths testable on its own, and it is the coordinate maths that
will bite if it is wrong: a signature 20 points too low on a government form is
a worse bug than a crash, because nothing complains.

## One coordinate space

Three coordinate systems meet here and they disagree about everything:

    browser     origin top-left,    y grows DOWN,   unit = CSS pixels
    PDF         origin bottom-left, y grows UP,     unit = points, and the
                MediaBox may not even start at (0, 0)
    Ghostscript origin top-left,    y grows DOWN,   unit = pixels at some DPI

So placements are stored in neither: they are FRACTIONS of the page (0..1) with
a top-left origin, which is the one representation that survives a round trip
through a landlord's browser at an arbitrary zoom. The conversion to PDF points
happens in exactly one function (`_box_to_points`) and nowhere else.

Page rotation is normalised away at upload time (`normalise_pdf`) rather than
carried around: `/Rotate 90` means the rasteriser and the content stream
disagree about which way is up, and every downstream consumer would have to
remember to compensate. Baking it in once means nothing downstream has to know
rotation exists.

## Why we stamp instead of filling AcroForm fields

RTB-8 happens to be a fillable AcroForm. A scanned addendum a landlord uploads
is not. If catalogue forms were filled through their form fields and custom
uploads were stamped, we would have two renderers that could disagree — the
exact failure mode leases/pdf.py's docstring describes for the old
build_lease_pdf(). So AcroForm widgets are used for ONE thing: as a source of
suggested placements (`placements_from_acroform`). Output is always a flattened
stamp, and the flattening explicitly removes /AcroForm and every page's /Annots
so an executed document has no editable layer left on top of the signatures.
"""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from pypdf import PdfReader
from pypdf import PdfWriter
from pypdf import Transformation
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

DEFAULT_RASTER_DPI = 150
MAX_RASTER_DPI = 300
GHOSTSCRIPT_TIMEOUT_SECONDS = 60

#: Calligraphic face for typed signatures. Z003 is URW's Chancery clone, which
#: ships with the urw-base35 set that Ghostscript already pulls into both the
#: local and production images (compose/*/django/Dockerfile). Registering it is
#: best-effort: if a future base image drops it, typed signatures fall back to a
#: base-14 italic rather than the whole signing flow failing.
SIGNATURE_FONT = "RentiumSignature"
_SIGNATURE_FONT_FALLBACK = "Times-Italic"
_SIGNATURE_TYPE1 = (
    "/usr/share/fonts/type1/urw-base35/Z003-MediumItalic.afm",
    "/usr/share/fonts/X11/Type1/Z003-MediumItalic.pfb",
)
BODY_FONT = "Helvetica"

_signature_font_ready: bool | None = None


def signature_font() -> str:
    """Register (once) and return the font name for typed signatures."""
    global _signature_font_ready
    if _signature_font_ready is None:
        _signature_font_ready = False
        afm, pfb = _SIGNATURE_TYPE1
        if Path(afm).exists() and Path(pfb).exists():
            try:
                face = pdfmetrics.EmbeddedType1Face(afm, pfb)
                pdfmetrics.registerTypeFace(face)
                pdfmetrics.registerFont(
                    pdfmetrics.Font(SIGNATURE_FONT, face.name, "WinAnsiEncoding")
                )
                _signature_font_ready = True
            except Exception:  # noqa: BLE001 - a missing font must not break signing
                logger.exception("could not register the signature typeface")
    return SIGNATURE_FONT if _signature_font_ready else _SIGNATURE_FONT_FALLBACK


class FormRenderError(ValidationError):
    """A PDF we cannot read, rasterise, or stamp."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageBox:
    """One page's MediaBox, in PDF points."""

    left: float
    bottom: float
    width: float
    height: float


def _reader(data: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(data))
        # Touching pages is what actually parses the xref table, so a corrupt
        # file fails here rather than three functions later.
        len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise FormRenderError(
            _("That file could not be read as a PDF (%(reason)s).")
            % {"reason": type(exc).__name__}
        ) from exc
    if not len(reader.pages):
        raise FormRenderError(_("That PDF has no pages."))
    return reader


def _page_box(page) -> PageBox:
    box = page.mediabox
    return PageBox(
        left=float(box.left),
        bottom=float(box.bottom),
        width=float(box.width),
        height=float(box.height),
    )


def inspect_pdf(data: bytes) -> dict:
    """Page geometry plus any AcroForm widgets, without modifying anything.

    `acroform_fields` carries each widget's page index and its /Rect in that
    page's own coordinates — enough for placements_from_acroform() to turn a
    fillable government form into a placed form for free.
    """
    reader = _reader(data)
    page_sizes: list[dict] = []
    fields: list[dict] = []
    rotated = False

    for index, page in enumerate(reader.pages):
        rotation = int(page.get("/Rotate") or 0) % 360
        box = _page_box(page)
        if rotation in (90, 270):
            rotated = True
            page_sizes.append({"width": box.height, "height": box.width})
        else:
            rotated = rotated or rotation != 0
            page_sizes.append({"width": box.width, "height": box.height})

        for annotation in page.get("/Annots") or []:
            try:
                obj = annotation.get_object()
            except Exception:  # noqa: BLE001 - a broken annot is not a broken form
                continue
            name = obj.get("/T")
            rect = obj.get("/Rect")
            if not name or not rect:
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in rect)
            except (TypeError, ValueError):
                continue
            fields.append(
                {
                    "name": str(name),
                    "type": str(obj.get("/FT") or ""),
                    "tooltip": str(obj.get("/TU") or ""),
                    "page": index,
                    "rect": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                }
            )

    return {
        "page_count": len(reader.pages),
        "page_sizes": page_sizes,
        "rotated": rotated,
        "acroform_fields": fields,
    }


#: Below this many characters, a PDF's own text layer is not worth classifying
#: from — it is a scan, and the real text has to come from OCR.
TEXT_LAYER_FLOOR = 40


def extract_text(data: bytes, *, max_pages: int = 5) -> str:
    """The PDF's embedded text layer, if it has one.

    Most government and office forms are born-digital and carry real text, so
    this answers "what is this document" instantly and for free. Scans return
    almost nothing, which is the signal to queue actual OCR — see
    leases/tasks.py. Only the first few pages are read: a form announces itself
    on page one, and a 60-page strata package should not be parsed to find that
    out.
    """
    try:
        reader = _reader(data)
    except FormRenderError:
        return ""
    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a page we cannot parse is not fatal
            continue
    return "\n".join(chunks).strip()


def has_text_layer(data: bytes) -> bool:
    return len(extract_text(data, max_pages=2)) >= TEXT_LAYER_FLOOR


def normalise_pdf(data: bytes) -> bytes:
    """Bake page rotation into the content so every consumer sees /Rotate 0.

    Returns the input unchanged when there is nothing to do, so an unrotated
    upload keeps its exact original bytes (and therefore its original checksum).
    """
    reader = _reader(data)
    if not any(int(page.get("/Rotate") or 0) % 360 for page in reader.pages):
        return data

    writer = PdfWriter()
    for page in reader.pages:
        if int(page.get("/Rotate") or 0) % 360:
            page.transfer_rotation_to_content()
        writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Placements from a fillable form
# ---------------------------------------------------------------------------

_DATE_HINTS = ("date", "dated", "day", "signed on")
_TIME_HINTS = ("time",)
_NAME_HINTS = ("name", "names", "first", "last", "middle")


def _slug(value: str, used: set[str]) -> str:
    cleaned = "".join(
        char if (char.isalnum() or char in "-_") else "_" for char in value.strip()
    ).strip("_")
    cleaned = (cleaned or "field").lower()[:100]
    candidate = cleaned
    suffix = 2
    while candidate in used:
        candidate = f"{cleaned}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _kind_for(field: dict):
    from .lease_forms import LeaseFormPlacement

    haystack = f"{field['name']} {field.get('tooltip', '')}".casefold()
    if field.get("type") == "/Sig":
        return LeaseFormPlacement.Kind.SIGNATURE
    if field.get("type") == "/Btn":
        return LeaseFormPlacement.Kind.CHECKBOX
    if any(hint in haystack for hint in _DATE_HINTS):
        return LeaseFormPlacement.Kind.DATE
    if any(hint in haystack for hint in _TIME_HINTS):
        return LeaseFormPlacement.Kind.TEXT
    if any(hint in haystack for hint in _NAME_HINTS):
        return LeaseFormPlacement.Kind.NAME
    return LeaseFormPlacement.Kind.TEXT


def placements_from_acroform(info: dict) -> list[dict]:
    """Turn inspect_pdf()'s widgets into placement dicts, in page fractions.

    Deliberately opinionated about kind (a /Sig widget is a signature, a field
    called "Date" is a date) and deliberately NOT opinionated about who signs:
    every box comes back assigned to TENANT/0 with no prefill. Guessing that
    "Last name s_2" is the tenant rather than the landlord is exactly the kind
    of inference that puts the wrong person's name on a legal form, so a human
    assigns roles. What this saves is the tedious part — 25 boxes traced by hand.
    """
    from .lease_forms import LeaseFormPlacement
    from .lease_forms import SignerRole

    placements: list[dict] = []
    used: set[str] = set()
    page_sizes = info.get("page_sizes") or []

    for order, field in enumerate(info.get("acroform_fields") or []):
        page = int(field["page"])
        try:
            size = page_sizes[page]
        except IndexError:
            continue
        page_width = float(size["width"])
        page_height = float(size["height"])
        if page_width <= 0 or page_height <= 0:
            continue

        x0, y0, x1, y1 = (float(v) for v in field["rect"])
        width = (x1 - x0) / page_width
        height = (y1 - y0) / page_height
        # PDF y grows up from the bottom; our fractions grow down from the top.
        x = x0 / page_width
        y = 1.0 - (y1 / page_height)
        if width <= 0 or height <= 0:
            continue
        # Some forms park a hidden field at ±32768 to hold scripts. It is not a
        # place anyone can sign.
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and width <= 1.0 and height <= 1.0):
            continue

        kind = _kind_for(field)
        placements.append(
            {
                "key": _slug(field["name"], used),
                "label": field.get("tooltip") or field["name"],
                "page": page,
                "x": round(x, 6),
                "y": round(y, 6),
                "width": round(width, 6),
                "height": round(height, 6),
                "kind": kind,
                "signer_role": SignerRole.TENANT,
                "signer_index": 0,
                "auto_source": "",
                # Only signatures are required by default. A date box we
                # detected from a widget has unknown meaning — it could be a
                # date signed, a vacate date, or a date of birth — and marking
                # every one of them mandatory would block sending on a field
                # nobody chose. The landlord marks what actually matters in the
                # placement editor; the seeded catalogue forms override this
                # explicitly because their fields ARE known.
                "required": kind == LeaseFormPlacement.Kind.SIGNATURE,
                "font_size": 10.0,
                "order": order,
            }
        )
    return placements


# ---------------------------------------------------------------------------
# Rasterising a page for the placement / signing UI
# ---------------------------------------------------------------------------


def render_page_png(data: bytes, page: int, dpi: int = DEFAULT_RASTER_DPI) -> bytes:
    """One page as a PNG, via Ghostscript.

    Ghostscript rather than a Python rasteriser because it is already installed
    in both images for OCRmyPDF (compose/*/django/Dockerfile), so page previews
    cost no new system package and no `pdfjs` on the frontend. At 72 dpi the
    output is exactly one pixel per PDF point, which makes the fraction maths
    trivial to verify by hand.
    """
    if page < 0:
        raise FormRenderError(_("Page numbers start at 1."))
    dpi = max(36, min(int(dpi or DEFAULT_RASTER_DPI), MAX_RASTER_DPI))

    with tempfile.TemporaryDirectory() as workdir:
        source = Path(workdir) / "in.pdf"
        target = Path(workdir) / "out.png"
        source.write_bytes(data)
        command = [
            "gs",
            "-dSAFER",
            "-dNOPAUSE",
            "-dBATCH",
            "-dQUIET",
            "-sDEVICE=png16m",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            f"-r{dpi}",
            f"-dFirstPage={page + 1}",
            f"-dLastPage={page + 1}",
            f"-sOutputFile={target}",
            str(source),
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                capture_output=True,
                timeout=GHOSTSCRIPT_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FormRenderError(
                _("Page previews need Ghostscript, which is not installed.")
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FormRenderError(_("That page took too long to render.")) from exc

        if not target.exists():
            logger.error(
                "ghostscript failed to render page %s: %s",
                page,
                (result.stderr or b"")[:400],
            )
            raise FormRenderError(_("That page could not be rendered."))
        return target.read_bytes()


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------


def _box_to_points(placement: dict, box: PageBox) -> tuple[float, float, float, float]:
    """(left, bottom, width, height) in PDF points for one placement.

    The single crossing point between top-left fractions and PDF's bottom-left
    points. Everything else in this module works in one space or the other.
    """
    width = float(placement["width"]) * box.width
    height = float(placement["height"]) * box.height
    left = box.left + float(placement["x"]) * box.width
    top_from_bottom = box.bottom + (1.0 - float(placement["y"])) * box.height
    return left, top_from_bottom - height, width, height


def _fit_text(text: str, font: str, size: float, max_width: float) -> float:
    """Largest size ≤ `size` at which `text` fits, floored at 5pt."""
    while size > 5.0 and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _draw_text(pdf, text, left, bottom, width, height, font, size):
    size = min(float(size or 10.0), max(height * 0.8, 5.0))
    size = _fit_text(text, font, size, max(width, 1.0))
    pdf.setFont(font, size)
    # Sit the baseline a little above the box's floor: form boxes are drawn
    # around the ruled line, and text on the line reads as filled in.
    pdf.drawString(left + 1.5, bottom + max((height - size) / 2.0, 1.0), text)


def _draw_image(pdf, png: bytes, left, bottom, width, height):
    image = ImageReader(io.BytesIO(png))
    source_width, source_height = image.getSize()
    if not source_width or not source_height:
        return
    scale = min(width / source_width, height / source_height)
    draw_width = source_width * scale
    draw_height = source_height * scale
    pdf.drawImage(
        image,
        left + (width - draw_width) / 2.0,
        bottom + (height - draw_height) / 2.0,
        width=draw_width,
        height=draw_height,
        mask="auto",
        preserveAspectRatio=True,
    )


def _overlay_for_page(box: PageBox, rows: list[tuple[dict, str, bytes | None]]) -> bytes:
    """A single-page PDF holding just the stamps for one page."""
    from .lease_forms import LeaseFormPlacement

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(box.width, box.height))
    script = signature_font()

    for placement, value, image in rows:
        # The overlay canvas starts at (0, 0); the MediaBox offset is applied
        # once, at merge time, so it is not baked into every draw call.
        left, bottom, width, height = _box_to_points(placement, box)
        left -= box.left
        bottom -= box.bottom
        kind = placement.get("kind")

        if image:
            _draw_image(pdf, image, left, bottom, width, height)
            continue
        if not value:
            continue
        if kind in (
            LeaseFormPlacement.Kind.SIGNATURE,
            LeaseFormPlacement.Kind.INITIALS,
        ):
            _draw_text(pdf, value, left, bottom, width, height, script, height * 0.7)
        elif kind == LeaseFormPlacement.Kind.CHECKBOX:
            _draw_text(
                pdf, "X", left, bottom, width, height, BODY_FONT, height * 0.7
            )
        else:
            _draw_text(
                pdf,
                value,
                left,
                bottom,
                width,
                height,
                BODY_FONT,
                placement.get("font_size") or 10.0,
            )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def stamp(
    data: bytes,
    placements: list[dict],
    values: dict[str, str],
    signature_images: dict[str, bytes] | None = None,
) -> bytes:
    """Burn values and signatures onto a blank form and flatten the result.

    Flattening is not cosmetic. A fillable PDF that still carries its /AcroForm
    after being signed can be reopened and re-typed in any viewer, and the file
    will happily show different text than the one everybody signed. Dropping
    /AcroForm and every page's /Annots is what makes the executed document a
    picture of an agreement rather than an editable draft of one.
    """
    signature_images = signature_images or {}
    reader = _reader(data)
    writer = PdfWriter()

    by_page: dict[int, list[tuple[dict, str, bytes | None]]] = {}
    for placement in placements:
        key = placement.get("key")
        image = signature_images.get(key)
        value = str(values.get(key) or "")
        if not image and not value:
            continue
        by_page.setdefault(int(placement.get("page") or 0), []).append(
            (placement, value, image)
        )

    for index, page in enumerate(reader.pages):
        rows = by_page.get(index)
        if rows:
            box = _page_box(page)
            overlay = PdfReader(io.BytesIO(_overlay_for_page(box, rows))).pages[0]
            page.merge_transformed_page(
                overlay, Transformation().translate(box.left, box.bottom)
            )
        # Whatever the page carried — widgets, links, a half-filled form layer —
        # does not survive into the executed document.
        if "/Annots" in page:
            del page["/Annots"]
        writer.add_page(page)

    root = writer._root_object  # noqa: SLF001 - pypdf exposes no public remover
    if "/AcroForm" in root:
        del root["/AcroForm"]

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
