"""
fix_factory_fk.py — Management command to backfill missing Factory FK links
=============================================================================

Run with:
    python manage.py fix_factory_fk
    python manage.py fix_factory_fk --dry-run   (preview only, no DB writes)

What it fixes
─────────────
1. Product.factory FK
   Every Product has a `factory_name` CharField. If `factory` FK is NULL,
   find the Factory whose name matches exactly and set the FK.

2. FactoryPayout.factory FK
   FactoryPayout already has a `factory` FK, but rows created before the
   Product.factory FK was populated may have it set correctly via
   delivery.pool.product.factory. This step verifies and repairs any
   FactoryPayout rows where factory is NULL by traversing the delivery chain.

3. Factory.wallet_balance recalculation
   Recomputes each factory's wallet_balance as the sum of all PAID
   FactoryPayout.net_payout rows. This is the authoritative source of truth.

Why WalletTransaction is NOT touched
─────────────────────────────────────
WalletTransaction records the PLATFORM wallet ledger — it has no factory FK
and is not supposed to. Factory earnings are tracked exclusively through
FactoryPayout. The factory_dashboard and factory_wallet views already query
FactoryPayout correctly using the factory FK.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Q


class Command(BaseCommand):
    help = 'Fix ALL Product.factory FK mismatches (null OR wrong), repair FactoryPayout.factory, recalculate wallet balances.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be changed without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        # ── Import models here to avoid app-registry issues ───────────────────
        from core.models import Factory, Product, FactoryPayout

        # ── Step 1: Build a name → Factory lookup map ─────────────────────────
        # Lower-cased for case-insensitive matching.
        factory_map = {f.name.strip().lower(): f for f in Factory.objects.all()}

        self.stdout.write('─' * 60)
        self.stdout.write('STEP 1 — Backfill Product.factory FK from factory_name')
        self.stdout.write('─' * 60)

        products_fixed   = 0
        products_skipped = 0   # already has FK
        products_no_match = 0  # factory_name doesn't match any Factory

        products_to_update = []

        for product in Product.objects.select_related('factory').all():
            correct_factory = factory_map.get((product.factory_name or '').strip().lower())

            if correct_factory is None:
                self.stdout.write(
                    self.style.WARNING(
                        f'  ⚠  Product #{product.id} "{product.name}" — '
                        f'no Factory found for name "{product.factory_name}" — skipped'
                    )
                )
                products_no_match += 1
                continue

            if product.factory_id == correct_factory.id:
                products_skipped += 1
                continue  # already correct — never overwrite

            old_name = product.factory.name if product.factory else 'NULL'
            self.stdout.write(
                f'  ✅  Product #{product.id} "{product.name}" '
                f'{old_name} → {correct_factory.name}'
            )
            product.factory = correct_factory
            products_to_update.append(product)
            products_fixed += 1

        if not dry_run and products_to_update:
            with transaction.atomic():
                Product.objects.bulk_update(products_to_update, ['factory'])

        self.stdout.write(
            f'\n  Fixed: {products_fixed}  |  '
            f'Already linked: {products_skipped}  |  '
            f'No match: {products_no_match}\n'
        )

        # ── Step 2: Repair FactoryPayout rows with NULL factory ───────────────
        self.stdout.write('─' * 60)
        self.stdout.write('STEP 2 — Repair FactoryPayout rows with NULL factory FK')
        self.stdout.write('─' * 60)

        payouts_fixed   = 0
        payouts_skipped = 0
        payouts_no_factory = 0

        payouts_to_update = []

        orphaned_payouts = FactoryPayout.objects.filter(
            factory__isnull=True
        ).select_related(
            'delivery__pool__product__factory',
            'pool__product__factory',
        )

        for payout in orphaned_payouts:
            # Try to resolve factory via delivery → pool → product → factory
            resolved = None
            if payout.delivery_id:
                try:
                    resolved = payout.delivery.pool.product.factory
                except Exception:
                    pass

            # Fallback: via pool → product → factory
            if resolved is None:
                try:
                    resolved = payout.pool.product.factory
                except Exception:
                    pass

            if resolved is None:
                self.stdout.write(
                    self.style.WARNING(
                        f'  ⚠  FactoryPayout #{payout.id} — '
                        f'cannot resolve factory (product has no factory FK) — skipped'
                    )
                )
                payouts_no_factory += 1
                continue

            self.stdout.write(
                f'  ✅  FactoryPayout #{payout.id} '
                f'(₹{payout.net_payout}) → Factory "{resolved.name}"'
            )
            payout.factory = resolved
            payouts_to_update.append(payout)
            payouts_fixed += 1

        if not dry_run and payouts_to_update:
            with transaction.atomic():
                FactoryPayout.objects.bulk_update(payouts_to_update, ['factory'])

        self.stdout.write(
            f'\n  Fixed: {payouts_fixed}  |  '
            f'No factory resolvable: {payouts_no_factory}\n'
        )

        # ── Step 3: Recalculate Factory.wallet_balance ────────────────────────
        # wallet_balance = sum of net_payout for all PAID FactoryPayout rows.
        # Pending payouts are NOT yet in the wallet — they're held by the platform.
        self.stdout.write('─' * 60)
        self.stdout.write('STEP 3 — Recalculate Factory.wallet_balance from FactoryPayout')
        self.stdout.write('─' * 60)

        factories_updated = 0
        factories_to_save = []

        for factory in Factory.objects.all():
            paid_sum = (
                FactoryPayout.objects
                .filter(factory=factory, status='paid')
                .aggregate(total=Sum('net_payout'))['total']
            ) or 0

            from decimal import Decimal
            paid_sum = Decimal(str(paid_sum))

            if factory.wallet_balance != paid_sum:
                self.stdout.write(
                    f'  ✅  Factory "{factory.name}" — '
                    f'wallet_balance: ₹{factory.wallet_balance} → ₹{paid_sum}'
                )
                factory.wallet_balance = paid_sum
                factories_to_save.append(factory)
                factories_updated += 1
            else:
                self.stdout.write(
                    f'  —   Factory "{factory.name}" — '
                    f'wallet_balance ₹{factory.wallet_balance} already correct'
                )

        if not dry_run and factories_to_save:
            with transaction.atomic():
                Factory.objects.bulk_update(factories_to_save, ['wallet_balance'])

        self.stdout.write(
            f'\n  Updated: {factories_updated} factory wallet balance(s)\n'
        )

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write('─' * 60)
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN complete — no changes were written.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Done.\n'
                    f'  Products linked:          {products_fixed}\n'
                    f'  FactoryPayouts repaired:  {payouts_fixed}\n'
                    f'  Wallet balances updated:  {factories_updated}'
                )
            )
