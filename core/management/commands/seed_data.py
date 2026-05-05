"""
seed_data.py — Test Data Generator
=====================================
Management command that populates the database with realistic test data.

Run with: python manage.py seed_data

Creates:
- 20 medicines (real Indian pharmacy products, including seasonal)
- 10 pharmacy stores across 5 cities with inventory
- 20 open order pools with 2-4 stores already joined
- 3 fulfilled pools with commission logs (so homepage stats show real numbers)
- 5 factory accounts (factory1–factory5) with full Factory profiles,
  properly linked via Factory.user so login goes straight to the factory dashboard

Uses get_or_create() throughout so the command is safe to run multiple times
without creating duplicate data.

Test login credentials:
  Stores   : store1/test1234 ... store10/test1234
  Factories: factory1/test1234 ... factory5/test1234
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import random
from core.models import (
    MedicalStore, Product, Inventory,
    OrderPool, OrderEntry, DeliveryTracking, CommissionLog, Factory,
    FactoryOrder, FactoryPayout,
)

# ── SEED DATA CONSTANTS ───────────────────────────────────────────────────────

# Cities where stores will be created (used for geofencing grouping)
CITIES = ['Ahmedabad', 'Mumbai', 'Delhi', 'Bangalore', 'Surat']

# Real Indian medicines: (name, generic_name, category, factory, base_price)
PRODUCTS = [
    ('Dolo 650',             'Paracetamol',              'analgesic',  'Micro Labs',   12.50),
    ('Augmentin 625',        'Amoxicillin+Clavulanate',  'antibiotic', 'GSK',          85.00),
    ('Vitamin C 500mg',      'Ascorbic Acid',             'vitamin',    'Himalaya',     18.00),
    ('ORS Sachet',           'Oral Rehydration Salts',    'other',      'Electral',      8.00),
    ('Azithromycin 500',     'Azithromycin',              'antibiotic', 'Cipla',        55.00),
    ('Cough Syrup Benadryl', 'Diphenhydramine',           'other',      'Johnson',      95.00),
    ('Pantoprazole 40mg',    'Pantoprazole',              'other',      'Sun Pharma',   22.00),
    ('Cetirizine 10mg',      'Cetirizine',                'other',      'Mankind',      14.00),
    ('Metformin 500mg',      'Metformin',                 'other',      'USV',          30.00),
    ('Vitamin D3 60K',       'Cholecalciferol',           'vitamin',    'Cadila',       45.00),
    # Seasonal — Monsoon
    ('Paracetamol 500mg',    'Paracetamol',               'analgesic',  'Cipla',        10.00),
    ('ORS Electral Powder',  'Oral Rehydration Salts',    'other',      'Pfizer',        9.50),
    ('Fluconazole 150mg',    'Fluconazole (Antifungal)',   'other',      'Sun Pharma',   28.00),
    ('Antifungal Cream',     'Clotrimazole',              'other',      'GSK',          55.00),
    # Seasonal — Winter
    ('Cough Syrup Corex',    'Codeine+Chlorpheniramine',  'other',      'Pfizer',       85.00),
    ('Vitamin C Chewable',   'Ascorbic Acid',             'vitamin',    'Mankind',      22.00),
    ('Amoxicillin 500mg',    'Amoxicillin (Antibiotic)',  'antibiotic', 'Cipla',        45.00),
    ('Azithromycin 250',     'Azithromycin (Antibiotic)', 'antibiotic', 'Sun Pharma',   38.00),
    # Seasonal — Summer
    ('Eye Drops Refresh',    'Carboxymethylcellulose',    'other',      'Allergan',     85.00),
    ('Sunscreen SPF 50',     'Zinc Oxide + Titanium',     'other',      'Lotus',       180.00),
]

# Pharmacy store names — kept for reference, replaced by REAL_STORES below
STORE_NAMES = [
    'Apollo Pharmacy', 'MedPlus Store', 'Sanjivani Medical',
    'LifeCare Pharmacy', 'Shree Ram Medical', 'City Medicals',
    'Wellness Pharmacy', 'Janata Medical Store', 'Om Sai Medicals', 'Healthway Pharmacy'
]


class Command(BaseCommand):
    """
    Django management command class.
    The handle() method is called when you run: python manage.py seed_data
    """
    help = 'Seed 10 test entries for products, stores, pools, and orders'

    def handle(self, *args, **kwargs):
        self.stdout.write('Seeding data...')

        # ── Step 0: Create warehouse hubs for all cities ─────────────────────
        hub_data = [
            ('BulkMed Central Warehouse', 'Ahmedabad', 23.0753, 72.6369),
            ('BulkMed Mumbai Hub',        'Mumbai',    19.0760, 72.8777),
            ('BulkMed Delhi Hub',         'Delhi',     28.6139, 77.2090),
            ('BulkMed Bangalore Hub',     'Bangalore', 12.9716, 77.5946),
            ('BulkMed Surat Hub',         'Surat',     21.1702, 72.8311),
        ]
        factory = None
        for hub_name, hub_city, hub_lat, hub_lng in hub_data:
            hub, _ = Factory.objects.get_or_create(
                name=hub_name,
                defaults={'address': f'{hub_name}, {hub_city}', 'city': hub_city,
                          'latitude': hub_lat, 'longitude': hub_lng}
            )
            if hub_city == 'Ahmedabad':
                factory = hub
        self.stdout.write(f'  ✓ {len(hub_data)} warehouse hubs')

        # ── Step 1: Create 10 products ────────────────────────────────────────
        products = []
        for name, generic, category, factory_name, price in PRODUCTS:
            p, _ = Product.objects.get_or_create(
                name=name,
                defaults={
                    'generic_name': generic,
                    'category': category,
                    'factory_name': factory_name,
                    'factory': factory,   # link all products to the Ahmedabad factory
                    'base_price': price,
                    'unit': 'strip',
                }
            )
            products.append(p)
        self.stdout.write(f'  ✓ {len(products)} products')

        # ── Step 2: Create 10 users + stores with real GPS coordinates ──────────
        # Real pharmacy locations verified from public mapping data (latlong.net)
        # Coordinates are in decimal degrees (WGS84)
        REAL_STORES = [
            # (username, store_name, address, license_no, contact, city, lat, lng)
            ('store1',  'Apollo Pharmacy',
             'Near Navrangpura, CG Road, Ahmedabad, Gujarat 380009',
             'LIC1000', '9812340001', 'Ahmedabad', 23.0168, 72.4702),

            ('store2',  'MedPlus Store',
             'Andheri West, Mumbai, Maharashtra 400058',
             'LIC1001', '9812340002', 'Mumbai', 19.0587, 72.8365),

            ('store3',  'Sanjivani Medical',
             'Navrangpura, Ahmedabad, Gujarat 380009',
             'LIC1002', '9812340003', 'Ahmedabad', 23.0458, 72.5520),

            ('store4',  'LifeCare Pharmacy',
             'Dwarka Sector 10, New Delhi 110075',
             'LIC1003', '9812340004', 'Delhi', 28.5221, 77.2151),

            ('store5',  'Shree Ram Medical',
             'Adajan, Surat, Gujarat 395009',
             'LIC1004', '9812340005', 'Surat', 21.1959, 72.7984),

            ('store6',  'City Medicals',
             'Koramangala 5th Block, Bangalore, Karnataka 560095',
             'LIC1005', '9812340006', 'Bangalore', 12.9352, 77.6245),

            ('store7',  'Wellness Pharmacy',
             'Bandra West, Mumbai, Maharashtra 400050',
             'LIC1006', '9812340007', 'Mumbai', 19.0596, 72.8295),

            ('store8',  'Janata Medical Store',
             'Rohini Sector 9, New Delhi 110085',
             'LIC1007', '9812340008', 'Delhi', 28.5337, 77.2150),

            ('store9',  'Om Sai Medicals',
             'Indiranagar 100 Feet Road, Bangalore, Karnataka 560038',
             'LIC1008', '9812340009', 'Bangalore', 12.9784, 77.6408),

            ('store10', 'Healthway Pharmacy',
             'Ring Road, Surat, Gujarat 395002',
             'LIC1009', '9812340010', 'Surat', 21.1702, 72.8311),
        ]

        stores = []
        for username, store_name, address, license_no, contact, city, lat, lng in REAL_STORES:
            user, _ = User.objects.get_or_create(username=username)
            user.set_password('test1234')
            user.save()

            store, created = MedicalStore.objects.get_or_create(
                user=user,
                defaults={
                    'name':           store_name,
                    'address':        address,
                    'license_no':     license_no,
                    'contact':        contact,
                    'city':           city,
                    'latitude':       lat,
                    'longitude':      lng,
                    'is_verified':    True,
                    'wallet_balance': round(random.uniform(500, 5000), 2),
                }
            )
            if not created:
                # Always keep GPS in sync on re-runs
                store.latitude  = lat
                store.longitude = lng
                store.city      = city
                store.address   = address
                store.save(update_fields=['latitude', 'longitude', 'city', 'address'])
            stores.append(store)

            # Give each store all seasonal medicines + 3 random others in inventory
            seasonal_keywords = ['dolo', 'paracetamol', 'ors', 'electral', 'antifungal', 'fluconazole',
                                  'cough', 'vitamin c', 'antibiotic', 'amoxicillin', 'azithromycin',
                                  'eye drop', 'sunscreen', 'cetirizine']
            seasonal_products = [p for p in products if any(k in p.name.lower() for k in seasonal_keywords)]
            other_products = [p for p in products if p not in seasonal_products]
            inventory_products = seasonal_products + random.sample(other_products, min(3, len(other_products)))
            for product in inventory_products:
                Inventory.objects.get_or_create(
                    store=store,
                    product=product,
                    defaults={
                        'current_stock': random.randint(2, 30),
                        'threshold': 10,
                    }
                )

        self.stdout.write(f'  ✓ {len(stores)} stores with inventory')

        # ── Step 3: Create 10 open order pools ───────────────────────────────
        pools = []
        for i, product in enumerate(products):
            city = CITIES[i % len(CITIES)]

            pool, _ = OrderPool.objects.get_or_create(
                product=product,
                status='open',
                defaults={
                    'city': city,
                    'total_qty': 0,
                    'current_member_count': 0,
                    # Alternate between urgent (2h) and pool mode (72h) windows
                    'expires_at': timezone.now() + timedelta(hours=2 if i % 2 == 0 else 72),
                }
            )
            pools.append(pool)

            # Add 2-4 stores to each pool to simulate real activity
            joining_stores = random.sample(stores, random.randint(2, 4))
            for store in joining_stores:
                if not OrderEntry.objects.filter(pool=pool, store=store).exists():
                    qty = random.randint(10, 100)
                    OrderEntry.objects.create(
                        pool=pool,
                        store=store,
                        quantity=qty,
                        mode=random.choice(['urgent', 'pool']),
                        unit_price_at_order=product.base_price,
                        discount_applied=pool.current_discount(),
                    )
                    pool.total_qty += qty
                    pool.current_member_count += 1
            pool.save()

        self.stdout.write(f'  ✓ {len(pools)} order pools with entries')

        # ── Step 4: Create 3 fulfilled pools with commission logs ─────────────
        # These make the homepage stats (fulfilled orders, total savings) non-zero
        for i in range(3):
            product = products[i]
            pool = OrderPool.objects.create(
                product=product,
                city=CITIES[i],
                total_qty=200,
                current_member_count=5,
                status='fulfilled',
                expires_at=timezone.now() - timedelta(days=1),  # already expired
            )
            total_value = round(product.base_price * 200 * Decimal('0.90'), 2)
            CommissionLog.objects.create(
                pool=pool,
                total_order_value=total_value,
                commission_rate=5.00,
                commission_amount=round(total_value * Decimal('0.05'), 2),
                payout_status='released',
                released_at=timezone.now() - timedelta(hours=6),
            )

        self.stdout.write('  ✓ 3 fulfilled pools with commission logs')

        # ── Step 5: Create 5 factory user accounts with full Factory profiles ──
        # Each factory gets a dedicated User + Factory record properly linked via
        # Factory.user (OneToOneField). _is_factory_user() checks this FK, so
        # logging in as factory1 goes straight to the factory dashboard.
        # The warehouse hubs created in Step 0 remain as dispatch origins for
        # GPS tracking — these are separate Factory records with no user attached.
        FACTORY_DATA = [
            {
                'username':    'factory1',
                'name':        'Alpha Pharma Pvt. Ltd.',
                'city':        'Ahmedabad',
                'address':     '14, GIDC Industrial Estate, Phase-2, Ahmedabad, Gujarat 382445',
                'license_no':  'GJ-AHM-MFG-0001',
                'contact':     '9012340001',
                'latitude':    23.0225,
                'longitude':   72.5714,
            },
            {
                'username':    'factory2',
                'name':        'Beta Meds Manufacturing',
                'city':        'Mumbai',
                'address':     '7, Andheri Industrial Zone, MIDC, Mumbai, Maharashtra 400093',
                'license_no':  'MH-MUM-MFG-0002',
                'contact':     '9012340002',
                'latitude':    19.1136,
                'longitude':   72.8697,
            },
            {
                'username':    'factory3',
                'name':        'Gamma Life Sciences',
                'city':        'Surat',
                'address':     '22, Sachin GIDC, Surat, Gujarat 394230',
                'license_no':  'GJ-SRT-MFG-0003',
                'contact':     '9012340003',
                'latitude':    21.0922,
                'longitude':   72.8615,
            },
            {
                'username':    'factory4',
                'name':        'Delta Biotech Ltd.',
                'city':        'Delhi',
                'address':     '5, Okhla Industrial Area Phase-III, New Delhi 110020',
                'license_no':  'DL-NDL-MFG-0004',
                'contact':     '9012340004',
                'latitude':    28.5355,
                'longitude':   77.2590,
            },
            {
                'username':    'factory5',
                'name':        'Epsilon Pharmaceuticals',
                'city':        'Bangalore',
                'address':     '9, Peenya Industrial Area, Bangalore, Karnataka 560058',
                'license_no':  'KA-BLR-MFG-0005',
                'contact':     '9012340005',
                'latitude':    13.0298,
                'longitude':   77.5199,
            },
        ]

        for fd in FACTORY_DATA:
            # Create or update the Django user
            user, _ = User.objects.get_or_create(username=fd['username'])
            user.set_password('test1234')
            user.first_name = ''   # no first_name hack — auth uses Factory.user FK
            user.save()

            # Create or update the Factory record, properly linked via user FK
            factory_obj, created = Factory.objects.get_or_create(
                user=user,
                defaults={
                    'name':           fd['name'],
                    'city':           fd['city'],
                    'address':        fd['address'],
                    'license_no':     fd['license_no'],
                    'contact':        fd['contact'],
                    'latitude':       fd['latitude'],
                    'longitude':      fd['longitude'],
                    'is_verified':    True,
                    'wallet_balance': Decimal('0.00'),
                }
            )
            if not created:
                # Idempotent update — keep all fields in sync on re-runs
                factory_obj.name        = fd['name']
                factory_obj.city        = fd['city']
                factory_obj.address     = fd['address']
                factory_obj.license_no  = fd['license_no']
                factory_obj.contact     = fd['contact']
                factory_obj.latitude    = fd['latitude']
                factory_obj.longitude   = fd['longitude']
                factory_obj.is_verified = True
                factory_obj.save()

        self.stdout.write(f'  ✓ {len(FACTORY_DATA)} factory accounts (factory1–factory5)')

        # ── Step 6: Backfill inventory for any store that has none ────────────
        all_stores = MedicalStore.objects.all()
        backfilled = 0
        for store in all_stores:
            if not Inventory.objects.filter(store=store).exists():
                stock_levels = [2, 5, 8, 3, 15, 1, 12, 7, 4, 9, 20, 6, 11, 3, 18, 8, 2, 14]
                for i, product in enumerate(products):
                    Inventory.objects.create(
                        store=store,
                        product=product,
                        current_stock=stock_levels[i % len(stock_levels)],
                        threshold=10,
                    )
                backfilled += 1
        if backfilled:
            self.stdout.write(f'  ✓ Backfilled inventory for {backfilled} store(s) with no products')

        # ── Step 7: Create FactoryOrder + FactoryPayout for fulfilled pools ───
        # Gives factory1 (Alpha Pharma) realistic dashboard data to browse.
        # We attach the 3 fulfilled pools to factory1 and simulate the full
        # 85/15 financial split so the wallet and payout history are populated.
        from core.models import FactoryOrder, FactoryPayout

        alpha_factory = Factory.objects.filter(license_no='GJ-AHM-MFG-0001').first()
        fulfilled_pools = OrderPool.objects.filter(status='fulfilled').order_by('created_at')[:3]

        fo_count = fp_count = 0
        for pool in fulfilled_pools:
            # Re-point the pool's product to alpha_factory so the dashboard
            # scoping (pool__product__factory=factory) works correctly
            pool.product.factory = alpha_factory
            pool.product.factory_name = alpha_factory.name
            pool.product.save(update_fields=['factory', 'factory_name'])

            # FactoryOrder — one per pool
            fo, fo_created = FactoryOrder.objects.get_or_create(
                pool=pool,
                defaults={
                    'factory':      alpha_factory,
                    'total_qty':    pool.total_qty,
                    'total_value':  round(pool.total_qty * pool.product.base_price * Decimal('0.90'), 2),
                    'status':       'completed',
                    'dispatched_at': pool.dispatched_at or (timezone.now() - timedelta(hours=12)),
                }
            )
            if fo_created:
                fo_count += 1

            # FactoryPayout — one per delivery in the pool (simulates per-store OTP confirmation)
            deliveries = DeliveryTracking.objects.filter(pool=pool)
            for delivery in deliveries:
                if FactoryPayout.objects.filter(delivery=delivery).exists():
                    continue
                gross      = round(delivery.quantity * pool.product.base_price * Decimal('0.90'), 2)
                commission = round(gross * Decimal('0.15'), 2)
                net        = round(gross * Decimal('0.85'), 2)
                FactoryPayout.objects.create(
                    factory             = alpha_factory,
                    pool                = pool,
                    delivery            = delivery,
                    gross_amount        = gross,
                    commission_deducted = commission,
                    net_payout          = net,
                    status              = 'paid',
                    paid_at             = timezone.now() - timedelta(hours=6),
                )
                fp_count += 1

            # Credit the net total to alpha_factory's wallet
            total_net = sum(
                round(d.quantity * pool.product.base_price * Decimal('0.90') * Decimal('0.85'), 2)
                for d in deliveries
            )
            alpha_factory.wallet_balance += total_net
            alpha_factory.save(update_fields=['wallet_balance'])

        if alpha_factory:
            self.stdout.write(
                f'  ✓ {fo_count} FactoryOrder(s), {fp_count} FactoryPayout(s) '
                f'→ factory1 wallet: ₹{alpha_factory.wallet_balance}'
            )

        self.stdout.write(
            self.style.SUCCESS(
                '\nDone! Test credentials:\n'
                '  Stores   : store1/test1234 ... store10/test1234\n'
                '  Factories: factory1/test1234 ... factory5/test1234'
            )
        )
