"""
emails.py — Email Notification System
=======================================
Handles all outgoing emails sent by BulkMed.

Currently uses Django's console email backend (prints to terminal).
To send real emails in production, update settings.py:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.gmail.com'
    EMAIL_HOST_USER = 'your@email.com'
    EMAIL_HOST_PASSWORD = 'your_app_password'

All functions use fail_silently=True so email failures never
crash the order flow — the order is saved regardless of email status.
"""

from django.core.mail import send_mail
from django.conf import settings


def send_order_confirmation(entry):
    """
    Sends a confirmation email to the store owner after they join a pool.

    Triggered in views.py → pool_detail() immediately after the
    OrderEntry is saved.

    The email includes:
    - Full order details (medicine, quantity, mode, discount, total)
    - Estimated arrival date and time
    - The cancellation policy (free while open, ₹200 penalty after lock)

    Args:
        entry: An OrderEntry instance (must already be saved so
               estimated_arrival and joined_at are populated)
    """
    store = entry.store
    product = entry.pool.product

    # Format the estimated arrival datetime for the email body
    arrival = (
        entry.estimated_arrival.strftime("%A, %d %b %Y at %I:%M %p")
        if entry.estimated_arrival
        else "TBD"
    )

    # Human-readable mode label
    mode_label = (
        "Fast Track (24h delivery)"
        if entry.mode == 'urgent'
        else "Pool Mode (~72h after pool closes)"
    )

    subject = f"BulkMed Order Confirmed — {product.name}"

    # Plain-text email body with order summary and policy notice
    message = f"""Dear {store.name},

Your order has been successfully placed on BulkMed.

ORDER DETAILS
─────────────────────────────
Medicine      : {product.name}
Quantity      : {entry.quantity} {product.unit}s
Order Mode    : {mode_label}
Discount      : {entry.discount_applied}%
Total Amount  : ₹{entry.total_amount()}
Est. Arrival  : {arrival}

IMPORTANT POLICY NOTICE
─────────────────────────────
✅ You may add other medicines to your order while the pool is still active.
✅ You can cancel this order FREE OF CHARGE while the pool is open.

⚠️  Once the pool is LOCKED (countdown hits zero), cancellation is NOT allowed.
⚠️  Any cancellation after lock-in will incur a penalty of ₹{entry.CANCELLATION_PENALTY}.

Track your order at: http://localhost:8000/dashboard/

Thank you for using BulkMed.
Team BulkMed
"""

    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,  # "BulkMed <noreply@bulkmed.in>"
            [store.user.email],           # recipient is the store's registered email
            fail_silently=True,           # don't crash if email fails
        )
    except Exception:
        # Silently ignore all email errors — order flow must not be interrupted
        pass


def send_cancellation_notice(entry, penalty_applied):
    """
    Sends a cancellation confirmation email to the store owner.

    Triggered in views.py → cancel_order() after the cancellation
    is processed.

    Two scenarios:
    1. penalty_applied=False → pool was open, free cancellation
    2. penalty_applied=True  → pool was locked, ₹200 deducted from wallet

    Args:
        entry:           The OrderEntry that was cancelled
        penalty_applied: Boolean — True if ₹200 was charged
    """
    store = entry.store
    product = entry.pool.product
    subject = f"BulkMed Order Cancelled — {product.name}"

    if penalty_applied:
        # Penalty was charged — show deduction and remaining balance
        body = f"""Dear {store.name},

Your order for {product.name} has been cancelled.

A penalty of ₹{entry.CANCELLATION_PENALTY} has been deducted from your wallet
because the pool was already locked at the time of cancellation.

Remaining wallet balance: ₹{store.wallet_balance}

Team BulkMed
"""
    else:
        # Free cancellation — pool was still open
        body = f"""Dear {store.name},

Your order for {product.name} has been cancelled successfully.

No charges have been applied as the pool was still open.

Team BulkMed
"""

    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [store.user.email],
            fail_silently=True,
        )
    except Exception:
        pass


# ── FACTORY NOTIFICATIONS ─────────────────────────────────────────────────────

def send_factory_order_notification(factory_order):
    """
    Notifies the factory when a new consolidated order is assigned to them.
    Triggered when a pool locks and a FactoryOrder record is created.

    Includes:
    - Medicine name, city, total quantity, gross value, net payout (85%)
    - Link to the factory order management page

    Args:
        factory_order: A FactoryOrder instance (already saved)
    """
    factory = factory_order.factory
    if not factory.user or not factory.user.email:
        return  # no email address on file — skip silently

    product = factory_order.pool.product
    subject = f"BulkMed New Order — {product.name} ({factory_order.pool.city})"

    message = f"""Dear {factory.name},

A new bulk order has been assigned to your factory on BulkMed.

ORDER DETAILS
─────────────────────────────
Medicine      : {product.name}
City          : {factory_order.pool.city}
Total Qty     : {factory_order.total_qty} {product.unit}s
Gross Value   : ₹{factory_order.total_value}
Your Payout   : ₹{factory_order.net_payout()} (85% after platform commission)
Received At   : {factory_order.received_at.strftime("%d %b %Y at %I:%M %p")}

NEXT STEPS
─────────────────────────────
1. Log in to your factory dashboard.
2. Review the per-store breakdown.
3. Click "Accept Order" to confirm you can fulfill.
4. Click "Dispatch" once the trucks are loaded.

Manage your orders at: http://localhost:8000/factory/orders/

Thank you for partnering with BulkMed.
Team BulkMed
"""

    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [factory.user.email],
            fail_silently=True,
        )
    except Exception:
        pass


def send_factory_payout_notification(factory_payout):
    """
    Notifies the factory when a payout is credited to their wallet.
    Triggered when a store confirms delivery via OTP and _release_commission()
    creates a FactoryPayout record with status='paid'.

    Includes:
    - Medicine name, store name, gross amount, commission deducted, net payout
    - Updated wallet balance

    Args:
        factory_payout: A FactoryPayout instance (already saved, status='paid')
    """
    factory = factory_payout.factory
    if not factory.user or not factory.user.email:
        return

    product  = factory_payout.pool.product
    store    = factory_payout.delivery.store if factory_payout.delivery else None
    subject  = f"BulkMed Payment Received — ₹{factory_payout.net_payout} for {product.name}"

    message = f"""Dear {factory.name},

A payment has been credited to your BulkMed wallet.

PAYMENT DETAILS
─────────────────────────────
Medicine          : {product.name}
Store             : {store.name if store else 'N/A'}
Gross Order Value : ₹{factory_payout.gross_amount}
Platform Fee (15%): −₹{factory_payout.commission_deducted}
Net Credited (85%): ₹{factory_payout.net_payout}
Paid At           : {factory_payout.paid_at.strftime("%d %b %Y at %I:%M %p") if factory_payout.paid_at else "N/A"}

WALLET BALANCE
─────────────────────────────
Current Balance: ₹{factory.wallet_balance}

View your wallet at: http://localhost:8000/factory/wallet/

Thank you for partnering with BulkMed.
Team BulkMed
"""

    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [factory.user.email],
            fail_silently=True,
        )
    except Exception:
        pass
