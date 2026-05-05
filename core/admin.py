"""
admin.py — Django Admin Panel Configuration
=============================================
Registers all BulkMed models with Django's built-in admin interface.

The admin panel is accessible at /admin/ and is the primary tool for:
- Adding products and creating order pools
- Verifying stores (giving them a blue tick)
- Monitoring deliveries and commission payouts
- Reviewing prediction alerts

Each @admin.register decorator tells Django to show that model
in the admin panel. The ModelAdmin class customises how it looks.

list_display: columns shown in the list view
list_filter:  sidebar filters for quick filtering
search_fields: fields searched when using the search box
actions:      bulk actions available via the "Action" dropdown
"""

from django.contrib import admin
from .models import (
    MedicalStore, Product, Inventory,
    OrderPool, OrderEntry, DeliveryTracking,
    CommissionLog, PredictionAlert, TruckLocation,
    Factory, WalletTransaction, PlatformWallet,
    FactoryOrder, FactoryPayout, WithdrawalRequest, Dispute,
)


@admin.register(MedicalStore)
class MedicalStoreAdmin(admin.ModelAdmin):
    list_display  = ('name', 'city', 'license_no', 'gstin', 'is_verified', 'wallet_balance')
    list_filter   = ('is_verified', 'city')
    search_fields = ('name', 'license_no', 'gstin')
    actions = ['verify_stores']

    def verify_stores(self, request, queryset):
        queryset.update(is_verified=True)
    verify_stores.short_description = "Mark selected stores as verified"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Admin view for the medicine catalog."""
    list_display = ('name', 'sku_code', 'hsn_code', 'factory_name', 'category', 'base_price', 'unit')
    list_filter = ('category',)
    search_fields = ('name', 'generic_name', 'sku_code', 'hsn_code', 'barcode', 'factory_name')


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    """
    Admin view for store inventory.
    is_low_stock() is a model method — shown as a column here.
    """
    list_display = ('store', 'product', 'current_stock', 'threshold', 'is_low_stock')
    list_filter = ('store__city',)


@admin.register(OrderPool)
class OrderPoolAdmin(admin.ModelAdmin):
    """
    Admin view for order pools.
    current_discount() is a model method — shown as a column here.

    save_model override: whenever a pool is saved with status='locked' or
    'fulfilled' via the admin, immediately create the FactoryOrder if one
    does not already exist. This mirrors what the Celery task and management
    command do, so the factory always sees the order regardless of how the
    pool was locked.
    """
    list_display = ('product', 'city', 'current_member_count', 'current_discount', 'status', 'expires_at')
    list_filter  = ('status', 'city')
    actions      = ['backfill_factory_orders']

    def save_model(self, request, obj, form, change):
        """
        After saving, ensure a FactoryOrder exists for locked/fulfilled pools.
        The post_save signal handles this too, but the admin override makes
        the intent explicit and provides a user-facing message.
        """
        super().save_model(request, obj, form, change)

        if obj.status in ('locked', 'fulfilled'):
            from .models import FactoryOrder, OrderEntry
            from decimal import Decimal

            factory = obj.product.factory
            if factory:
                entries     = OrderEntry.objects.filter(pool=obj, status='active')
                total_qty   = sum(e.quantity for e in entries)
                total_value = sum(e.total_amount() for e in entries)

                fo, created = FactoryOrder.objects.get_or_create(
                    pool=obj,
                    defaults={
                        'factory':     factory,
                        'total_qty':   total_qty,
                        'total_value': total_value,
                        'status':      'received',
                    },
                )
                if created:
                    self.message_user(
                        request,
                        f'FactoryOrder #{fo.id} created for {factory.name} '
                        f'({total_qty} units, ₹{total_value}).',
                    )

    @admin.action(description='Backfill missing FactoryOrders for locked/fulfilled pools')
    def backfill_factory_orders(self, request, queryset):
        """
        Admin bulk action: creates FactoryOrders for any selected locked or
        fulfilled pool that is missing one. Use this to repair pools that were
        manually locked before the auto-create signal was added.
        """
        from .models import FactoryOrder, OrderEntry

        created_count = 0
        skipped_count = 0

        for pool in queryset.filter(status__in=('locked', 'fulfilled')):
            factory = pool.product.factory
            if not factory:
                skipped_count += 1
                continue

            entries     = OrderEntry.objects.filter(pool=pool, status='active')
            total_qty   = sum(e.quantity for e in entries)
            total_value = sum(e.total_amount() for e in entries)

            fo, created = FactoryOrder.objects.get_or_create(
                pool=pool,
                defaults={
                    'factory':     factory,
                    'total_qty':   total_qty,
                    'total_value': total_value,
                    'status':      'received',
                },
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1

        self.message_user(
            request,
            f'Backfill complete: {created_count} FactoryOrder(s) created, '
            f'{skipped_count} already existed or skipped (no factory).',
        )


@admin.register(OrderEntry)
class OrderEntryAdmin(admin.ModelAdmin):
    """Admin view for individual store order entries."""
    list_display = ('store', 'pool', 'quantity', 'mode', 'discount_applied', 'total_amount', 'status')
    list_filter = ('mode', 'status')


@admin.register(DeliveryTracking)
class DeliveryTrackingAdmin(admin.ModelAdmin):
    """
    Admin view for delivery records.

    NOTE: DeliveryTracking has no direct ForeignKey to OrderEntry by design.
    The link is always resolved via delivery.pool + delivery.store → OrderEntry.
    The 'Linked Order' column below shows the derived OrderEntry for each row.

    Use the 'Release commission for selected deliveries' action to manually
    trigger _release_commission() for any confirmed delivery that is missing
    its FactoryPayout / WalletTransaction ledger entries.
    """
    list_display   = (
        'id', 'store', 'pool_product', 'pool_city',
        'quantity', 'status', 'delivery_otp', 'otp_verified',
        'linked_order', 'payout_exists',
        'dispatched_at', 'delivered_at',
    )
    list_filter    = ('status', 'otp_verified', 'store__city')
    search_fields  = ('store__name', 'pool__product__name', 'delivery_otp')
    readonly_fields = (
        'delivery_otp', 'otp_verified', 'driver_token',
        'dispatched_at', 'delivered_at',
        'linked_order_detail', 'payout_exists',
    )
    ordering       = ('-id',)
    actions        = ['release_commission_action']

    # ── Computed columns ──────────────────────────────────────────────────

    @admin.display(description='Medicine', ordering='pool__product__name')
    def pool_product(self, obj):
        return obj.pool.product.name

    @admin.display(description='City', ordering='pool__city')
    def pool_city(self, obj):
        return obj.pool.city

    @admin.display(description='Linked OrderEntry')
    def linked_order(self, obj):
        """Shows the OrderEntry derived from pool+store (no direct FK exists)."""
        entry = OrderEntry.objects.filter(
            pool=obj.pool, store=obj.store
        ).exclude(status__in=('cancelled_free', 'cancelled_penalty')).first()
        if entry:
            return f'#{entry.id} — {entry.get_status_display()} — ₹{entry.total_amount()}'
        return '⚠ No OrderEntry found'

    @admin.display(description='Payout Created?', boolean=True)
    def payout_exists(self, obj):
        """True if a FactoryPayout record exists for this delivery."""
        from .models import FactoryPayout
        return FactoryPayout.objects.filter(delivery=obj).exists()

    @admin.display(description='Linked Order (detail)')
    def linked_order_detail(self, obj):
        """Full breakdown shown on the change form."""
        entry = OrderEntry.objects.filter(
            pool=obj.pool, store=obj.store
        ).exclude(status__in=('cancelled_free', 'cancelled_penalty')).first()
        if not entry:
            return (
                'No OrderEntry found for this pool+store combination. '
                'Run: python manage.py fix_orphaned_deliveries --commit'
            )
        from decimal import Decimal
        gross      = entry.total_amount()
        commission = round(gross * Decimal('0.15'), 2)
        net_payout = round(gross * Decimal('0.85'), 2)
        return (
            f'OrderEntry #{entry.id} | Status: {entry.get_status_display()} | '
            f'Qty: {entry.quantity} {obj.pool.product.unit}s | '
            f'Unit price: ₹{entry.unit_price_at_order} | '
            f'Discount: {entry.discount_applied}% | '
            f'Gross: ₹{gross} | Commission (15%): ₹{commission} | '
            f'Net payout (85%): ₹{net_payout}'
        )

    # ── Admin action ──────────────────────────────────────────────────────

    def release_commission_action(self, request, queryset):
        """
        Manually triggers _release_commission() for selected deliveries.
        Safe to run on already-processed deliveries — the function is idempotent.
        """
        from django.db import transaction as dbt
        from core.views import _release_commission

        released = 0
        skipped  = 0
        errors   = []

        for delivery in queryset.filter(otp_verified=True, status='delivered'):
            try:
                with dbt.atomic():
                    result = _release_commission(delivery)
                if result and result > 0:
                    released += 1
                else:
                    skipped += 1
            except Exception as exc:
                errors.append(f'Delivery #{delivery.id}: {exc}')

        msg = f'Released commission for {released} delivery/deliveries.'
        if skipped:
            msg += f' {skipped} already processed (skipped).'
        if errors:
            msg += f' Errors: {"; ".join(errors)}'
            self.message_user(request, msg, level='WARNING')
        else:
            self.message_user(request, msg)

    release_commission_action.short_description = (
        'Release commission for selected confirmed deliveries'
    )


@admin.register(CommissionLog)
class CommissionLogAdmin(admin.ModelAdmin):
    """Admin view for commission records. Shows payout status for financial tracking."""
    list_display = ('pool', 'total_order_value', 'commission_amount', 'payout_status', 'created_at')
    list_filter = ('payout_status',)


@admin.register(PredictionAlert)
class PredictionAlertAdmin(admin.ModelAdmin):
    """Admin view for AI-generated demand alerts."""
    list_display = ('store', 'product', 'predicted_demand', 'demand_spike_date', 'reason', 'is_read')
    list_filter = ('is_read',)


@admin.register(TruckLocation)
class TruckLocationAdmin(admin.ModelAdmin):
    """Admin view for live truck GPS positions."""
    list_display = ('delivery', 'latitude', 'longitude', 'speed_kmh', 'updated_at')


@admin.register(Factory)
class FactoryAdmin(admin.ModelAdmin):
    """Admin view for factory/warehouse locations."""
    list_display  = ('name', 'user', 'city', 'license_no', 'gstin', 'is_verified', 'wallet_balance', 'contact')
    list_filter   = ('is_verified', 'city')
    search_fields = ('name', 'license_no', 'gstin')
    actions       = ['verify_factories']

    def verify_factories(self, request, queryset):
        queryset.update(is_verified=True)
    verify_factories.short_description = "Mark selected factories as verified"


@admin.register(FactoryOrder)
class FactoryOrderAdmin(admin.ModelAdmin):
    """
    Admin view for consolidated factory orders.
    One record per fulfilled pool — shows what the factory needs to produce and ship.
    """
    list_display   = ('id', 'factory', 'pool_product', 'pool_city', 'total_qty', 'total_value', 'status', 'received_at', 'dispatched_at')
    list_filter    = ('status', 'factory__city', 'factory')
    search_fields  = ('factory__name', 'pool__product__name', 'pool__city')
    readonly_fields = ('received_at',)
    ordering       = ('-received_at',)

    @admin.display(description='Medicine', ordering='pool__product__name')
    def pool_product(self, obj):
        return obj.pool.product.name

    @admin.display(description='City', ordering='pool__city')
    def pool_city(self, obj):
        return obj.pool.city


@admin.register(FactoryPayout)
class FactoryPayoutAdmin(admin.ModelAdmin):
    """
    Admin view for factory payout records.
    Payouts are created with status='pending' after OTP confirmation.
    Use the 'Release selected payouts' action to credit the factory wallet.
    """
    list_display   = ('id', 'factory', 'pool_product', 'store_name', 'gross_amount', 'commission_deducted', 'net_payout', 'status', 'created_at', 'paid_at')
    list_filter    = ('status', 'factory__city', 'factory')
    search_fields  = ('factory__name', 'pool__product__name', 'delivery__store__name')
    readonly_fields = ('created_at',)
    ordering       = ('-created_at',)
    actions        = ['release_payouts']

    @admin.display(description='Medicine', ordering='pool__product__name')
    def pool_product(self, obj):
        return obj.pool.product.name

    @admin.display(description='Store')
    def store_name(self, obj):
        return obj.delivery.store.name if obj.delivery else '—'

    def release_payouts(self, request, queryset):
        """
        Credits the factory wallet for all selected pending payouts.
        Creates a WalletTransaction debit on PlatformWallet for each release.
        """
        from django.utils import timezone as tz
        released = 0
        for payout in queryset.filter(status='pending'):
            from django.db import transaction as dbt
            with dbt.atomic():
                # Credit factory wallet
                payout.factory.wallet_balance += payout.net_payout
                payout.factory.save(update_fields=['wallet_balance'])
                # Mark payout as paid
                payout.status  = 'paid'
                payout.paid_at = tz.now()
                payout.save(update_fields=['status', 'paid_at'])
                # Record the outflow from platform wallet
                from .models import PlatformWallet
                PlatformWallet.get().transactions.create(
                    amount           = payout.net_payout,
                    transaction_type = 'debit',
                    description      = (
                        f'Factory payout released — {payout.pool.product.name} '
                        f'→ {payout.factory.name} (FactoryPayout #{payout.id})'
                    ),
                )
                released += 1
        self.message_user(request, f'{released} payout(s) released to factory wallets.')
    release_payouts.short_description = 'Release selected payouts → factory wallet'


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'transaction_type', 'amount', 'description', 'delivery')
    list_filter = ('transaction_type',)
    readonly_fields = ('wallet', 'amount', 'transaction_type', 'description', 'delivery', 'created_at')


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    """Admin view for factory withdrawal requests."""
    list_display  = ('factory', 'amount', 'status', 'requested_at', 'resolved_at', 'admin_note')
    list_filter   = ('status', 'factory__city')
    search_fields = ('factory__name',)
    readonly_fields = ('requested_at',)
    actions = ['approve_withdrawals', 'reject_withdrawals']

    def approve_withdrawals(self, request, queryset):
        from django.utils import timezone
        for wr in queryset.filter(status='pending'):
            wr.status = 'approved'
            wr.resolved_at = timezone.now()
            wr.save()
    approve_withdrawals.short_description = "Approve selected withdrawal requests"

    def reject_withdrawals(self, request, queryset):
        from django.utils import timezone
        from decimal import Decimal
        for wr in queryset.filter(status='pending'):
            wr.factory.wallet_balance += wr.amount
            wr.factory.save(update_fields=['wallet_balance'])
            wr.status = 'rejected'
            wr.resolved_at = timezone.now()
            wr.save()
    reject_withdrawals.short_description = "Reject selected requests (refund balance)"


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    """
    Admin view for disputes. Open disputes = factory payout is locked.
    Payment logic is handled by the post_save signal in models.py —
    this admin class just controls the display.
    """
    list_display    = ('id', 'store_name', 'product_name', 'status', 'raised_at', 'resolved_at', 'admin_note')
    list_filter     = ('status',)
    search_fields   = ('delivery__store__name', 'delivery__pool__product__name', 'reason')
    fields          = ('delivery', 'reason', 'photo', 'status', 'admin_note', 'raised_at', 'resolved_at')
    readonly_fields = ('raised_at', 'resolved_at', 'delivery', 'reason', 'photo')
    ordering        = ('-raised_at',)

    @admin.display(description='Store')
    def store_name(self, obj):
        return obj.delivery.store.name

    @admin.display(description='Medicine')
    def product_name(self, obj):
        return obj.delivery.pool.product.name


# Safety net: unregister and re-register explicitly
try:
    admin.site.unregister(Dispute)
except admin.sites.NotRegistered:
    pass
admin.site.register(Dispute, DisputeAdmin)
