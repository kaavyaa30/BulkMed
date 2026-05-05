"""
Migration: add driver_token to DeliveryTracking.

driver_token is a 32-char random hex string generated once per delivery.
It is embedded in the PWA URL so the driver can push GPS updates without
needing a Django session login.

  /driver/<delivery_id>/?token=<driver_token>
"""
import secrets
from django.db import migrations, models


def populate_tokens(apps, schema_editor):
    DeliveryTracking = apps.get_model('core', 'DeliveryTracking')
    for d in DeliveryTracking.objects.filter(driver_token=''):
        d.driver_token = secrets.token_hex(16)
        d.save(update_fields=['driver_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_dispute_module'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliverytracking',
            name='driver_token',
            field=models.CharField(
                max_length=32, blank=True, default='',
                help_text='One-time token embedded in the driver PWA URL for tokenised GPS auth.',
            ),
        ),
        migrations.RunPython(populate_tokens, migrations.RunPython.noop),
    ]
