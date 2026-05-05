"""
fix_orphaned_deliveries.py
==========================
Management command to fix DeliveryTracking records that are confirmed
(otp_verified=True / status='delivered') but have no OrderEntry and no
FactoryPayout / WalletTransaction ledger entries.

This happens when deliveries were created directly by the admin via the
Control Panel, bypassing the pool_join_after_payment flow entirely.

Usage:
    python manage.py fix_orphaned_deliveries           # dry-run
    python manage.py fix_orphaned_deliveries --commit  # apply fixes
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from decimal import Decimal


class Command(BaseCommand):
    help = 'Fix orphaned deliveries: create synthetic OrderEntry + ledger records.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            default=False,
            help='Write changes. Without this flag the command is a dry-run.',
        )

    def handle(self, *args, **options):
        commit = options['commit']

        from core.models import (
            DeliveryTracking, OrderEntry, PlatformWallet,
            WalletTransaction, CommissionLog, FactoryPayout,
        )

        if not commit:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN — no changes written. Add --commit to apply.\n'
            ))

        broken = (
            DeliveryTracking.objects
            .filter(otp_verified=True, status='delivered')
            .exclude(factory_payout__isnull=False)
            .select_related('pool__product__factory', 'store__user', 'pool__factory_order')
            .order_by('id')
        )

        total  = broken.count()
        fixed  = 0
        errors = []

        self.stdout.write(f'\nOrphaned deliveries found: {total}\n')

        for d in broken:
            pool    = d.pool
            store   = d.store
            product = pool.product

            self.stdout.write(
                f'--- Delivery #{d.id}  {product.name} -> {store.name} ---'
            )

            # ── Find or create OrderEntry ─────────────────────────────────
            entry = OrderEntry.objects.filter(pool=pool, store=store).first()

            if not entry:
                discount   = pool.current_discount()
                unit_price = round(
                    product.base_price * (1 - Decimal(str(discount)) / 100), 2
                )
                order_value = round(unit_price * d.quantity, 2)
                self.stdout.write(
                    f'  No OrderEntry found. Will create synthetic entry: '
                    f'qty={d.quantity}  unit=Rs.{unit_price}  '
                    f'disc={discount}%  total=Rs.{order_value}'
                )
            else:
                order_value = entry.total_amount()
                self.stdout.write(
                    f'  Found OrderEntry #{entry.id}  status={entry.status}  '
                    f'total=Rs.{order_value}'
                )

            commission = round(order_value * Decimal('0.15'), 2)
            net_payout = round(order_value * Decimal('0.85'), 2)
            factory    = product.factory

            self.stdout.write(
                f'  Gross=Rs.{order_value}  '
                f'Commission=Rs.{commission}  '
                f'Payout=Rs.{net_payout}  '
                f'Factory={factory.name if factory else "NONE"}'
            )

            if not commit:
                fixed += 1
                continue

            try:
                with transaction.atomic():
                    # 1. Create synthetic OrderEntry if missing
                    if not entry:
                        discount   = pool.current_discount()
                        unit_price = round(
                            product.base_price * (1 - Decimal(str(discount)) / 100), 2
                        )
                        entry = OrderEntry.objects.create(
                            pool                = pool,
                            store               = store,
                            quantity            = d.quantity,
                            mode                = pool.pool_mode,
                            unit_price_at_order = unit_price,
                            discount_applied    = Decimal(str(discount)),
                            status              = 'delivered',
                            escrow_amount       = Decimal('0'),
                            estimated_arrival   = d.delivered_at or timezone.now(),
                        )
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'  Created OrderEntry #{entry.id}'
                            )
                        )

                    # 2. WalletTransaction — final payment
                    platform = PlatformWallet.get()
                    if not WalletTransaction.objects.filter(
                        delivery=d, transaction_label='final_payment'
                    ).exists():
                        platform.transactions.create(
                            amount            = order_value,
                            transaction_type  = 'credit',
                            transaction_label = 'final_payment',
                            order_entry       = entry,
                            description       = (
                                f'+90% Final Payment (backfill) — '
                                f'{product.name} -> {store.name}'
                            ),
                            delivery          = d,
                        )
                        self.stdout.write('  Created WalletTransaction: final_payment')

                    # 3. WalletTransaction — commission
                    if not WalletTransaction.objects.filter(
                        delivery=d, transaction_label='commission'
                    ).exists():
                        platform.transactions.create(
                            amount            = commission,
                            transaction_type  = 'credit',
                            transaction_label = 'commission',
                            order_entry       = entry,
                            description       = (
                                f'Platform commission 15% (backfill) — '
                                f'{product.name} -> {store.name}'
                            ),
                            delivery          = d,
                        )
                        self.stdout.write('  Created WalletTransaction: commission')

                    # 4. Recalculate wallet balance
                    platform.recalculate()

                    # 5. CommissionLog
                    log, created = CommissionLog.objects.get_or_create(
                        pool=pool,
                        defaults={
                            'total_order_value': order_value,
                            'commission_rate':   Decimal('15.00'),
                            'commission_amount': commission,
                        }
                    )
                    if log.payout_status != 'released':
                        log.payout_status     = 'released'
                        log.released_at       = d.delivered_at or timezone.now()
                        log.commission_amount = (
                            log.commission_amount or Decimal('0')
                        ) + commission
                        log.save()
                        self.stdout.write(
                            f'  CommissionLog {"created" if created else "updated"} -> released'
                        )

                    # 6. FactoryPayout
                    if factory:
                        FactoryPayout.objects.create(
                            factory             = factory,
                            pool                = pool,
                            delivery            = d,
                            gross_amount        = order_value,
                            commission_deducted = commission,
                            net_payout          = net_payout,
                            status              = 'pending',
                        )
                        self.stdout.write(
                            f'  Created FactoryPayout (pending) -> {factory.name}'
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                '  No factory on product — FactoryPayout skipped.'
                            )
                        )

                    # 7. Mark OrderEntry delivered
                    if entry.status == 'active':
                        entry.status = 'delivered'
                        entry.save(update_fields=['status'])
                        self.stdout.write('  OrderEntry status -> delivered')

                    # 8. Auto-complete FactoryOrder
                    try:
                        fo = pool.factory_order
                        all_done = not DeliveryTracking.objects.filter(
                            pool=pool
                        ).exclude(status__in=('delivered', 'failed')).exists()
                        if all_done and fo.status != 'completed':
                            fo.status = 'completed'
                            fo.save(update_fields=['status'])
                            self.stdout.write('  FactoryOrder -> completed')
                    except Exception:
                        pass

                fixed += 1
                self.stdout.write(self.style.SUCCESS(f'  FIXED\n'))

            except Exception as exc:
                import traceback
                errors.append((d.id, str(exc)))
                self.stdout.write(self.style.ERROR(f'  ERROR: {exc}'))
                traceback.print_exc()

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write('=' * 60)
        if commit:
            self.stdout.write(self.style.SUCCESS(f'Fixed:   {fixed}'))
            if errors:
                self.stdout.write(self.style.ERROR(f'Errors:  {len(errors)}'))
                for did, msg in errors:
                    self.stdout.write(self.style.ERROR(f'  Delivery #{did}: {msg}'))

            # Verification
            from core.models import PlatformWallet, WalletTransaction
            remaining = (
                DeliveryTracking.objects
                .filter(otp_verified=True, status='delivered')
                .exclude(factory_payout__isnull=False)
                .count()
            )
            wallet = PlatformWallet.get()
            txn_count = WalletTransaction.objects.filter(
                transaction_label__in=['final_payment', 'commission']
            ).count()
            self.stdout.write(f'\nRemaining orphaned deliveries: {remaining}')
            self.stdout.write(f'Platform wallet balance:       Rs.{wallet.balance}')
            self.stdout.write(f'Platform total earned:         Rs.{wallet.total_earned}')
            self.stdout.write(
                f'Audit trail entries (payment+commission): {txn_count}'
            )
            self.stdout.write(self.style.SUCCESS('\nDone.\n'))
        else:
            self.stdout.write(self.style.WARNING(
                f'Would fix {fixed} deliveries. Run with --commit to apply.\n'
            ))
