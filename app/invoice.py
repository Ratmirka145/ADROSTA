"""Immutable invoice snapshots and offline PDF rendering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Literal

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.enums import TA_CENTER, TA_RIGHT  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import (  # type: ignore[import-untyped]
    ParagraphStyle,
    getSampleStyleSheet,
)
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


InvoiceStatus = Literal["pending", "generated", "failed"]


@dataclass(frozen=True, slots=True)
class InvoiceItemDraft:
    line_number: int
    line_type: Literal["product", "delivery"]
    sku: str | None
    name: str
    quantity: int
    unit: str
    unit_price_kopecks: int
    line_amount_kopecks: int


@dataclass(frozen=True, slots=True)
class InvoiceDraft:
    template_version: str
    seller_legal_name: str
    seller_inn: str
    seller_kpp: str
    seller_legal_address: str
    seller_bank_name: str
    seller_bik: str
    seller_checking_account: str
    seller_correspondent_account: str
    seller_phone: str | None
    seller_email: str | None
    buyer_name: str
    buyer_inn: str | None
    buyer_kpp: str | None
    buyer_legal_address: str | None
    products_amount_kopecks: int
    delivery_amount_kopecks: int
    grand_total_kopecks: int
    tax_text: str
    payment_purpose_template: str
    items: tuple[InvoiceItemDraft, ...]

    def materialize(
        self,
        *,
        invoice_id: str,
        order_id: str,
        invoice_number: str,
        issued_at: int,
    ) -> "InvoiceSnapshot":
        invoice_date = datetime.fromtimestamp(issued_at, UTC).strftime("%d.%m.%Y")
        try:
            payment_purpose = self.payment_purpose_template.format(
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                order_id=order_id,
            )
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError("invalid invoice payment purpose template") from exc
        return InvoiceSnapshot(
            id=invoice_id,
            order_id=order_id,
            invoice_number=invoice_number,
            issued_at=issued_at,
            status="pending",
            template_version=self.template_version,
            seller_legal_name=self.seller_legal_name,
            seller_inn=self.seller_inn,
            seller_kpp=self.seller_kpp,
            seller_legal_address=self.seller_legal_address,
            seller_bank_name=self.seller_bank_name,
            seller_bik=self.seller_bik,
            seller_checking_account=self.seller_checking_account,
            seller_correspondent_account=self.seller_correspondent_account,
            seller_phone=self.seller_phone,
            seller_email=self.seller_email,
            buyer_name=self.buyer_name,
            buyer_inn=self.buyer_inn,
            buyer_kpp=self.buyer_kpp,
            buyer_legal_address=self.buyer_legal_address,
            products_amount_kopecks=self.products_amount_kopecks,
            delivery_amount_kopecks=self.delivery_amount_kopecks,
            grand_total_kopecks=self.grand_total_kopecks,
            tax_text=self.tax_text,
            payment_purpose=payment_purpose,
            pdf_bytes=None,
            pdf_sha256=None,
            generated_at=None,
            created_at=issued_at,
            updated_at=issued_at,
            items=self.items,
        )


@dataclass(frozen=True, slots=True)
class InvoiceSnapshot:
    id: str
    order_id: str
    invoice_number: str
    issued_at: int
    status: InvoiceStatus
    template_version: str
    seller_legal_name: str
    seller_inn: str
    seller_kpp: str
    seller_legal_address: str
    seller_bank_name: str
    seller_bik: str
    seller_checking_account: str
    seller_correspondent_account: str
    seller_phone: str | None
    seller_email: str | None
    buyer_name: str
    buyer_inn: str | None
    buyer_kpp: str | None
    buyer_legal_address: str | None
    products_amount_kopecks: int
    delivery_amount_kopecks: int
    grand_total_kopecks: int
    tax_text: str
    payment_purpose: str
    pdf_bytes: bytes | None
    pdf_sha256: str | None
    generated_at: int | None
    created_at: int
    updated_at: int
    items: tuple[InvoiceItemDraft, ...]

    def generated(self, *, pdf_bytes: bytes, pdf_sha256: str, timestamp: int) -> "InvoiceSnapshot":
        return replace(
            self,
            status="generated",
            pdf_bytes=pdf_bytes,
            pdf_sha256=pdf_sha256,
            generated_at=timestamp,
            updated_at=timestamp,
        )


def format_money_kopecks(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("money must be non-negative integer kopecks")
    rubles, kopecks = divmod(value, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


class InvoicePdfRenderer:
    """Render an invoice without network, browser, remote assets or float money."""

    def __init__(self, font_path: str) -> None:
        path = Path(font_path)
        if not path.is_file():
            raise ValueError("invoice font file is unavailable")
        self.font_path = str(path)
        self.font_name = "AdrostaInvoiceSans"
        self.bold_font_name = "AdrostaInvoiceSansBold"
        pdfmetrics.registerFont(TTFont(self.font_name, self.font_path))
        bold_path = path.with_name("DejaVuSans-Bold.ttf")
        pdfmetrics.registerFont(
            TTFont(
                self.bold_font_name,
                str(bold_path if bold_path.is_file() else path),
            )
        )

    def render(self, invoice: InvoiceSnapshot) -> bytes:
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
            title=f"Счёт на оплату № {invoice.invoice_number}",
            author="ADROSTA",
        )
        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            "InvoiceNormal",
            parent=styles["Normal"],
            fontName=self.font_name,
            fontSize=9,
            leading=12,
            wordWrap="CJK",
        )
        small = ParagraphStyle(
            "InvoiceSmall", parent=normal, fontSize=8, leading=10
        )
        title = ParagraphStyle(
            "InvoiceTitle",
            parent=normal,
            fontName=self.bold_font_name,
            fontSize=15,
            leading=19,
            alignment=TA_CENTER,
            spaceAfter=8 * mm,
        )
        right = ParagraphStyle(
            "InvoiceRight",
            parent=normal,
            fontName=self.bold_font_name,
            alignment=TA_RIGHT,
        )

        issued_date = datetime.fromtimestamp(invoice.issued_at, UTC).strftime("%d.%m.%Y")
        story = [
            Paragraph(
                f"Счёт на оплату № {escape(invoice.invoice_number)}<br/>от {issued_date}",
                title,
            ),
            Paragraph("<b>Поставщик</b>", normal),
            Paragraph(escape(invoice.seller_legal_name), normal),
            Paragraph(
                f"ИНН {escape(invoice.seller_inn)}, КПП {escape(invoice.seller_kpp)}",
                normal,
            ),
            Paragraph(escape(invoice.seller_legal_address), normal),
        ]
        seller_contacts = []
        if invoice.seller_phone:
            seller_contacts.append(f"тел. {escape(invoice.seller_phone)}")
        if invoice.seller_email:
            seller_contacts.append(f"email {escape(invoice.seller_email)}")
        if seller_contacts:
            story.append(Paragraph("; ".join(seller_contacts), normal))
        story.extend([
            Paragraph(
                f"Банк: {escape(invoice.seller_bank_name)}; БИК {escape(invoice.seller_bik)}; "
                f"р/с {escape(invoice.seller_checking_account)}; "
                f"к/с {escape(invoice.seller_correspondent_account)}",
                small,
            ),
            Spacer(1, 4 * mm),
            Paragraph("<b>Покупатель</b>", normal),
            Paragraph(escape(invoice.buyer_name), normal),
        ])
        buyer_details = []
        if invoice.buyer_inn:
            buyer_details.append(f"ИНН {escape(invoice.buyer_inn)}")
        if invoice.buyer_kpp:
            buyer_details.append(f"КПП {escape(invoice.buyer_kpp)}")
        if buyer_details:
            story.append(Paragraph(", ".join(buyer_details), normal))
        if invoice.buyer_legal_address:
            story.append(Paragraph(escape(invoice.buyer_legal_address), normal))
        story.append(Spacer(1, 5 * mm))

        data = [[
            Paragraph("№", small),
            Paragraph("Наименование", small),
            Paragraph("Кол-во", small),
            Paragraph("Ед.", small),
            Paragraph("Цена", small),
            Paragraph("Сумма", small),
        ]]
        for item in invoice.items:
            data.append([
                str(item.line_number),
                Paragraph(escape(item.name), small),
                str(item.quantity),
                escape(item.unit),
                format_money_kopecks(item.unit_price_kopecks),
                format_money_kopecks(item.line_amount_kopecks),
            ])
        table = Table(
            data,
            colWidths=[9 * mm, 76 * mm, 19 * mm, 14 * mm, 30 * mm, 32 * mm],
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), self.font_name),
            ("FONTNAME", (0, 0), (-1, 0), self.bold_font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEAEA")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#666666")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([table, Spacer(1, 5 * mm)])
        story.append(Paragraph(
            f"Товары: {format_money_kopecks(invoice.products_amount_kopecks)}", right
        ))
        if invoice.delivery_amount_kopecks > 0:
            story.append(Paragraph(
                f"Доставка: {format_money_kopecks(invoice.delivery_amount_kopecks)}", right
            ))
        story.extend([
            Paragraph(
                f"Итого / К оплате: {format_money_kopecks(invoice.grand_total_kopecks)}",
                right,
            ),
            Spacer(1, 4 * mm),
            Paragraph(escape(invoice.tax_text), normal),
            Paragraph(f"Назначение платежа: {escape(invoice.payment_purpose)}", normal),
        ])
        document.build(story)
        return buffer.getvalue()


__all__ = [
    "InvoiceDraft",
    "InvoiceItemDraft",
    "InvoicePdfRenderer",
    "InvoiceSnapshot",
    "InvoiceStatus",
    "format_money_kopecks",
]
