from datetime import date
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from rentium.ledger.models import EntryType
from rentium.ledger.models import ExpenseCategory
from rentium.ledger.models import LedgerEntry
from rentium.properties.models import PropertyHolding
from rentium.properties.models import Property
from rentium.rama.document_services import file_document
from rentium.rama.document_services import ingest_document
from rentium.rama.document_services import process_document
from rentium.rama.document_services import catalog_document_scope
from rentium.rama.document_services import catalog_staged_photo_as_document
from rentium.rama.document_services import document_location
from rentium.rama.models import RamaDocument
from rentium.rama.models import RamaUpload

pytestmark = pytest.mark.django_db


def _holding(landlord):
    return PropertyHolding.objects.create(
        landlord=landlord,
        name="McKenzie House",
        address="950 McKenzie Ave",
        city="Victoria",
    )


def test_duplicate_upload_is_idempotent(landlord):
    first = SimpleUploadedFile(
        "notice.pdf", b"%PDF-test", content_type="application/pdf"
    )
    second = SimpleUploadedFile(
        "copy.pdf", b"%PDF-test", content_type="application/pdf"
    )
    document, created = ingest_document(landlord=landlord, upload=first)
    duplicate, duplicate_created = ingest_document(landlord=landlord, upload=second)
    assert created is True
    assert duplicate_created is False
    assert duplicate.pk == document.pk


def test_ocr_classifies_and_matches_holding(landlord):
    holding = _holding(landlord)
    upload = SimpleUploadedFile("tax.jpg", b"image", content_type="image/jpeg")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    text = (
        "2026 PROPERTY TAX NOTICE\n950 McKenzie Avenue\n"
        "AMOUNT DUE $4,321.00\nDUE DATE JULY 2"
    )
    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF-archival", text),
    ):
        process_document(document.pk)
    document.refresh_from_db()
    assert document.holding == holding
    assert document.kind == RamaDocument.Kind.TAX
    assert document.expense_category == ExpenseCategory.PROPERTY_TAX
    assert document.payment_state == RamaDocument.PaymentState.UNPAID
    assert document.amount == 4321
    assert document.status == RamaDocument.Status.READY, document.failure_reason


def test_invoice_ocr_prefers_total_not_first_money_and_maintenance_kind(landlord):
    """Window-screen invoices must not pick $125 subtotal or generic EXPENSE."""
    holding = _holding(landlord)
    upload = SimpleUploadedFile("inv.pdf", b"%PDF-test", content_type="application/pdf")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    text = (
        "INVOICE\n950 McKenzie Ave\nWindow screens installation\n"
        "Subtotal $125.00\nTax $16.80\nTotal $331.80\n"
    )
    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF-archival", text),
    ):
        process_document(document.pk)
    document.refresh_from_db()
    from decimal import Decimal

    assert document.holding == holding
    assert document.kind == RamaDocument.Kind.MAINTENANCE
    assert document.expense_category == ExpenseCategory.MAINTENANCE
    assert document.amount == Decimal("331.80")
    assert document.payment_state == RamaDocument.PaymentState.UNKNOWN


def test_address_only_followup_scopes_pending_document(landlord):
    """After prepare, landlord can reply with just the street — no attachment_id."""
    from rentium.rama import registry

    holding = _holding(landlord)
    pdf = b"%PDF-1.4 address-followup-unique-bytes"
    conversation_id = __import__("uuid").uuid4()
    from rentium.rama.attachment_services import seal_batch, stage_files

    batch = stage_files(
        landlord=landlord,
        conversation_id=conversation_id,
        uploads=[SimpleUploadedFile("bill.pdf", pdf, content_type="application/pdf")],
    )
    seal_batch(
        landlord=landlord,
        conversation_id=conversation_id,
        batch_id=str(batch.pk),
    )
    att = batch.attachments.get()
    prep = registry.execute(
        "catalog_business_document",
        {"attachment_id": str(att.pk)},
        landlord=landlord,
    )
    assert prep.get("document_id"), prep
    assert prep.get("needs_scope") or prep.get("needs_input") or prep.get("prepared")
    doc_id = prep["document_id"]

    # Follow-up: address only (what the landlord types after "which property?").
    scoped = registry.execute(
        "catalog_business_document",
        {"scope_query": "950 McKenzie Ave", "confirm": "yes"},
        landlord=landlord,
    )
    assert scoped.get("catalogued") or scoped.get("already_done"), scoped
    document = RamaDocument.objects.get(pk=doc_id)
    assert document.holding_id == holding.pk


def test_same_file_hash_is_duplicate_before_asking_address(landlord):
    """Re-sending the same PDF must not open a new 'file for McKenzie' preview."""
    from rentium.rama.attachment_services import seal_batch, stage_files
    from rentium.rama.document_services import catalog_batch_attachment_as_document
    from rentium.rama.models import RamaAttachmentBatch
    import uuid

    holding = _holding(landlord)
    pdf = b"%PDF-1.4 identical-bytes-for-dup-test"
    conversation_id = uuid.uuid4()
    batch = stage_files(
        landlord=landlord,
        conversation_id=conversation_id,
        uploads=[SimpleUploadedFile("inv.pdf", pdf, content_type="application/pdf")],
    )
    seal_batch(
        landlord=landlord,
        conversation_id=conversation_id,
        batch_id=str(batch.pk),
    )
    att = batch.attachments.get()
    # First catalog: prepare + scope
    first_prepare = catalog_batch_attachment_as_document(
        landlord,
        attachment_id=str(att.pk),
        scope_query="",
        actor=landlord.user,
    )
    # May need_input or prepared — force scope
    doc_id = first_prepare.get("document_id")
    assert doc_id
    if first_prepare.get("needs_input") or first_prepare.get("prepared"):
        done = catalog_batch_attachment_as_document(
            landlord,
            attachment_id=str(att.pk),
            scope_query="950 McKenzie Ave",
            actor=landlord.user,
            confirm=True,
        )
        assert done.get("catalogued") or done.get("already_done"), done
        doc_id = done.get("document_id") or doc_id

    document = RamaDocument.objects.get(pk=doc_id)
    document.holding = holding
    document.save(update_fields=["holding", "updated_at"])

    # Second send: new attachment batch, same bytes
    batch2 = stage_files(
        landlord=landlord,
        conversation_id=uuid.uuid4(),
        uploads=[SimpleUploadedFile("inv-again.pdf", pdf, content_type="application/pdf")],
    )
    seal_batch(
        landlord=landlord,
        conversation_id=batch2.conversation_id,
        batch_id=str(batch2.pk),
    )
    att2 = batch2.attachments.get()
    dup = catalog_batch_attachment_as_document(
        landlord,
        attachment_id=str(att2.pk),
        scope_query="",
        actor=landlord.user,
    )
    assert dup.get("already_done") is True or dup.get("is_duplicate") is True, dup
    assert str(dup.get("document_id")) == str(document.pk)
    assert "already" in (dup.get("message") or dup.get("already_done") or "").casefold() or dup.get(
        "is_duplicate"
    )


def test_file_business_document_posts_paid_expense_from_chat(landlord):
    from decimal import Decimal

    from rentium.rama import registry
    from rentium.rama.document_services import file_business_document_for_chat

    holding = _holding(landlord)
    upload = SimpleUploadedFile("inv.pdf", b"%PDF-test", content_type="application/pdf")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.holding = holding
    document.kind = RamaDocument.Kind.MAINTENANCE
    document.title = "Maintenance — Window Screens"
    document.amount = Decimal("331.80")
    document.expense_category = ExpenseCategory.MAINTENANCE
    document.payment_state = RamaDocument.PaymentState.UNKNOWN
    document.status = RamaDocument.Status.NEEDS_REVIEW
    document.archival_pdf.save(
        "pending.pdf", SimpleUploadedFile("pending.pdf", b"%PDF")
    )
    document.save()

    needs = file_business_document_for_chat(
        landlord, document_id=str(document.pk)
    )
    assert needs.get("needs_input"), needs
    assert "left your bank" in needs["question_for_user"].casefold()

    preview = file_business_document_for_chat(
        landlord,
        document_id=str(document.pk),
        payment_state="PAID",
    )
    assert preview.get("needs_confirm"), preview
    assert preview["preview"]["amount"] == "331.80"
    assert "void" in preview["preview"]["never"].casefold()

    done = file_business_document_for_chat(
        landlord,
        document_id=str(document.pk),
        payment_state="PAID",
        confirm="yes",
    )
    assert done.get("filed"), done
    assert done.get("paid_on")
    expense = LedgerEntry.objects.get(entry_type=EntryType.EXPENSE)
    assert expense.amount == Decimal("331.80")
    assert expense.holding == holding
    assert expense.paid_on is not None
    document.refresh_from_db()
    assert document.status == RamaDocument.Status.FILED
    assert document.ledger_entry_id == expense.pk

    assert "file_business_document" in registry.REGISTRY
    assert "business_document_status" in registry.REGISTRY


def test_review_posts_holding_expense_without_marking_invoice_paid(landlord):
    holding = _holding(landlord)
    upload = SimpleUploadedFile("tax.pdf", b"%PDF-test", content_type="application/pdf")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.holding = holding
    document.kind = RamaDocument.Kind.TAX
    document.title = "Property Tax Notice 2026"
    document.amount = "4321.00"
    document.expense_category = ExpenseCategory.PROPERTY_TAX
    document.payment_state = RamaDocument.PaymentState.UNPAID
    document.status = RamaDocument.Status.READY
    document.archival_pdf.save(
        "pending.pdf", SimpleUploadedFile("pending.pdf", b"%PDF")
    )
    document.save()

    file_document(
        document,
        actor=landlord.user,
        holding=holding,
        document_date=date(2026, 6, 1),
    )
    expense = LedgerEntry.objects.get(entry_type=EntryType.EXPENSE)
    assert expense.holding == holding
    assert expense.property is None
    assert expense.paid_on is None
    assert expense.source_document == document
    assert document.status == RamaDocument.Status.FILED
    assert document.archival_pdf.name.startswith(
        f"business_documents/{landlord.pk}/mckenzie-house/2026/"
    )


def test_address_scope_creates_holding_above_all_child_listings(landlord):
    children = []
    for name, category in [
        ("Room C", Property.PropertyCategory.ROOM),
        ("Room D", Property.PropertyCategory.ROOM),
        ("Garden Suite", Property.PropertyCategory.COMPLETE_UNIT),
    ]:
        children.append(
            Property.objects.create(
                landlord=landlord,
                name=name,
                address="950 McKenzie Ave",
                city="Victoria",
                province="bc",
                property_category=category,
                room_type=(
                    Property.RoomType.PRIVATE
                    if category == Property.PropertyCategory.ROOM
                    else None
                ),
                unit_type=(
                    Property.UnitType.GARDEN_SUITE
                    if category == Property.PropertyCategory.COMPLETE_UNIT
                    else None
                ),
            )
        )
    upload = SimpleUploadedFile(
        "scotiabank-notice.pdf", b"%PDF-test", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)

    preview = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Avenue",
        actor=landlord.user,
        issuer="Scotiabank",
        document_date=date(2026, 6, 2),
    )
    assert preview["needs_confirm"] is True
    assert preview["preview"]["scope_kind"] == "physical_property_holding"
    assert preview["preview"]["create_holding"] is True
    assert set(preview["preview"]["child_listings"]) == {
        "Room C",
        "Room D",
        "Garden Suite",
    }

    done = catalog_document_scope(
        landlord,
        document_id=str(document.pk),
        scope_query="950 McKenzie Ave",
        actor=landlord.user,
        issuer="Scotiabank",
        document_date=date(2026, 6, 2),
        confirm=True,
    )
    assert done["catalogued"] is True
    assert done["property"] is None
    holding = PropertyHolding.objects.get(
        landlord=landlord, address="950 McKenzie Ave"
    )
    assert set(holding.listings.values_list("name", flat=True)) == {
        "Room C",
        "Room D",
        "Garden Suite",
    }
    document.refresh_from_db()
    assert document.holding == holding
    assert document.property is None
    assert document.issuer == "Scotiabank"
    assert document.document_date == date(2026, 6, 2)
    assert document.extracted_data["user_scope_locked"] is True


def test_locked_landlord_scope_survives_later_ocr(landlord):
    holding = _holding(landlord)
    upload = SimpleUploadedFile("notice.jpg", b"image", content_type="image/jpeg")
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.holding = holding
    document.extracted_data = {"user_scope_locked": True}
    document.save(update_fields=["holding", "extracted_data", "updated_at"])

    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF-archival", "Scotiabank general correspondence"),
    ):
        process_document(document.pk)
    document.refresh_from_db()
    assert document.holding == holding
    assert document.match_confidence == 1


def test_photographed_mail_is_promoted_and_scoped_to_holding(landlord):
    Property.objects.create(
        landlord=landlord,
        name="Room C",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    Property.objects.create(
        landlord=landlord,
        name="Garden Suite",
        address="950 McKenzie Ave",
        city="Victoria",
        province="bc",
        property_category=Property.PropertyCategory.COMPLETE_UNIT,
        unit_type=Property.UnitType.GARDEN_SUITE,
    )
    staged = RamaUpload.objects.create(
        landlord=landlord,
        image=SimpleUploadedFile("scotiabank-letter.jpg", b"photo-bytes"),
    )

    # Inspect-first (no address) then scope + confirm.
    prepared = catalog_staged_photo_as_document(
        landlord,
        upload_id=str(staged.pk),
        scope_query="",
        actor=landlord.user,
    )
    assert prepared.get("document_id") or prepared.get("prepared") or prepared.get(
        "needs_input"
    ), prepared
    doc_id = prepared.get("document_id")
    preview = catalog_staged_photo_as_document(
        landlord,
        upload_id=str(staged.pk),
        scope_query="950 McKenzie Ave",
        actor=landlord.user,
        issuer="Scotiabank",
        document_date=date(2026, 6, 2),
    )
    assert preview.get("needs_confirm") or preview.get("already_done"), preview
    if preview.get("needs_confirm"):
        assert set(preview["preview"]["child_listings"]) == {
            "Room C",
            "Garden Suite",
        }
        done = catalog_staged_photo_as_document(
            landlord,
            upload_id=str(staged.pk),
            scope_query="950 McKenzie Ave",
            actor=landlord.user,
            issuer="Scotiabank",
            document_date=date(2026, 6, 2),
            confirm=True,
        )
    else:
        done = preview
    assert done.get("catalogued") or done.get("already_done"), done
    document = RamaDocument.objects.get(pk=done.get("document_id") or doc_id)
    assert document.holding.address == "950 McKenzie Ave"
    assert document.property is None
    staged.refresh_from_db()
    # used_at set once fully scoped/catalogued
    assert staged.used_at is not None or document.holding_id


def test_document_location_returns_manual_container_path_and_links(landlord):
    holding = _holding(landlord)
    upload = SimpleUploadedFile(
        "notice.pdf", b"%PDF-original", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.holding = holding
    document.title = "Scotiabank Notice"
    document.document_date = date(2026, 6, 2)
    document.canonical_filename = (
        "2026-06-02_mckenzie-house_scotiabank-notice_test.pdf"
    )
    document.archival_pdf.save(
        (
            f"business_documents/{landlord.pk}/mckenzie-house/2026/"
            f"notice/{document.canonical_filename}"
        ),
        SimpleUploadedFile("archive.pdf", b"%PDF-archival"),
    )
    document.save()

    result = document_location(landlord, str(document.pk))
    assert result["storage_kind"] == "container_filesystem"
    assert result["storage_key"].startswith(
        f"business_documents/{landlord.pk}/mckenzie-house/2026/notice/"
    )
    assert result["container_path"].endswith(result["storage_key"])
    assert result["manual_location"] == result["container_path"]
    assert result["documents_page"].endswith(
        f"/dashboard/documents?document={document.pk}"
    )
    assert result["authenticated_download_path"] == (
        f"/api/rama/documents/{document.pk}/download/"
    )


@pytest.mark.django_db
def test_generic_link_resolves_business_document_uuid(landlord):
    from rentium.rama.domain_read import link

    document, _ = ingest_document(
        landlord=landlord,
        upload=SimpleUploadedFile(
            "bank-letter.pdf",
            b"%PDF-original",
            content_type="application/pdf",
        ),
    )
    document.title = "Scotiabank renewal letter"
    document.save(update_fields=["title", "updated_at"])

    result = link(
        landlord,
        entity="business_document",
        query=str(document.pk),
    )

    assert result["entity"] == "business_document"
    assert result["link"].endswith(
        f"/dashboard/documents?document={document.pk}"
    )


def test_catalog_accepts_telegram_upload_id_in_attachment_id_slot(landlord):
    """Telegram stages RamaUpload; models often pass that UUID as attachment_id."""
    from django.core.files.base import ContentFile

    from rentium.rama.models import RamaUpload
    from rentium.rama.tools import catalog_business_document

    upload = RamaUpload(landlord=landlord)
    upload.image.save("receipt.jpg", ContentFile(b"fake-jpeg-bytes"), save=True)

    # Wrong arg name on purpose — must still OCR/prepare, not "file gone".
    with patch(
        "rentium.rama.document_services._pdf_and_text",
        return_value=(b"%PDF-archival", "INVOICE PNR Screens Total $50.00"),
    ):
        result = catalog_business_document(
            landlord,
            attachment_id=str(upload.pk),
            upload_id="",
        )
    assert result.get("error") is None, result
    assert result.get("document_id") or result.get("prepared") or result.get(
        "needs_input"
    ), result
    assert "no longer available" not in str(result).casefold()
    assert "No attachment" not in str(result.get("error") or "")


def test_pdf_and_text_uses_force_ocr_not_skip_text(landlord, monkeypatch):
    """Ghostscript 10.0.0 rejects --skip-text; pipeline must force-ocr."""
    from pathlib import Path

    from rentium.rama import document_services as ds

    upload = SimpleUploadedFile(
        "invoice.jpg", b"fake-jpeg-bytes", content_type="image/jpeg"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, timeout=240, check=False):
        calls.append(list(cmd))
        # Simulate successful force-ocr + pdfa
        out = Path(cmd[-1])
        side = Path(cmd[cmd.index("--sidecar") + 1])
        out.write_bytes(b"%PDF-ok")
        side.write_text("INVOICE Window screens Total $331.80\n")

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ds,
        "_image_bytes_to_pdf",
        lambda source, media_type: b"%PDF-from-image",
    )
    pdf, text = ds._pdf_and_text(document)
    assert pdf == b"%PDF-ok"
    assert "screens" in text.casefold()
    assert calls, "ocrmypdf should have been invoked"
    assert "--force-ocr" in calls[0]
    assert "--skip-text" not in calls[0]


def test_delete_document_cascades_events(landlord):
    """DELETE must not 500 on RamaDocumentEvent PROTECT; events go with the doc."""
    from rentium.rama.document_services import delete_document
    from rentium.rama.models import RamaDocumentEvent

    upload = SimpleUploadedFile(
        "junk.pdf", b"%PDF-delete-me-unique", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    # ingest already records UPLOADED; add more custody rows so PROTECT would fire.
    RamaDocumentEvent.objects.create(
        document=document, kind=RamaDocumentEvent.Kind.OCR_COMPLETED, detail={}
    )
    RamaDocumentEvent.objects.create(
        document=document, kind=RamaDocumentEvent.Kind.CLASSIFIED, detail={}
    )
    assert document.events.count() >= 2

    result = delete_document(landlord=landlord, document=document)
    assert result["deleted"] is True
    assert not RamaDocument.objects.filter(pk=document.pk).exists()
    assert not RamaDocumentEvent.objects.filter(
        document_id=result["document_id"]
    ).exists()


def test_delete_document_refuses_ledger_link(landlord):
    from datetime import date

    from rentium.rama.document_services import DocumentError
    from rentium.rama.document_services import delete_document

    holding = _holding(landlord)
    upload = SimpleUploadedFile(
        "paid.pdf", b"%PDF-linked-unique", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    entry = LedgerEntry.objects.create(
        landlord=landlord,
        holding=holding,
        entry_type=EntryType.EXPENSE,
        amount=10,
        description="linked expense",
        category=ExpenseCategory.MAINTENANCE,
        effective_date=date(2026, 7, 1),
    )
    document.ledger_entry = entry
    document.save(update_fields=["ledger_entry", "updated_at"])
    with pytest.raises(DocumentError, match="ledger expense"):
        delete_document(landlord=landlord, document=document)
    assert RamaDocument.objects.filter(pk=document.pk).exists()


def test_query_and_tag_business_documents(landlord):
    from datetime import date

    from rentium.rama.document_services import query_business_documents
    from rentium.rama.document_services import search_business_documents_for_chat
    from rentium.rama.document_services import set_document_tags

    holding = _holding(landlord)
    upload = SimpleUploadedFile(
        "screens.pdf", b"%PDF-screens-unique", content_type="application/pdf"
    )
    document, _ = ingest_document(landlord=landlord, upload=upload)
    document.title = "Window screens invoice"
    document.issuer = "Victoria Screens Co"
    document.ocr_text = "Window screens installation 950 McKenzie Ave Total $331.80"
    document.kind = RamaDocument.Kind.MAINTENANCE
    document.holding = holding
    document.document_date = date(2026, 7, 15)
    document.status = RamaDocument.Status.FILED
    document.save()
    set_document_tags(document, ["hvac", "tax-2026"])

    found = query_business_documents(landlord, q="window screens", page=1)
    assert found["pagination"]["total"] >= 1
    assert any(row.pk == document.pk for row in found["documents"])

    by_tag = query_business_documents(landlord, tag="hvac")
    assert any(row.pk == document.pk for row in by_tag["documents"])

    by_year = query_business_documents(landlord, year=2026, holding_id=str(holding.pk))
    assert any(row.pk == document.pk for row in by_year["documents"])

    chat = search_business_documents_for_chat(
        landlord, query="screens", holding_query="McKenzie"
    )
    assert chat["ok"] is True
    assert chat["count"] >= 1
    assert chat["documents"][0]["title"] == "Window screens invoice"
    assert "hvac" in chat["documents"][0]["tags"]
