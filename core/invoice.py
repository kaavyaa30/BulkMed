"""
invoice.py — GST Invoice PDF Generator
========================================
Generates a B2B GST-compliant tax invoice PDF using ReportLab.

Tax logic (pharmaceutical bulk supply, GST rate = 12%):
  - Intra-state  (factory state == store state): CGST 6% + SGST 6%
  - Inter-state  (factory state != store state): IGST 12%

State code is extracted from the first 2 digits of the GSTIN.
If either party has no GSTIN, we default to intra-state (CGST+SGST).

Called from views.py → factory_dispatch_order() when status → 'dispatched'.
The PDF is saved to media/invoices/ and the path stored in FactoryOrder.invoice_pdf.
"""

from decimal import Decimal
from datetime import date
from io import BytesIO

from django.core.files.base import ContentFile

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable,
)
from reportlab.lib.enums import TA_RIGHT, TA_CENTER


# ── Colour palette ────────────────────────────────────────────────────────────
DARK   = colors.HexColor('#0f172a')
GREEN  = colors.HexColor('#059669')
LIGHT  = colors.HexColor('#f0fdf4')
GREY   = colors.HexColor('#6b7280')
BORDER = colors.HexColor('#e5e7eb')
WHITE  = colors.white

GST_RATE  = Decimal('0.12')
CGST_RATE = Decimal('0.06')
SGST_RATE = Decimal('0.06')
IGST_RATE = Decimal('0.12')


def _state_code(gstin: str) -> str:
    """Extract 2-digit state code from GSTIN (first 2 characters)."""
    gstin = (gstin or '').strip()
    return gstin[:2] if len(gstin) >= 2 else ''


def _is_inter_state(factory_gstin: str, store_gstin: str) -> bool:
    """
    Returns True if the supply is inter-state (IGST applies).
    Compares the state codes embedded in both GSTINs.
    Falls back to intra-state if either GSTIN is missing.
    """
    fc = _state_code(factory_gstin)
    sc = _state_code(store_gstin)
    if not fc or not sc:
        return False
    return fc != sc


def _styles():
    return {
        'h1':    ParagraphStyle('h1',    fontSize=18, fontName='Helvetica-Bold',  textColor=DARK,  spaceAfter=2),
        'h2':    ParagraphStyle('h2',    fontSize=11, fontName='Helvetica-Bold',  textColor=DARK,  spaceAfter=2),
        'label': ParagraphStyle('label', fontSize=7,  fontName='Helvetica-Bold',  textColor=GREY,  spaceAfter=1),
        'value': ParagraphStyle('value', fontSize=9,  fontName='Helvetica',       textColor=DARK,  spaceAfter=2),
        'small': ParagraphStyle('small', fontSize=7,  fontName='Helvetica',       textColor=GREY),
        'right': ParagraphStyle('right', fontSize=9,  fontName='Helvetica',       textColor=DARK,  alignment=TA_RIGHT),
        'bold':  ParagraphStyle('bold',  fontSize=9,  fontName='Helvetica-Bold',  textColor=DARK),
        'total': ParagraphStyle('total', fontSize=11, fontName='Helvetica-Bold',  textColor=GREEN),
    }


def _tax_for_entry(entry, factory_gstin: str) -> dict:
    """
    Compute tax breakdown for a single OrderEntry.
    Returns a dict with: disc_price, taxable, cgst, sgst, igst, total, inter_state
    """
    store_gstin = entry.store.gstin or ''
    inter = _is_inter_state(factory_gstin, store_gstin)

    disc_price = entry.unit_price_at_order * (1 - entry.discount_applied / Decimal('100'))
    # Prices are GST-inclusive; back-calculate the taxable base
    taxable = round(disc_price * entry.quantity / (1 + GST_RATE), 2)

    if inter:
        igst = round(taxable * IGST_RATE, 2)
        cgst = sgst = Decimal('0')
    else:
        cgst = round(taxable * CGST_RATE, 2)
        sgst = round(taxable * SGST_RATE, 2)
        igst = Decimal('0')

    total = taxable + cgst + sgst + igst
    return {
        'disc_price': disc_price,
        'taxable':    taxable,
        'cgst':       cgst,
        'sgst':       sgst,
        'igst':       igst,
        'total':      total,
        'inter_state': inter,
    }


def generate_invoice(factory_order) -> str:
    """
    Generates a GST-compliant B2B invoice PDF for the given FactoryOrder.

    - Automatically applies CGST+SGST for intra-state or IGST for inter-state
      based on the first 2 digits of each party's GSTIN.
    - Saves the PDF to media/invoices/ and updates factory_order.invoice_pdf.

    Args:
        factory_order: FactoryOrder instance (status should be 'dispatched')

    Returns:
        str: Relative path to the saved PDF (e.g. 'invoices/INV-00042-Mumbai.pdf')
    """
    pool    = factory_order.pool
    factory = factory_order.factory
    product = pool.product

    from .models import OrderEntry
    entries = list(
        OrderEntry.objects.filter(pool=pool, status='active')
        .select_related('store')
    )

    # Pre-compute tax rows so we can decide column layout
    tax_rows = [_tax_for_entry(e, factory.gstin) for e in entries]
    any_inter = any(r['inter_state'] for r in tax_rows)
    any_intra = any(not r['inter_state'] for r in tax_rows)
    # Show IGST column if any inter-state entry exists; show CGST/SGST if any intra-state
    show_igst  = any_inter
    show_split = any_intra

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm,  bottomMargin=15*mm,
    )
    W = A4[0] - 30*mm
    s = _styles()
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    header_data = [[
        Paragraph('💊 BulkMed', s['h1']),
        Paragraph('TAX INVOICE', ParagraphStyle(
            'inv', fontSize=16, fontName='Helvetica-Bold',
            textColor=GREEN, alignment=TA_RIGHT
        )),
    ]]
    header_tbl = Table(header_data, colWidths=[W * 0.6, W * 0.4])
    header_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), LIGHT),
        ('TOPPADDING',    (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING',   (0, 0), (0, -1),  10),
        ('RIGHTPADDING',  (-1, 0), (-1, -1), 10),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Invoice meta ──────────────────────────────────────────────────────────
    inv_no   = f"INV-{factory_order.id:05d}"
    inv_date = (factory_order.dispatched_at or date.today()).strftime('%d %b %Y')
    supply_type = 'Inter-State (IGST)' if any_inter and not any_intra else \
                  'Intra-State (CGST+SGST)' if not any_inter else 'Mixed'

    meta_data = [[
        Paragraph(f'<b>Invoice No:</b> {inv_no}', s['value']),
        Paragraph(f'<b>Date:</b> {inv_date}', s['value']),
        Paragraph(f'<b>Supply Type:</b> {supply_type}', s['value']),
        Paragraph(f'<b>Pool ID:</b> {str(pool.id)[:8]}…', s['value']),
    ]]
    meta_tbl = Table(meta_data, colWidths=[W / 4] * 4)
    meta_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BOX',           (0, 0), (-1, -1), 0.5, BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Seller / Buyer ────────────────────────────────────────────────────────
    def party_block(title, name, address, city, gstin_val, state_code=''):
        sc_label = f' (State Code: {state_code})' if state_code else ''
        return [
            Paragraph(title, s['label']),
            Paragraph(f'<b>{name}</b>', s['h2']),
            Paragraph(address or city or '—', s['value']),
            Paragraph(city or '—', s['value']),
            Paragraph(f'GSTIN: {gstin_val or "Not provided"}{sc_label}', s['small']),
        ]

    factory_sc  = _state_code(factory.gstin)
    buyer_name  = f"{pool.city} — {len(entries)} Pharmacies"
    buyer_addr  = f"Consolidated bulk order — {pool.city}"
    buyer_gstin = ', '.join(e.store.gstin for e in entries if e.store.gstin) or 'Not provided'

    party_data = [[
        party_block('SELLER (SUPPLIER)', factory.name, factory.address, factory.city,
                    factory.gstin, factory_sc),
        party_block('BUYER (RECIPIENT)', buyer_name, buyer_addr, pool.city, buyer_gstin),
    ]]
    party_tbl = Table(party_data, colWidths=[W * 0.5, W * 0.5])
    party_tbl.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('BOX',           (0, 0), (0, -1),  0.5, BORDER),
        ('BOX',           (1, 0), (1, -1),  0.5, BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
    ]))
    story.append(party_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Line items ────────────────────────────────────────────────────────────
    story.append(Paragraph('Order Details', s['h2']))
    story.append(Spacer(1, 2 * mm))

    # Build dynamic column headers based on which tax types are present
    col_heads = ['#', 'Medicine / Store', 'HSN', 'Unit', 'Qty', 'Rate (₹)', 'Taxable (₹)']
    if show_split:
        col_heads += ['CGST 6%', 'SGST 6%']
    if show_igst:
        col_heads += ['IGST 12%']
    col_heads += ['Total (₹)']

    rows = [col_heads]
    grand_taxable = grand_cgst = grand_sgst = grand_igst = grand_total = Decimal('0')

    for i, (entry, tx) in enumerate(zip(entries, tax_rows), 1):
        row = [
            str(i),
            f"{product.name}\n{entry.store.name}",
            product.hsn_code or '—',
            product.unit,
            str(entry.quantity),
            f"₹{tx['disc_price']:.2f}",
            f"₹{tx['taxable']:.2f}",
        ]
        if show_split:
            row += [f"₹{tx['cgst']:.2f}", f"₹{tx['sgst']:.2f}"]
        if show_igst:
            row += [f"₹{tx['igst']:.2f}"]
        row += [f"₹{tx['total']:.2f}"]
        rows.append(row)

        grand_taxable += tx['taxable']
        grand_cgst    += tx['cgst']
        grand_sgst    += tx['sgst']
        grand_igst    += tx['igst']
        grand_total   += tx['total']

    # Totals row
    totals_row = ['', 'TOTAL', '', '', str(factory_order.total_qty), '', f"₹{grand_taxable:.2f}"]
    if show_split:
        totals_row += [f"₹{grand_cgst:.2f}", f"₹{grand_sgst:.2f}"]
    if show_igst:
        totals_row += [f"₹{grand_igst:.2f}"]
    totals_row += [f"₹{grand_total:.2f}"]
    rows.append(totals_row)

    # Dynamic column widths
    n_tax_cols = (2 if show_split else 0) + (1 if show_igst else 0)
    tax_col_w  = 18 * mm
    fixed_w    = 8 + 45 + 18 + 12 + 12 + 20 + 22  # mm for fixed cols
    total_col_w = 22 * mm
    remaining  = W - (fixed_w * mm) - (n_tax_cols * tax_col_w) - total_col_w
    rate_col_w = max(remaining, 18 * mm)

    col_w = [8*mm, 45*mm, 18*mm, 12*mm, 12*mm, 20*mm, rate_col_w]
    if show_split:
        col_w += [tax_col_w, tax_col_w]
    if show_igst:
        col_w += [tax_col_w]
    col_w += [total_col_w]

    items_tbl = Table(rows, colWidths=col_w, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0),  DARK),
        ('TEXTCOLOR',     (0, 0), (-1, 0),  WHITE),
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, 0),  7),
        ('TOPPADDING',    (0, 0), (-1, 0),  6),
        ('BOTTOMPADDING', (0, 0), (-1, 0),  6),
        ('FONTSIZE',      (0, 1), (-1, -2), 7),
        ('ROWBACKGROUNDS',(0, 1), (-1, -2), [WHITE, colors.HexColor('#f9fafb')]),
        ('TOPPADDING',    (0, 1), (-1, -2), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -2), 4),
        ('BACKGROUND',    (0, -1), (-1, -1), LIGHT),
        ('FONTNAME',      (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE',      (0, -1), (-1, -1), 8),
        ('TOPPADDING',    (0, -1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
        ('GRID',          (0, 0), (-1, -1), 0.3, BORDER),
        ('ALIGN',         (4, 0), (-1, -1), 'RIGHT'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Tax summary ───────────────────────────────────────────────────────────
    tax_summary = [['Tax Summary', '', '']]
    tax_summary.append(['Taxable Amount', '', f"₹{grand_taxable:.2f}"])
    if show_split:
        tax_summary.append(['CGST @ 6%', '', f"₹{grand_cgst:.2f}"])
        tax_summary.append(['SGST @ 6%', '', f"₹{grand_sgst:.2f}"])
    if show_igst:
        tax_summary.append(['IGST @ 12%', '', f"₹{grand_igst:.2f}"])
    total_gst = grand_cgst + grand_sgst + grand_igst
    tax_summary.append(['Total GST (12%)', '', f"₹{total_gst:.2f}"])
    tax_summary.append(['GRAND TOTAL', '', f"₹{grand_total:.2f}"])

    tax_tbl = Table(tax_summary, colWidths=[W * 0.5, W * 0.2, W * 0.3])
    tax_tbl.setStyle(TableStyle([
        ('SPAN',          (0, 0), (-1, 0)),
        ('BACKGROUND',    (0, 0), (-1, 0),  DARK),
        ('TEXTCOLOR',     (0, 0), (-1, 0),  WHITE),
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, 0),  9),
        ('TOPPADDING',    (0, 0), (-1, 0),  6),
        ('BOTTOMPADDING', (0, 0), (-1, 0),  6),
        ('LEFTPADDING',   (0, 0), (-1, 0),  8),
        ('FONTSIZE',      (0, 1), (-1, -2), 8),
        ('TOPPADDING',    (0, 1), (-1, -2), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -2), 4),
        ('LEFTPADDING',   (0, 1), (-1, -2), 8),
        ('ALIGN',         (2, 1), (2, -1),  'RIGHT'),
        ('RIGHTPADDING',  (2, 1), (2, -1),  8),
        ('BACKGROUND',    (0, -1), (-1, -1), LIGHT),
        ('FONTNAME',      (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE',      (0, -1), (-1, -1), 10),
        ('TEXTCOLOR',     (2, -1), (2, -1),  GREEN),
        ('TOPPADDING',    (0, -1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
        ('BOX',           (0, 0), (-1, -1),  0.5, BORDER),
        ('LINEBELOW',     (0, -2), (-1, -2), 0.5, BORDER),
        ('ALIGN',         (0, 0), (0, -1),   'LEFT'),
    ]))

    tax_wrapper = Table([[None, tax_tbl]], colWidths=[W * 0.35, W * 0.65])
    tax_wrapper.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(tax_wrapper)
    story.append(Spacer(1, 8 * mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width=W, thickness=0.5, color=BORDER))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        'This is a computer-generated invoice and does not require a physical signature. '
        'Subject to jurisdiction of courts in India.',
        s['small']
    ))
    story.append(Paragraph(
        'BulkMed Platform — B2B Bulk Medicine Procurement | support@bulkmed.in',
        ParagraphStyle('footer', fontSize=7, fontName='Helvetica', textColor=GREEN, alignment=TA_CENTER)
    ))

    # ── Build & save ──────────────────────────────────────────────────────────
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"INV-{factory_order.id:05d}-{pool.city.replace(' ', '_')}.pdf"
    factory_order.invoice_pdf.save(filename, ContentFile(pdf_bytes), save=True)

    return factory_order.invoice_pdf.name
