"""
sync_delivered_orders.py
========================
Management command to backfill the Financial Audit Trail for deliveries
that were confirmed (otp_verified=True / status='delivered') but whose
OrderEntry is still 'active' and/or whose FactoryPayout / WalletTransaction
records are missing — caused by the nested transaction.atomic() bug.

Usage:
    python manage.py sync_delivered_orders            # dry-run (shows what would change)
    python manage.py sync_delivered_orders --commit   # actually writes to the database

Safety:
    - Idempotent: safe to run multiple times. Skips deliveries that already
      have a FactoryPayout record (meaning _release_commission already ran).
    - Wraps each delivery in its own transaction so one failure doesn't
      block the rest.
    - Prints a full summary at the end.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from decimal import Decimal


class Command(BaseCommand):
    help = 'Backfill ledger entries for delivered orders that are missing FactoryPayout / WalletTransaction records.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            default=False,
            help='Actually write changes. Without this flag the command runs in dry-run mode.',
        )

    def handle(self, *args, **options):
        commit = options['commit']

        from core.models import (
            DeliveryTracking, OrderEntry, FactoryPayout,
            WalletTransaction, PlatformWallet, CommissionLog,
        )

        if not commit:
            self.stdout.write(self.style.WARNING(
                '\n⚠  DRY-RUN MODE — no changes will be written.\n'
                '   Re-run with --commit to apply fixes.\n'
            ))

        # Find every delivery that is confirmed but has no FactoryPayout
        broken = (
            DeliveryTracking.objects
            .filter(otp_verified=True, status='delivered')
            .exclude(factory_payout__isnull=False)   # skip already-fixed ones
            .select_related('pool__product__factory', 'store__user', 'pool__factory_order')
            .order_by('id')
        )

        total   = broken.count()
        fixed   = 0
        skipped = 0
        errors  = []

        self.stdout.write(f'\nFound {total} delivered deliveries with missing ledger entries.\n')

        for delivery in broken:
            # Find the matching OrderEntry (active OR already-delivered)
            entry = (
                OrderEntry.objects
                .filter(pool=delivery.pool, store=delivery.store)
                .exclude(status__in=('cancelled_free', 'cancelled_penalty'))
                .first()
            )

            if not entry:
                self.stdout.write(self.style.WARNING(
                    f'  SKIP  Delivery #{delivery.id} — no matching OrderEntry found.'
                ))
                skipped += 1
                continue

            order_value = entry.total_amount()
            if order_value <= 0:
                self.stdout.write(self.style.WARNING(
                    f'  SKIP  Delivery #{delivery.id} — order_value is zero.'
                ))
                skipped += 1
                continue

            commission = round(order_value * Decimal('0.15'), 2)
            net_payout = round(order_value * Decimal('0.85'), 2)
            factory    = delivery.pool.product.factory

            self.stdout.write(
                f'  {"FIX " if commit else "WOULD FIX"}  '
                f'Delivery #{delivery.id} | {delivery.pool.product.name} → {delivery.store.name} | '
                f'Gross ₹{order_value} | Commission ₹{commission} | Payout ₹{net_payout}'
            )

            if not commit:
                fixed += 1
                continue

            try:
                with transaction.atomic():
                    platform = PlatformWallet.get()

                    # 1. WalletTransaction — final payment
                    if not WalletTransaction.objects.filter(
                        delivery=delivery, transaction_label='final_payment'
                    ).exists():
                        platform.transactions.create(
                            amount            = order_value,
                            transaction_type  = 'credit',
                            transaction_label = 'final_payment',
                            order_entry       = entry,
                            description       = (
                                f'+90% Final Payment (backfill) — '
                                f'{delivery.pool.product.name} → {delivery.store.name}'
                            ),
                            delivery          = delivery,
                        )

                    # 2. WalletTransaction — commission
                    if not WalletTransaction.objects.filter(
                        delivery=delivery, transaction_label='commission'
                    ).exists():
                        platform.transactions.create(
                            amount            = commission,
                            transaction_type  = 'credit',
                            transaction_label = 'commission',
                            order_entry       = entry,
                            description       = (
                                f'Platform commission 15% (backfill) — '
                                f'{delivery.pool.product.name} → {delivery.store.name}'
                            ),
                            delivery          = delivery,
                        )

                    # 3. Recalculate wallet balance
                    platform.recalculate()

                    # 4. CommissionLog
                    log, _ = CommissionLog.objects.get_or_create(
                        pool=delivery.pool,
                        defaults={
                            'total_order_value': order_value,
                            'commission_rate':   Decimal('15.00'),
                            'commission_amount': commission,
                        }
                    )
                    if log.payout_status != 'released':
                        log.payout_status     = 'released'
                        log.released_at       = delivery.delivered_at or timezone.now()
                        log.commission_amount = (log.commission_amount or Decimal('0')) + commission
                        log.save()

                    # 5. FactoryPayout
                    if factory and not FactoryPayout.objects.filter(delivery=delivery).exists():
                        FactoryPayout.objects.create(
                            factory             = factory,
                            pool                = delivery.pool,
                            delivery            = delivery,
                            gross_amount        = order_value,
                            commission_deducted = commission,
                            net_payout          = net_payout,
                            status              = 'pending',
                        )

                    # 6. OrderEntry status
                    if entry.status == 'active':
                        entry.status = 'delivered'
                        entry.save(update_fields=['status'])

                    # 7. FactoryOrder auto-complete
                    try:
                        fo = delivery.pool.factory_order
                        all_done = not DeliveryTracking.objects.filter(
                            pool=delivery.pool
                        ).exclude(status__in=('delivered', 'failed')).exists()
                        if all_done and fo.status != 'completed':
                            fo.status = 'completed'
                            fo.save(update_fields=['status'])
                    except Exception:
                        pass

                fixed += 1
                self.stdout.write(self.style.SUCCESS(f'    ✓ Fixed delivery #{delivery.id}'))

            except Exception as exc:
                errors.append((delivery.id, str(exc)))
                self.stdout.write(self.style.ERROR(f'    ✗ Error on delivery #{delivery.id}: {exc}'))

        # ── Summary ──────────────────────────────────────────────────────────
        self.stdout.write('\n' + '─' * 60)
        if commit:
            self.stdout.write(self.style.SUCCESS(f'  Fixed:   {fixed}'))
            self.stdout.write(self.style.WARNING(f'  Skipped: {skipped}'))
            if errors:
                self.stdout.write(self.style.ERROR(f'  Errors:  {len(errors)}'))
                for did, msg in errors:
                    self.stdout.write(self.style.ERROR(f'    Delivery #{did}: {msg}'))
            self.stdout.write(self.style.SUCCESS('\n✅ Backfill complete.\n'))
        else:
            self.stdout.write(self.style.WARNING(
                f'  Would fix {fixed} deliveries (dry-run).\n'
                f'  Run with --commit to apply.\n'
            ))
