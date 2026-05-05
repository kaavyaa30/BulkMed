"""
tasks.py — BulkMed Autonomous Background Tasks
================================================
All tasks run via Celery Beat on a schedule defined in settings.py.

Run the worker:
  celery -A bulkmed worker --loglevel=info

Run the beat scheduler:
  celery -A bulkmed beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler

Task inventory:
  task_lock_expired_pools          — hourly   — lock pools, create deliveries, assign nearest factory
  task_update_seasonal_predictions — daily    — AI demand spike alerts
  task_auto_release_payouts        — every 6h — release pending payouts after 48h dispute-free window
  task_low_stock_alerts            — every 4h — push low-stock notifications to store owners
  task_disable_expiring_products   — daily    — auto-disable products within 30 days of expiry
"""

import random
import string
import logging
from datetime import timedelta
from decimal import Decimal
from math import radians, sin, cos, sqrt, atan2

from celery import shared_task
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY: Haversine distance in kilometres
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """
    Returns the great-circle distance in kilometres between two GPS points.
    Used for smart factory assignment — pick the nearest verified factory.
    """
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _nearest_factory(store, product):
    """
    Returns the nearest verified Factory that manufactures `product`,
    ranked by Haversine distance from `store`.

    Falls back to the product's assigned factory if no GPS data is available,
    and to any verified factory as a last resort.
    """
    from core.models import Factory

    # Prefer the product's own factory first
    assigned = product.factory
    if assigned and assigned.is_verified:
        return assigned

    # No GPS on store — can't compute distance, return any verified factory
    if not store.latitude or not store.longitude:
        return Factory.objects.filter(is_verified=True).first()

    candidates = Factory.objects.filter(
        is_verified=True,
        latitude__isnull=False,
        longitude__isnull=False,
    )
    if not candidates.exists():
        return assigned  # fallback to unverified assigned factory

    nearest, min_dist = None, float('inf')
    for factory in candidates:
        dist = _haversine_km(
            store.latitude, store.longitude,
            factory.latitude, factory.longitude,
        )
        if dist < min_dist:
            min_dist = dist
            nearest = factory

    logger.info(f'Nearest factory to {store.name}: {nearest.name} ({min_dist:.1f} km)')
    return nearest


# ─────────────────────────────────────────────────────────────────────────────
# TASK 1: Lock expired pools + smart factory assignment
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_lock_expired_pools', bind=True, max_retries=3)
def task_lock_expired_pools(self):
    """
    Runs every hour. For each expired open pool:
      1. Locks the pool
      2. Assigns the nearest verified factory (Haversine)
      3. Creates DeliveryTracking + TruckLocation per store
      4. Creates CommissionLog
      5. Creates FactoryOrder for the assigned factory
      6. Pushes WebSocket notification to all stores in the pool
    """
    from core.models import (
        OrderPool, OrderEntry, DeliveryTracking,
        TruckLocation, CommissionLog, FactoryOrder,
    )
    from core.notifications import push_notification

    try:
        now = timezone.now()
        expired = OrderPool.objects.filter(
            status='open', expires_at__lte=now
        ).select_related('product__factory')

        total_locked = 0
        total_deliveries = 0

        for pool in expired:
            pool.status = 'locked'
            pool.save(update_fields=['status'])
            total_locked += 1

            is_urgent = pool.pool_mode == 'urgent'
            sla_delta = timedelta(hours=24) if is_urgent else timedelta(days=7)
            estimated_arrival = now + sla_delta

            entries = OrderEntry.objects.filter(
                pool=pool, status='active'
            ).select_related('store')

            # ── Smart factory assignment ──────────────────────────────────
            # Use the first store's location as the centroid proxy.
            # In production you'd average all store coordinates.
            first_entry = entries.first()
            if first_entry:
                factory = _nearest_factory(first_entry.store, pool.product)
            else:
                factory = pool.product.factory

            if factory and factory.latitude and factory.longitude:
                start_lat, start_lng = factory.latitude, factory.longitude
            else:
                start_lat, start_lng = Decimal('23.0753'), Decimal('72.6369')

            # ── Create deliveries ─────────────────────────────────────────
            for entry in entries:
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
                    defaults={
                        'latitude':  start_lat,
                        'longitude': start_lng,
                        'speed_kmh': 40,
                    }
                )
                total_deliveries += 1

                # Notify store owner
                if entry.store.user_id:
                    push_notification(
                        user_id    = entry.store.user_id,
                        notif_type = 'pool_locked',
                        title      = 'Order Confirmed 🔒',
                        body       = (
                            f'Your pool for {pool.product.name} has locked. '
                            f'Delivery expected by {estimated_arrival.strftime("%d %b, %I:%M %p")}.'
                        ),
                        url        = '/order-history/',
                    )

            # ── CommissionLog ─────────────────────────────────────────────
            total_value = sum(e.total_amount() for e in entries)
            if total_value > 0 and not pool.commissions.exists():
                CommissionLog.objects.create(
                    pool=pool,
                    total_order_value=total_value,
                    commission_rate=Decimal('15.00'),
                    commission_amount=round(total_value * Decimal('0.15'), 2),
                )

            # ── FactoryOrder ──────────────────────────────────────────────
            if factory and not hasattr(pool, 'factory_order'):
                try:
                    FactoryOrder.objects.get_or_create(
                        pool=pool,
                        defaults={
                            'factory':    factory,
                            'total_qty':  pool.total_qty,
                            'total_value': total_value,
                            'status':     'received',
                        }
                    )
                except Exception as e:
                    logger.warning(f'FactoryOrder creation failed for pool {pool.id}: {e}')

            pool.status       = 'fulfilled'
            pool.dispatched_at = now
            pool.save(update_fields=['status', 'dispatched_at'])

            # ── Auto-recreate a successor pool ────────────────────────────
            # Called AFTER the pool is fully saved as 'fulfilled' so the
            # duplicate guard in maybe_recreate_pool sees the correct state.
            try:
                from core.services import maybe_recreate_pool
                new_pool = maybe_recreate_pool(pool)
                if new_pool:
                    logger.info(
                        f'Auto-created successor pool {new_pool.id} for '
                        f'{pool.product.name} ({pool.city})'
                    )
            except Exception as exc:
                logger.error(f'Pool re-creation failed for {pool.id}: {exc}')

            logger.info(
                f'Pool locked: {pool.product.name} ({pool.city}) — '
                f'{entries.count()} deliveries | factory: {factory.name if factory else "none"} | '
                f'SLA: {"24h" if is_urgent else "7 days"}'
            )

        logger.info(
            f'task_lock_expired_pools: {total_locked} pools locked, '
            f'{total_deliveries} deliveries created.'
        )
        return {'locked': total_locked, 'deliveries': total_deliveries}

    except Exception as exc:
        logger.error(f'task_lock_expired_pools failed: {exc}')
        raise self.retry(exc=exc, countdown=60)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 2: Seasonal demand predictions
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_update_seasonal_predictions', bind=True, max_retries=3)
def task_update_seasonal_predictions(self):
    """Generates seasonal demand prediction alerts for all stores. Runs daily at midnight."""
    from core.models import MedicalStore
    from core.prediction import generate_alerts_for_store

    try:
        stores = MedicalStore.objects.all()
        total = 0
        for store in stores:
            count = generate_alerts_for_store(store)
            total += count
            if count:
                logger.info(f'  {store.name}: {count} alert(s) created')

        logger.info(f'task_update_seasonal_predictions: {total} total alerts generated.')
        return {'alerts_created': total}

    except Exception as exc:
        logger.error(f'task_update_seasonal_predictions failed: {exc}')
        raise self.retry(exc=exc, countdown=300)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 3: Auto-release pending payouts after 48-hour dispute-free window
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_auto_release_payouts', bind=True, max_retries=3)
def task_auto_release_payouts(self):
    """
    Runs every 6 hours.

    For every FactoryPayout with status='pending':
      - Check that the associated delivery has been 'delivered' for > 48 hours
      - Check that no open dispute exists on that delivery
      - If both conditions pass: credit factory wallet, mark payout 'paid',
        create a WalletTransaction debit on PlatformWallet, send notification

    This makes the payout lifecycle fully hands-free:
      OTP confirmed → payout pending → 48h dispute window → auto-paid
    """
    from core.models import FactoryPayout, PlatformWallet
    from core.notifications import push_notification

    DISPUTE_WINDOW = timedelta(hours=48)
    cutoff = timezone.now() - DISPUTE_WINDOW

    try:
        # Payouts eligible for auto-release:
        # - status is pending
        # - delivery was confirmed more than 48h ago
        # - no open dispute on the delivery
        eligible = FactoryPayout.objects.filter(
            status='pending',
            delivery__delivered_at__lte=cutoff,
            delivery__otp_verified=True,
        ).select_related('factory', 'pool__product', 'delivery__store')

        released = 0
        for payout in eligible:
            # Double-check: skip if an open dispute exists
            try:
                if payout.delivery.dispute.status == 'open':
                    logger.info(
                        f'Skipping payout #{payout.id} — open dispute on delivery #{payout.delivery_id}'
                    )
                    continue
            except Exception:
                pass  # no dispute record — safe to proceed

            from django.db import transaction as dbt
            with dbt.atomic():
                # 1. Credit factory wallet
                payout.factory.wallet_balance += payout.net_payout
                payout.factory.save(update_fields=['wallet_balance'])

                # 2. Mark payout as paid
                payout.status  = 'paid'
                payout.paid_at = timezone.now()
                payout.save(update_fields=['status', 'paid_at'])

                # 3. Record outflow from platform wallet (audit trail)
                PlatformWallet.get().transactions.create(
                    amount            = payout.net_payout,
                    transaction_type  = 'debit',
                    transaction_label = 'factory_payout',
                    description       = (
                        f'Auto-released payout (48h window) — '
                        f'{payout.pool.product.name} → {payout.factory.name} '
                        f'(FactoryPayout #{payout.id})'
                    ),
                    delivery          = payout.delivery,
                )

                # 4. Notify factory
                if payout.factory.user_id:
                    push_notification(
                        user_id    = payout.factory.user_id,
                        notif_type = 'payout_released',
                        title      = 'Payout Released 💰',
                        body       = (
                            f'₹{payout.net_payout} for {payout.pool.product.name} '
                            f'has been credited to your wallet automatically '
                            f'(48-hour dispute window passed).'
                        ),
                        url        = '/factory/wallet/',
                    )

                released += 1
                logger.info(
                    f'Auto-released payout #{payout.id}: ₹{payout.net_payout} '
                    f'→ {payout.factory.name}'
                )

        logger.info(f'task_auto_release_payouts: {released} payout(s) released.')
        return {'released': released}

    except Exception as exc:
        logger.error(f'task_auto_release_payouts failed: {exc}')
        raise self.retry(exc=exc, countdown=300)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 4: Low-stock alerts — push real-time notifications to store owners
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_low_stock_alerts', bind=True, max_retries=3)
def task_low_stock_alerts(self):
    """
    Runs every 4 hours.

    Scans every store's inventory. For each item where current_stock <= threshold:
      - Pushes a real-time WebSocket notification to the store owner
      - Links directly to the open pool for that product (if one exists)

    Avoids notification spam: only fires if the store hasn't received a
    low-stock alert for this product in the last 12 hours.
    """
    from core.models import Inventory, OrderPool
    from core.notifications import push_notification

    COOLDOWN = timedelta(hours=12)
    cooldown_cutoff = timezone.now() - COOLDOWN

    # Cache: store_id → set of product_ids already alerted recently
    # We use PredictionAlert.created_at as a proxy for "recently notified"
    from core.models import PredictionAlert
    recent_alerts = set(
        PredictionAlert.objects.filter(
            created_at__gte=cooldown_cutoff
        ).values_list('store_id', 'product_id')
    )

    try:
        low_items = Inventory.objects.filter(
            current_stock__lte=models.F('threshold')
        ).select_related('store__user', 'product')

        notified = 0
        for inv in low_items:
            if not inv.store.user_id:
                continue

            # Skip if already notified recently
            if (inv.store_id, inv.product_id) in recent_alerts:
                continue

            # Find an open pool for this product in the store's city
            open_pool = OrderPool.objects.filter(
                product=inv.product,
                city__iexact=inv.store.city,
                status='open',
            ).first()
            pool_url = f'/pools/{open_pool.id}/' if open_pool else '/pools/'

            push_notification(
                user_id    = inv.store.user_id,
                notif_type = 'warning',
                title      = f'Low Stock: {inv.product.name} ⚠️',
                body       = (
                    f'Only {inv.current_stock} {inv.product.unit}(s) left '
                    f'(threshold: {inv.threshold}). '
                    f'{"Join the open pool now →" if open_pool else "Browse pools to restock."}'
                ),
                url        = pool_url,
            )
            notified += 1

        logger.info(f'task_low_stock_alerts: {notified} notification(s) sent.')
        return {'notified': notified}

    except Exception as exc:
        logger.error(f'task_low_stock_alerts failed: {exc}')
        raise self.retry(exc=exc, countdown=120)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 5: Auto-disable products near expiry
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_disable_expiring_products', bind=True, max_retries=3)
def task_disable_expiring_products(self):
    """
    Runs daily at 1 AM.

    Scans all active products with an expiry_date set:
      - Products expiring within 30 days → set is_active=False, notify factory
      - Products already expired → set is_active=False (safety net)

    Inactive products are excluded from pool creation and search results.
    The factory receives a real-time notification for each disabled product.
    """
    from core.models import Product
    from core.notifications import push_notification

    WARN_DAYS = 30
    today = timezone.now().date()
    warn_cutoff = today + timedelta(days=WARN_DAYS)

    try:
        # Products expiring within 30 days OR already expired
        expiring = Product.objects.filter(
            is_active=True,
            expiry_date__isnull=False,
            expiry_date__lte=warn_cutoff,
        ).select_related('factory__user')

        disabled = 0
        for product in expiring:
            product.is_active = False
            product.save(update_fields=['is_active'])
            disabled += 1

            days_left = (product.expiry_date - today).days
            status_label = 'expired' if days_left <= 0 else f'expiring in {days_left} day(s)'

            logger.warning(
                f'Product disabled: {product.name} (SKU: {product.sku_code}) — '
                f'{status_label} | Factory: {product.factory.name if product.factory else "N/A"}'
            )

            # Notify the factory owner
            if product.factory and product.factory.user_id:
                push_notification(
                    user_id    = product.factory.user_id,
                    notif_type = 'error',
                    title      = f'Product Disabled: {product.name}',
                    body       = (
                        f'{product.name} (SKU: {product.sku_code}) has been automatically '
                        f'disabled — {status_label}. '
                        f'Update the expiry date to re-enable it.'
                    ),
                    url        = '/factory/products/',
                )

        logger.info(f'task_disable_expiring_products: {disabled} product(s) disabled.')
        return {'disabled': disabled}

    except Exception as exc:
        logger.error(f'task_disable_expiring_products failed: {exc}')
        raise self.retry(exc=exc, countdown=300)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 6: Minimum Active Pool Guarantee
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='core.tasks.task_ensure_pool_coverage', bind=True, max_retries=3)
def task_ensure_pool_coverage(self):
    """
    Runs every hour at :30 (offset from lock task at :00).

    Guarantees every active product with a factory city always has at least
    one genuinely open pool. Uses product.is_active as the availability
    signal — factory Inventory rows are store-side stock, not factory supply.

    Algorithm:
      1. Find every active Product with a linked factory that has a city.
      2. Check whether a live open pool exists: status='open' AND expires_at > now
      3. If not → create a Pool Mode pool (3-day window) in the factory's city.
    """
    from core.models import Product, OrderPool
    from django.db import transaction as dbt

    now = timezone.now()
    created = 0
    skipped_has_pool = 0
    skipped_no_city  = 0
    errors = []

    try:
        products = (
            Product.objects
            .filter(is_active=True, factory__isnull=False)
            .select_related('factory')
        )

        for product in products:
            factory = product.factory
            city    = (factory.city or '').strip()

            if not city:
                skipped_no_city += 1
                continue

            has_live_pool = OrderPool.objects.filter(
                product        = product,
                city__iexact   = city,
                status         = 'open',
                expires_at__gt = now,
            ).exists()

            if has_live_pool:
                skipped_has_pool += 1
                continue

            expires_at = now + timedelta(days=3)
            try:
                with dbt.atomic():
                    new_pool = OrderPool.objects.create(
                        product              = product,
                        city                 = city,
                        pool_mode            = 'pool',
                        status               = 'open',
                        expires_at           = expires_at,
                        total_qty            = 0,
                        current_member_count = 0,
                    )
                created += 1
                logger.info(
                    f'[pool_coverage] Created pool for {product.name} / {city} — '
                    f'expires {expires_at.strftime("%d %b %Y %H:%M")}'
                )
            except Exception as exc:
                errors.append(f'{product.name}: {exc}')
                logger.error(f'[pool_coverage] Failed for {product.name}: {exc}')

        summary = {
            'created':          created,
            'skipped_has_pool': skipped_has_pool,
            'skipped_no_city':  skipped_no_city,
            'errors':           len(errors),
        }
        logger.info(f'task_ensure_pool_coverage: {summary}')
        return summary

    except Exception as exc:
        logger.error(f'task_ensure_pool_coverage failed: {exc}')
        raise self.retry(exc=exc, countdown=120)
