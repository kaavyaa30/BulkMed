from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_store_topup'),
    ]

    operations = [
        migrations.AddField(
            model_name='wallettransaction',
            name='order_entry',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='wallet_transactions',
                to='core.orderentry',
            ),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='transaction_label',
            field=models.CharField(
                choices=[
                    ('escrow_advance',  '+10% Advance (Escrow)'),
                    ('final_payment',   '+90% Final Payment'),
                    ('commission',      'Platform Commission (15%)'),
                    ('escrow_refund',   'Escrow Refund'),
                    ('factory_payout',  'Factory Payout (85%)'),
                    ('dispute_refund',  'Dispute Refund'),
                    ('other',           'Other'),
                ],
                default='other',
                max_length=20,
            ),
        ),
    ]
