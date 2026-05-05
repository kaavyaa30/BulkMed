"""
models.py — BulkMed Database Schema
=====================================
Defines all database tables (as Django model classes) for the platform.
Each class = one table. Each field = one column.

Relationships at a glance:
  User (Django built-in)
    └── MedicalStore (one store per user account)
          └── Inventory (many products per store)
          └── OrderEntry (many pool participations per store)
    └── Factory (one factory per user account)
          └── FactoryOrder (one consolidated order per fulfilled pool)
          └── FactoryPayout (one payout record per OTP-confirmed delivery)

  Product
    └── OrderPool (one pool per product per city)
          └── OrderEntry (many stores per pool)
          └── DeliveryTracking (one delivery per store per pool)
                └── TruckLocation (live GPS for the delivery truck)
          └── CommissionLog (one commission record per pool)
          └── FactoryOrder (one consolidated order sent to the factory)

  PredictionAlert (AI-generated demand warnings per store per product)
"""

from django.db import models, transaction
from django.contrib.auth.models import User  # Django's built-in user model
from django.utils import timezone
import uuid
from decimal import Decimal


# ─────────────────────────────────────────────
# 0. FACTORY
# ─────────────────────────────────────────────

class Factory(models.Model):
    """
    Represents a medicine manufacturing facility (or BulkMed warehouse hub).

    Mirrors MedicalStore in structure — each factory owner has one Django
    User account linked via a OneToOneField (null=True so that warehouse
    hubs seeded without a user account still work).

    Key fields:
    - user: the factory owner's login account (null for admin-seeded hubs)
    - license_no: drug manufacturing license, unique per factory
    - is_verified: admin verifies the factory before it can receive orders
    - wallet_balance: factory's earnings wallet, credited with 85% of each
      order value when the store confirms delivery via OTP
    - latitude/longitude: used as the truck dispatch origin on the tracking map
    """

    # null=True allows warehouse hubs seeded by seed_data.py (no user account) to exist
    user = models.OneToOneField(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='factory'
    )

    name = models.CharField(max_length=255)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Drug manufacturing license — unique per facility; null=True avoids unique
    # constraint collisions on existing rows that have no license yet
    license_no = models.CharField(max_length=100, unique=True, null=True, blank=True)
    contact = models.CharField(max_length=15, blank=True)

    # Admin verifies the factory after reviewing their license documents
    is_verified = models.BooleanField(default=False)

    # Earnings wallet — credited with 85% of order value on OTP confirmation
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # GST Identification Number for B2B invoicing
    gstin = models.CharField(max_length=15, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({'✓' if self.is_verified else 'Unverified'}) — {self.city}"

    class Meta:
        verbose_name_plural = 'Factories'


# ─────────────────────────────────────────────
# 1. MEDICAL STORE
# ─────────────────────────────────────────────

class MedicalStore(models.Model):
    """
    Represents a registered pharmacy on the platform.

    Each pharmacy owner creates one Django User account, and that account
    is linked here via a OneToOneField. This model stores all the
    business-specific details about the pharmacy.

    Key fields:
    - wallet_balance: acts as an escrow wallet. Money is deposited here
      before orders and released to factories after OTP confirmation.
    - is_verified: admin manually verifies stores to give them a blue tick
      on the map and increase trust.
    - latitude/longitude: used for geofencing — grouping nearby stores
      into the same order pool.
    """

    # Link to Django's built-in User model (login credentials live there)
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='store'
    )

    name = models.CharField(max_length=255)           # e.g. "Apollo Pharmacy"
    address = models.TextField()                       # full street address
    license_no = models.CharField(max_length=100, unique=True)  # drug license number
    contact = models.CharField(max_length=15)          # phone number
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    city = models.CharField(max_length=100, blank=True)  # used for pool grouping

    # Admin sets this to True after verifying the store's license
    is_verified = models.BooleanField(default=False)

    # Escrow wallet — stores deposit funds here before ordering
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # GST Identification Number for B2B invoicing
    gstin = models.CharField(max_length=15, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        # Shows verification status in admin panel for quick identification
        return f"{self.name} ({'✓' if self.is_verified else 'Unverified'})"


# ─────────────────────────────────────────────
# 2. PRODUCT (MEDICINE CATALOG)
# ─────────────────────────────────────────────

class Product(models.Model):
    """
    Represents a medicine available for bulk ordering.

    The platform admin adds products here. Stores don't add products —
    they only order from this catalog. The base_price is the factory's
    standard price BEFORE any bulk discount is applied.
    """

    CATEGORY_CHOICES = [
        ('antibiotic', 'Antibiotic'),
        ('analgesic', 'Analgesic'),
        ('vitamin', 'Vitamin'),
        ('antiviral', 'Antiviral'),
        ('other', 'Other'),
    ]

    name = models.CharField(max_length=255)            # e.g. "Dolo 650"
    generic_name = models.CharField(max_length=255, blank=True)  # e.g. "Paracetamol"
    sku_code = models.CharField(max_length=100, unique=True, db_index=True, blank=True, default='')
    hsn_code = models.CharField(max_length=20, blank=True, default='')
    barcode = models.CharField(max_length=100, unique=True, null=True, blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    factory_name = models.CharField(max_length=255)
    factory = models.ForeignKey('Factory', on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='other')
    unit = models.CharField(max_length=50, default='strip')
    image = models.ImageField(upload_to='products/', null=True, blank=True)

    # Expiry & availability — used by the auto-disable background task
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text='Batch expiry date. Products within 30 days of expiry are auto-disabled.'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive products are hidden from pools and search. Auto-set False near expiry.'
    )

    def __str__(self):
        return f"{self.name} by {self.factory_name}"


# ─────────────────────────────────────────────
# 3. INVENTORY
# ─────────────────────────────────────────────

class Inventory(models.Model):
    """
    Tracks how much stock a specific store has for a specific product.

    The AI prediction engine reads this to decide whether to send an alert.
    If current_stock drops to or below threshold, the store sees a
    "Low Stock" warning on their dashboard.

    unique_together ensures one inventory record per (store, product) pair —
    you can't have two rows for the same store + medicine combination.
    """

    store = models.ForeignKey(MedicalStore, on_delete=models.CASCADE, related_name='inventory')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    current_stock = models.PositiveIntegerField(default=0)   # how many units in stock right now
    threshold = models.PositiveIntegerField(default=10)       # alert when stock falls to this level
    last_updated = models.DateTimeField(auto_now=True)        # auto-updates on every save

    class Meta:
        unique_together = ('store', 'product')  # one row per store-product pair

    def is_low_stock(self):
        """Returns True if current stock is at or below the alert threshold."""
        return self.current_stock <= self.threshold

    def __str__(self):
        return f"{self.store.name} - {self.product.name}: {self.current_stock}"


# ─────────────────────────────────────────────
# 4. ORDER POOL
# ─────────────────────────────────────────────

class OrderPool(models.Model):
    """
    The core model of BulkMed — represents a group buying event for one medicine.

    How it works:
    1. Admin creates a pool for a product in a city with an expiry time.
    2. Stores join the pool (creating OrderEntry records).
    3. As more stores join, the discount tier increases for EVERYONE.
    4. When the timer expires, the pool locks and the consolidated order
       goes to the factory.

    Discount tiers (TIER_DISCOUNTS):
      3 stores  → 5% off
      5 stores  → 10% off
      10 stores → 15% off

    Delivery time estimates (DELIVERY_HOURS):
      urgent → 24 hours from order
      pool   → 72 hours after pool closes (+ 24h processing = ~96h total)
    """

    STATUS_CHOICES = [
        ('open', 'Open'),           # accepting new store joins
        ('locked', 'Locked'),       # countdown expired, being processed
        ('fulfilled', 'Fulfilled'), # factory shipped, deliveries created
        ('cancelled', 'Cancelled'), # pool cancelled (not enough stores, etc.)
    ]

    # Discount formula: max(2, floor(members/5)*5) — infinite scaling, no cap.
    # Kept for reference only; current_discount() uses the formula directly.
    # Examples: 5→5%, 10→10%, 20→25%, 50→55%, 100→105%

    # Pool open windows (hours before the pool locks)
    POOL_WINDOW_HOURS = {
        'urgent': 2,   # Fast Track: 2-hour window, then locks → delivered within 24h
        'pool':   72,  # Pool Mode: 3-day window, then locks → delivered ~72h after close
    }

    # Maps order mode → hours until delivery after pool locks
    DELIVERY_HOURS = {
        'urgent': 24,  # Fast Track: dispatched within 24h of order
        'pool':   72,  # Pool Mode: delivered ~72h after pool closes
    }

    # UUID as primary key — safer than sequential integers for public URLs
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='pools')
    total_qty = models.PositiveIntegerField(default=0)           # sum of all entries' quantities
    current_member_count = models.PositiveIntegerField(default=0) # how many stores have joined
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    expires_at = models.DateTimeField()                           # when the pool closes
    created_at = models.DateTimeField(auto_now_add=True)
    city = models.CharField(max_length=100, blank=True)          # pools are city-specific
    dispatched_at = models.DateTimeField(null=True, blank=True)  # when factory dispatched the order
    pool_mode = models.CharField(max_length=10, choices=[('urgent','Fast Track'),('pool','Pool Mode')], default='pool')  # determines SLA

    MAX_MEMBERS = 15  # Pool closes to new joins once this is reached

    # Strict tier discounts
    # 1–4 stores  → 1%
    # 5–9 stores  → 5%
    # 10–15 stores → 10% (maximum)
    TIER_DISCOUNTS = [(1, 1), (5, 5), (10, 10)]

    def current_discount(self):
        """
        Strict tier-based discount, capped at 10%.
        1–4 stores=1%, 5–9 stores=5%, 10–15 stores=10%.
        """
        discount = 0
        for threshold, pct in self.TIER_DISCOUNTS:
            if self.current_member_count >= threshold:
                discount = pct
        return discount

    def next_tier_info(self):
        """
        Returns (stores_needed, next_discount_pct) for the next tier.
        Returns (0, 10) if already at max discount or pool is full.
        """
        if self.current_discount() >= 10 or self.current_member_count >= self.MAX_MEMBERS:
            return (0, 10)
        for threshold, pct in self.TIER_DISCOUNTS:
            if self.current_member_count < threshold:
                return (threshold - self.current_member_count, pct)
        return (0, 10)

    def is_full(self):
        """Returns True when the pool has reached MAX_MEMBERS (15)."""
        return self.current_member_count >= self.MAX_MEMBERS

    def cancellation_locked(self):
        """Returns True if pool is past 80% of its lifespan — no cancellations allowed."""
        if self.status != 'open':
            return True
        total_duration = (self.expires_at - self.created_at).total_seconds()
        elapsed = (timezone.now() - self.created_at).total_seconds()
        return elapsed >= (total_duration * 0.8)

    def cancellation_lockout_time(self):
        """Returns the datetime when cancellations will be locked (80% mark)."""
        total_duration = self.expires_at - self.created_at
        return self.created_at + (total_duration * 0.8)

    def is_expired(self):
        """Returns True if the pool's countdown has hit zero."""
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"Pool: {self.product.name} | {self.current_member_count} stores | {self.status}"


# ─────────────────────────────────────────────
# 5. ORDER ENTRY
# ─────────────────────────────────────────────

class OrderEntry(models.Model):
    """
    Records a single store's participation in an OrderPool.

    When a store clicks "Join Pool", one OrderEntry is created.
    This tracks what they ordered, at what price, with what discount,
    and when they can expect delivery.

    Cancellation policy (enforced in views.py):
    - While pool is OPEN → free cancellation (status = 'cancelled_free')
    - After pool LOCKS → ₹200 penalty (status = 'cancelled_penalty')
    """

    MODE_CHOICES = [
        ('urgent', 'Fast Track'),  # pay now, get delivery in 24h
        ('pool', 'Pool Mode'),     # wait for pool to fill, get higher discount
    ]

    STATUS_CHOICES = [
        ('active',             'Active'),
        ('delivered',          'Delivered'),
        ('cancelled_free',     'Cancelled (No Charge)'),
        ('cancelled_penalty',  'Cancelled (Penalty Applied)'),
    ]

    # Fixed penalty amount for cancelling after pool locks
    CANCELLATION_PENALTY = 200  # ₹200

    pool = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='entries')
    store = models.ForeignKey(MedicalStore, on_delete=models.CASCADE, related_name='order_entries')
    quantity = models.PositiveIntegerField()

    # urgent = Fast Track (24h), pool = Pool Mode (72h after close)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='pool')

    # Price locked at time of joining (discount may increase later as more stores join)
    unit_price_at_order = models.DecimalField(max_digits=10, decimal_places=2)
    discount_applied = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    joined_at = models.DateTimeField(auto_now_add=True)

    # Computed and stored when the entry is created (see compute_estimated_arrival)
    estimated_arrival = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    penalty_charged = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    escrow_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # 10% advance deposit

    class Meta:
        unique_together = ('pool', 'store')  # a store can only join a pool once

    def total_amount(self):
        """
        Calculates the total payable amount after applying the discount.
        Formula: unit_price × (1 - discount/100) × quantity
        """
        discounted_price = self.unit_price_at_order * (1 - self.discount_applied / Decimal('100'))
        return round(discounted_price * self.quantity, 2)

    def compute_escrow(self):
        """10% of total order value as advance deposit."""
        return round(self.total_amount() * Decimal('0.10'), 2)

    def compute_estimated_arrival(self):
        """
        Calculates when the store can expect their delivery.

        Called BEFORE the entry is saved (so joined_at is not yet set).
        Falls back to timezone.now() if joined_at is None.

        Logic:
        - Urgent mode: arrival = now + 24 hours
        - Pool mode: arrival = max(pool expiry, now) + 24h processing + 72h delivery
          The max() ensures we don't calculate an arrival in the past if the
          pool has already expired.
        """
        from datetime import timedelta
        from django.utils import timezone as tz

        hours = OrderPool.DELIVERY_HOURS.get(self.mode, 72)
        now = self.joined_at if self.joined_at else tz.now()

        if self.mode == 'urgent':
            # Fast Track: 24h from order time
            return now + timedelta(hours=24)
        else:
            # Pool Mode: pool closes + 7 days SLA
            base = max(self.pool.expires_at, now)
            return base + timedelta(days=7)

    def hours_until_arrival(self):
        """
        Returns how many hours remain until the estimated arrival.
        Returns 0 if the arrival time has already passed.
        Used to show the countdown badge on the dashboard.
        """
        if not self.estimated_arrival:
            # If not yet computed, calculate it on the fly
            self.estimated_arrival = self.compute_estimated_arrival()
        diff = self.estimated_arrival - timezone.now()
        return max(0, int(diff.total_seconds() / 3600))

    def __str__(self):
        return f"{self.store.name} → {self.pool.product.name} ({self.mode})"


# ─────────────────────────────────────────────
# 6. TRUCK LOCATION (LIVE GPS)
# ─────────────────────────────────────────────

class TruckLocation(models.Model):
    """
    Stores the real-time GPS position of the delivery truck.

    OneToOneField means each DeliveryTracking record has at most one
    TruckLocation. When the driver/admin pushes a new GPS coordinate,
    we use update_or_create() to overwrite the existing record rather
    than creating a new one — so there's always just one "current" position.

    auto_now=True on updated_at means it automatically records the
    timestamp of the last GPS update without any manual code.
    """

    # Each delivery has exactly one truck location record
    delivery = models.OneToOneField(
        'DeliveryTracking', on_delete=models.CASCADE, related_name='truck_location'
    )

    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    speed_kmh = models.FloatField(default=0)       # current speed in km/h (from driver app)
    updated_at = models.DateTimeField(auto_now=True)  # auto-stamps every GPS update

    def __str__(self):
        return f"Truck @ ({self.latitude}, {self.longitude}) — {self.delivery}"


# ─────────────────────────────────────────────
# 7. DELIVERY TRACKING
# ─────────────────────────────────────────────

class DeliveryTracking(models.Model):
    """
    Tracks the physical delivery of medicines from factory to store.

    This is the drop-shipping record. When a pool is fulfilled, the admin
    creates one DeliveryTracking per store in the pool. The factory ships
    directly to each store.

    OTP-based confirmation (escrow release mechanism):
    1. Admin sets a 6-digit delivery_otp when creating the delivery.
    2. The delivery agent gives this OTP to the store owner on arrival.
    3. Store owner enters OTP on the platform → otp_verified = True.
    4. This triggers the escrow release in views.py (CommissionLog updated).

    This ensures the factory only gets paid AFTER confirmed delivery —
    zero payment disputes.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),       # delivery created, not yet dispatched
        ('dispatched', 'Dispatched'), # truck is on the way (TruckLocation exists)
        ('delivered', 'Delivered'),   # OTP confirmed, payment released
        ('failed', 'Failed'),         # delivery attempt failed
    ]

    pool = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='deliveries')
    store = models.ForeignKey(MedicalStore, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    delivery_otp = models.CharField(max_length=6, blank=True)  # 6-digit code given to delivery agent
    otp_verified = models.BooleanField(default=False)           # True after store confirms receipt

    # One-time token embedded in the driver PWA URL — no login required for GPS push
    driver_token = models.CharField(max_length=32, blank=True, default='',
        help_text='Tokenised auth for the driver PWA. Auto-generated on first save.')

    estimated_arrival = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)  # set when OTP is verified

    # ── 3PL Logistics fields ──────────────────────────────────────────────────
    # Populated when the factory dispatches via a 3PL provider (Delhivery, Shadowfax, etc.)
    # Left blank for local/manual deliveries using the built-in driver PWA.

    DELIVERY_METHOD_CHOICES = [
        ('local',  'Local Contract Driver'),   # built-in driver PWA + GPS tracking
        ('3pl',    '3PL API (Delhivery etc.)'), # third-party logistics API
    ]
    delivery_method = models.CharField(
        max_length=10, choices=DELIVERY_METHOD_CHOICES, default='local',
        help_text='local = built-in driver PWA; 3pl = third-party logistics API.'
    )
    logistics_partner = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Provider name, e.g. "Delhivery" or "Shadowfax". Blank for local deliveries.'
    )
    shipping_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Shipping cost charged by the 3PL provider in ₹. '
                  'Deducted from the factory\'s net payout.'
    )
    waybill_id = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Tracking/waybill number assigned by the 3PL provider.'
    )
    shipping_label_url = models.URLField(
        max_length=500, blank=True, default='',
        help_text='URL to the PDF shipping label from the 3PL provider.'
    )

    def save(self, *args, **kwargs):
        if not self.driver_token:
            import secrets
            self.driver_token = secrets.token_hex(16)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Delivery: {self.store.name} | {self.status}"


# ─────────────────────────────────────────────
# 8. COMMISSION LOG
# ─────────────────────────────────────────────

class CommissionLog(models.Model):
    """
    Records the platform's 5% commission for each fulfilled order pool.

    How the money flows:
    1. Store deposits money into their wallet (wallet_balance on MedicalStore).
    2. When pool is fulfilled, a CommissionLog is created with payout_status='pending'.
    3. When the store confirms delivery via OTP, payout_status → 'released'.
    4. At that point: factory receives 95% of total_order_value,
       platform keeps commission_amount (5%).

    This is the escrow model — money is held until delivery is confirmed.
    """

    PAYOUT_STATUS = [
        ('pending', 'Pending'),   # waiting for delivery confirmation
        ('released', 'Released'), # OTP confirmed, factory paid, commission taken
    ]

    pool = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='commissions')
    total_order_value = models.DecimalField(max_digits=12, decimal_places=2)  # total value of all entries
    commission_rate = models.DecimalField(max_digits=4, decimal_places=2, default=5.00)  # always 5%
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)  # = total × 0.05
    payout_status = models.CharField(max_length=20, choices=PAYOUT_STATUS, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)  # timestamp of escrow release

    def __str__(self):
        return f"Commission ₹{self.commission_amount} | {self.payout_status}"


# ─────────────────────────────────────────────
# 9. PREDICTION ALERT
# ─────────────────────────────────────────────

class PredictionAlert(models.Model):
    """
    Stores AI-generated demand spike warnings for a store.

    Generated by the prediction engine (core/prediction.py) which runs
    daily via: python manage.py run_predictions

    The engine looks 15 days ahead and checks if seasonal patterns
    (monsoon, winter) suggest a demand spike for any medicine in the
    store's inventory. If predicted demand > current stock, an alert
    is created here.

    Alerts appear on the store's dashboard with a "Join Pool Now" button.
    is_read allows the store to dismiss alerts they've seen.
    """

    store = models.ForeignKey(MedicalStore, on_delete=models.CASCADE, related_name='alerts')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    predicted_demand = models.PositiveIntegerField()       # estimated units needed
    alert_date = models.DateField()                        # when this alert was generated
    demand_spike_date = models.DateField()                 # when the spike is expected (15 days out)
    reason = models.CharField(max_length=255, blank=True)  # e.g. "Monsoon season — fever spike expected"
    is_read = models.BooleanField(default=False)           # True once store dismisses the alert
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Alert: {self.store.name} → {self.product.name} on {self.demand_spike_date}"


# ─────────────────────────────────────────────
# 10. PLATFORM WALLET
# ─────────────────────────────────────────────

class PlatformWallet(models.Model):
    """
    Singleton — balance is computed from WalletTransaction sum, never set directly.
    Call PlatformWallet.get() to retrieve the single instance.
    """
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    total_earned = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    last_updated = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        wallet, _ = cls.objects.get_or_create(id=1)
        return wallet

    def credit(self, amount, description='Commission', delivery=None,
               gateway_fee_amount=None, gateway_gst_amount=None):
        """
        Create a Credit transaction. Balance is synced via post_save signal.
        Direct balance += is intentionally removed.

        gateway_fee_amount / gateway_gst_amount: pass these for Razorpay-charged
        transactions so the ledger records the true net received amount.
        """
        from decimal import Decimal as _D
        kwargs = dict(
            wallet=self,
            amount=_D(str(amount)),
            transaction_type='credit',
            description=description,
            delivery=delivery,
        )
        if gateway_fee_amount is not None:
            kwargs['gateway_fee_amount'] = _D(str(gateway_fee_amount))
        if gateway_gst_amount is not None:
            kwargs['gateway_gst_amount'] = _D(str(gateway_gst_amount))
        WalletTransaction.objects.create(**kwargs)

    def recalculate(self):
        """Recompute balance from all transactions. Called by post_save signal."""
        from django.db.models import Sum
        agg = self.transactions.aggregate(
            credits=Sum('amount', filter=models.Q(transaction_type='credit')),
            debits=Sum('amount',  filter=models.Q(transaction_type='debit')),
        )
        credits = agg['credits'] or 0
        debits  = agg['debits']  or 0
        self.balance = credits - debits
        self.total_earned = credits
        self.save(update_fields=['balance', 'total_earned', 'last_updated'])

    def __str__(self):
        return f"Platform Wallet: ₹{self.balance}"


class WalletTransaction(models.Model):
    """
    Immutable ledger entry. Every money movement creates one of these.
    The PlatformWallet.balance is always the sum of all transactions.

    Gateway Fee Fields (gateway_fee_amount, gateway_gst_amount)
    ────────────────────────────────────────────────────────────
    Razorpay charges 2% of the transaction amount as a payment gateway fee,
    and the Government of India levies 18% GST on that fee. These two fields
    record those deductions directly on the transaction row so that:

      1. The Admin Control Panel can compute TRUE net revenue without a 2% mismatch:
            net_received = amount - gateway_fee_amount - gateway_gst_amount

      2. The Financial Audit Trail can show a separate "Gateway Cost" column
         that reconciles exactly with Razorpay's bank settlement report.

      3. Profit calculations are always: commission_earned - total_gateway_costs

    These fields are populated only for transactions that go through Razorpay
    (escrow_advance, topup_credit). Internal ledger movements (commission,
    escrow_refund, factory_payout) leave them at 0 — no gateway fee applies.

    Label Choices
    ─────────────
    escrow_advance  — 10% advance deposit when a store joins a pool (Razorpay)
    final_payment   — 90% final payment collected on OTP confirmation (internal)
    commission      — Platform's 15% commission cut (internal)
    escrow_refund   — Refund of escrow advance on free cancellation (internal)
    factory_payout  — 85% payout to factory (internal)
    dispute_refund  — Full refund to store on dispute resolution (internal)
    topup_credit    — Direct wallet top-up via Razorpay (Razorpay)
    gateway_fee     — Standalone gateway fee debit row (reserved for future use)
    other           — Catch-all for legacy / miscellaneous entries
    """
    TYPE_CHOICES = [('credit', 'Credit'), ('debit', 'Debit')]

    # Human-readable audit label — shown in the Financial Audit Trail
    LABEL_CHOICES = [
        ('escrow_advance',   '+10% Advance (Escrow)'),
        ('final_payment',    '+90% Final Payment'),
        ('commission',       'Platform Commission (15%)'),
        ('escrow_refund',    'Escrow Refund'),
        ('factory_payout',   'Factory Payout (85%)'),
        ('dispute_refund',   'Dispute Refund'),
        ('topup_credit',     'Wallet Top-up (Razorpay)'),
        ('gateway_fee',      'Payment Gateway Fee'),
        ('shipping_cost',    '3PL Shipping Cost'),
        ('other',            'Other'),
    ]

    # Labels that involve a Razorpay charge — used for gateway cost aggregation
    RAZORPAY_LABELS = frozenset({'escrow_advance', 'topup_credit'})

    wallet           = models.ForeignKey(PlatformWallet, on_delete=models.CASCADE, related_name='transactions')
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=6, choices=TYPE_CHOICES)
    description      = models.CharField(max_length=255, blank=True)
    delivery         = models.ForeignKey('DeliveryTracking', on_delete=models.SET_NULL, null=True, blank=True, related_name='wallet_transactions')
    # Links a transaction back to the specific store order for the audit trail
    order_entry      = models.ForeignKey('OrderEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='wallet_transactions')
    transaction_label = models.CharField(max_length=20, choices=LABEL_CHOICES, default='other')

    # ── Razorpay Gateway Fee Components ──────────────────────────────────────
    # Populated for every transaction that goes through Razorpay (escrow_advance,
    # topup_credit). Zero for internal ledger movements (commission, refunds, etc.)
    # These fields let the admin reconcile platform revenue against bank settlements
    # without a 2% mismatch.
    #
    # Invariant (when non-zero):
    #   gateway_gst_amount  = round(gateway_fee_amount × 0.18, 2)
    #   total_fee           = gateway_fee_amount + gateway_gst_amount
    #   net_received        = amount - total_fee
    gateway_fee_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='Razorpay 2% gateway fee charged on this transaction. '
                  'Zero for internal ledger movements.'
    )
    gateway_gst_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='18% GST levied on the gateway fee. '
                  'Zero for internal ledger movements.'
    )

    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def total_gateway_cost(self):
        """Total Razorpay cost = gateway fee + GST on fee."""
        return self.gateway_fee_amount + self.gateway_gst_amount

    @property
    def net_received(self):
        """
        Amount actually received by the platform after Razorpay deductions.
        For internal movements (no gateway fee) this equals `amount`.
        For Razorpay transactions: amount - gateway_fee - gateway_gst.
        """
        return self.amount - self.total_gateway_cost

    def __str__(self):
        sign = '+' if self.transaction_type == 'credit' else '-'
        return f"{sign}₹{self.amount} — {self.description} ({self.created_at:%d %b %Y})"


# ─────────────────────────────────────────────
# 11. FACTORY ORDER
# ─────────────────────────────────────────────

class FactoryOrder(models.Model):
    """
    Consolidated order sent to a factory when a pool locks.

    One FactoryOrder is created per fulfilled pool. It aggregates all
    individual store OrderEntry records into a single production and
    dispatch instruction for the factory.

    Lifecycle:
      received   → pool just locked, factory sees the order on their dashboard
      processing → factory confirmed they can fulfill, preparing stock
      dispatched → factory has shipped; DeliveryTracking records already exist
      completed  → all deliveries for this pool have been OTP-confirmed

    OneToOneField on pool guarantees exactly one FactoryOrder per pool.
    """

    STATUS_CHOICES = [
        ('received',   'Order Received'),
        ('processing', 'Processing'),
        ('dispatched', 'Dispatched'),
        ('completed',  'Completed'),
    ]

    pool = models.OneToOneField(
        OrderPool, on_delete=models.CASCADE, related_name='factory_order'
    )
    factory = models.ForeignKey(
        Factory, on_delete=models.CASCADE, related_name='factory_orders'
    )

    total_qty   = models.PositiveIntegerField(default=0)                          # sum of all entry quantities
    total_value = models.DecimalField(max_digits=12, decimal_places=2, default=0) # gross value before commission

    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='received')
    received_at  = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)

    # PDF invoice generated on dispatch
    invoice_pdf = models.FileField(upload_to='invoices/', null=True, blank=True)

    # Optional notes from the factory (e.g. partial stock, substitution notice)
    notes = models.TextField(blank=True)

    def net_payout(self):
        """Factory's 85% share — computed on the fly before a FactoryPayout record exists."""
        return round(self.total_value * Decimal('0.85'), 2)

    def __str__(self):
        return (
            f"FactoryOrder: {self.pool.product.name} → {self.factory.name} "
            f"| {self.total_qty} units | {self.status}"
        )

    class Meta:
        ordering = ['-received_at']


# ─────────────────────────────────────────────
# 12. FACTORY PAYOUT
# ─────────────────────────────────────────────

class FactoryPayout(models.Model):
    """
    Financial ledger record for the factory's 85% share of an order.

    One FactoryPayout is created per OTP-confirmed DeliveryTracking — so a
    pool with 5 stores generates 5 payout records, one per confirmed delivery.
    This lets the factory get paid incrementally rather than waiting for the
    entire pool to be delivered.

    Money flow:
      Store wallet  →  escrow (10% advance on join)
      Pool locks    →  CommissionLog created (pending)
      OTP confirmed →  CommissionLog released (platform keeps 15%)
                    →  FactoryPayout created  (factory receives 85%)
                    →  factory.wallet_balance += net_payout

    Status:
      pending → record created, wallet not yet credited
      paid    → factory.wallet_balance has been incremented
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid',    'Paid'),
    ]

    factory  = models.ForeignKey(Factory,  on_delete=models.CASCADE, related_name='payouts')
    pool     = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='factory_payouts')
    delivery = models.OneToOneField(
        'DeliveryTracking', on_delete=models.CASCADE,
        related_name='factory_payout', null=True, blank=True
    )

    gross_amount        = models.DecimalField(max_digits=12, decimal_places=2)  # 100% of order value
    commission_deducted = models.DecimalField(max_digits=10, decimal_places=2)  # 15% platform cut
    net_payout          = models.DecimalField(max_digits=12, decimal_places=2)  # 85% factory receives

    status   = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)   # when OTP was confirmed
    paid_at    = models.DateTimeField(null=True, blank=True)  # when wallet was credited

    def __str__(self):
        return (
            f"Payout ₹{self.net_payout} → {self.factory.name} "
            f"| {self.pool.product.name} | {self.status}"
        )

    class Meta:
        ordering = ['-created_at']


# ── Signal: sync PlatformWallet balance after every WalletTransaction ────────
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

@receiver(post_save, sender=WalletTransaction)
def sync_wallet_balance(sender, instance, created, **kwargs):
    """Recompute wallet balance whenever a transaction is saved."""
    if created:
        instance.wallet.recalculate()


# ── Signal: auto-replenish pool when one locks or fulfils ────────────────────
# Fires after ANY save that transitions an OrderPool to 'locked' or 'fulfilled',
# regardless of whether it came from the management command, a Celery task,
# or a direct admin action. Calls maybe_recreate_pool() which has its own
# duplicate guard, so it is safe to call multiple times.

@receiver(post_save, sender=OrderPool)
def auto_replenish_pool(sender, instance, **kwargs):
    """
    Immediately create a successor open pool whenever a pool locks or fulfils.
    This guarantees the storefront is never empty.
    """
    if instance.status not in ('locked', 'fulfilled'):
        return
    try:
        from core.services import maybe_recreate_pool
        maybe_recreate_pool(instance)
    except Exception:
        pass  # never crash a pool save due to replenishment failure


# ── Signal: auto-create FactoryOrder when a pool locks or fulfils ─────────────
# This is the critical signal that was missing. The FactoryOrder creation logic
# previously only ran inside the management command and Celery task. When an
# admin manually sets a pool to 'locked' or 'fulfilled', neither of those ran,
# so the factory never saw the order on their dashboard.
#
# This signal fires after ANY save — admin, command, task, or API — and creates
# the FactoryOrder if one does not already exist for this pool.

@receiver(post_save, sender=OrderPool)
def auto_create_factory_order(sender, instance, **kwargs):
    """
    Create a FactoryOrder whenever a pool transitions to 'locked' or 'fulfilled',
    regardless of how the status change was triggered.

    Guards:
    - Only fires for locked/fulfilled status.
    - Uses get_or_create so it is idempotent — safe to call multiple times.
    - Skips if the pool has no active entries (nothing to order).
    - Skips if no factory can be resolved for the product.
    """
    if instance.status not in ('locked', 'fulfilled'):
        return

    # Avoid circular import — models.py imports are deferred
    try:
        from core.models import FactoryOrder, OrderEntry
        from decimal import Decimal as _D

        # Resolve factory from the product FK
        factory = instance.product.factory
        if factory is None:
            return  # no factory linked — nothing to create

        # Calculate total value from active entries
        entries = OrderEntry.objects.filter(
            pool=instance, status='active'
        )
        if not entries.exists():
            return  # empty pool — no order needed

        total_qty   = sum(e.quantity for e in entries)
        total_value = sum(e.total_amount() for e in entries)

        FactoryOrder.objects.get_or_create(
            pool=instance,
            defaults={
                'factory':     factory,
                'total_qty':   total_qty,
                'total_value': total_value,
                'status':      'received',
            },
        )
    except Exception:
        pass  # never crash a pool save due to FactoryOrder creation failure


# ── Signal: auto-sync Product.factory FK from factory_name ───────────────────

@receiver(pre_save, sender='core.Product')
def sync_product_factory_fk(sender, instance, **kwargs):
    """
    Permanent safety net: whenever a Product is saved, ensure the factory FK
    matches the factory_name string.

    This prevents the "wrong FK" drift that occurs when products are created
    or updated via the admin, seed scripts, or the factory products form
    without explicitly setting the factory FK.

    Logic:
    - If factory_name is blank → leave factory FK as-is (no change).
    - If factory FK is already correct (name matches) → no DB hit needed.
    - Otherwise → look up the Factory by name (case-insensitive) and set it.

    The lookup is intentionally case-insensitive so "cipla" matches "Cipla".
    If no matching Factory exists the FK is left unchanged — the admin can
    fix it manually without this signal crashing the save.
    """
    if not instance.factory_name:
        return  # nothing to match against

    # Fast path: FK already set and name matches — no query needed
    if instance.factory_id is not None:
        try:
            # Access cached _factory_cache if available (avoids extra query)
            current_name = instance.factory.name
            if current_name.strip().lower() == instance.factory_name.strip().lower():
                return  # already correct
        except Exception:
            pass  # factory not cached yet — fall through to lookup

    # Lookup by name and assign
    try:
        matched = Factory.objects.get(name__iexact=instance.factory_name.strip())
        instance.factory = matched
    except Factory.DoesNotExist:
        pass  # no matching factory — leave FK unchanged, don't crash
    except Factory.MultipleObjectsReturned:
        # Ambiguous — take the first match (shouldn't happen with unique names)
        matched = Factory.objects.filter(
            name__iexact=instance.factory_name.strip()
        ).first()
        if matched:
            instance.factory = matched


# ── Signal: refund escrow when admin cancels an OrderEntry ───────────────────

@receiver(pre_save, sender='core.OrderEntry')
def refund_escrow_on_admin_cancel(sender, instance, **kwargs):
    """
    Automatically refunds the 10% escrow advance to the store's wallet
    whenever an OrderEntry status is changed to a cancelled state.

    Covers both cancellation types:
      cancelled_free    — pool was still open, no penalty
      cancelled_penalty — pool was locked, ₹200 penalty already charged
                          (escrow is still refunded; penalty is a separate charge)

    Guards:
      - Only fires when status actually transitions TO cancelled (not on
        every save, not on creation, not on re-saving an already-cancelled entry).
      - Only refunds if escrow_amount > 0.

    This covers the Django Admin case where status is changed directly
    without going through the cancel_order view.
    """
    CANCELLED_STATUSES = ('cancelled_free', 'cancelled_penalty')

    # Skip brand-new entries — nothing to refund yet
    if not instance.pk:
        return

    # Only act when the new status is a cancellation
    if instance.status not in CANCELLED_STATUSES:
        return

    # Only act when there is an escrow amount to refund
    if not instance.escrow_amount or instance.escrow_amount <= 0:
        return

    # Fetch the current DB state to compare — prevents double-refund on re-save
    try:
        previous = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    # Already cancelled in DB — status hasn't changed, skip
    if previous.status in CANCELLED_STATUSES:
        return

    # ── Perform the refund ────────────────────────────────────────────────────
    store   = instance.store
    refund  = instance.escrow_amount
    product = instance.pool.product.name

    with transaction.atomic():
        # 1. Credit the store's escrow wallet
        store.wallet_balance += refund
        store.save(update_fields=['wallet_balance'])

        # 2. Stamp cancellation time if not already set
        if not instance.cancelled_at:
            instance.cancelled_at = timezone.now()

        # 3. Create a WalletTransaction record on the PlatformWallet so
        #    the refund is visible in the admin passbook as a debit
        #    (platform is returning money it was holding in escrow).
        cancel_label = 'Free cancellation' if instance.status == 'cancelled_free' \
                       else 'Penalty cancellation'
        PlatformWallet.get().transactions.create(
            amount            = refund,
            transaction_type  = 'debit',
            transaction_label = 'escrow_refund',
            description       = (
                f'Escrow refund — {product} | {store.name} | '
                f'{cancel_label} (OrderEntry #{instance.pk})'
            ),
        )


# ─────────────────────────────────────────────
# 13. WITHDRAWAL REQUEST (Factory)
# ─────────────────────────────────────────────

class WithdrawalRequest(models.Model):
    """
    A factory's request to withdraw earned funds from their wallet_balance.

    Lifecycle:
      pending  → factory submitted the request; balance already deducted
      approved → admin approved; funds transferred externally
      rejected → admin rejected; balance refunded to factory wallet

    Balance is deducted immediately on creation so the factory cannot
    double-spend. If rejected, the amount is refunded back.
    """

    STATUS_CHOICES = [
        ('pending',  'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    factory      = models.ForeignKey(Factory, on_delete=models.CASCADE, related_name='withdrawal_requests')
    amount       = models.DecimalField(max_digits=12, decimal_places=2)
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at  = models.DateTimeField(null=True, blank=True)
    admin_note   = models.CharField(max_length=255, blank=True)  # optional rejection reason

    def __str__(self):
        return f"Withdrawal ₹{self.amount} — {self.factory.name} | {self.status}"

    class Meta:
        ordering = ['-requested_at']


# ─────────────────────────────────────────────
# 14. DISPUTE
# ─────────────────────────────────────────────

class Dispute(models.Model):
    """
    A store raises a dispute against a delivery for damaged/missing goods.

    When a dispute is open, _release_commission() skips the 85% factory
    payout — the money stays locked until an admin resolves it.

    Resolution options (admin):
      resolved_payout  → admin approves delivery; factory gets 85%
      resolved_refund  → admin sides with store; store gets refund
    """

    STATUS_CHOICES = [
        ('open',             'Open'),
        ('resolved_payout',  'Resolved — Payout Released'),
        ('resolved_refund',  'Resolved — Store Refunded'),
    ]

    delivery   = models.OneToOneField(
        DeliveryTracking, on_delete=models.CASCADE, related_name='dispute'
    )
    reason     = models.TextField()
    photo      = models.ImageField(upload_to='disputes/', null=True, blank=True)
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    raised_at  = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    admin_note  = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"Dispute #{self.id} — {self.delivery.store.name} | {self.status}"

    class Meta:
        ordering = ['-raised_at']


# ─────────────────────────────────────────────
# 15. STORE WALLET TOP-UP
# ─────────────────────────────────────────────

class StoreTopUp(models.Model):
    """
    Immutable ledger record for every successful Razorpay wallet top-up.

    Created inside razorpay_verify_payment() immediately after the HMAC
    signature is verified and store.wallet_balance is credited.

    This makes Razorpay top-ups visible in the store's Transaction History
    table alongside escrow deposits, penalties, and refunds.
    """
    store            = models.ForeignKey(MedicalStore, on_delete=models.CASCADE, related_name='topups')
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    razorpay_order_id   = models.CharField(max_length=100, blank=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'TopUp ₹{self.amount} — {self.store.name} ({self.created_at:%d %b %Y})'


# ── Signal: execute payment transfer when a Dispute is resolved ──────────────

@receiver(pre_save, sender=Dispute)
def dispute_resolution_payment(sender, instance, **kwargs):
    """
    Fires BEFORE a Dispute is saved. Detects open → resolved transitions
    and executes the correct payment transfer.

    NEW MEDIATED PAYOUT LOGIC (resolved_payout):
    ─────────────────────────────────────────────
    1. Collect full order amount → PlatformWallet (credit)
    2. Calculate 15% commission → PlatformWallet (stays as credit)
    3. Calculate 85% factory share → FactoryPayout (status='pending')
    4. Factory wallet is NOT credited yet — admin releases it manually later

    This creates a full audit trail:
      - WalletTransaction #1: "Dispute resolution — full amount collected"
      - WalletTransaction #2: "Platform commission (15%)" [implicit via PlatformWallet.credit()]
      - FactoryPayout record: status='pending', net_payout=85%

    resolved_refund → credits the full order amount back to the store wallet
    """
    RESOLVED = ('resolved_payout', 'resolved_refund')

    if not instance.pk:
        return
    if instance.status not in RESOLVED:
        return

    try:
        previous = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    if previous.status in RESOLVED:
        return

    if not instance.resolved_at:
        instance.resolved_at = timezone.now()

    delivery = instance.delivery

    if instance.status == 'resolved_payout':
        # ── MEDIATED PAYOUT: Platform collects, then releases to factory ──
        entry = OrderEntry.objects.filter(
            pool=delivery.pool,
            store=delivery.store,
            status='active',
        ).first()

        if not entry:
            return

        order_value = entry.total_amount()
        commission  = round(order_value * Decimal('0.15'), 2)
        net_payout  = round(order_value * Decimal('0.85'), 2)
        factory     = delivery.pool.product.factory

        if not factory or order_value <= 0:
            return

        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            # Mark delivery as delivered
            if not delivery.otp_verified:
                delivery.otp_verified = True
                delivery.status       = 'delivered'
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=['otp_verified', 'status', 'delivered_at'])

            # Step 1: Collect full order amount → PlatformWallet
            PlatformWallet.get().transactions.create(
                amount           = order_value,
                transaction_type = 'credit',
                description      = (
                    f'Dispute resolution — full amount collected | '
                    f'{delivery.pool.product.name} → {delivery.store.name} | '
                    f'Dispute #{instance.pk}'
                ),
                delivery         = delivery,
            )

            # Step 2: Record 15% commission (stays in platform wallet)
            PlatformWallet.get().credit(
                amount      = commission,
                description = (
                    f'Platform commission (15%) — dispute resolution | '
                    f'{delivery.pool.product.name} | Dispute #{instance.pk}'
                ),
                delivery    = delivery,
            )

            # Step 3: Create FactoryPayout with status='pending' (NOT paid yet)
            # The factory wallet is NOT credited — admin releases it manually later
            if not FactoryPayout.objects.filter(delivery=delivery).exists():
                FactoryPayout.objects.create(
                    factory             = factory,
                    pool                = delivery.pool,
                    delivery            = delivery,
                    gross_amount        = order_value,
                    commission_deducted = commission,
                    net_payout          = net_payout,
                    status              = 'pending',  # ← NOT 'paid'
                    # paid_at is left NULL — set when admin approves
                )

            # Step 4: Update CommissionLog
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

    elif instance.status == 'resolved_refund':
        # ── Admin sides with store: refund full order value ───────────────
        entry = OrderEntry.objects.filter(
            pool=delivery.pool,
            store=delivery.store,
            status='active',
        ).first()

        if entry:
            refund_amount = entry.total_amount()
            store = delivery.store

            from django.db import transaction as db_transaction
            with db_transaction.atomic():
                # 1. Credit the store wallet
                store.wallet_balance += refund_amount
                store.save(update_fields=['wallet_balance'])

                # 2. Create a WalletTransaction debit (platform returns escrow)
                PlatformWallet.get().transactions.create(
                    amount           = refund_amount,
                    transaction_type = 'debit',
                    description      = (
                        f'Dispute refund — {delivery.pool.product.name} | '
                        f'{store.name} | Dispute #{instance.pk}'
                    ),
                )

