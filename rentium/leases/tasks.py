"""
Async work for lease form packs.

Only one job so far, and it exists because of a specific failure: a landlord
photographs or scans a form, uploads it, and the system has nothing to say about
it — no text, no suggestion, so RAMA can only ask "what is this?" with no
reading of its own. Running the same OCRmyPDF pipeline the document inbox uses
turns that into "this looks like a mutual agreement to end a tenancy — file it
that way?", which is a question a landlord can answer in one word.

Queued, not inline: OCR takes seconds to minutes and an upload has to return
straight away.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from celery import shared_task

logger = logging.getLogger(__name__)

OCR_TIMEOUT_SECONDS = 240


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def ocr_lease_form_template(self, template_id: str) -> dict:
    """Read a scanned blank form, then re-run the purpose classifier.

    Text only — the original file is never replaced. A blank form is what gets
    stamped and signed, so substituting an OCRmyPDF-rewritten copy would change
    the bytes underneath every placement already traced onto it.
    """
    from .form_services import classify_template
    from .lease_forms import LeaseFormTemplate

    template = LeaseFormTemplate.objects.filter(pk=template_id).first()
    if template is None or not template.file:
        return {"template": template_id, "status": "missing"}

    template.file.open("rb")
    try:
        data = template.file.read()
    finally:
        template.file.close()

    try:
        text = _ocr_pdf_text(data)
    except Exception:  # noqa: BLE001
        logger.exception("OCR failed for lease form template %s", template_id)
        # A form without text is still usable — the landlord just has to say
        # what it is. Not worth retrying past the celery default.
        return {"template": template_id, "status": "failed"}

    template.ocr_text = text
    template.save(update_fields=["ocr_text", "updated_at"])
    classify_template(template)

    return {
        "template": template_id,
        "status": "ok",
        "characters": len(text),
        "suggested_stage": template.suggested_stage,
    }


def _ocr_pdf_text(data: bytes) -> str:
    """Text from a scanned PDF, via the same engine the document inbox uses."""
    from rentium.rama.document_services import _run_ocrmypdf

    with tempfile.TemporaryDirectory(prefix="rentium-form-ocr-") as tmp:
        source = Path(tmp) / "in.pdf"
        target = Path(tmp) / "out.pdf"
        sidecar = Path(tmp) / "ocr.txt"
        source.write_bytes(data)

        # Same ordering rationale as document_services._pdf_and_text: Debian
        # bookworm's Ghostscript rejects the --skip-text/PDF-A combination, so
        # force-ocr is tried first and plain PDF output is the fallback.
        for force_ocr, output_type in ((True, "pdf"), (False, "pdf")):
            for path in (target, sidecar):
                if path.exists():
                    path.unlink()
            try:
                completed = _run_ocrmypdf(
                    source, target, sidecar, force_ocr=force_ocr, output_type=output_type
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"OCR engine unavailable: {exc}") from exc
            if completed.returncode in {0, 6}:
                break
        else:
            raise RuntimeError("OCR produced no readable text")

        return sidecar.read_text(errors="replace").strip() if sidecar.exists() else ""
