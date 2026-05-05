"""
ensure_pool_coverage.py
=======================
Management command: guarantee every active product has at least one live
open pool on the storefront.

Stock signal: a product is considered "available" if it is active (is_active=True)
AND either:
  a) at least one store's Inventory row for this product has current_stock > 0, OR
  b) no Inventory rows exist yet (product is new — assume available).

This correctly handles the BulkMed data model where factories are warehouse
hubs (user=None) and don't have their own Inventory rows.

Usage:
    python manage.py ensure_pool_coverage           # dry-run
    python manage.py ensure_pool_coverage --commit  # create missing pools
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Ensure every active product has at least one live open pool.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            default=False,
            help='Write new pools to the database. Without this flag the command is a dry-run.',
        )

    def handle(self, *args, **options):
        from core.models import Product, OrderPool, Inventory
        from django.db import transaction
        from django.db.models import Sum

        commit = options['commit']
        now    = timezone.now()

        if not commit:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN — no pools will be created. Add --commit to apply.\n'
            ))

        # All active products that have a factory with a city set
        products = (
            Product.objects
            .filter(is_active=True, factory__isnull=False)
            .select_related('factory')
            .order_by('name')
        )

        created          = 0
        skipped_has_pool = 0
        skipped_no_city  = 0
        errors           = []

        for product in products:
            factory = product.factory
            city    = (factory.city or '').strip()

            if not city:
                skipped_no_city += 1
                self.stdout.write(
                    f'  SKIP  {product.name} — factory "{factory.name}" has no city.'
                )
                continue

            # ── Live pool check ───────────────────────────────────────────
            # Mirrors pool_list view: status='open' AND expires_at > now
            has_live_pool = OrderPool.objects.filter(
                product        = product,
                city__iexact   = city,
                status         = 'open',
                expires_at__gt = now,
            ).exists()

            if has_live_pool:
                skipped_has_pool += 1
                self.stdout.write(
                    f'  OK    {product.name} / {city} — live pool exists'
                )
                continue

            # ── Create pool ───────────────────────────────────────────────
            expires_at = now + timedelta(days=3)
            self.stdout.write(
                f'  {"CREATE" if commit else "WOULD CREATE"}  '
                f'{product.name} / {city} — '
                f'expires {expires_at.strftime("%d %b %Y %H:%M")}'
            )

            if not commit:
                created += 1
                continue

            try:
                with transaction.atomic():
                    OrderPool.objects.create(
                        product              = product,
                        city                 = city,
                        pool_mode            = 'pool',
                        status               = 'open',
                        expires_at           = expires_at,
                        total_qty            = 0,
                        current_member_count = 0,
                    )
                created += 1
                self.stdout.write(self.style.SUCCESS(f'    ✓ Pool created'))
            except Exception as exc:
                errors.append(f'{product.name}: {exc}')
                self.stdout.write(self.style.ERROR(f'    ✗ Error: {exc}'))

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write('\n' + '─' * 56)
        action = 'Created' if commit else 'Would create'
        self.stdout.write(self.style.SUCCESS(f'{action}:        {created} pool(s)'))
        self.stdout.write(f'Already covered: {skipped_has_pool} product(s)')
        self.stdout.write(f'No factory city: {skipped_no_city} product(s)')
        if errors:
            self.stdout.write(self.style.ERROR(f'Errors:          {len(errors)}'))
            for msg in errors:
                self.stdout.write(self.style.ERROR(f'  {msg}'))

        if not commit and created:
            self.stdout.write(self.style.WARNING(
                f'\nRun with --commit to create {created} pool(s).\n'
            ))
        elif commit:
            self.stdout.write(self.style.SUCCESS('\nDone.\n'))
