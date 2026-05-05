"""
lock_pools.py — Auto-lock expired pools and create deliveries
=============================================================
Run via cron every minute:  * * * * * python manage.py lock_pools

What it does:
1. Finds all 'open' pools whose expires_at has passed.
2. Locks them (status → 'locked').
3. For each active OrderEntry in the pool, creates a DeliveryTracking
   record with the correct SLA:
     - Fast Track (urgent): estimated_arrival = locked_at + 24 hours
     - Pool Mode:           estimated_arrival = locked_at + 7 days
4. Seeds a TruckLocation at the factory origin so tracking works immediately.
5. Marks the pool as 'fulfilled'.
"""

import random
import string
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import OrderPool, OrderEntry, DeliveryTracking, TruckLocation, CommissionLog, PlatformWallet
from decimal import Decimal


class Command(BaseCommand):
    help = 'Lock expired pools and auto-create deliveries per SLA'

    def handle(self, *args, **kwargs):
        now = timezone.now()
        expired = OrderPool.objects.filter(status='open', expires_at__lte=now)
        total_locked = 0
        total_deliveries = 0

        for pool in expired:
            # Lock the pool
            pool.status = 'locked'
            pool.save()
            total_locked += 1

            # Determine SLA based on pool_mode
            is_urgent = pool.pool_mode == 'urgent'
            if is_urgent:
                sla_delta = timedelta(hours=24)
                sla_label = '24h (Fast Track)'
            else:
                sla_delta = timedelta(days=7)
                sla_label = '7 days (Pool Mode)'

            estimated_arrival = now + sla_delta

            # Factory origin for truck start position
            factory = pool.product.factory
            if factory and factory.latitude and factory.longitude:
                start_lat, start_lng = factory.latitude, factory.longitude
            else:
                start_lat, start_lng = 23.0753, 72.6369  # BulkMed warehouse fallback

            # Create one delivery per active entry
            entries = OrderEntry.objects.filter(pool=pool, status='active').select_related('store')
            for entry in entries:
                # Skip if delivery already exists for this store+pool
                if DeliveryTracking.objects.filter(pool=pool, store=entry.store).exists():
                    continue

                otp = ''.join(random.choices(string.digits, k=6))
                delivery = DeliveryTracking.objects.create(
                    pool=pool,
                    store=entry.store,
                    quantity=entry.quantity,
                    status='dispatched',
                    delivery_otp=otp,
                    dispatched_at=now,
                    estimated_arrival=estimated_arrival,
                )
                TruckLocation.objects.update_or_create(
                    delivery=delivery,
                    defaults={'latitude': start_lat, 'longitude': start_lng, 'speed_kmh': 40}
                )
                total_deliveries += 1

            # Create commission log at 15%
            total_value = sum(e.total_amount() for e in entries)
            if total_value > 0 and not pool.commissions.exists():
                commission_amount = round(total_value * Decimal('0.15'), 2)
                CommissionLog.objects.create(
                    pool=pool,
                    total_order_value=total_value,
                    commission_rate=Decimal('15.00'),
                    commission_amount=commission_amount,
                )

            # Mark fulfilled
            pool.status = 'fulfilled'
            pool.dispatched_at = now
            pool.save()

            # ── Auto-recreate a successor pool ────────────────────────────
            try:
                from core.services import maybe_recreate_pool
                new_pool = maybe_recreate_pool(pool)
                if new_pool:
                    self.stdout.write(
                        f'    ↳ Successor pool created: {new_pool.id} '
                        f'(expires {new_pool.expires_at.strftime("%d %b %Y %H:%M")})'
                    )
                else:
                    self.stdout.write(
                        f'    ↳ No successor pool created (stock low or duplicate).'
                    )
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(f'    ↳ Pool re-creation failed: {exc}')
                )

            self.stdout.write(
                f'  Pool {pool.product.name} ({pool.city}) locked → '
                f'{entries.count()} deliveries, SLA: {sla_label}'
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'Done. {total_locked} pool(s) locked, {total_deliveries} delivery(ies) created.'
            )
        )
