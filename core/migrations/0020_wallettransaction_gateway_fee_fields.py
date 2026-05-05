"""
Migration 0020 — WalletTransaction: gateway fee fields + new label choices

Adds two Decimal fields to WalletTransaction so every Razorpay-charged
transaction independently records the 2% gateway fee and 18% GST on that
fee. This lets the Admin Control Panel compute true net platform revenue
that reconciles exactly with Razorpay's bank settlement report.

New fields
──────────
  gateway_fee_amount  DecimalField(10,2)  default=0.00
      Razorpay 2% fee charged on this transaction.
      Non-zero only for escrow_advance and topup_credit rows.

  gateway_gst_amount  DecimalField(10,2)  default=0.00
      18% GST levied on the gateway fee.
      Non-zero only for escrow_advance and topup_credit rows.

Updated LABEL_CHOICES
─────────────────────
  + topup_credit   'Wallet Top-up (Razorpay)'
  + gateway_fee    'Payment Gateway Fee'

The max_length of transaction_label is unchanged (20 chars) — the longest
new label key is 'topup_credit' (12 chars), well within the limit.

Existing rows
─────────────
All existing WalletTransaction rows default to gateway_fee_amount=0.00 and
gateway_gst_amount=0.00, which is correct — they were created before the
fee-tracking system existed and their amounts already represent gross values.
"""

from django.db import migrations, models
import decimal


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_orderentry_delivered_status'),
    ]

    operations = [
        # ── 1. Add gateway_fee_amount ─────────────────────────────────────────
        migrations.AddField(
            model_name='wallettransaction',
            name='gateway_fee_amount',
            field=models.DecimalField(
                max_digits=10,
                decimal_places=2,
                default=decimal.Decimal('0.00'),
                help_text=(
                    'Razorpay 2% gateway fee charged on this transaction. '
                    'Zero for internal ledger movements (commission, refunds, payouts).'
                ),
            ),
        ),

        # ── 2. Add gateway_gst_amount ─────────────────────────────────────────
        migrations.AddField(
            model_name='wallettransaction',
            name='gateway_gst_amount',
            field=models.DecimalField(
                max_digits=10,
                decimal_places=2,
                default=decimal.Decimal('0.00'),
                help_text=(
                    '18% GST levied on the Razorpay gateway fee. '
                    'Zero for internal ledger movements.'
                ),
            ),
        ),

        # ── 3. Update transaction_label choices to include new labels ─────────
        # AlterField is needed so Django's validation and admin dropdowns
        # reflect the two new choices (topup_credit, gateway_fee).
        # The column type and max_length are unchanged — this is metadata only.
        migrations.AlterField(
            model_name='wallettransaction',
            name='transaction_label',
            field=models.CharField(
                max_length=20,
                default='other',
                choices=[
                    ('escrow_advance',  '+10% Advance (Escrow)'),
                    ('final_payment',   '+90% Final Payment'),
                    ('commission',      'Platform Commission (15%)'),
                    ('escrow_refund',   'Escrow Refund'),
                    ('factory_payout',  'Factory Payout (85%)'),
                    ('dispute_refund',  'Dispute Refund'),
                    ('topup_credit',    'Wallet Top-up (Razorpay)'),
                    ('gateway_fee',     'Payment Gateway Fee'),
                    ('other',           'Other'),
                ],
            ),
        ),
    ]
