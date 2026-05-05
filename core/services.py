"""
services.py — BulkMed Business Logic Layer
==========================================
Pure functions that encapsulate domain logic shared across views, tasks,
and management commands. No Django request/response objects here.

Current services:
  - compute_payment_fee_breakdown(subtotal, logistics)
                               — Razorpay 2% + 18% GST fee breakdown
  - maybe_recreate_pool(pool)  — auto-creates a successor pool when one locks
"""

import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT FEE BREAKDOWN
# ─────────────────────────────────────────────────────────────────────────────

# Razorpay charges 2% of the transaction amount as a payment gateway fee.
# GST (18%) is levied on that gateway fee by the Government of India.
# These constants are defined here so a single change propagates everywhere.
RAZORPAY_FEE_RATE = Decimal('0.02')   # 2%
GST_ON_FEE_RATE   = Decimal('0.18')   # 18% GST on the gateway fee


def compute_payment_fee_breakdown(subtotal: Decimal, logistics: Decimal = Decimal('0')) -> dict:
    """
    Compute the transparent Razorpay fee + GST breakdown for any payment.

    The fee base is (subtotal + logistics) — Razorpay charges on the full
    amount being processed, not just the medicine cost.

    Args:
        subtotal   : Pool subtotal (medicine cost) in ₹ — Decimal
        logistics  : Estimated logistics charge in ₹ — Decimal (default 0)

    Returns a dict with all line items as Decimal, rounded to 2 dp:
        subtotal          — medicine cost
        logistics         — logistics charge
        gateway_fee       — 2% of (subtotal + logistics)
        gst_on_fee        — 18% of gateway_fee
        total_fee         — gateway_fee + gst_on_fee
        total_payable     — subtotal + logistics + total_fee
        total_paise       — total_payable converted to paise (int, for Razorpay API)

    Example:
        subtotal  = ₹1000, logistics = ₹50
        base      = ₹1050
        gateway   = ₹21.00  (2% of 1050)
        gst       = ₹3.78   (18% of 21)
        total_fee = ₹24.78
        payable   = ₹1074.78
    """
    TWO_DP = Decimal('0.01')

    subtotal  = Decimal(str(subtotal)).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    logistics = Decimal(str(logistics)).quantize(TWO_DP, rounding=ROUND_HALF_UP)

    base        = subtotal + logistics
    gateway_fee = (base * RAZORPAY_FEE_RATE).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    gst_on_fee  = (gateway_fee * GST_ON_FEE_RATE).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    total_fee   = (gateway_fee + gst_on_fee).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    total_payable = (base + total_fee).quantize(TWO_DP, rounding=ROUND_HALF_UP)

    return {
        'subtotal':      subtotal,
        'logistics':     logistics,
        'gateway_fee':   gateway_fee,
        'gst_on_fee':    gst_on_fee,
        'total_fee':     total_fee,
        'total_payable': total_payable,
        'total_paise':   int(total_payable * 100),  # Razorpay expects paise (integer)
    }




# Minimum number of units that must be available before a successor pool
# is created via maybe_recreate_pool (called from the lock flow).
# For the storefront coverage task, any active product is eligible —
# the factory's supply is implied by the product being active.
MIN_STOCK_FOR_NEW_POOL = 10


def maybe_recreate_pool(pool):
    """
    Attempt to create a successor pool for `pool` after it transitions to
    'locked' or 'fulfilled'.

    Rules enforced here:
    1. Only fires for locked/fulfilled pools — no-op for any other status.
    2. Duplicate guard — skips if an open pool already exists for the same
       product + city + mode combination (prevents double-creation when both
       the Celery task and the management command run close together).
    3. Product must be active (is_active=True).
    4. Inherits all settings from the parent pool:
         - product, city, pool_mode (Fast Track / Pool Mode)
         - expiry window: urgent → +2 hours, pool → +3 days
    5. Wrapped in its own transaction.atomic() so a failure here never
       rolls back the parent pool's lock/fulfil transition.

    Returns:
        OrderPool | None  — the newly created pool, or None if skipped.

    Caller contract:
        Call this AFTER the parent pool has been saved with its final status.
        Do NOT call from inside a post_save signal on OrderPool — that creates
        a recursive signal loop. Call explicitly from the lock task / command.
    """
    from django.db import transaction
    from core.models import OrderPool

    # ── Guard 1: only act on locked/fulfilled pools ───────────────────────
    if pool.status not in ('locked', 'fulfilled'):
        return None

    product = pool.product
    city    = pool.city
    mode    = pool.pool_mode

    # ── Guard 2: product must still be active ─────────────────────────────
    if not product.is_active:
        logger.info(
            f'[pool_recreate] Skipped — product {product.name} is inactive.'
        )
        return None

    # ── Guard 3: duplicate check ──────────────────────────────────────────
    now = timezone.now()
    already_open = OrderPool.objects.filter(
        product      = product,
        city         = city,
        pool_mode    = mode,
        status       = 'open',
        expires_at__gt = now,
    ).exists()

    if already_open:
        logger.info(
            f'[pool_recreate] Skipped — live open pool already exists for '
            f'{product.name} / {city} / {mode}.'
        )
        return None

    # ── Create successor pool ─────────────────────────────────────────────
    if mode == 'urgent':
        window = timedelta(hours=2)
    else:
        window = timedelta(days=3)

    expires_at = now + window

    try:
        with transaction.atomic():
            new_pool = OrderPool.objects.create(
                product              = product,
                city                 = city,
                pool_mode            = mode,
                status               = 'open',
                expires_at           = expires_at,
                total_qty            = 0,
                current_member_count = 0,
            )

        logger.info(
            f'[pool_recreate] Created successor pool #{new_pool.id} for '
            f'{product.name} / {city} / {mode} — expires {expires_at.strftime("%d %b %Y %H:%M")}.'
        )
        return new_pool

    except Exception as exc:
        logger.error(
            f'[pool_recreate] Failed to create successor pool for '
            f'{product.name} / {city}: {exc}'
        )
        return None
