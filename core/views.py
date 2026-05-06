# -*- coding: utf-8 -*-
"""
views.py -- BulkMed Page Controllers
"""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from django.contrib import messages
from django.db.models import Sum
from decimal import Decimal
from .models import (
    MedicalStore, Product, Inventory,
    OrderPool, OrderEntry, DeliveryTracking,
    CommissionLog, PredictionAlert, TruckLocation, PlatformWallet, WalletTransaction,
    Factory
)
from .forms import OrderEntryForm, StoreSetupForm, RegistrationForm, PoolEditForm, StoreEditForm, ProductForm, PoolCreateForm
from .emails import send_order_confirmation, send_cancellation_notice, send_factory_payout_notification

# Generic CRUD API — delegates to AdminCRUDView in crud_api.py
from .crud_api import AdminCRUDView
admin_crud = AdminCRUDView.as_view()


def _release_commission(delivery):
    """
    Mediated payout flow — triggered automatically on OTP confirmation.

    Step 1: Collect full order amount → PlatformWallet (credit, label=final_payment)
    Step 2: Record 15% commission    → PlatformWallet (credit, label=commission)
    Step 3: Create FactoryPayout     → status='pending', then immediately credit
                                       factory.wallet_balance (status → 'paid')
    Step 4: Update CommissionLog     → payout_status='released'
    Step 5: Update OrderEntry        → status='delivered'
    Step 6: Auto-complete FactoryOrder if all deliveries for the pool are now delivered

    Factory resolution order (most specific → most general):
      1. delivery.pool.product.factory  (FK — preferred, always try first)
      2. Factory.objects.filter(name__iexact=factory_name)  (name fallback)
      3. If neither resolves → log an error, skip payout creation, return 0

    RULES:
    - MUST be called from within an existing transaction.atomic() block.
    - Do NOT add an inner transaction.atomic() — nested savepoints silently
      roll back all ledger writes while the outer delivery.save() commits.
    - Idempotent: safe to call twice — guards prevent duplicate records.
    """
    import logging
    from decimal import Decimal
    from .models import FactoryPayout

    logger = logging.getLogger(__name__)

    # Guard: skip if a dispute is still open on this delivery
    try:
        if delivery.dispute.status == 'open':
            return Decimal('0')
    except Exception:
        pass

    # Guard: skip if commission already released for this delivery
    if FactoryPayout.objects.filter(delivery=delivery).exists():
        return Decimal('0')

    # Find the OrderEntry — accept both 'active' and 'delivered' so this
    # function is idempotent if called a second time after Step 5 already ran.
    entry = OrderEntry.objects.filter(
        pool=delivery.pool, store=delivery.store
    ).exclude(
        status__in=('cancelled_free', 'cancelled_penalty')
    ).first()

    order_value = entry.total_amount() if entry else Decimal('0')
    if order_value <= 0:
        return Decimal('0')

    commission = round(order_value * Decimal('0.15'), 2)
    net_payout = round(order_value * Decimal('0.85'), 2)

    # ── Deduct 3PL shipping cost from factory net payout ─────────────────
    # If this delivery was dispatched via a 3PL provider, the shipping cost
    # is deducted from the factory's 85% share. The platform absorbs it as
    # an operating expense (already debited from PlatformWallet at dispatch).
    shipping_cost = getattr(delivery, 'shipping_cost', Decimal('0')) or Decimal('0')
    net_payout    = max(round(net_payout - shipping_cost, 2), Decimal('0'))

    # ── Factory resolution ────────────────────────────────────────────────
    # Try the FK first (fast, no extra query when populated).
    # Fall back to a case-insensitive name lookup so deliveries confirmed
    # before fix_factory_fk was run still produce a correct payout.
    factory = delivery.pool.product.factory

    if factory is None:
        factory_name = (delivery.pool.product.factory_name or '').strip()
        if factory_name:
            from .models import Factory as _Factory
            factory = _Factory.objects.filter(
                name__iexact=factory_name
            ).first()

    if factory is None:
        # Neither FK nor name resolved — log clearly and skip payout creation.
        # The platform wallet steps (Steps 1–2) still run so the ledger is
        # complete; only the factory-side payout is skipped.
        logger.error(
            '_release_commission: could not resolve Factory for delivery #%s '
            '(product: "%s", factory_name: "%s"). '
            'FactoryPayout NOT created. Run fix_factory_fk to repair.',
            delivery.id,
            delivery.pool.product.name,
            delivery.pool.product.factory_name,
        )

    platform = PlatformWallet.get()

    # ── Step 1: Collect full order amount into platform wallet ────────────
    platform.transactions.create(
        amount            = order_value,
        transaction_type  = 'credit',
        transaction_label = 'final_payment',
        order_entry       = entry,
        description       = (
            f'+90% Final Payment — {delivery.pool.product.name} '
            f'→ {delivery.store.name}'
        ),
        delivery          = delivery,
    )

    # ── Step 2: Record 15% platform commission ────────────────────────────
    platform.transactions.create(
        amount            = commission,
        transaction_type  = 'credit',
        transaction_label = 'commission',
        order_entry       = entry,
        description       = (
            f'Platform commission (15%) — {delivery.pool.product.name} '
            f'→ {delivery.store.name}'
        ),
        delivery          = delivery,
    )
    # Recalculate once explicitly (post_save signals also fire, but we want
    # the balance correct before the transaction commits)
    platform.recalculate()

    # ── Step 3: CommissionLog ─────────────────────────────────────────────
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
        log.released_at       = timezone.now()
        log.commission_amount = (log.commission_amount or Decimal('0')) + commission
        log.save()

    # ── Step 4: Create FactoryPayout and immediately credit factory wallet ─
    # The payout is created as 'paid' and the factory wallet is credited in
    # the same atomic block so the factory dashboard shows the balance
    # immediately after OTP confirmation — no separate admin release step.
    if factory:
        try:
            FactoryPayout.objects.create(
                factory             = factory,
                pool                = delivery.pool,
                delivery            = delivery,
                gross_amount        = order_value,
                commission_deducted = commission,
                net_payout          = net_payout,
                status              = 'paid',
                paid_at             = timezone.now(),
            )
            # Credit the factory wallet immediately
            factory.wallet_balance = (factory.wallet_balance or Decimal('0')) + net_payout
            factory.save(update_fields=['wallet_balance'])

            # ── Audit trail: shipping cost deduction line ─────────────────
            # Record the 3PL shipping cost as a separate debit on the platform
            # wallet so the superadmin's Financial Audit Trail shows it as a
            # named line item ("3PL Shipping Cost — Waybill: BLKxxxxxxxx").
            # This is the payout-time record; a matching debit was also written
            # at dispatch time. Both are needed: the dispatch debit shows the
            # cost was incurred; this debit shows it was deducted from the
            # factory's net payout at settlement.
            if shipping_cost > 0:
                waybill_label = (
                    f' | Waybill: {delivery.waybill_id}' if delivery.waybill_id else ''
                )
                platform.transactions.create(
                    amount            = shipping_cost,
                    transaction_type  = 'debit',
                    transaction_label = 'shipping_cost',
                    order_entry       = entry,
                    delivery          = delivery,
                    description       = (
                        f'3PL Shipping Cost — {delivery.logistics_partner or "3PL"}'
                        f'{waybill_label} | '
                        f'{delivery.pool.product.name} → {delivery.store.name} | '
                        f'Deducted from {factory.name} net payout'
                    ),
                )
                platform.recalculate()

            logger.info(
                '_release_commission: FactoryPayout created for "%s" — '
                'net ₹%s credited to wallet (new balance: ₹%s)',
                factory.name, net_payout, factory.wallet_balance,
            )
        except Exception as exc:
            # Log but do not re-raise — the platform ledger (Steps 1–2) has
            # already committed and must not be rolled back by a payout error.
            logger.error(
                '_release_commission: failed to create FactoryPayout for '
                'factory "%s", delivery #%s — %s',
                factory.name, delivery.id, exc,
            )

    # ── Step 5: Mark the OrderEntry as delivered ──────────────────────────
    if entry and entry.status == 'active':
        entry.status = 'delivered'
        entry.save(update_fields=['status'])

    # ── Step 6: Auto-complete FactoryOrder when all deliveries confirmed ──
    try:
        factory_order = delivery.pool.factory_order
        all_done = not DeliveryTracking.objects.filter(
            pool=delivery.pool
        ).exclude(status__in=('delivered', 'failed')).exists()
        if all_done and factory_order.status != 'completed':
            factory_order.status = 'completed'
            factory_order.save(update_fields=['status'])
    except Exception:
        pass

    return commission


def _get_store_or_none(user):
    """Returns the MedicalStore for a user, or None if not set up yet."""
    try:
        return MedicalStore.objects.get(user=user)
    except MedicalStore.DoesNotExist:
        return None


def _is_factory_user(user):
    """Returns True if this user has a linked Factory record."""
    return Factory.objects.filter(user=user).exists()


def _get_factory_or_none(user):
    """Returns the Factory linked to this user via OneToOneField, or None."""
    try:
        return Factory.objects.get(user=user)
    except Factory.DoesNotExist:
        return None


def _fast_track_available(store):
    """
    Returns True if there is a registered Factory/warehouse in the store city.
    Fast Track is only available where we have a local dispatch hub.
    """
    if not store or not store.city:
        return False
    return Factory.objects.filter(city__iexact=store.city.strip()).exists()


def register(request):
    """New user registration -- creates account with Store or Factory profile."""
    if request.user.is_authenticated:
        return smart_redirect(request)
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            account_type  = form.cleaned_data['account_type']
            business_name = form.cleaned_data['business_name']
            license_no    = form.cleaned_data['license_no']
            contact       = form.cleaned_data['contact']
            city          = form.cleaned_data['city']
            if account_type == 'store':
                MedicalStore.objects.create(
                    user=user, name=business_name, license_no=license_no,
                    contact=contact, city=city, address=city,
                )
            else:
                from .models import Factory
                Factory.objects.create(
                    user=user, name=business_name, license_no=license_no,
                    contact=contact, city=city, address=city,
                )
            from django.contrib.auth import login as auth_login
            auth_login(request, user)
            messages.success(request, f"Welcome to BulkMed, {business_name}! Your account is pending verification.")
            if account_type == 'factory':
                return redirect('factory_dashboard')
            return redirect('dashboard')
    else:
        form = RegistrationForm()
    return render(request, 'registration/register.html', {'form': form})


def home(request):
    """
    Public landing page — now lives at /home/.
    Authenticated users are redirected straight to their dashboard.
    """
    if request.user.is_authenticated:
        return smart_redirect(request)
    stats = {
        'verified_stores': MedicalStore.objects.filter(is_verified=True).count(),
        'fulfilled_pools':  OrderPool.objects.filter(status='fulfilled').count(),
        'total_commission': CommissionLog.objects.filter(
            payout_status='released'
        ).aggregate(total=Sum('commission_amount'))['total'] or 0,
    }
    return render(request, 'core/home.html', {'stats': stats})


def login_redirect(request):
    """
    Root URL handler ( / ).
    - Authenticated users → their dashboard (via smart_redirect)
    - Guests → Django's built-in login page
    """
    if request.user.is_authenticated:
        return smart_redirect(request)
    from django.shortcuts import redirect as _redirect
    return _redirect('login')


def smart_redirect(request):
    """
    Role-based redirect after login (also used by LOGIN_REDIRECT_URL = '/go/').
    - Factory user  -- /factory/
    - Store user    -- /dashboard/
    - Staff/admin   -- /dashboard/  (can navigate to both via navbar)
    - No profile    -- /setup/
    """
    if not request.user.is_authenticated:
        return redirect('login')
    if _is_factory_user(request.user):
        return redirect('factory_dashboard')
    if _get_store_or_none(request.user):
        return redirect('dashboard')
    # Staff with no store/factory profile -- control panel
    if request.user.is_staff:
        return redirect('control_panel')
    return redirect('store_setup')


@login_required
def store_setup(request):
    """First-time store profile creation. Redirects factory users and already-setup stores."""
    if _is_factory_user(request.user):
        return redirect('factory_dashboard')
    if _get_store_or_none(request.user):
        return redirect('dashboard')
    if request.method == 'POST':
        form = StoreSetupForm(request.POST)
        if form.is_valid():
            store = form.save(commit=False)
            store.user = request.user
            store.save()
            messages.success(request, "Store profile created! Welcome to BulkMed.")
            return redirect('dashboard')
    else:
        form = StoreSetupForm()
    return render(request, 'core/store_setup.html', {'form': form})


@login_required
def factory_dashboard(request):
    """
    Factory dashboard — scoped entirely to the logged-in factory user.

    Widgets:
      - Incoming Orders   : FactoryOrder with status='received'
      - Active Dispatches : DeliveryTracking dispatched for this factory's pools
      - Pending Payouts   : FactoryPayout with status='pending', with full
                            gross / commission / net breakdown per row
      - Revenue Summary   : this-month earnings, total paid, total pending,
                            plus aggregated gross and commission totals so the
                            factory can see exactly how the 85/15 split works
      - Active Products   : factory products that have at least one open pool
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    if not factory:
        messages.warning(request, "Your factory profile is not set up yet. Please contact admin.")
        return redirect('home')

    from .models import FactoryOrder, FactoryPayout
    from django.db.models import Sum, Q
    from datetime import date

    # ── Incoming Orders ───────────────────────────────────────────────────────
    incoming_orders = (
        FactoryOrder.objects
        .filter(factory=factory, status='received')
        .select_related('pool__product')
        .order_by('-received_at')
    )

    # ── Active Dispatches ─────────────────────────────────────────────────────
    active_dispatches = (
        DeliveryTracking.objects
        .filter(pool__product__factory=factory, status='dispatched')
        .select_related('pool__product', 'store')
        .order_by('-dispatched_at')
    )

    # ── Pending Payouts — with full breakdown ─────────────────────────────────
    # Each FactoryPayout row already stores gross_amount, commission_deducted,
    # and net_payout. We fetch them all and annotate a commission_pct property
    # on each object so the template can render the percentage without extra math.
    pending_payouts = (
        FactoryPayout.objects
        .filter(factory=factory, status='pending')
        .select_related('pool__product', 'delivery__store')
        .order_by('-created_at')
    )

    # Annotate each payout with its commission percentage (always 15, but
    # computed from stored values so it stays accurate if the rate ever changes)
    for payout in pending_payouts:
        if payout.gross_amount and payout.gross_amount > 0:
            payout.commission_pct = round(
                (payout.commission_deducted / payout.gross_amount) * 100, 1
            )
            payout.net_pct = round(
                (payout.net_payout / payout.gross_amount) * 100, 1
            )
        else:
            payout.commission_pct = Decimal('15.0')
            payout.net_pct        = Decimal('85.0')

    # ── Pending payout breakdown totals ──────────────────────────────────────
    # Aggregated so the dashboard can show a single "total pending" card that
    # breaks down into gross / commission / net — no mismatch possible.
    pending_agg = FactoryPayout.objects.filter(
        factory=factory, status='pending'
    ).aggregate(
        pending_gross      = Sum('gross_amount'),
        pending_commission = Sum('commission_deducted'),
        pending_net        = Sum('net_payout'),
    )
    pending_gross_total      = pending_agg['pending_gross']      or Decimal('0')
    pending_commission_total = pending_agg['pending_commission'] or Decimal('0')
    pending_net_total        = pending_agg['pending_net']        or Decimal('0')

    # ── Open disputes (payouts locked) ────────────────────────────────────────
    from .models import Dispute
    open_disputes_count = Dispute.objects.filter(
        delivery__pool__product__factory=factory, status='open'
    ).count()

    # ── Revenue Summary ───────────────────────────────────────────────────────
    today       = date.today()
    month_start = today.replace(day=1)
    revenue = FactoryPayout.objects.filter(factory=factory).aggregate(
        total_earned  = Sum('net_payout'),
        total_pending = Sum('net_payout',        filter=Q(status='pending')),
        total_paid    = Sum('net_payout',        filter=Q(status='paid')),
        month_earned  = Sum('net_payout',        filter=Q(status='paid', paid_at__date__gte=month_start)),
        # Gross and commission totals for the lifetime summary card
        lifetime_gross      = Sum('gross_amount'),
        lifetime_commission = Sum('commission_deducted'),
    )
    total_earned        = revenue['total_earned']        or Decimal('0')
    total_pending       = revenue['total_pending']       or Decimal('0')
    total_paid          = revenue['total_paid']          or Decimal('0')
    month_earned        = revenue['month_earned']        or Decimal('0')
    lifetime_gross      = revenue['lifetime_gross']      or Decimal('0')
    lifetime_commission = revenue['lifetime_commission'] or Decimal('0')

    # ── Active Products ───────────────────────────────────────────────────────
    active_products = (
        Product.objects
        .filter(factory=factory, pools__status='open')
        .distinct()
    )
    for product in active_products:
        open_pools = product.pools.filter(status='open')
        product.open_pool_count = open_pools.count()
        product.total_members   = sum(p.current_member_count for p in open_pools)

    return render(request, 'core/factory_dashboard.html', {
        'factory':              factory,
        'incoming_orders':      incoming_orders,
        'active_dispatches':    active_dispatches,
        'pending_payouts':      pending_payouts,
        # Per-row breakdown is on each payout object (commission_pct, net_pct)
        # Aggregated pending breakdown totals
        'pending_gross_total':      pending_gross_total,
        'pending_commission_total': pending_commission_total,
        'pending_net_total':        pending_net_total,
        # Revenue summary
        'total_earned':         total_earned,
        'total_pending':        total_pending,
        'total_paid':           total_paid,
        'month_earned':         month_earned,
        'lifetime_gross':       lifetime_gross,
        'lifetime_commission':  lifetime_commission,
        # Badge counts for the hero pills
        'incoming_count':       incoming_orders.count(),
        'dispatch_count':       active_dispatches.count(),
        'pending_payout_count': pending_payouts.count(),
        'open_disputes_count':  open_disputes_count,
    })


# -------------------------------- Factory Order Management --------------------------------

@login_required
def factory_order_list(request):
    """
    All FactoryOrders for this factory, grouped by status.
    Supports ?status= filter for quick tab switching.
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryOrder
    status_filter = request.GET.get('status', '')
    qs = FactoryOrder.objects.filter(factory=factory).select_related('pool__product').order_by('-received_at')
    if status_filter:
        qs = qs.filter(status=status_filter)
    from django.db.models import Count
    counts = FactoryOrder.objects.filter(factory=factory).values('status').annotate(n=Count('id'))
    status_counts = {c['status']: c['n'] for c in counts}
    return render(request, 'core/factory_order_list.html', {
        'factory':       factory,
        'orders':        qs,
        'status_filter': status_filter,
        'status_counts': status_counts,
    })


@login_required
def factory_order_detail(request, order_id):
    """
    Detail view for a single FactoryOrder.
    Shows per-store entry breakdown, delivery statuses, and action buttons
    (Accept -- Processing, Dispatch -- Dispatched).
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryOrder
    order = get_object_or_404(FactoryOrder, id=order_id, factory=factory)
    entries    = OrderEntry.objects.filter(pool=order.pool, status='active').select_related('store')
    deliveries = DeliveryTracking.objects.filter(pool=order.pool).select_related('store')
    entry_map = {e.store_id: e for e in entries}
    for d in deliveries:
        d.entry = entry_map.get(d.store_id)
    return render(request, 'core/factory_order_detail.html', {
        'factory':    factory,
        'order':      order,
        'entries':    entries,
        'deliveries': deliveries,
    })


@login_required
def factory_accept_order(request, order_id):
    """
    Factory confirms they can fulfill the order.
    Transitions FactoryOrder status: received → processing.
    Pushes a real-time WebSocket notification to every store in the pool.
    POST only — redirects back to detail page.
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryOrder
    order = get_object_or_404(FactoryOrder, id=order_id, factory=factory)
    if request.method == 'POST':
        if order.status == 'received':
            order.status = 'processing'
            order.save(update_fields=['status'])
            messages.success(request, "Order accepted. Status updated to Processing.")

            # ── Real-time notification → every store in this pool ─────────
            from .notifications import push_notification
            product_name = order.pool.product.name
            active_entries = OrderEntry.objects.filter(
                pool=order.pool, status='active'
            ).select_related('store__user')
            for entry in active_entries:
                push_notification(
                    user_id    = entry.store.user_id,
                    notif_type = 'order_accepted',
                    title      = 'Order Accepted ✅',
                    body       = (
                        f'{factory.name} has accepted your order for '
                        f'{product_name}. It is now being processed.'
                    ),
                    url        = '/order-history/',
                )
        else:
            messages.warning(request, f"Order is already {order.get_status_display()}.")
    return redirect('factory_order_detail', order_id=order.id)


@login_required
def factory_dispatch_order(request, order_id):
    """
    Factory marks the order as dispatched.

    Hybrid logistics flow:
      - If LOGISTICS_PROVIDER != 'mock' (or if the factory explicitly selects 3PL):
          → Calls the 3PL API (Delhivery / Shadowfax) for each delivery.
          → Saves waybill_id, shipping_label_url, shipping_cost on DeliveryTracking.
          → Records a 'shipping_cost' debit on the PlatformWallet audit trail.
      - Always seeds TruckLocation at factory GPS origin so the live map works.
      - Transitions FactoryOrder status: processing → dispatched.
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryOrder
    order = get_object_or_404(FactoryOrder, id=order_id, factory=factory)
    if request.method == 'POST':
        if order.status not in ('received', 'processing'):
            messages.warning(request, f"Cannot dispatch — order is already {order.get_status_display()}.")
            return redirect('factory_order_detail', order_id=order.id)

        # ── Determine logistics mode ──────────────────────────────────────────
        # POST param 'logistics_mode' lets the factory choose per-dispatch.
        # Falls back to the site-wide LOGISTICS_PROVIDER setting.
        from django.conf import settings as _settings
        site_provider = getattr(_settings, 'LOGISTICS_PROVIDER', 'mock').lower()
        logistics_mode = request.POST.get('logistics_mode', site_provider)
        use_3pl = (logistics_mode != 'local')

        from .logistics_provider import get_provider, build_shipment_payload

        with transaction.atomic():
            order.status = 'dispatched'
            order.dispatched_at = timezone.now()
            order.save(update_fields=['status', 'dispatched_at'])

            start_lat = float(factory.latitude)  if factory.latitude  else 23.0753
            start_lng = float(factory.longitude) if factory.longitude else 72.6369
            seeded = 0
            label_urls = []   # collect for success message

            for delivery in DeliveryTracking.objects.filter(pool=order.pool):
                if delivery.status == 'pending':
                    delivery.status       = 'dispatched'
                    delivery.dispatched_at = timezone.now()

                # ── 3PL API call ──────────────────────────────────────────────
                if use_3pl:
                    provider = get_provider()
                    payload  = build_shipment_payload(delivery, factory)
                    result   = provider.create_shipment(payload)

                    if result.success:
                        delivery.delivery_method    = '3pl'
                        delivery.logistics_partner  = result.provider_name
                        delivery.waybill_id         = result.waybill_id
                        delivery.shipping_label_url = result.shipping_label_url
                        delivery.shipping_cost      = result.shipping_rate

                        if result.shipping_label_url:
                            label_urls.append(result.shipping_label_url)

                        # ── Audit trail: debit shipping cost from platform wallet ──
                        # The shipping cost is a platform expense (we absorb it and
                        # deduct it from the factory's net payout at settlement).
                        if result.shipping_rate > 0:
                            PlatformWallet.get().transactions.create(
                                amount            = result.shipping_rate,
                                transaction_type  = 'debit',
                                transaction_label = 'shipping_cost',
                                delivery          = delivery,
                                description       = (
                                    f'3PL Shipping Cost — {result.provider_name} | '
                                    f'Waybill: {result.waybill_id} | '
                                    f'{delivery.pool.product.name} → {delivery.store.name}'
                                ),
                            )
                    else:
                        # 3PL failed — fall back to local delivery silently
                        import logging as _logging
                        _logging.getLogger(__name__).error(
                            'factory_dispatch_order: 3PL create_shipment failed for '
                            'delivery #%s — %s. Falling back to local.',
                            delivery.id, result.error,
                        )
                        delivery.delivery_method = 'local'
                        messages.warning(
                            request,
                            f'3PL booking failed for {delivery.store.name}: {result.error}. '
                            f'Delivery #{delivery.id} will use local tracking.'
                        )
                else:
                    delivery.delivery_method = 'local'

                delivery.save()

                # Always seed TruckLocation so the live map works for local deliveries
                TruckLocation.objects.update_or_create(
                    delivery=delivery,
                    defaults={'latitude': start_lat, 'longitude': start_lng, 'speed_kmh': 40},
                )
                seeded += 1

        # ── Success message ───────────────────────────────────────────────────
        mode_label = '3PL' if use_3pl else 'local driver'
        messages.success(
            request,
            f"Order dispatched via {mode_label}. "
            f"GPS seeded for {seeded} delivery truck{'' if seeded == 1 else 's'}."
        )
        if label_urls:
            messages.info(request, f"{len(label_urls)} shipping label(s) generated. Download from the delivery table.")

        # ── Real-time notification → every store in this pool ─────────────────
        from .notifications import push_notification
        product_name = order.pool.product.name
        active_entries = OrderEntry.objects.filter(
            pool=order.pool, status='active'
        ).select_related('store__user')
        for entry in active_entries:
            push_notification(
                user_id    = entry.store.user_id,
                notif_type = 'order_dispatched',
                title      = 'Order Dispatched 🚚',
                body       = (
                    f'Your {product_name} order has been dispatched by '
                    f'{factory.name}. Track your delivery live.'
                ),
                url        = '/dashboard/',
            )

        try:
            from .invoice import generate_invoice
            generate_invoice(order)
            messages.info(request, "GST invoice generated successfully.")
        except Exception as e:
            messages.warning(request, f"Order dispatched, but invoice generation failed: {e}")

    return redirect('factory_order_detail', order_id=order.id)


@login_required
def download_invoice(request, order_id):
    """
    Serves the GST invoice PDF for a FactoryOrder.
    - Factory users: can download their own orders' invoices
    - Store users: can download invoices for pools they participated in
    - Staff: can download any invoice
    """
    from django.http import FileResponse, Http404
    from .models import FactoryOrder
    order = get_object_or_404(FactoryOrder, id=order_id)
    if request.user.is_staff:
        pass
    elif _is_factory_user(request.user):
        factory = _get_factory_or_none(request.user)
        if order.factory != factory:
            raise Http404
    else:
        store = _get_store_or_none(request.user)
        if not store:
            return redirect('store_setup')
        from .models import OrderEntry
        if not OrderEntry.objects.filter(pool=order.pool, store=store).exists():
            raise Http404
    if not order.invoice_pdf:
        try:
            from .invoice import generate_invoice
            generate_invoice(order)
            order.refresh_from_db()
        except Exception as e:
            messages.error(request, f"Invoice not available: {e}")
            return redirect(request.META.get('HTTP_REFERER', 'dashboard'))
    response = FileResponse(
        order.invoice_pdf.open('rb'),
        content_type='application/pdf',
    )
    response['Content-Disposition'] = (
        f'attachment; filename="INV-{order.id:05d}.pdf"'
    )
    return response


@login_required
def factory_deliveries(request):
    """
    All deliveries for this factory pools.
    Supports ?status= filter (dispatched / delivered / failed).
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    status_filter = request.GET.get('status', '')
    qs = (
        DeliveryTracking.objects
        .filter(pool__product__factory=factory)
        .select_related('pool__product', 'pool__factory_order', 'store')
        .order_by('-dispatched_at')
    )
    if status_filter:
        qs = qs.filter(status=status_filter)
    from django.db.models import Count
    counts = (
        DeliveryTracking.objects
        .filter(pool__product__factory=factory)
        .values('status').annotate(n=Count('id'))
    )
    status_counts = {c['status']: c['n'] for c in counts}
    return render(request, 'core/factory_deliveries.html', {
        'factory':       factory,
        'deliveries':    qs,
        'status_filter': status_filter,
        'status_counts': status_counts,
    })


@login_required
def factory_wallet(request):
    """
    Factory earnings wallet — shows balance, this-month earnings,
    and a full paginated payout history with gross / commission / net breakdown.

    Each FactoryPayout row already stores all three values (gross_amount,
    commission_deducted, net_payout), so no extra computation is needed —
    we just aggregate them for the summary cards and pass the queryset through.
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryPayout
    from django.db.models import Q
    from datetime import date

    payouts = (
        FactoryPayout.objects
        .filter(factory=factory)
        .select_related('pool__product', 'delivery__store')
        .order_by('-created_at')
    )

    today       = date.today()
    month_start = today.replace(day=1)

    agg = FactoryPayout.objects.filter(factory=factory).aggregate(
        # Net payout summaries (what the factory actually receives)
        total_paid         = Sum('net_payout',   filter=Q(status='paid')),
        total_pending      = Sum('net_payout',   filter=Q(status='pending')),
        month_earned       = Sum('net_payout',   filter=Q(status='paid', paid_at__date__gte=month_start)),
        # Gross and commission totals — for the breakdown summary card
        # These let the factory verify the 85/15 split across all transactions
        total_gross        = Sum('gross_amount'),
        total_commission   = Sum('commission_deducted'),
        # Pending breakdown — shown in the "Awaiting Release" summary
        pending_gross      = Sum('gross_amount',        filter=Q(status='pending')),
        pending_commission = Sum('commission_deducted', filter=Q(status='pending')),
    )

    total_paid         = agg['total_paid']         or Decimal('0')
    total_pending      = agg['total_pending']       or Decimal('0')
    month_earned       = agg['month_earned']        or Decimal('0')
    total_gross        = agg['total_gross']         or Decimal('0')
    total_commission   = agg['total_commission']    or Decimal('0')
    pending_gross      = agg['pending_gross']       or Decimal('0')
    pending_commission = agg['pending_commission']  or Decimal('0')

    return render(request, 'core/factory_wallet.html', {
        'factory':           factory,
        'payouts':           payouts,
        # Net summaries
        'total_paid':        total_paid,
        'total_pending':     total_pending,
        'month_earned':      month_earned,
        # Lifetime gross / commission breakdown
        'total_gross':       total_gross,
        'total_commission':  total_commission,
        # Pending breakdown (for the "Awaiting Release" card)
        'pending_gross':     pending_gross,
        'pending_commission': pending_commission,
    })


@login_required
def factory_products(request):
    """
    Factory product catalog -- list, create, and edit products scoped
    strictly to this factory FK. Factories cannot see or touch other
    factories' products.
    GET  /factory/products/              -- list all products for this factory
    POST /factory/products/              -- create a new product
    GET  /factory/products/?edit=<id>    -- pre-fill form for editing
    POST /factory/products/?edit=<id>    -- save edits to existing product
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .forms import FactoryProductForm
    edit_id  = request.GET.get('edit') or request.POST.get('edit_id')
    instance = None
    if edit_id:
        instance = get_object_or_404(Product, id=edit_id, factory=factory)
    if request.method == 'POST':
        form = FactoryProductForm(request.POST, instance=instance)
        if form.is_valid():
            product = form.save(commit=False)
            product.factory      = factory
            product.factory_name = factory.name
            product.save()
            messages.success(
                request,
                f"{'Updated' if instance else 'Created'} product: {product.name}"
            )
            return redirect('factory_products')
    else:
        form = FactoryProductForm(instance=instance)
    products = (
        Product.objects
        .filter(factory=factory)
        .prefetch_related('pools')
        .order_by('name')
    )
    for p in products:
        p.open_pool_count = p.pools.filter(status='open').count()
    return render(request, 'core/factory_products.html', {
        'factory':   factory,
        'products':  products,
        'form':      form,
        'edit_id':   edit_id,
        'instance':  instance,
    })


@login_required
def factory_analytics(request):
    """Demand forecast and revenue trends for the factory."""
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import FactoryOrder, FactoryPayout
    from django.db.models import Count, Sum, Q
    total_orders     = FactoryOrder.objects.filter(factory=factory).count()
    completed_orders = FactoryOrder.objects.filter(factory=factory, status='completed').count()
    total_revenue    = FactoryPayout.objects.filter(factory=factory, status='paid').aggregate(t=Sum('net_payout'))['t'] or Decimal('0')
    total_products   = Product.objects.filter(factory=factory).count()
    active_pools     = OrderPool.objects.filter(product__factory=factory, status='open').count()
    stores_served    = (
        DeliveryTracking.objects
        .filter(pool__product__factory=factory, status='delivered')
        .values('store').distinct().count()
    )
    top_products = list(
        Product.objects
        .filter(factory=factory)
        .annotate(
            order_count=Count('pools__entries', filter=Q(pools__status='fulfilled')),
            total_qty=Sum('pools__total_qty', filter=Q(pools__status='fulfilled')),
            open_pools=Count('pools', filter=Q(pools__status='open')),
        )
        .order_by('-order_count')[:10]
    )
    for p in top_products:
        p.total_qty = p.total_qty or 0
    recent_pools = (
        OrderPool.objects
        .filter(product__factory=factory, status='fulfilled')
        .select_related('product')
        .order_by('-dispatched_at')[:20]
    )
    return render(request, 'core/factory_analytics.html', {
        'factory':          factory,
        'total_orders':     total_orders,
        'completed_orders': completed_orders,
        'total_revenue':    total_revenue,
        'total_products':   total_products,
        'active_pools':     active_pools,
        'stores_served':    stores_served,
        'top_products':     top_products,
        'recent_pools':     recent_pools,
    })


@csrf_exempt
def update_truck_location(request, delivery_id):
    """
    Factory/driver endpoint to push live GPS coordinates.

    Accepts two auth methods:
      1. Session auth  — factory user or staff logged in via browser
      2. Token auth    — ?token=<DeliveryTracking.driver_token> in query string
         (used by the PWA driver app which runs outside a browser session)

    POST body (JSON): { latitude, longitude, speed_kmh? }
    Returns: { ok, delivery_id, status }
    """
    from django.http import JsonResponse
    import json

    delivery = get_object_or_404(DeliveryTracking, id=delivery_id)

    # ── Auth: session OR driver token ─────────────────────────────────────
    token = request.GET.get('token', '')
    token_valid = token and token == delivery.driver_token
    session_valid = (
        request.user.is_authenticated
        and (request.user.is_staff or _is_factory_user(request.user))
    )
    if not token_valid and not session_valid:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
        lat  = float(data['latitude'])
        lng  = float(data['longitude'])
    except (KeyError, ValueError, json.JSONDecodeError):
        return JsonResponse({'error': 'latitude and longitude are required numbers.'}, status=400)

    speed = float(data.get('speed_kmh', 0))

    TruckLocation.objects.update_or_create(
        delivery=delivery,
        defaults={'latitude': lat, 'longitude': lng, 'speed_kmh': speed},
    )
    if delivery.status == 'pending':
        delivery.status = 'dispatched'
        delivery.dispatched_at = timezone.now()
        delivery.save(update_fields=['status', 'dispatched_at'])

    return JsonResponse({
        'ok':         True,
        'delivery_id': delivery.id,
        'status':      delivery.status,
    })


@login_required
def dashboard(request):
    """
    Store owner dashboard — or Executive Admin Overview for superusers.
    Factory users are redirected to their own dashboard.
    """
    if _is_factory_user(request.user):
        return redirect('factory_dashboard')

    # ── Superuser: show Executive Admin Overview instead of store widgets ──
    if request.user.is_superuser:
        from .models import FactoryOrder, FactoryPayout, Dispute
        from django.db.models import Q, Count
        from datetime import date, timedelta
        import json as _json

        platform_wallet = PlatformWallet.get()

        # Revenue summary
        total_revenue   = CommissionLog.objects.filter(
            payout_status='released'
        ).aggregate(total=Sum('commission_amount'))['total'] or 0

        # Funds in escrow = sum of all active OrderEntry escrow amounts
        from django.db.models import Sum as _Sum
        escrow_balance = OrderEntry.objects.filter(
            status='active'
        ).aggregate(total=_Sum('escrow_amount'))['total'] or 0

        # Available balance = platform wallet balance (already computed from transactions)
        available_balance = platform_wallet.balance

        # Key counts
        open_disputes   = Dispute.objects.filter(status='open').count()
        pending_payouts = FactoryPayout.objects.filter(status='pending').count()
        active_pools    = OrderPool.objects.filter(status='open').count()
        total_stores    = MedicalStore.objects.filter(is_verified=True).count()
        total_factories = Factory.objects.filter(is_verified=True).count()
        pending_orders  = FactoryOrder.objects.filter(status='received').count()

        # Recent disputes — no longer shown on dashboard (managed in Control Panel)
        # recent_disputes kept for potential future use

        # Recent pending payouts — no longer shown on dashboard
        # recent_payouts kept for potential future use

        # ── Revenue chart: daily commission for last 30 days ──────────────
        today      = date.today()
        thirty_ago = today - timedelta(days=29)
        daily_qs   = (
            CommissionLog.objects
            .filter(payout_status='released', released_at__date__gte=thirty_ago)
            .extra(select={'day': "date(released_at)"})
            .values('day')
            .annotate(total=Sum('commission_amount'))
            .order_by('day')
        )
        # Build a full 30-day series (fill missing days with 0)
        day_map = {str(r['day']): float(r['total']) for r in daily_qs}
        chart_labels = []
        chart_values = []
        for i in range(30):
            d = thirty_ago + timedelta(days=i)
            chart_labels.append(d.strftime('%d %b'))
            chart_values.append(day_map.get(str(d), 0))
        chart_labels_json = _json.dumps(chart_labels)
        chart_values_json = _json.dumps(chart_values)

        # ── Registration queue: unverified stores + factories ─────────────
        unverified_stores    = list(MedicalStore.objects.filter(is_verified=False).order_by('-created_at')[:10])
        unverified_factories = list(Factory.objects.filter(is_verified=False).order_by('-created_at')[:10])
        pending_registrations = len(unverified_stores) + len(unverified_factories)

        # ── AI demand signals: top seasonal spikes across all products ────
        from .prediction import SEASONAL_RULES, get_seasonal_multiplier
        ai_demand_signals = []
        seen = set()
        spike_date = today + timedelta(days=15)
        for product in Product.objects.filter(is_active=True).only('name', 'id'):
            multiplier, reason = get_seasonal_multiplier(product.name, spike_date)
            if multiplier > 1.0 and product.name not in seen:
                seen.add(product.name)
                ai_demand_signals.append({
                    'product':    product.name,
                    'reason':     reason,
                    'multiplier': multiplier,
                })
        # Sort by highest multiplier, cap at 5
        ai_demand_signals.sort(key=lambda x: x['multiplier'], reverse=True)
        ai_demand_signals = ai_demand_signals[:5]

        # ── Audit feed: last 5 wallet transactions as activity proxy ──────
        audit_feed = WalletTransaction.objects.order_by('-created_at')[:5]

        return render(request, 'core/dashboard.html', {
            'is_admin_view':         True,
            'platform_wallet':       platform_wallet,
            'total_revenue':         total_revenue,
            'escrow_balance':        escrow_balance,
            'available_balance':     available_balance,
            'open_disputes':         open_disputes,
            'pending_payouts':       pending_payouts,
            'active_pools':          active_pools,
            'total_stores':          total_stores,
            'total_factories':       total_factories,
            'pending_orders':        pending_orders,
            'chart_labels_json':     chart_labels_json,
            'chart_values_json':     chart_values_json,
            'unverified_stores':     unverified_stores,
            'unverified_factories':  unverified_factories,
            'pending_registrations': pending_registrations,
            'ai_demand_signals':     ai_demand_signals,
            'audit_feed':            audit_feed,
        })

    # ── Normal store user ──────────────────────────────────────────────────
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    low_stock = [i for i in Inventory.objects.filter(store=store).select_related('product') if i.is_low_stock()]
    alerts = PredictionAlert.objects.filter(store=store, is_read=False).select_related('product')
    my_entries = OrderEntry.objects.filter(store=store, status='active').select_related('pool__product')
    active_deliveries = DeliveryTracking.objects.filter(
        store=store, status='dispatched'
    ).select_related('pool__product').order_by('-dispatched_at')
    low_stock_pids = [i.product_id for i in low_stock]
    open_pool_map = {
        str(p['product_id']): str(p['id'])
        for p in OrderPool.objects.filter(product_id__in=low_stock_pids, status='open').values('product_id', 'id')
    }
    for inv in low_stock:
        inv.open_pool_id = open_pool_map.get(str(inv.product_id))

    # AI Predictions count = unread PredictionAlerts + low-stock items that have
    # no alert yet. This ensures the widget shows a non-zero count even when the
    # daily Celery prediction task hasn't run (e.g., in development without Celery).
    alerted_product_ids = set(alerts.values_list('product_id', flat=True))
    low_stock_without_alert = [i for i in low_stock if i.product_id not in alerted_product_ids]
    ai_prediction_count = alerts.count() + len(low_stock_without_alert)

    return render(request, 'core/dashboard.html', {
        'store':               store,
        'low_stock':           low_stock,
        'alerts':              alerts,
        'ai_prediction_count': ai_prediction_count,
        'my_entries':          my_entries,
        'active_deliveries':   active_deliveries,
    })


@login_required
def ai_predictions(request):
    """Dedicated AI Predictions page -- seasonal demand forecast for the store."""
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    from datetime import date, timedelta
    from .prediction import SEASONAL_RULES
    alerts = PredictionAlert.objects.filter(store=store).select_related('product').order_by('-created_at')
    today = date.today()
    inv_map = {
        inv.product.name.lower(): inv
        for inv in Inventory.objects.filter(store=store).select_related('product')
    }
    MONTH_SEASON = {}
    all_entries = []
    for (start, end), rules in SEASONAL_RULES.items():
        season = _season_label(start, end)
        if start <= end:
            season_months = list(range(start, end + 1))
        else:
            season_months = list(range(start, 13)) + list(range(1, end + 1))
        for m in season_months:
            MONTH_SEASON[m] = season
        for keyword, reason, multiplier in rules:
            matched = Product.objects.filter(name__icontains=keyword)
            if matched.exists():
                for product in matched:
                    inv = inv_map.get(product.name.lower())
                    threshold = inv.threshold if inv else 10
                    predicted = int(threshold * multiplier)
                    all_entries.append({
                        'product_name': product.name,
                        'generic': product.generic_name,
                        'category': product.category,
                        'reason': reason,
                        'multiplier': multiplier,
                        'season': season,
                        'season_months': season_months,
                        'current_stock': inv.current_stock if inv else None,
                        'predicted_demand': predicted,
                        'sufficient': (inv.current_stock >= predicted) if inv else False,
                        'in_inventory': inv is not None,
                        'pool_url': None,
                    })
            else:
                all_entries.append({
                    'product_name': keyword.title(),
                    'generic': '',
                    'category': '',
                    'reason': reason,
                    'multiplier': multiplier,
                    'season': season,
                    'season_months': season_months,
                    'current_stock': None,
                    'predicted_demand': None,
                    'sufficient': False,
                    'in_inventory': False,
                    'pool_url': None,
                })
    open_pools = {str(p.product_id): str(p.id) for p in OrderPool.objects.filter(status='open').only('id', 'product_id')}
    for entry in all_entries:
        prod = Product.objects.filter(name=entry['product_name']).first()
        if prod:
            entry['pool_url'] = open_pools.get(str(prod.id))
    active_season = MONTH_SEASON.get(today.month)
    import calendar as cal_mod
    months = []
    for i in range(1, 13):
        if 6 <= i <= 9:
            cls, icon = 'monsoon', '🌧'
        elif i >= 11 or i <= 2:
            cls, icon = 'winter', '❄'
        else:
            cls, icon = 'normal', '☀'
        months.append({
            'num': i,
            'label': cal_mod.month_abbr[i],
            'cls': cls,
            'icon': icon,
            'active': i == today.month,
            'has_data': i in MONTH_SEASON,
        })
    monsoon_entries = [e for e in all_entries if e['season'] == 'Monsoon']
    winter_entries  = [e for e in all_entries if e['season'] == 'Winter']
    return render(request, 'core/ai_predictions.html', {
        'store': store,
        'alerts': alerts,
        'monsoon_entries': monsoon_entries,
        'winter_entries': winter_entries,
        'all_entries_json': _entries_to_json(all_entries),
        'active_season': active_season,
        'today': today,
        'spike_date': today + timedelta(days=15),
        'months': months,
    })


def _entries_to_json(entries):
    import json
    safe = []
    for e in entries:
        safe.append({
            'product_name': e['product_name'],
            'generic': e['generic'],
            'reason': e['reason'],
            'multiplier': str(e['multiplier']),
            'season': e['season'],
            'season_months': e['season_months'],
            'current_stock': e['current_stock'],
            'predicted_demand': e['predicted_demand'],
            'sufficient': e['sufficient'],
            'in_inventory': e['in_inventory'],
            'pool_url': e['pool_url'],
        })
    return json.dumps(safe)


def _season_label(start, end):
    if start == 3:
        return 'Summer'
    if start == 6:
        return 'Monsoon'
    if start == 11:
        return 'Winter'
    return f'Season ({start}-{end})'


def _month_range_str(start, end):
    import calendar
    if start <= end:
        return f"{calendar.month_abbr[start]} -- {calendar.month_abbr[end]}"
    return f"{calendar.month_abbr[start]} -- {calendar.month_abbr[end]} (wraps)"


@login_required
def low_stock_list(request):
    """Dedicated page listing all low-stock inventory items for the store."""
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    low_stock = [i for i in Inventory.objects.filter(store=store).select_related('product') if i.is_low_stock()]
    low_stock_pids = [i.product_id for i in low_stock]
    open_pool_map = {
        str(p['product_id']): str(p['id'])
        for p in OrderPool.objects.filter(product_id__in=low_stock_pids, status='open').values('product_id', 'id')
    }
    for inv in low_stock:
        inv.open_pool_id = open_pool_map.get(str(inv.product_id))
    return render(request, 'core/low_stock_list.html', {'store': store, 'low_stock': low_stock})


@login_required
def medicine_autocomplete(request):
    """JSON endpoint for search autocomplete -- returns matching products with name, generic, SKU."""
    from django.http import JsonResponse
    from django.db.models import Q
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse([], safe=False)
    products = Product.objects.filter(
        Q(name__icontains=q) |
        Q(generic_name__icontains=q) |
        Q(sku_code__icontains=q) |
        Q(hsn_code__icontains=q) |
        Q(barcode__icontains=q)
    ).values('name', 'generic_name', 'sku_code')[:10]
    results = [
        {
            'label': f"{p['name']}{' -- ' + p['generic_name'] if p['generic_name'] else ''}{' | SKU: ' + p['sku_code'] if p['sku_code'] else ''}",
            'value': p['name'],
        }
        for p in products
    ]
    return JsonResponse(results, safe=False)


@login_required
def search_medicines(request):
    """Search medicines by name and show all active pools with price comparison."""
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    query = request.GET.get('q', '').strip()
    pools = []
    if query:
        from django.db.models import Q
        matching_products = Product.objects.filter(
            Q(name__icontains=query) |
            Q(generic_name__icontains=query) |
            Q(sku_code__icontains=query) |
            Q(hsn_code__icontains=query) |
            Q(barcode__icontains=query)
        )
        pools = (OrderPool.objects
                 .filter(status='open', product__in=matching_products)
                 .select_related('product')
                 .order_by('product__name'))
        my_pool_ids = set(
            OrderEntry.objects.filter(store=store, status='active')
            .values_list('pool_id', flat=True)
        )
        pools = sorted(pools, key=lambda p: p.current_discount(), reverse=True)
        for pool in pools:
            disc = pool.current_discount()
            pool.discounted_price = round(pool.product.base_price * (1 - Decimal(str(disc)) / 100), 2)
            pool.already_joined = pool.id in my_pool_ids
            stores_needed, next_disc = pool.next_tier_info()
            pool.stores_needed = stores_needed
            pool.next_discount = next_disc
    return render(request, 'core/search_medicines.html', {
        'store': store, 'query': query, 'pools': pools,
    })


@login_required
def order_history(request):
    """Full order history -- all entries (active, cancelled) for the store."""
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    entries = (OrderEntry.objects
               .filter(store=store)
               .select_related('pool__product', 'pool__factory_order')
               .prefetch_related('pool__deliveries')
               .order_by('-joined_at'))
    return render(request, 'core/order_history.html', {'store': store, 'entries': entries})


@login_required
def store_wallet(request):
    """
    Shows: current balance, Add Funds (Razorpay), escrow history,
    penalty charges, refunds, and Razorpay top-ups — full ledger.
    """
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    from django.db.models import Q
    from .models import StoreTopUp

    entries = (OrderEntry.objects
               .filter(store=store)
               .select_related('pool__product')
               .order_by('-joined_at'))

    transactions = []

    # ── Razorpay top-ups ──────────────────────────────────────────────────────
    for topup in StoreTopUp.objects.filter(store=store).order_by('-created_at'):
        transactions.append({
            'date':        topup.created_at,
            'type':        'credit',
            'amount':      topup.amount,
            'description': f'Wallet top-up via Razorpay (ID: {topup.razorpay_payment_id or "—"})',
            'status':      'topup',
        })

    # ── Order-based transactions ──────────────────────────────────────────────
    for e in entries:
        if e.escrow_amount and e.escrow_amount > 0:
            transactions.append({
                'date':        e.joined_at,
                'type':        'debit',
                'amount':      e.escrow_amount,
                'description': f'Escrow deposit -- {e.pool.product.name} ({e.pool.city})',
                'status':      e.status,
            })
        if e.penalty_charged and e.penalty_charged > 0:
            transactions.append({
                'date':        e.cancelled_at or e.joined_at,
                'type':        'debit',
                'amount':      e.penalty_charged,
                'description': f'Cancellation penalty -- {e.pool.product.name}',
                'status':      'penalty',
            })
        if e.status == 'cancelled_free' and e.escrow_amount and e.escrow_amount > 0:
            transactions.append({
                'date':        e.cancelled_at or e.joined_at,
                'type':        'credit',
                'amount':      e.escrow_amount,
                'description': f'Escrow refund -- {e.pool.product.name}',
                'status':      'refund',
            })

    transactions.sort(key=lambda x: x['date'], reverse=True)

    total_deposited = sum(t['amount'] for t in transactions if t['type'] == 'debit')
    total_refunded  = sum(t['amount'] for t in transactions if t['type'] == 'credit')
    active_escrow   = sum(
        e.escrow_amount for e in entries
        if e.status == 'active' and e.escrow_amount
    )
    return render(request, 'core/store_wallet.html', {
        'store':           store,
        'transactions':    transactions,
        'total_deposited': total_deposited,
        'total_refunded':  total_refunded,
        'active_escrow':   active_escrow,
    })


@login_required
def pool_list(request):
    """
    Lists order pools available to the requesting store.

    Privacy rules:
    - Public listing shows ONLY pools that are status='open' AND have not
      yet expired (expires_at > now). This hides pools whose timer has run
      out but whose status hasn't been updated by the Celery task yet.
    - Staff see all pools regardless of status, but expired-but-still-open
      pools are annotated so the template can flag them visually.
    - A store that has joined a locked/fulfilled pool can still see it via
      pool_detail (membership check there), but it won't appear in this list.
    """
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')

    now = timezone.now()

    if request.user.is_staff:
        # Staff see everything — annotate stale pools so the template can
        # show a warning badge without a separate query.
        from django.db.models import Case, When, BooleanField
        pools = (
            OrderPool.objects
            .select_related('product')
            .annotate(
                is_stale=Case(
                    When(status='open', expires_at__lte=now, then=True),
                    default=False,
                    output_field=BooleanField(),
                )
            )
            .order_by('status', 'expires_at')
        )
    else:
        # Stores only see pools that are genuinely open and not yet expired.
        # expires_at__gt=now is the real-time guard — it hides pools whose
        # countdown has hit zero even if the cron job hasn't locked them yet.
        pools = (
            OrderPool.objects
            .filter(status='open', expires_at__gt=now)
            .select_related('product')
            .order_by('expires_at')
        )

    return render(request, 'core/pool_list.html', {'pools': pools, 'store': store})


@login_required
@login_required
def pool_check_stock(request, pool_id):
    """
    Step 1 of Pay & Join: validate stock and create a Razorpay order for the escrow amount.

    POST body: { quantity: int, mode: 'pool'|'urgent' }

    Returns on success:
      { ok: true, order_id, amount, currency, key_id, name, email, contact,
        escrow_amount, quantity, mode, discount }

    Returns on failure:
      { ok: false, error: '...' }

    The escrow amount (10% of order value) is charged via Razorpay.
    The OrderEntry is NOT created here — it's created in pool_join_after_payment
    only after Razorpay confirms the payment.
    """
    import traceback
    import json
    import razorpay
    from decimal import Decimal
    from django.http import JsonResponse
    from django.conf import settings

    # ── Auth & method guards ──────────────────────────────────────────────────
    store = _get_store_or_none(request.user)
    if not store:
        return JsonResponse({'ok': False, 'error': 'Store profile not found.'}, status=400)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only.'}, status=405)

    # ── Parse JSON body ───────────────────────────────────────────────────────
    try:
        data     = json.loads(request.body)
        quantity = int(data.get('quantity', 0))
        mode     = data.get('mode', 'pool')
    except (ValueError, json.JSONDecodeError) as exc:
        traceback.print_exc()
        return JsonResponse({'ok': False, 'error': f'Invalid request data: {exc}'}, status=400)

    if quantity < 4:
        return JsonResponse({'ok': False, 'error': 'Minimum order quantity is 4 units.'}, status=400)
    if mode not in ('pool', 'urgent'):
        return JsonResponse({'ok': False, 'error': 'Invalid mode.'}, status=400)

    # ── Fetch pool ────────────────────────────────────────────────────────────
    pool = get_object_or_404(OrderPool, id=pool_id)

    # ── Pool state validation ─────────────────────────────────────────────────
    if pool.status != 'open':
        return JsonResponse({'ok': False, 'error': 'This pool is no longer accepting orders.'}, status=400)

    # Real-time expiry check — pool may have expired since the page loaded
    if pool.expires_at <= timezone.now():
        return JsonResponse({'ok': False, 'error': 'This pool has expired. Please refresh the page.'}, status=400)

    if pool.is_full():
        return JsonResponse({'ok': False, 'error': 'This pool is full (15 stores maximum).'}, status=400)
    if OrderEntry.objects.filter(pool=pool, store=store, status='active').exists():
        return JsonResponse({'ok': False, 'error': 'You have already joined this pool.'}, status=400)
    if mode == 'urgent' and not _fast_track_available(store):
        return JsonResponse({'ok': False, 'error': 'Fast Track is not available in your city.'}, status=400)

    # ── Stock check (before taking any money) ─────────────────────────────────
    # NOTE: factory.user may be None for warehouse hubs — skip stock check in that case.
    factory = pool.product.factory
    if factory and factory.user_id:
        from .models import Inventory as FactoryInventory
        factory_stock = FactoryInventory.objects.filter(
            store__user=factory.user, product=pool.product
        ).first()
        if factory_stock and factory_stock.current_stock < quantity:
            return JsonResponse({
                'ok':    False,
                'error': (
                    f'Insufficient stock available. '
                    f'Only {factory_stock.current_stock} {pool.product.unit}(s) in stock — '
                    f'you requested {quantity}. Please reduce your quantity.'
                ),
            }, status=400)

    # ── Calculate escrow amount ───────────────────────────────────────────────
    try:
        from .services import compute_payment_fee_breakdown

        discount      = pool.current_discount()
        disc_price    = pool.product.base_price * (1 - Decimal(str(discount)) / 100)
        total_amount  = round(disc_price * quantity, 2)
        escrow_amount = round(total_amount * Decimal('0.10'), 2)

        # Logistics is dynamic (set to 0 here; can be wired to a real estimate later)
        logistics_estimate = Decimal('0')

        # Fee breakdown on the escrow amount (the actual Razorpay charge)
        breakdown = compute_payment_fee_breakdown(
            subtotal=escrow_amount,
            logistics=logistics_estimate,
        )
        escrow_paise = breakdown['total_paise']  # Razorpay uses paise

        if escrow_paise < 100:  # Razorpay minimum is ₹1
            escrow_paise = 100
    except Exception as exc:
        traceback.print_exc()
        return JsonResponse({'ok': False, 'error': f'Price calculation error: {exc}'}, status=500)

    # ── Create Razorpay order for the escrow amount ───────────────────────────
    try:
        client   = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        rz_order = client.order.create({
            'amount':   escrow_paise,
            'currency': 'INR',
            'receipt':  f'pool_{str(pool_id)[:8]}_store_{store.id}',
            'notes': {
                'pool_id':  str(pool_id),
                'store_id': store.id,
                'quantity': quantity,
                'mode':     mode,
                'type':     'pool_escrow',
            },
        })
    except Exception as exc:
        traceback.print_exc()
        return JsonResponse({'ok': False, 'error': f'Payment gateway error: {exc}'}, status=400)

    return JsonResponse({
        'ok':            True,
        'order_id':      rz_order['id'],
        'amount':        escrow_paise,
        'currency':      'INR',
        'key_id':        settings.RAZORPAY_KEY_ID,
        'name':          store.name,
        'email':         request.user.email,
        'contact':       store.contact or '',
        'escrow_amount': str(escrow_amount),
        'quantity':      quantity,
        'mode':          mode,
        'discount':      discount,
        'product_name':  pool.product.name,
        # Full order context for the fee breakdown table
        'order_subtotal':  str(total_amount),
        'logistics':       str(logistics_estimate),
        # Fee breakdown on the escrow charge
        'fee_breakdown': {
            'subtotal':      str(breakdown['subtotal']),
            'logistics':     str(breakdown['logistics']),
            'gateway_fee':   str(breakdown['gateway_fee']),
            'gst_on_fee':    str(breakdown['gst_on_fee']),
            'total_fee':     str(breakdown['total_fee']),
            'total_payable': str(breakdown['total_payable']),
        },
    })


@login_required
def pool_join_after_payment(request, pool_id):
    """
    Step 2 of Pay & Join: verify Razorpay payment and create the OrderEntry.

    Called only after Razorpay confirms payment in the frontend handler.

    POST body: {
        razorpay_order_id, razorpay_payment_id, razorpay_signature,
        quantity, mode, escrow_amount
    }

    On success: creates OrderEntry, deducts escrow from wallet (since Razorpay
    already charged the user, we credit the wallet first then deduct escrow),
    returns { ok: true, redirect_url }.

    On failure: returns { ok: false, error } — OrderEntry is NOT created.
    """
    from django.http import JsonResponse
    from django.conf import settings
    from decimal import Decimal
    import razorpay, json

    store = _get_store_or_none(request.user)
    if not store:
        return JsonResponse({'ok': False, 'error': 'Store profile not found.'}, status=400)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only.'}, status=405)

    pool = get_object_or_404(OrderPool, id=pool_id)
    data = json.loads(request.body)

    # ── Verify Razorpay signature ─────────────────────────────────────────────
    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id':   data['razorpay_order_id'],
            'razorpay_payment_id': data['razorpay_payment_id'],
            'razorpay_signature':  data['razorpay_signature'],
        })
    except Exception:
        return JsonResponse({
            'ok':    False,
            'error': 'Payment verification failed. Your card has not been charged. Please try again.',
        }, status=400)

    quantity      = int(data.get('quantity', 0))
    mode          = data.get('mode', 'pool')
    escrow_amount = Decimal(str(data.get('escrow_amount', '0')))

    # ── Final server-side guards (inside atomic — race condition safe) ─────────
    with transaction.atomic():
        # Re-check pool is still open and not full
        pool.refresh_from_db()
        if pool.status != 'open':
            return JsonResponse({'ok': False, 'error': 'Pool closed while payment was processing.'}, status=400)
        if pool.is_full():
            return JsonResponse({'ok': False, 'error': 'Pool became full while payment was processing.'}, status=400)
        if OrderEntry.objects.filter(pool=pool, store=store, status='active').exists():
            return JsonResponse({'ok': False, 'error': 'You have already joined this pool.'}, status=400)

        # Stock re-check with row lock
        factory = pool.product.factory
        if factory:
            from .models import Inventory as FactoryInventory
            factory_stock = FactoryInventory.objects.select_for_update().filter(
                store__user=factory.user, product=pool.product
            ).first()
            if factory_stock and factory_stock.current_stock < quantity:
                return JsonResponse({
                    'ok':    False,
                    'error': f'Stock ran out while payment was processing. You will receive a full refund within 5-7 business days.',
                }, status=400)

        # ── Credit wallet with escrow (Razorpay charged the user) ────────────
        # The escrow amount was paid via Razorpay. We credit it to the wallet
        # so the standard escrow deduction logic works correctly.
        store.refresh_from_db()
        store.wallet_balance += escrow_amount
        store.save(update_fields=['wallet_balance'])

        # Record the top-up
        from .models import StoreTopUp
        StoreTopUp.objects.create(
            store               = store,
            amount              = escrow_amount,
            razorpay_order_id   = data.get('razorpay_order_id', ''),
            razorpay_payment_id = data.get('razorpay_payment_id', ''),
        )

        # ── Create OrderEntry ─────────────────────────────────────────────────
        any_entry = OrderEntry.objects.filter(pool=pool, store=store).first()
        if any_entry:
            entry = any_entry
            entry.quantity      = quantity
            entry.mode          = mode
            entry.status        = 'active'
            entry.cancelled_at  = None
            entry.penalty_charged = 0
        else:
            entry = OrderEntry(pool=pool, store=store, quantity=quantity, mode=mode)

        entry.unit_price_at_order = pool.product.base_price
        entry.discount_applied    = pool.current_discount()
        entry.estimated_arrival   = entry.compute_estimated_arrival()
        entry.escrow_amount       = escrow_amount

        # Deduct escrow from wallet
        store.wallet_balance -= escrow_amount
        store.save(update_fields=['wallet_balance'])
        entry.save()

        pool.total_qty           += quantity
        pool.current_member_count += 1
        pool.save()

        # ── Audit Trail: log the 10% escrow advance ───────────────────────
        # Compute the gateway fee that was charged on this Razorpay transaction
        # so the admin ledger can reconcile against bank settlements.
        from .services import compute_payment_fee_breakdown
        escrow_breakdown = compute_payment_fee_breakdown(
            subtotal=escrow_amount,
            logistics=Decimal('0'),
        )
        PlatformWallet.get().transactions.create(
            amount            = escrow_amount,
            transaction_type  = 'credit',
            transaction_label = 'escrow_advance',
            order_entry       = entry,
            gateway_fee_amount = escrow_breakdown['gateway_fee'],
            gateway_gst_amount = escrow_breakdown['gst_on_fee'],
            description       = (
                f'+10% Advance (Escrow) — {pool.product.name} | '
                f'{store.name} | {quantity} {pool.product.unit}(s) '
                f'@ ₹{entry.unit_price_at_order} '
                f'[Gateway: ₹{escrow_breakdown["gateway_fee"]} + GST ₹{escrow_breakdown["gst_on_fee"]}]'
            ),
        )

    return JsonResponse({
        'ok':          True,
        'message':     f'Successfully joined the pool for {pool.product.name}!',
        'redirect_url': f'/pools/{pool_id}/?joined=1',
    })


@login_required
def pool_detail(request, pool_id):
    """
    Pool detail page. Handles join (action=join) and modify quantity (action=modify).
    Works for any pool status -- locked pools show a closed message.
    show_confirmation=True triggers the success modal after joining.

    Privacy rules (enforced here, not in pool_list):
    - Open pools: visible to all verified stores.
    - Locked / Fulfilled / Cancelled pools: visible ONLY to:
        • Staff / superusers
        • Stores that have an OrderEntry for this pool (any status)
      Any other store gets a 'This pool is now private' redirect.
    """
    pool = get_object_or_404(OrderPool, id=pool_id)
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')

    # ── Privacy gate ──────────────────────────────────────────────────────
    if pool.status != 'open' and not request.user.is_staff:
        is_member = OrderEntry.objects.filter(pool=pool, store=store).exists()
        if not is_member:
            messages.warning(
                request,
                'This pool is now private. Only stores that joined before it '
                'locked can view its details.'
            )
            return redirect('pool_list')
    my_entry = OrderEntry.objects.filter(pool=pool, store=store, status='active').first()
    any_entry = OrderEntry.objects.filter(pool=pool, store=store).first()
    show_confirmation = False
    form = OrderEntryForm(instance=my_entry) if my_entry else OrderEntryForm()
    if request.method == 'POST' and pool.status == 'open':
        action = request.POST.get('action', 'join')
        if action == 'join' and not my_entry:
            if pool.is_full():
                messages.error(request, "This pool is full (15 stores maximum). Please join another pool.")
                return redirect('pool_detail', pool_id=pool_id)
            form = OrderEntryForm(request.POST)
            if form.is_valid():
                if form.cleaned_data['mode'] == 'urgent' and not _fast_track_available(store):
                    messages.error(
                        request,
                        "Fast Track is not available in your city. "
                        "Please select Pool Mode or contact support to add a warehouse in your area."
                    )
                    return redirect('pool_detail', pool_id=pool_id)
                with transaction.atomic():
                    # ── Server-side stock validation (inside atomic to prevent race conditions) ──
                    requested_qty = form.cleaned_data['quantity']

                    # Check 1: Pool member cap
                    if pool.is_full():
                        messages.error(request, "This pool is full (15 stores maximum). Please join another pool.")
                        return redirect('pool_detail', pool_id=pool_id)

                    # Check 2: Factory inventory stock
                    # Lock the inventory row with select_for_update() to prevent
                    # two stores simultaneously booking the same last units.
                    factory = pool.product.factory
                    if factory:
                        from .models import Inventory as FactoryInventory
                        factory_stock = FactoryInventory.objects.select_for_update().filter(
                            store__user=factory.user,
                            product=pool.product,
                        ).first()
                        if factory_stock and factory_stock.current_stock < requested_qty:
                            messages.error(
                                request,
                                f"Insufficient stock available. "
                                f"Only {factory_stock.current_stock} {pool.product.unit}(s) in stock — "
                                f"you requested {requested_qty}. Please reduce your quantity."
                            )
                            return redirect('pool_detail', pool_id=pool_id)

                    if any_entry:
                        entry = any_entry
                        entry.quantity = requested_qty
                        entry.mode = form.cleaned_data['mode']
                        entry.status = 'active'
                        entry.cancelled_at = None
                        entry.penalty_charged = 0
                    else:
                        entry = form.save(commit=False)
                        entry.pool = pool
                        entry.store = store
                    entry.unit_price_at_order = pool.product.base_price
                    entry.discount_applied = pool.current_discount()
                    entry.estimated_arrival = entry.compute_estimated_arrival()
                    escrow = entry.compute_escrow()
                    if store.wallet_balance < escrow:
                        messages.error(
                            request,
                            f"Insufficient wallet balance for 10% advance deposit (Rs.{escrow}). "
                            f"Please add funds to your wallet."
                        )
                        return redirect('pool_detail', pool_id=pool_id)
                    store.wallet_balance -= escrow
                    store.save(update_fields=['wallet_balance'])
                    entry.escrow_amount = escrow
                    entry.save()
                    pool.total_qty += entry.quantity
                    pool.current_member_count += 1
                    pool.save()
                # send_order_confirmation(entry)
                my_entry = entry
                show_confirmation = True
        elif action == 'modify' and my_entry:
            new_qty = int(request.POST.get('quantity', my_entry.quantity))
            if new_qty > 0:
                with transaction.atomic():
                    pool.total_qty = pool.total_qty - my_entry.quantity + new_qty
                    my_entry.quantity = new_qty
                    my_entry.discount_applied = pool.current_discount()
                    my_entry.estimated_arrival = my_entry.compute_estimated_arrival()
                    my_entry.save()
                    pool.save()
                messages.success(request, f"Order updated to {new_qty} {pool.product.unit}s.")
            return redirect('pool_detail', pool_id=pool_id)
    next_stores, next_discount = pool.next_tier_info()
    fast_track_ok = _fast_track_available(store)
    return render(request, 'core/pool_detail.html', {
        'pool': pool, 'form': form, 'my_entry': my_entry,
        'show_confirmation': show_confirmation,
        'next_stores_needed': next_stores, 'next_discount': next_discount,
        'cancellation_locked': pool.cancellation_locked(),
        'cancellation_lockout_time': pool.cancellation_lockout_time(),
        'fast_track_available': fast_track_ok,
        'escrow_amount': round(
            (pool.product.base_price * (1 - Decimal(str(pool.current_discount())) / 100)) * Decimal('0.10'),
            2
        ),
    })


@login_required
def cancel_order(request, entry_id):
    """
    Two-tier cancellation: free if pool open, Rs200 penalty if locked.
    Blocks cancellation if wallet balance is insufficient for penalty.
    """
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    entry = get_object_or_404(OrderEntry, id=entry_id, store=store, status='active')
    pool = entry.pool
    if request.method == 'POST':
        if pool.cancellation_locked():
            messages.error(request, "Cancellations are locked for this pool as it is nearing its expiry.")
            return redirect('pool_detail', pool_id=pool.id)
        with transaction.atomic():
            pool_is_locked = pool.status != 'open'
            delivery_exists = DeliveryTracking.objects.filter(pool=pool, store=store).exists()
            if pool_is_locked or delivery_exists:
                penalty = OrderEntry.CANCELLATION_PENALTY
                if store.wallet_balance >= penalty:
                    store.wallet_balance -= penalty
                    store.save()
                    entry.penalty_charged = penalty
                    entry.status = 'cancelled_penalty'
                    entry.cancelled_at = timezone.now()
                    entry.save()
                    # send_cancellation_notice(entry, penalty_applied=True)
                    messages.warning(request, f"Order cancelled. Rs{penalty} penalty deducted.")
                else:
                    messages.error(request, f"Insufficient wallet balance to cover Rs{penalty} penalty.")
                    return redirect('dashboard')
            else:
                pool.total_qty = max(0, pool.total_qty - entry.quantity)
                pool.current_member_count = max(0, pool.current_member_count - 1)
                pool.save()
                if entry.escrow_amount > 0:
                    store.wallet_balance += entry.escrow_amount
                    store.save(update_fields=['wallet_balance'])
                entry.status = 'cancelled_free'
                entry.cancelled_at = timezone.now()
                entry.save()
                # send_cancellation_notice(entry, penalty_applied=False)
                messages.success(request, f"Order cancelled. Rs.{entry.escrow_amount} advance deposit refunded to your wallet.")
        return redirect('dashboard')
    pool_is_locked = pool.status != 'open'
    return render(request, 'core/cancel_order.html', {
        'entry': entry, 'pool_is_locked': pool_is_locked,
        'penalty': OrderEntry.CANCELLATION_PENALTY,
        'cancellation_locked': pool.cancellation_locked(),
        'cancellation_lockout_time': pool.cancellation_lockout_time(),
    })


@login_required
def confirm_delivery(request, delivery_id):
    """
    OTP confirmation — the single trigger for the entire automated payment flow.

    On correct OTP:
      1. delivery.status → 'delivered', otp_verified = True
      2. _release_commission() runs automatically
      3. GST invoice auto-generated
      4. Real-time WebSocket notifications sent to store and factory

    If the delivery is already confirmed, redirects gracefully to order history
    instead of raising a 404.
    """
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')

    # Fetch without ownership filter first — check if already delivered
    # This prevents a 404 when a user navigates to a stale confirm URL
    # for a delivery that was already confirmed (possibly by another session).
    from django.http import Http404
    try:
        delivery = DeliveryTracking.objects.get(id=delivery_id)
    except DeliveryTracking.DoesNotExist:
        raise Http404

    # If already confirmed AND the ledger is complete, redirect gracefully.
    # If delivered but FactoryPayout is missing (old bug), fall through so
    # the POST path can re-run _release_commission to backfill the ledger.
    if delivery.status == 'delivered' or delivery.otp_verified:
        from .models import FactoryPayout
        ledger_complete = FactoryPayout.objects.filter(delivery=delivery).exists()
        if ledger_complete:
            messages.info(
                request,
                'This delivery has already been confirmed. '
                'Your invoice is available in Order History.'
            )
            return redirect('order_history')
        # Ledger incomplete — allow the view to re-run commission release
        # (POST will be blocked by the OTP check, so we handle it here directly)
        if delivery.store == store:
            with transaction.atomic():
                _release_commission(delivery)
            messages.success(request, 'Delivery records have been synchronised.')
        return redirect('order_history')

    # Ownership check — only the correct store can confirm a pending delivery
    if delivery.store != store:
        raise Http404
    if request.method == 'POST':
        otp_input = request.POST.get('otp', '')
        if otp_input == delivery.delivery_otp:
            with transaction.atomic():
                delivery.otp_verified = True
                delivery.status       = 'delivered'
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=['otp_verified', 'status', 'delivered_at'])
                _release_commission(delivery)

            # ── Auto-generate GST invoice on delivery confirmation ────────
            # Runs immediately after OTP — non-blocking, never fails the delivery.
            # Regenerates if the file is missing from disk (e.g. after a server move).
            try:
                factory_order = delivery.pool.factory_order
                if factory_order:
                    from .invoice import generate_invoice
                    import os
                    from django.conf import settings as django_settings
                    needs_generation = (
                        not factory_order.invoice_pdf
                        or not os.path.exists(
                            os.path.join(django_settings.MEDIA_ROOT, str(factory_order.invoice_pdf))
                        )
                    )
                    if needs_generation:
                        generate_invoice(factory_order)
            except Exception:
                pass  # invoice generation is non-blocking — delivery still confirmed

            # ── Real-time notifications ───────────────────────────────────
            from .notifications import push_notification
            product_name = delivery.pool.product.name

            # Notify the store
            push_notification(
                user_id    = store.user_id,
                notif_type = 'delivery_otp',
                title      = 'Delivery Confirmed ✅',
                body       = (
                    f'Your {product_name} order has been delivered. '
                    f'Payment has been processed automatically.'
                ),
                url        = '/order-history/',
            )

            # Notify the factory
            factory = delivery.pool.product.factory
            if factory and factory.user_id:
                push_notification(
                    user_id    = factory.user_id,
                    notif_type = 'payout_released',
                    title      = 'Delivery Confirmed — Payout Pending 💰',
                    body       = (
                        f'{store.name} confirmed receipt of {product_name}. '
                        f'Your payout is pending admin release.'
                    ),
                    url        = '/factory/wallet/',
                )

            messages.success(
                request,
                'Delivery confirmed. Payment has been processed automatically.'
            )
        else:
            messages.error(request, 'Invalid OTP. Please try again.')
    return render(request, 'core/confirm_delivery.html', {'delivery': delivery})


@login_required
def track_delivery(request, delivery_id):
    """
    Live tracking map.
    - Staff: see any delivery
    - Store owners: see only their own deliveries
    - Factory users: see deliveries for their factory products
    """
    delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
    if request.user.is_staff:
        pass
    elif _is_factory_user(request.user):
        factory = _get_factory_or_none(request.user)
        if not factory or delivery.pool.product.factory != factory:
            messages.error(request, "You do not have permission to view this delivery.")
            return redirect('factory_deliveries')
    else:
        store = _get_store_or_none(request.user)
        if not store or delivery.store != store:
            messages.error(request, "You do not have permission to view this delivery.")
            return redirect('dashboard')
    return render(request, 'core/track_delivery.html', {
        'delivery': delivery, 'store': delivery.store,
    })


def delivery_location_api(request, delivery_id):
    """
    JSON API polled by the tracking page.
    Returns truck GPS, speed, distance, ETA, factory coords, and dispatched_at.
    """
    from django.http import JsonResponse
    from math import radians, sin, cos, sqrt, atan2
    delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
    try:
        loc = delivery.truck_location
    except Exception:
        return JsonResponse({'error': 'No truck location available yet.'}, status=404)

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = radians(float(lat2) - float(lat1))
        dlon = radians(float(lon2) - float(lon1))
        a = sin(dlat/2)**2 + cos(radians(float(lat1))) * cos(radians(float(lat2))) * sin(dlon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    store = delivery.store
    store_lat = float(store.latitude) if store.latitude else 23.0225
    store_lng = float(store.longitude) if store.longitude else 72.5714
    if not store.latitude:
        store.latitude = store_lat
        store.longitude = store_lng
        store.save(update_fields=['latitude', 'longitude'])
    dist_km = round(haversine(loc.latitude, loc.longitude, store_lat, store_lng), 2)
    speed = float(loc.speed_kmh) if loc.speed_kmh and loc.speed_kmh > 0 else 40.0
    eta_minutes = max(1, round((dist_km / speed) * 60))
    factory = delivery.pool.product.factory
    factory_lat = float(factory.latitude) if factory and factory.latitude else float(loc.latitude)
    factory_lng = float(factory.longitude) if factory and factory.longitude else float(loc.longitude)
    factory_name = factory.name if factory else 'Factory'
    dispatched_at = delivery.dispatched_at.isoformat() if delivery.dispatched_at else None
    return JsonResponse({
        'latitude': float(loc.latitude), 'longitude': float(loc.longitude),
        'speed_kmh': speed, 'updated_at': loc.updated_at.strftime('%H:%M:%S'),
        'distance_km': dist_km, 'eta_minutes': eta_minutes,
        'store_lat': store_lat, 'store_lng': store_lng,
        'store_name': store.name, 'status': delivery.status,
        'factory_lat': factory_lat, 'factory_lng': factory_lng,
        'factory_name': factory_name,
        'dispatched_at': dispatched_at,
    })


def driver_app(request, delivery_id):
    """
    Serves the driver PWA page.
    Auth: ?token=<driver_token> — no Django session required.
    The page is a standalone HTML PWA; the driver opens this URL on their phone.
    """
    from django.http import Http404
    delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
    token = request.GET.get('token', '')
    if not token or token != delivery.driver_token:
        raise Http404  # silently 404 — don't reveal delivery details to strangers
    return render(request, 'core/driver_app.html', {
        'delivery': delivery,
        'token':    token,
    })


def pwa_manifest(request):
    """Serves the PWA manifest.json for the driver app."""
    import json
    from django.http import JsonResponse
    manifest = {
        "name": "BulkMed Driver",
        "short_name": "Driver",
        "description": "BulkMed live GPS tracker for delivery drivers",
        "start_url": "/driver/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#059669",
        "orientation": "portrait",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return JsonResponse(manifest, content_type='application/manifest+json')


def pwa_service_worker(request):
    """Serves the service worker JS for offline caching."""
    from django.http import HttpResponse
    sw = """
const CACHE = 'bulkmed-driver-v1';
const PRECACHE = ['/driver/offline.html'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

// Network-first for API calls, cache-first for static assets
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/location/update/')) return; // never cache GPS posts
  e.respondWith(
    fetch(e.request).catch(() =>
      caches.match(e.request).then(r => r || caches.match('/driver/offline.html'))
    )
  );
});
""".strip()
    return HttpResponse(sw, content_type='application/javascript')


def auto_deliver(request, delivery_id):
    """
    Previously used for timer-based auto-delivery. Now disabled.
    Delivery status only changes via OTP confirmation (confirm_delivery view).
    Kept as endpoint to avoid 404s from old clients.
    """
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
    return JsonResponse({'ok': False, 'status': delivery.status,
                         'message': 'Auto-delivery disabled. Use OTP confirmation.'})


@login_required
def map_view(request):
    """
    Supply Chain Logistics Map.
    Shows: active order pools (💊), verified stores (🏥), verified factories (🏭).
    Superusers and staff see all entities; store users see their city + nearby.
    """
    import json as _json
    store = _get_store_or_none(request.user)
    is_admin = request.user.is_superuser or request.user.is_staff

    # Redirect non-store, non-admin users to setup
    if not store and not is_admin:
        return redirect('store_setup')

    # ── Pool data ─────────────────────────────────────────────────────────
    open_pools = OrderPool.objects.filter(status='open').select_related('product')
    pool_data = [{
        'id':           str(pool.id),
        'product':      pool.product.name,
        'city':         pool.city,
        'member_count': pool.current_member_count,
        'discount':     pool.current_discount(),
        'expires_at':   pool.expires_at.isoformat(),
        'next_tier':    pool.next_tier_info(),
    } for pool in open_pools]

    # ── Factory data (only those with GPS coordinates) ────────────────────
    factories_qs = Factory.objects.filter(
        is_verified=True,
        latitude__isnull=False,
        longitude__isnull=False,
    )
    factory_data = [{
        'id':       f.id,
        'name':     f.name,
        'city':     f.city or '',
        'lat':      float(f.latitude),
        'lng':      float(f.longitude),
        'contact':  f.contact or '',
        'verified': f.is_verified,
    } for f in factories_qs]

    # ── Store data (only those with GPS coordinates) ──────────────────────
    stores_qs = MedicalStore.objects.filter(
        is_verified=True,
        latitude__isnull=False,
        longitude__isnull=False,
    )
    store_data = [{
        'id':       s.id,
        'name':     s.name,
        'city':     s.city or '',
        'lat':      float(s.latitude),
        'lng':      float(s.longitude),
        'verified': s.is_verified,
    } for s in stores_qs]

    # Map centre: use current store's city, or India centre for admins
    city_coords = {
        'Ahmedabad': [23.0225, 72.5714], 'Mumbai': [19.0760, 72.8777],
        'Delhi':     [28.6139, 77.2090], 'Bangalore': [12.9716, 77.5946],
        'Surat':     [21.1702, 72.8311], 'Pune': [18.5204, 73.8567],
        'Chennai':   [13.0827, 80.2707], 'Hyderabad': [17.3850, 78.4867],
        'Kolkata':   [22.5726, 88.3639], 'Jaipur': [26.9124, 75.7873],
    }
    if store and store.city in city_coords:
        map_centre = city_coords[store.city]
    elif store and store.latitude and store.longitude:
        map_centre = [float(store.latitude), float(store.longitude)]
    else:
        map_centre = [22.9734, 78.6569]  # India centre

    return render(request, 'core/map_view.html', {
        'pool_data':     _json.dumps(pool_data),
        'factory_data':  _json.dumps(factory_data),
        'store_data':    _json.dumps(store_data),
        'map_centre':    _json.dumps(map_centre),
        'store':         store,
        'is_admin':      is_admin,
        'pool_count':    len(pool_data),
        'factory_count': len(factory_data),
        'store_count':   len(store_data),
    })


@login_required
def edit_store(request, store_id):
    """AJAX: GET returns store JSON for drawer; POST saves via StoreEditForm."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    store = get_object_or_404(MedicalStore, id=store_id)
    if request.method == 'POST':
        form = StoreEditForm(request.POST, instance=store)
        if form.is_valid():
            form.save()
            return JsonResponse({'ok': True, 'message': f'{store.name} updated successfully.'})
        return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    return JsonResponse({
        'id': store.id, 'name': store.name, 'address': store.address,
        'license_no': store.license_no, 'contact': store.contact, 'city': store.city,
        'latitude': str(store.latitude or ''), 'longitude': str(store.longitude or ''),
        'is_verified': store.is_verified, 'wallet_balance': str(store.wallet_balance),
        'gstin': store.gstin or '',
    })


@login_required
def edit_pool(request, pool_id):
    """AJAX: GET returns pool JSON for drawer; POST saves via PoolEditForm."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    pool = get_object_or_404(OrderPool, id=pool_id)
    if request.method == 'POST':
        form = PoolEditForm(request.POST, instance=pool)
        if form.is_valid():
            form.save()
            return JsonResponse({'ok': True, 'message': f'{pool.product.name} updated.'})
        return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    return JsonResponse({
        'id': str(pool.id), 'product_name': pool.product.name, 'city': pool.city,
        'status': pool.status, 'expires_at': pool.expires_at.strftime('%Y-%m-%dT%H:%M'),
        'current_member_count': pool.current_member_count, 'total_qty': pool.total_qty,
    })


@login_required
def control_panel(request):
    """Unified staff control panel: stores, deliveries, simulator, tracking, pools, products, factories."""
    if not request.user.is_staff:
        return redirect('dashboard')
    from .models import FactoryOrder, FactoryPayout
    stores       = MedicalStore.objects.select_related('user').order_by('city', 'name')
    deliveries   = DeliveryTracking.objects.select_related('pool__product', 'store').order_by('-id')
    pools_all    = OrderPool.objects.select_related('product').order_by('-created_at')
    products     = Product.objects.order_by('name')
    orders_all   = OrderEntry.objects.select_related('pool__product', 'store').order_by('-joined_at')
    platform_wallet = PlatformWallet.get()
    transactions = WalletTransaction.objects.select_related('delivery__pool__product', 'delivery__store').order_by('-created_at')[:200]
    factories           = Factory.objects.select_related('user').order_by('city', 'name')
    factory_orders      = FactoryOrder.objects.select_related('pool__product', 'factory').order_by('-received_at')
    factory_payouts     = FactoryPayout.objects.select_related('pool__product', 'factory', 'delivery__store').order_by('-created_at')[:200]
    factory_products    = Product.objects.filter(factory__isnull=False).select_related('factory').order_by('factory__name', 'name')
    factory_deliveries  = DeliveryTracking.objects.filter(pool__product__factory__isnull=False).select_related('pool__product__factory', 'store').order_by('-dispatched_at')
    factory_pools       = OrderPool.objects.filter(product__factory__isnull=False).select_related('product__factory').order_by('-created_at')
    from .models import WithdrawalRequest
    withdrawal_requests = WithdrawalRequest.objects.select_related('factory').order_by('-requested_at')
    from .models import Dispute
    disputes = Dispute.objects.select_related('delivery__store', 'delivery__pool__product').order_by('-raised_at')
    return render(request, 'core/control_panel.html', {
        'stores': stores, 'deliveries': deliveries, 'pools_all': pools_all,
        'products': products, 'orders_all': orders_all,
        'pool_create_form': PoolCreateForm(),
        'product_form': ProductForm(),
        'platform_wallet': platform_wallet,
        'transactions': transactions,
        'factories':          factories,
        'factory_orders':     factory_orders,
        'factory_payouts':    factory_payouts,
        'factory_products':   factory_products,
        'factory_deliveries': factory_deliveries,
        'factory_pools':      factory_pools,
        'withdrawal_requests': withdrawal_requests,
        'disputes':            disputes,
    })


@login_required
def financial_audit_trail(request):
    """
    Superuser-only: detailed ledger showing every money movement with full
    order context — store, medicine, qty, unit price, and transaction type.

    Summary aggregates include gateway fee totals so the admin can reconcile
    platform revenue against Razorpay's bank settlement report exactly.
    """
    if not request.user.is_superuser:
        return redirect('dashboard')

    from .models import WalletTransaction

    # Pull all transactions with full related data in one query
    transactions = (
        WalletTransaction.objects
        .select_related(
            'order_entry__store__user',
            'order_entry__pool__product',
            'delivery__store',
            'delivery__pool__product',
        )
        .order_by('-created_at')
    )

    # Summary aggregates
    from django.db.models import Sum, Q
    agg = transactions.aggregate(
        total_credits      = Sum('amount', filter=Q(transaction_type='credit')),
        total_debits       = Sum('amount', filter=Q(transaction_type='debit')),
        escrow_in          = Sum('amount', filter=Q(transaction_label='escrow_advance')),
        topup_in           = Sum('amount', filter=Q(transaction_label='topup_credit')),
        final_in           = Sum('amount', filter=Q(transaction_label='final_payment')),
        commission         = Sum('amount', filter=Q(transaction_label='commission')),
        refunds_out        = Sum('amount', filter=Q(transaction_label='escrow_refund')),
        payouts_out        = Sum('amount', filter=Q(transaction_label='factory_payout')),
        # ── Gateway fee totals — reconcile against Razorpay settlement ──────
        # Sum of all 2% Razorpay fees across escrow_advance + topup_credit rows
        total_gateway_fees = Sum('gateway_fee_amount'),
        # Sum of all 18% GST on those fees
        total_gateway_gst  = Sum('gateway_gst_amount'),
    )
    # Replace None with 0
    for k in agg:
        agg[k] = agg[k] or Decimal('0')

    # Net platform revenue = commission earned − total gateway costs
    # This is what actually lands in the bank after Razorpay deductions.
    agg['total_gateway_cost'] = agg['total_gateway_fees'] + agg['total_gateway_gst']
    agg['net_platform_revenue'] = agg['commission'] - agg['total_gateway_cost']

    return render(request, 'core/financial_audit_trail.html', {
        'transactions': transactions,
        'agg':          agg,
        'net_balance':  agg['total_credits'] - agg['total_debits'],
    })


@login_required
def admin_stats_api(request):
    """
    JSON endpoint for the Executive Command Center AJAX refresh.
    Returns live counts for the 4 status cards.
    Only accessible to superusers.
    """
    from django.http import JsonResponse
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    from .models import FactoryOrder, FactoryPayout, Dispute
    return JsonResponse({
        'open_disputes':   Dispute.objects.filter(status='open').count(),
        'pending_payouts': FactoryPayout.objects.filter(status='pending').count(),
        'active_pools':    OrderPool.objects.filter(status='open').count(),
        'pending_orders':  FactoryOrder.objects.filter(status='received').count(),
        'platform_balance': str(PlatformWallet.get().balance),
    })


@login_required
def verify_store(request, store_id):
    """One-click store verification from Control Panel."""
    if not request.user.is_staff:
        return redirect('dashboard')
    if request.method == 'POST':
        store = get_object_or_404(MedicalStore, id=store_id)
        store.is_verified = True
        store.save()
        messages.success(request, f"{store.name} verified.")
    return redirect('control_panel')


@login_required
def delete_store(request, store_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        store = get_object_or_404(MedicalStore, id=store_id)
        name = store.name
        store.user.delete()
        return JsonResponse({'ok': True, 'message': f'{name} deleted.'})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def create_delivery(request):
    """Creates a dispatched DeliveryTracking record from Control Panel."""
    if not request.user.is_staff:
        return redirect('dashboard')
    if request.method == 'POST':
        import random, string
        pool = get_object_or_404(OrderPool, id=request.POST['pool_id'])
        store = get_object_or_404(MedicalStore, id=request.POST['store_id'])
        otp = request.POST.get('otp', ''.join(random.choices(string.digits, k=6)))
        delivery = DeliveryTracking.objects.create(
            pool=pool, store=store,
            quantity=int(request.POST.get('quantity', 50)),
            status='dispatched', delivery_otp=otp, dispatched_at=timezone.now(),
        )
        factory = pool.product.factory
        if factory and factory.latitude and factory.longitude:
            start_lat, start_lng = factory.latitude, factory.longitude
        else:
            start_lat, start_lng = 23.0753, 72.6369
        TruckLocation.objects.update_or_create(
            delivery=delivery,
            defaults={'latitude': start_lat, 'longitude': start_lng, 'speed_kmh': 40}
        )
        messages.success(request, f"Delivery #{delivery.id} created. OTP: {otp}")
    return redirect('control_panel')


@login_required
def delete_delivery(request, delivery_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
        delivery.delete()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def cancel_delivery(request, delivery_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
        delivery.status = 'failed'
        delivery.save()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def create_pool(request):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        product_id = request.POST.get('product')
        city = request.POST.get('city', '').strip()
        mode = request.POST.get('mode', 'pool')
        if not product_id or not city:
            return JsonResponse({'ok': False, 'errors': {'__all__': 'Product and city are required.'}}, status=400)
        product = get_object_or_404(Product, id=product_id)
        from datetime import timedelta
        window_hours = OrderPool.POOL_WINDOW_HOURS.get(mode, 72)
        expires_at = timezone.now() + timedelta(hours=window_hours)
        existing = OrderPool.objects.filter(
            product=product, city=city, status='open', pool_mode=mode
        ).first()
        if existing:
            return JsonResponse({'ok': True, 'message': f'Pool already exists for {product.name} in {city}.', 'pool_id': str(existing.id)})
        pool = OrderPool.objects.create(
            product=product, city=city, status='open',
            expires_at=expires_at, pool_mode=mode,
        )
        label = 'Fast Track (2h)' if mode == 'urgent' else 'Pool Mode (3 days)'
        return JsonResponse({'ok': True, 'message': f'{label} pool for {pool.product.name} created.'})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def delete_pool(request, pool_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        pool = get_object_or_404(OrderPool, id=pool_id)
        pool.delete()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def manage_product(request, product_id=None):
    """Create or edit a product. GET returns JSON, POST saves."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        instance = get_object_or_404(Product, id=product_id) if product_id else None
        form = ProductForm(request.POST, instance=instance)
        if form.is_valid():
            p = form.save()
            return JsonResponse({'ok': True, 'message': f'{p.name} saved.'})
        return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    if product_id:
        p = get_object_or_404(Product, id=product_id)
        return JsonResponse({
            'id': p.id, 'name': p.name, 'generic_name': p.generic_name,
            'category': p.category, 'factory_name': p.factory_name,
            'base_price': str(p.base_price), 'unit': p.unit,
            'sku_code': p.sku_code, 'hsn_code': p.hsn_code, 'barcode': p.barcode or '',
        })
    return JsonResponse({'error': 'GET requires product_id'}, status=400)


@login_required
def delete_product(request, product_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        p = get_object_or_404(Product, id=product_id)
        name = p.name
        p.delete()
        return JsonResponse({'ok': True, 'message': f'{name} deleted.'})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def trigger_lock_pools(request):
    """Staff endpoint to manually trigger pool locking (also runs via cron)."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    from django.core.management import call_command
    from io import StringIO
    out = StringIO()
    call_command('lock_pools', stdout=out)
    return JsonResponse({'ok': True, 'output': out.getvalue()})


@login_required
def cancel_order_admin(request, entry_id):
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        entry = get_object_or_404(OrderEntry, id=entry_id)
        entry.status = 'cancelled_free'
        entry.cancelled_at = timezone.now()
        entry.save()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


# -------------------------------- Factory Admin AJAX endpoints --------------------------------

@login_required
def edit_factory(request, factory_id):
    """GET -- JSON for drawer; POST -- save edits."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    factory = get_object_or_404(Factory, id=factory_id)
    if request.method == 'POST':
        factory.name           = request.POST.get('name', factory.name).strip()
        factory.address        = request.POST.get('address', factory.address).strip()
        factory.city           = request.POST.get('city', factory.city).strip()
        factory.license_no     = request.POST.get('license_no', factory.license_no) or None
        factory.contact        = request.POST.get('contact', factory.contact).strip()
        factory.wallet_balance = request.POST.get('wallet_balance', factory.wallet_balance)
        factory.is_verified    = 'is_verified' in request.POST
        factory.gstin          = request.POST.get('gstin', factory.gstin or '').strip()
        try:
            factory.latitude  = float(request.POST['latitude'])  if request.POST.get('latitude')  else factory.latitude
            factory.longitude = float(request.POST['longitude']) if request.POST.get('longitude') else factory.longitude
        except ValueError:
            pass
        factory.save()
        return JsonResponse({'ok': True, 'message': f'{factory.name} updated.'})
    return JsonResponse({
        'id':             factory.id,
        'name':           factory.name,
        'address':        factory.address,
        'city':           factory.city,
        'license_no':     factory.license_no or '',
        'contact':        factory.contact,
        'latitude':       str(factory.latitude or ''),
        'longitude':      str(factory.longitude or ''),
        'wallet_balance': str(factory.wallet_balance),
        'is_verified':    factory.is_verified,
        'gstin':          factory.gstin or '',
    })


@login_required
def delete_factory(request, factory_id):
    """Delete a factory and its linked user account."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        factory = get_object_or_404(Factory, id=factory_id)
        name = factory.name
        if factory.user:
            factory.user.delete()
        factory.delete()
        return JsonResponse({'ok': True, 'message': f'{name} deleted.'})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def verify_factory(request, factory_id):
    """One-click factory verification."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        factory = get_object_or_404(Factory, id=factory_id)
        factory.is_verified = True
        factory.save(update_fields=['is_verified'])
        return JsonResponse({'ok': True, 'message': f'{factory.name} verified.'})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def edit_factory_order(request, order_id):
    """Update FactoryOrder status and notes."""
    from django.http import JsonResponse
    from .models import FactoryOrder
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    order = get_object_or_404(FactoryOrder, id=order_id)
    if request.method == 'POST':
        new_status = request.POST.get('status', order.status)
        if new_status in dict(FactoryOrder.STATUS_CHOICES):
            order.status = new_status
            if new_status == 'dispatched' and not order.dispatched_at:
                order.dispatched_at = timezone.now()
        order.notes = request.POST.get('notes', order.notes)
        order.save()
        return JsonResponse({'ok': True, 'message': f'Order #{order.id} updated to {order.get_status_display()}.'})
    return JsonResponse({
        'id':     order.id,
        'status': order.status,
        'notes':  order.notes,
        'factory': order.factory.name,
        'product': order.pool.product.name,
    })


@login_required
def delete_factory_order(request, order_id):
    """Delete a FactoryOrder record."""
    from django.http import JsonResponse
    from .models import FactoryOrder
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        order = get_object_or_404(FactoryOrder, id=order_id)
        order.delete()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def delete_factory_payout(request, payout_id):
    """Delete a FactoryPayout record."""
    from django.http import JsonResponse
    from .models import FactoryPayout
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        payout = get_object_or_404(FactoryPayout, id=payout_id)
        payout.delete()
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


@login_required
def manage_factory_product(request, product_id=None):
    """Create or edit a factory-linked product. GET returns JSON, POST saves."""
    from django.http import JsonResponse
    from .forms import FactoryProductForm
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        instance = get_object_or_404(Product, id=product_id) if product_id else None
        form = FactoryProductForm(request.POST, instance=instance)
        if form.is_valid():
            p = form.save(commit=False)
            fid = request.POST.get('factory_id')
            if fid:
                try:
                    p.factory = Factory.objects.get(id=fid)
                    p.factory_name = p.factory.name
                except Factory.DoesNotExist:
                    pass
            elif not p.factory_name:
                p.factory_name = p.factory.name if p.factory else ''
            p.save()
            return JsonResponse({'ok': True, 'message': f'{p.name} saved.'})
        return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    if product_id:
        p = get_object_or_404(Product, id=product_id)
        return JsonResponse({
            'id': p.id, 'name': p.name, 'generic_name': p.generic_name,
            'category': p.category, 'factory_name': p.factory_name,
            'factory_id': p.factory_id or '',
            'base_price': str(p.base_price), 'unit': p.unit,
            'sku_code': p.sku_code, 'hsn_code': p.hsn_code, 'barcode': p.barcode or '',
        })
    return JsonResponse({'error': 'GET requires product_id'}, status=400)


@login_required
def cancel_factory_delivery(request, delivery_id):
    """Mark a factory delivery as failed."""
    from django.http import JsonResponse
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'POST':
        delivery = get_object_or_404(DeliveryTracking, id=delivery_id)
        delivery.status = 'failed'
        delivery.save(update_fields=['status'])
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'POST only'}, status=405)


# -------------------------------- Razorpay -- Store Wallet Top-up --------------------------------

@login_required
def razorpay_create_order(request):
    """
    Creates a Razorpay order for the store wallet top-up.

    POST body: { amount: <rupees as integer> }

    Fee structure (transparent to the store):
      - Razorpay gateway fee : 2% of amount
      - GST on gateway fee   : 18% of gateway fee
      - Total payable        : amount + gateway_fee + gst_on_fee

    Returns: { order_id, amount_paise, currency, key_id, fee_breakdown }
    The `amount_paise` sent to Razorpay is the TOTAL PAYABLE (in paise),
    so the store is charged exactly what the breakdown shows.
    """
    from django.http import JsonResponse
    from django.conf import settings
    import razorpay, json
    from .services import compute_payment_fee_breakdown

    store = _get_store_or_none(request.user)
    if not store:
        return JsonResponse({'error': 'Store profile not found.'}, status=400)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        data   = json.loads(request.body)
        amount = int(data.get('amount', 0))
        if amount < 1:
            return JsonResponse({'error': 'Minimum top-up is ₹1.'}, status=400)
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Invalid amount.'}, status=400)

    # ── Compute transparent fee breakdown ─────────────────────────────────
    breakdown = compute_payment_fee_breakdown(
        subtotal=Decimal(str(amount)),
        logistics=Decimal('0'),   # wallet top-up has no logistics charge
    )

    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )
    try:
        order = client.order.create({
            'amount':   breakdown['total_paise'],   # ← total payable in paise
            'currency': 'INR',
            'receipt':  f'store_{store.id}_topup',
            'notes':    {
                'store_id':    store.id,
                'store_name':  store.name,
                'base_amount': str(amount),
                'gateway_fee': str(breakdown['gateway_fee']),
                'gst_on_fee':  str(breakdown['gst_on_fee']),
            },
        })
    except Exception as e:
        return JsonResponse({'error': f'Razorpay API error: {str(e)}'}, status=400)

    return JsonResponse({
        'order_id':     order['id'],
        'amount':       breakdown['total_paise'],   # paise — what Razorpay charges
        'currency':     'INR',
        'key_id':       settings.RAZORPAY_KEY_ID,
        'name':         store.name,
        'email':        request.user.email,
        'contact':      store.contact or '',
        # Fee breakdown — used by the frontend to render the price table
        'fee_breakdown': {
            'subtotal':      str(breakdown['subtotal']),
            'logistics':     str(breakdown['logistics']),
            'gateway_fee':   str(breakdown['gateway_fee']),
            'gst_on_fee':    str(breakdown['gst_on_fee']),
            'total_fee':     str(breakdown['total_fee']),
            'total_payable': str(breakdown['total_payable']),
        },
    })


@login_required
def razorpay_verify_payment(request):
    """
    Verifies Razorpay payment signature and credits the store wallet.

    IMPORTANT: The Razorpay order was created for `total_payable` (base + fee).
    We credit only the BASE amount to the wallet — the gateway fee + GST are
    the cost of the transaction and are NOT added to the spendable balance.

    POST body: {
        razorpay_order_id, razorpay_payment_id, razorpay_signature,
        amount,       ← total_paise charged by Razorpay
        base_amount   ← original top-up amount in rupees (no fees)
    }
    """
    from django.http import JsonResponse
    from django.conf import settings
    import razorpay, json
    store = _get_store_or_none(request.user)
    if not store:
        return JsonResponse({'error': 'Store profile not found.'}, status=400)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = json.loads(request.body)
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )
    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id':   data['razorpay_order_id'],
            'razorpay_payment_id': data['razorpay_payment_id'],
            'razorpay_signature':  data['razorpay_signature'],
        })
    except razorpay.errors.SignatureVerificationError:
        return JsonResponse({'ok': False, 'error': 'Payment signature verification failed.'}, status=400)

    # Credit only the BASE amount — fees are the cost of the transaction
    # base_amount is sent by the frontend (it knows the breakdown from the create-order response)
    base_amount_rupees = Decimal(str(data.get('base_amount', 0)))
    if base_amount_rupees <= 0:
        # Fallback: derive from total paise (less accurate but safe)
        total_paise = int(data.get('amount', 0))
        base_amount_rupees = Decimal(str(total_paise / 100))

    # Recompute the fee breakdown server-side so the ledger is authoritative
    # (never trust the client to send the correct fee values)
    from .services import compute_payment_fee_breakdown
    topup_breakdown = compute_payment_fee_breakdown(
        subtotal=base_amount_rupees,
        logistics=Decimal('0'),
    )

    with transaction.atomic():
        store.wallet_balance += base_amount_rupees
        store.save(update_fields=['wallet_balance'])

        # Create an immutable ledger record so the top-up appears
        # in the store's Transaction History table immediately.
        from .models import StoreTopUp
        StoreTopUp.objects.create(
            store               = store,
            amount              = base_amount_rupees,
            razorpay_order_id   = data.get('razorpay_order_id', ''),
            razorpay_payment_id = data.get('razorpay_payment_id', ''),
        )

        # Record the top-up on the Platform Wallet ledger with gateway fee breakdown
        PlatformWallet.get().transactions.create(
            amount             = base_amount_rupees,
            transaction_type   = 'credit',
            transaction_label  = 'topup_credit',
            gateway_fee_amount = topup_breakdown['gateway_fee'],
            gateway_gst_amount = topup_breakdown['gst_on_fee'],
            description        = (
                f'Wallet Top-up — {store.name} | '
                f'₹{base_amount_rupees} credited '
                f'[Gateway: ₹{topup_breakdown["gateway_fee"]} + GST ₹{topup_breakdown["gst_on_fee"]}]'
            ),
        )

    return JsonResponse({
        'ok':      True,
        'message': f'₹{base_amount_rupees} added to your wallet.',
        'balance': str(store.wallet_balance),
    })


# -------------------------------- Factory Withdrawal Requests --------------------------------

@login_required
def factory_withdraw(request):
    """
    Factory submits a withdrawal request.
    Balance is deducted immediately; admin approves/rejects the actual transfer.
    POST body: { amount: <rupees> }
    """
    from django.http import JsonResponse
    from .models import WithdrawalRequest
    import json
    if not _is_factory_user(request.user):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    factory = _get_factory_or_none(request.user)
    if not factory:
        return JsonResponse({'error': 'Factory profile not found.'}, status=400)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        data   = json.loads(request.body)
        amount = Decimal(str(data.get('amount', 0)))
        if amount <= 0:
            return JsonResponse({'error': 'Amount must be greater than zero.'}, status=400)
    except Exception:
        return JsonResponse({'error': 'Invalid amount.'}, status=400)
    if factory.wallet_balance < amount:
        return JsonResponse({'error': f'Insufficient balance. Available: Rs.{factory.wallet_balance}'}, status=400)
    with transaction.atomic():
        factory.wallet_balance -= amount
        factory.save(update_fields=['wallet_balance'])
        WithdrawalRequest.objects.create(factory=factory, amount=amount)
    return JsonResponse({
        'ok':      True,
        'message': f'Withdrawal request of Rs.{amount} submitted. Pending admin approval.',
        'balance': str(factory.wallet_balance),
    })


@login_required
def admin_withdrawal_action(request, request_id):
    """
    Staff endpoint to approve or reject a WithdrawalRequest.
    POST body: { action: 'approve' | 'reject', note: '...' }
    """
    from django.http import JsonResponse
    from .models import WithdrawalRequest
    import json
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    wr = get_object_or_404(WithdrawalRequest, id=request_id)
    if wr.status != 'pending':
        return JsonResponse({'error': f'Already {wr.status}.'}, status=400)
    data   = json.loads(request.body)
    action = data.get('action')
    note   = data.get('note', '')
    with transaction.atomic():
        if action == 'approve':
            wr.status     = 'approved'
            wr.admin_note = note
            wr.resolved_at = timezone.now()
            wr.save()
            return JsonResponse({'ok': True, 'message': f'Withdrawal of Rs.{wr.amount} approved.'})
        elif action == 'reject':
            wr.factory.wallet_balance += wr.amount
            wr.factory.save(update_fields=['wallet_balance'])
            wr.status     = 'rejected'
            wr.admin_note = note
            wr.resolved_at = timezone.now()
            wr.save()
            return JsonResponse({'ok': True, 'message': f'Withdrawal rejected. Rs.{wr.amount} refunded.'})
        else:
            return JsonResponse({'error': 'action must be approve or reject.'}, status=400)


# -------------------------------- Dispute views --------------------------------

@login_required
def raise_dispute(request, delivery_id):
    """
    Store raises a dispute for a damaged/missing delivery.
    Accepts multipart POST: reason (text) + photo (image, optional).
    Once raised, the factory 85% payout is blocked until admin resolves.
    """
    store = _get_store_or_none(request.user)
    if not store:
        return redirect('store_setup')
    delivery = get_object_or_404(DeliveryTracking, id=delivery_id, store=store)
    if request.method == 'POST':
        from .models import Dispute
        reason = request.POST.get('reason', '').strip()
        photo  = request.FILES.get('photo')
        if not reason:
            messages.error(request, 'Please describe the issue before raising a dispute.')
            return redirect('confirm_delivery', delivery_id=delivery_id)
        if hasattr(delivery, 'dispute'):
            messages.warning(request, 'A dispute has already been raised for this delivery.')
            return redirect('confirm_delivery', delivery_id=delivery_id)
        Dispute.objects.create(delivery=delivery, reason=reason, photo=photo)
        messages.warning(
            request,
            'Dispute raised. The factory payout is locked pending admin review. '
            'Our team will contact you within 24 hours.'
        )
        return redirect('confirm_delivery', delivery_id=delivery_id)
    return redirect('confirm_delivery', delivery_id=delivery_id)


@login_required
def resolve_dispute(request, dispute_id):
    """
    Staff endpoint to resolve a dispute.
    POST body: { action: 'payout' | 'refund', note: '...' }

    payout → admin sides with factory: releases 85% payout, delivery marked delivered.
    refund → admin sides with store: refunds store wallet, factory gets nothing.
    """
    from django.http import JsonResponse
    from .models import Dispute
    import json
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if dispute.status != 'open':
        return JsonResponse({'error': f'Dispute already {dispute.get_status_display()}.'}, status=400)
    data   = json.loads(request.body)
    action = data.get('action')
    note   = data.get('note', '').strip()
    with transaction.atomic():
        if action == 'payout':
            # Admin sides with factory — release the 85% payout
            dispute.status      = 'resolved_payout'
            dispute.admin_note  = note
            dispute.resolved_at = timezone.now()
            dispute.save()
            # Mark delivery as delivered so _release_commission works
            delivery = dispute.delivery
            if not delivery.otp_verified:
                delivery.otp_verified = True
                delivery.status       = 'delivered'
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=['otp_verified', 'status', 'delivered_at'])
            _release_commission(delivery)
            return JsonResponse({'ok': True, 'message': 'Dispute resolved — factory payout released.'})

        elif action == 'refund':
            # Admin sides with store — refund the order value to store wallet
            dispute.status      = 'resolved_refund'
            dispute.admin_note  = note
            dispute.resolved_at = timezone.now()
            dispute.save()
            # Refund the full order amount to the store wallet
            delivery = dispute.delivery
            entry = OrderEntry.objects.filter(
                pool=delivery.pool, store=delivery.store, status='active'
            ).first()
            if entry:
                refund_amount = entry.total_amount()
                store = delivery.store
                store.wallet_balance += refund_amount
                store.save(update_fields=['wallet_balance'])
            return JsonResponse({'ok': True, 'message': 'Dispute resolved — store refunded.'})

        else:
            return JsonResponse({'error': "action must be 'payout' or 'refund'."}, status=400)


@login_required
def factory_disputes(request):
    """
    Factory view: all disputes raised against this factory's deliveries.
    Shows open disputes prominently so the factory knows which payouts are locked.
    """
    if not _is_factory_user(request.user):
        return redirect('dashboard')
    factory = _get_factory_or_none(request.user)
    from .models import Dispute
    disputes = (
        Dispute.objects
        .filter(delivery__pool__product__factory=factory)
        .select_related('delivery__store', 'delivery__pool__product')
        .order_by('-raised_at')
    )
    open_count = disputes.filter(status='open').count()
    return render(request, 'core/factory_disputes.html', {
        'factory':    factory,
        'disputes':   disputes,
        'open_count': open_count,
    })


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ORDER HISTORY
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def admin_order_history(request):
    """
    Superuser-only: full order history grouped by pool.

    For every pool that has at least one OrderEntry, shows:
      - Pool metadata (product, city, mode, status, member count)
      - Per-store breakdown: store name, user, qty, unit price, discount, total
      - Server-side aggregates: pool gross total, total commission, total payout

    All monetary calculations are done here — nothing is trusted from the client.
    """
    if not request.user.is_superuser:
        return redirect('dashboard')

    from .models import OrderPool, OrderEntry, CommissionLog
    from django.db.models import Sum, Count, Q

    # Fetch every pool that has at least one entry, newest first
    pools = (
        OrderPool.objects
        .filter(entries__isnull=False)
        .distinct()
        .select_related('product__factory')
        .prefetch_related(
            'entries__store__user',
        )
        .order_by('-created_at')
    )

    # Build enriched pool objects entirely server-side
    pool_rows = []
    grand_orders   = 0
    grand_gross    = Decimal('0')
    grand_commission = Decimal('0')
    grand_payout   = Decimal('0')

    for pool in pools:
        entries = [e for e in pool.entries.all()]
        if not entries:
            continue

        entry_rows = []
        pool_gross = Decimal('0')

        for e in entries:
            gross       = e.total_amount()          # unit_price × (1−disc%) × qty — server-side
            commission  = round(gross * Decimal('0.15'), 2)
            net_payout  = round(gross * Decimal('0.85'), 2)
            escrow_paid = e.escrow_amount or Decimal('0')
            balance_due = round(gross - escrow_paid, 2)

            entry_rows.append({
                'entry':       e,
                'store_name':  e.store.name,
                'username':    e.store.user.username,
                'city':        e.store.city,
                'qty':         e.quantity,
                'unit':        pool.product.unit,
                'unit_price':  e.unit_price_at_order,
                'discount':    e.discount_applied,
                'gross':       gross,
                'commission':  commission,
                'net_payout':  net_payout,
                'escrow_paid': escrow_paid,
                'balance_due': balance_due,
                'status':      e.status,
                'joined_at':   e.joined_at,
            })
            pool_gross += gross

        pool_commission = round(pool_gross * Decimal('0.15'), 2)
        pool_payout     = round(pool_gross * Decimal('0.85'), 2)

        pool_rows.append({
            'pool':            pool,
            'product_name':    pool.product.name,
            'city':            pool.city,
            'mode':            pool.pool_mode,
            'status':          pool.status,
            'member_count':    len(entries),
            'entries':         entry_rows,
            'pool_gross':      pool_gross,
            'pool_commission': pool_commission,
            'pool_payout':     pool_payout,
        })

        grand_orders     += len(entries)
        grand_gross      += pool_gross
        grand_commission += pool_commission
        grand_payout     += pool_payout

    return render(request, 'core/admin_order_history.html', {
        'pool_rows':        pool_rows,
        'grand_orders':     grand_orders,
        'grand_gross':      grand_gross,
        'grand_commission': grand_commission,
        'grand_payout':     grand_payout,
    })


@login_required
def admin_order_history_csv(request):
    """
    Superuser-only: streams the full order history as a UTF-8 CSV download.
    All calculations are server-side — identical logic to admin_order_history.
    """
    import csv
    from django.http import StreamingHttpResponse
    from .models import OrderPool

    if not request.user.is_superuser:
        return redirect('dashboard')

    class Echo:
        """Minimal write-adapter for csv.writer → StreamingHttpResponse."""
        def write(self, value):
            return value

    def generate_rows():
        writer = csv.writer(Echo())

        # Header
        yield writer.writerow([
            'Pool ID', 'Medicine', 'City', 'Mode', 'Pool Status',
            'Store Name', 'Username', 'Store City',
            'Qty', 'Unit', 'Unit Price (₹)', 'Discount (%)',
            'Gross Amount (₹)', 'Platform Commission 15% (₹)',
            'Net Factory Payout 85% (₹)', 'Escrow Paid (₹)',
            'Balance Due (₹)', 'Order Status', 'Joined At',
        ])

        pools = (
            OrderPool.objects
            .filter(entries__isnull=False)
            .distinct()
            .select_related('product')
            .prefetch_related('entries__store__user')
            .order_by('-created_at')
        )

        for pool in pools:
            for e in pool.entries.all():
                gross      = e.total_amount()
                commission = round(gross * Decimal('0.15'), 2)
                net_payout = round(gross * Decimal('0.85'), 2)
                escrow     = e.escrow_amount or Decimal('0')
                balance    = round(gross - escrow, 2)

                yield writer.writerow([
                    str(pool.id),
                    pool.product.name,
                    pool.city,
                    pool.get_pool_mode_display(),
                    pool.get_status_display(),
                    e.store.name,
                    e.store.user.username,
                    e.store.city,
                    e.quantity,
                    pool.product.unit,
                    str(e.unit_price_at_order),
                    str(e.discount_applied),
                    str(gross),
                    str(commission),
                    str(net_payout),
                    str(escrow),
                    str(balance),
                    e.get_status_display(),
                    e.joined_at.strftime('%d %b %Y %H:%M'),
                ])

    response = StreamingHttpResponse(generate_rows(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="bulkmed_order_history.csv"'
    # BOM so Excel opens UTF-8 correctly
    response['X-Accel-Buffering'] = 'no'
    return response


# ── Factory List — Staff Control Panel ───────────────────────────────────────

@login_required
def factory_list_view(request):
    """
    Staff-only page: lists every Factory with its linked username and
    all associated products. Accessible at /control-panel/factories/.

    Uses select_related('user') to avoid N+1 on the OneToOneField and
    prefetch_related('products') to batch-load all product rows in a
    single extra query instead of one per factory.
    """
    if not request.user.is_staff:
        return redirect('dashboard')

    factories = (
        Factory.objects
        .select_related('user')       # JOIN users table — avoids N+1 for username
        .prefetch_related('products') # batch-load products — avoids N+1 per factory
        .order_by('name')
    )
    return render(request, 'core/factory_list.html', {
        'factories':     factories,
        'factory_count': factories.count(),
    })


# ── 3PL Webhook Listener ──────────────────────────────────────────────────────

def logistics_webhook(request):
    """
    Receives real-time tracking status updates from 3PL providers.

    Auth: ?token=<settings.LOGISTICS_WEBHOOK_TOKEN>
    Method: POST
    Content-Type: application/json

    Expected body (provider-agnostic normalised format):
    {
        "waybill_id":    "DEL1234567890",
        "status":        "out_for_delivery",   # picked_up | in_transit | out_for_delivery | delivered | failed
        "status_detail": "Out for delivery in Bangalore",
        "location":      "Bangalore",
        "timestamp":     "2026-05-01T14:30:00+05:30"
    }

    Status mapping to DeliveryTracking.status:
      picked_up        → dispatched  (already dispatched, no change needed)
      in_transit       → dispatched  (no change)
      out_for_delivery → dispatched  (no change — driver is nearby)
      delivered        → delivered   (triggers _release_commission automatically)
      failed           → failed

    Security:
    - Token validated against settings.LOGISTICS_WEBHOOK_TOKEN
    - If token is not set, webhook is disabled (returns 403)
    - All errors are logged; webhook always returns 200 to prevent provider retries
      on our errors (providers retry on non-2xx responses)
    """
    import json
    import logging as _logging
    from django.http import JsonResponse
    from django.conf import settings as _settings
    from django.views.decorators.csrf import csrf_exempt

    logger = _logging.getLogger(__name__)

    # ── Token auth ────────────────────────────────────────────────────────────
    expected_token = getattr(_settings, 'LOGISTICS_WEBHOOK_TOKEN', '')
    if not expected_token:
        logger.warning('logistics_webhook: LOGISTICS_WEBHOOK_TOKEN not set — webhook disabled.')
        return JsonResponse({'error': 'Webhook not configured.'}, status=403)

    provided_token = request.GET.get('token', '')
    if provided_token != expected_token:
        logger.warning('logistics_webhook: invalid token received.')
        return JsonResponse({'error': 'Forbidden'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    # ── Parse body ────────────────────────────────────────────────────────────
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error('logistics_webhook: invalid JSON — %s', exc)
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=200)

    waybill_id    = data.get('waybill_id', '').strip()
    raw_status    = data.get('status', '').strip().lower()
    status_detail = data.get('status_detail', '')
    location      = data.get('location', '')

    if not waybill_id:
        return JsonResponse({'ok': False, 'error': 'waybill_id required'}, status=200)

    # ── Find the delivery ─────────────────────────────────────────────────────
    try:
        delivery = DeliveryTracking.objects.get(waybill_id=waybill_id)
    except DeliveryTracking.DoesNotExist:
        logger.warning('logistics_webhook: no delivery found for waybill %s', waybill_id)
        return JsonResponse({'ok': False, 'error': f'No delivery for waybill {waybill_id}'}, status=200)
    except DeliveryTracking.MultipleObjectsReturned:
        logger.error('logistics_webhook: multiple deliveries for waybill %s', waybill_id)
        return JsonResponse({'ok': False, 'error': 'Ambiguous waybill'}, status=200)

    # ── Map provider status → our status ─────────────────────────────────────
    STATUS_MAP = {
        'picked_up':        'dispatched',
        'in_transit':       'dispatched',
        'out_for_delivery': 'dispatched',
        'delivered':        'delivered',
        'failed':           'failed',
        'rto':              'failed',
    }
    new_status = STATUS_MAP.get(raw_status)

    if not new_status:
        logger.info('logistics_webhook: unrecognised status "%s" for waybill %s — ignored', raw_status, waybill_id)
        return JsonResponse({'ok': True, 'message': 'Status ignored (unrecognised)'}, status=200)

    if delivery.status == new_status:
        return JsonResponse({'ok': True, 'message': 'No status change needed'}, status=200)

    # ── Apply status update ───────────────────────────────────────────────────
    logger.info(
        'logistics_webhook: delivery #%s waybill=%s %s → %s (%s)',
        delivery.id, waybill_id, delivery.status, new_status, status_detail,
    )

    if new_status == 'delivered':
        # Trigger the full payout flow — same as OTP confirmation
        with transaction.atomic():
            delivery.otp_verified = True
            delivery.status       = 'delivered'
            delivery.delivered_at = timezone.now()
            delivery.save(update_fields=['otp_verified', 'status', 'delivered_at'])
            _release_commission(delivery)

        # Notify the store
        from .notifications import push_notification
        push_notification(
            user_id    = delivery.store.user_id,
            notif_type = 'delivery_otp',
            title      = 'Delivery Confirmed ✅',
            body       = (
                f'Your {delivery.pool.product.name} order has been delivered '
                f'by {delivery.logistics_partner or "the courier"}. '
                f'Payment has been released automatically.'
            ),
            url        = '/order-history/',
        )

    elif new_status == 'failed':
        delivery.status = 'failed'
        delivery.save(update_fields=['status'])

    else:
        # dispatched — update GPS location if provided
        delivery.status = 'dispatched'
        if not delivery.dispatched_at:
            delivery.dispatched_at = timezone.now()
        delivery.save(update_fields=['status', 'dispatched_at'])

        # Update TruckLocation with a placeholder if no real coords
        TruckLocation.objects.update_or_create(
            delivery=delivery,
            defaults={'latitude': 20.5937, 'longitude': 78.9629, 'speed_kmh': 0},
        )

    return JsonResponse({
        'ok':         True,
        'delivery_id': delivery.id,
        'new_status':  new_status,
    }, status=200)


# Make the webhook CSRF-exempt (providers POST without a CSRF token)
from django.views.decorators.csrf import csrf_exempt
logistics_webhook = csrf_exempt(logistics_webhook)