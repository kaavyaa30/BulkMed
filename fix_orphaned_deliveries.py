"""
fix_orphaned_deliveries.py
==========================
Run with:  python manage.py shell < fix_orphaned_deliveries.py

Fixes DeliveryTracking records that are marked 'delivered' but have no
OrderEntry (admin-created deliveries that bypassed the payment flow) and
no FactoryPayout / WalletTransaction ledger entries.

For each orphaned delivery this script:
  1. Creates a synthetic OrderEntry (price derived from pool's product base_price
     and current discount tier — the only source of truth available).
  2. Writes the two WalletTransaction rows (final_payment + commission).
  3. Creates the FactoryPayout record (status='pending').
  4. Updates/creates the CommissionLog.
  5. Marks the OrderEntry as 'delivered'.
  6. Auto-completes the FactoryOrder if all deliveries in the pool are done.

All writes for each delivery are wrapped in their own transaction.atomic()
so one failure does not block the others.
"""

import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bulkmed.settings')

from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from core.models import (
    DeliveryTracking, OrderEntry, PlatformWallet,
    WalletTransaction, CommissionLog, FactoryPayout,
)

# ── Find every confirmed delivery with no FactoryPayout ──────────────────────
broken = (
    DeliveryTracking.objects
    .filter(otp_verified=True, status='delivered')
    .exclude(factory_payout__isnull=False)
    .select_related('pool__product__factory', 'store__user', 'pool__factory_order')
    .order_by('id')
)

print(f"\nOrphaned deliveries to fix: {broken.count()}\n")

fixed  = 0
errors = []

for d in broken:
    pool    = d.pool
    store   = d.store
    product = pool.product

    print(f"--- Delivery #{d.id}  {product.name} -> {store.name} ---")

    try:
        # ── 1. Find or create OrderEntry ─────────────────────────────────
        entry = OrderEntry.objects.filter(pool=pool, store=store).first()

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
            print(f"  Created synthetic OrderEntry #{entry.id}  "
                  f"unit=Rs.{unit_price}  total=Rs.{entry.total_amount()}")
        else:
            print(f"  Found existing OrderEntry #{entry.id}  status={entry.status}")

        # ── 2. Guard: skip if payout already exists ───────────────────────
        if FactoryPayout.objects.filter(delivery=d).exists():
            print("  FactoryPayout already exists — skipping.")
            continue

        order_value = entry.total_amount()
        if order_value <= 0:
            print("  order_value=0 — skipping.")
            continue

        commission = round(order_value * Decimal('0.15'), 2)
        net_payout = round(order_value * Decimal('0.85'), 2)
        factory    = product.factory

        print(f"  Gross=Rs.{order_value}  Commission=Rs.{commission}  Payout=Rs.{net_payout}")

        # ── 3. Write all ledger records atomically ────────────────────────
        with transaction.atomic():
            platform = PlatformWallet.get()

            # WalletTransaction — final payment
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
                print("  Created WalletTransaction: final_payment")

            # WalletTransaction — commission
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
                print("  Created WalletTransaction: commission")

            # Recalculate wallet balance
            platform.recalculate()

            # CommissionLog
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
                log.commission_amount = (log.commission_amount or Decimal('0')) + commission
                log.save()
                print(f"  CommissionLog {'created' if created else 'updated'} -> released")

            # FactoryPayout
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
                print(f"  Created FactoryPayout (pending) -> {factory.name}")
            else:
                print("  WARNING: No factory linked to product — FactoryPayout skipped.")

            # Mark OrderEntry delivered
            if entry.status == 'active':
                entry.status = 'delivered'
                entry.save(update_fields=['status'])
                print("  OrderEntry status -> delivered")

            # Auto-complete FactoryOrder
            try:
                fo = pool.factory_order
                all_done = not DeliveryTracking.objects.filter(
                    pool=pool
                ).exclude(status__in=('delivered', 'failed')).exists()
                if all_done and fo.status != 'completed':
                    fo.status = 'completed'
                    fo.save(update_fields=['status'])
                    print("  FactoryOrder -> completed")
            except Exception:
                pass

        fixed += 1
        print(f"  DONE\n")

    except Exception as exc:
        import traceback
        errors.append((d.id, str(exc)))
        print(f"  ERROR: {exc}")
        traceback.print_exc()
        print()

# ── Summary ───────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"Fixed:   {fixed}")
print(f"Errors:  {len(errors)}")
if errors:
    for did, msg in errors:
        print(f"  Delivery #{did}: {msg}")

# ── Verification ──────────────────────────────────────────────────────────────
print("\n=== Verification ===")
all_broken = (
    DeliveryTracking.objects
    .filter(otp_verified=True, status='delivered')
    .exclude(factory_payout__isnull=False)
)
print(f"Remaining orphaned deliveries: {all_broken.count()}")

wallet = PlatformWallet.get()
print(f"Platform wallet balance: Rs.{wallet.balance}")
print(f"Platform total earned:   Rs.{wallet.total_earned}")

txn_count = WalletTransaction.objects.filter(
    transaction_label__in=['final_payment', 'commission']
).count()
print(f"Audit trail entries (final_payment + commission): {txn_count}")
