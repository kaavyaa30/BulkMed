"""
Migration 0022 — DeliveryTracking: 3PL logistics fields

Adds five new fields to DeliveryTracking to support the hybrid
Local Contract / 3PL API logistics model:

  delivery_method    — 'local' (driver PWA) or '3pl' (Delhivery/Shadowfax)
  logistics_partner  — provider name string (e.g. "Delhivery")
  shipping_cost      — ₹ cost charged by the 3PL provider
  waybill_id         — tracking/waybill number from the provider
  shipping_label_url — URL to the PDF shipping label

Also updates WalletTransaction.transaction_label choices to include
'shipping_cost' for the audit trail entry.

All new fields default to blank/zero so existing delivery rows are
unaffected — they remain 'local' deliveries with no shipping cost.
"""

from django.db import migrations, models
import decimal


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_alter_wallettransaction_gateway_fee_amount_and_more'),
    ]

    operations = [
        # ── 1. delivery_method ────────────────────────────────────────────────
        migrations.AddField(
            model_name='deliverytracking',
            name='delivery_method',
            field=models.CharField(
                max_length=10,
                default='local',
                choices=[
                    ('local', 'Local Contract Driver'),
                    ('3pl',   '3PL API (Delhivery etc.)'),
                ],
                help_text='local = built-in driver PWA; 3pl = third-party logistics API.',
            ),
        ),

        # ── 2. logistics_partner ──────────────────────────────────────────────
        migrations.AddField(
            model_name='deliverytracking',
            name='logistics_partner',
            field=models.CharField(
                max_length=100,
                blank=True,
                default='',
                help_text='Provider name, e.g. "Delhivery". Blank for local deliveries.',
            ),
        ),

        # ── 3. shipping_cost ──────────────────────────────────────────────────
        migrations.AddField(
            model_name='deliverytracking',
            name='shipping_cost',
            field=models.DecimalField(
                max_digits=10,
                decimal_places=2,
                default=decimal.Decimal('0'),
                help_text='Shipping cost charged by the 3PL provider in ₹.',
            ),
        ),

        # ── 4. waybill_id ─────────────────────────────────────────────────────
        migrations.AddField(
            model_name='deliverytracking',
            name='waybill_id',
            field=models.CharField(
                max_length=100,
                blank=True,
                default='',
                help_text='Tracking/waybill number assigned by the 3PL provider.',
            ),
        ),

        # ── 5. shipping_label_url ─────────────────────────────────────────────
        migrations.AddField(
            model_name='deliverytracking',
            name='shipping_label_url',
            field=models.URLField(
                max_length=500,
                blank=True,
                default='',
                help_text='URL to the PDF shipping label from the 3PL provider.',
            ),
        ),

        # ── 6. WalletTransaction label choices — add 'shipping_cost' ─────────
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
                    ('shipping_cost',   '3PL Shipping Cost'),
                    ('other',           'Other'),
                ],
            ),
        ),
    ]
