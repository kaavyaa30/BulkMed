from django.db import migrations, models
import re


def generate_skus(apps, schema_editor):
    Product = apps.get_model('core', 'Product')
    seen = set()
    for p in Product.objects.all():
        base = re.sub(r'[^A-Z0-9]', '-', p.name.upper())
        base = re.sub(r'-+', '-', base).strip('-')[:20]
        sku = base
        counter = 1
        while sku in seen:
            sku = f"{base}-{counter}"
            counter += 1
        seen.add(sku)
        p.sku_code = sku
        p.save(update_fields=['sku_code'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_platform_wallet'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='sku_code',
            field=models.CharField(blank=True, db_index=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='product',
            name='hsn_code',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='product',
            name='barcode',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.RunPython(generate_skus, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='product',
            name='sku_code',
            field=models.CharField(blank=True, db_index=True, default='', max_length=100, unique=True),
        ),
    ]
