"""
prediction.py — AI Demand Prediction Engine
=============================================
This module implements a rule-based seasonal demand forecasting system.

How it works:
1. Every day, run: python manage.py run_predictions
2. For each store, it checks every product in their inventory.
3. It looks 15 days into the future and checks if any seasonal rule
   matches the product name and the upcoming date.
4. If predicted demand > current stock, a PredictionAlert is created.
5. The alert appears on the store's dashboard with a "Join Pool Now" button.

Why rule-based instead of ML?
- No historical data exists yet (new platform).
- Rules are transparent and explainable to pharmacy owners.
- Can be upgraded to scikit-learn / Facebook Prophet later without
  changing the interface — just swap out get_seasonal_multiplier().

Seasonal rules format:
  (start_month, end_month): [
      (product_keyword, reason_text, demand_multiplier),
      ...
  ]
  A multiplier of 2.0 means demand is expected to double.
"""

from datetime import date, timedelta
from .models import Inventory, PredictionAlert


# ─────────────────────────────────────────────
# SEASONAL DEMAND RULES
# ─────────────────────────────────────────────

SEASONAL_RULES = {
    # Summer season: March (3) through May (5)
    # Causes heatstroke, dehydration, sunburn, eye infections
    (3, 5): [
        ('ors',         'Summer heat — dehydration & heatstroke cases rise',  2.0),
        ('electral',    'Summer heat — electrolyte loss rises',                2.0),
        ('eye drop',    'Summer — dust & UV exposure causes eye infections',   1.6),
        ('sunscreen',   'Summer — UV protection demand rises',                 1.5),
        ('cetirizine',  'Summer — allergic rhinitis & heat rash cases rise',   1.5),
    ],

    # Monsoon season: June (6) through September (9)
    # Causes fever, dehydration, fungal infections
    (6, 9): [
        ('dolo',        'Monsoon season — fever/cold spike expected',     2.0),
        ('paracetamol', 'Monsoon season — fever spike expected',          2.0),
        ('ors',         'Monsoon season — dehydration cases rise',        1.8),
        ('antifungal',  'Monsoon season — fungal infections rise',        1.5),
    ],

    # Winter season: November (11) through February (2)
    # Causes respiratory infections, vitamin deficiency
    (11, 2): [
        ('vitamin c',   'Winter season — immunity demand rises',          1.6),
        ('cough',       'Winter season — respiratory cases rise',         1.7),
        ('antibiotic',  'Winter season — bacterial infections rise',      1.4),
    ],
}


def get_seasonal_multiplier(product_name: str, check_date: date):
    """
    Checks if a product is expected to see increased demand on a given date.

    Args:
        product_name: The medicine name (e.g., "Dolo 650")
        check_date:   The future date to check (typically today + 15 days)

    Returns:
        (multiplier, reason) tuple.
        multiplier = 1.0 means no spike expected (normal demand).
        multiplier > 1.0 means demand is expected to increase by that factor.
        reason is a human-readable explanation shown in the alert.

    How month range matching works:
        Normal range (6, 9): month must be between 6 and 9 inclusive.
        Wrap-around range (11, 2): month >= 11 OR month <= 2
        (handles December → January crossover).
    """
    month = check_date.month
    name_lower = product_name.lower()  # case-insensitive keyword matching

    for (start, end), rules in SEASONAL_RULES.items():
        # Check if the date falls within this season's month range
        if start <= end:
            # Normal range: e.g., June (6) to September (9)
            in_range = (start <= month <= end)
        else:
            # Wrap-around range: e.g., November (11) to February (2)
            in_range = (month >= start or month <= end)

        if in_range:
            # Check if any keyword matches the product name
            for keyword, reason, multiplier in rules:
                if keyword in name_lower:
                    return multiplier, reason

    # No seasonal rule matched — return neutral multiplier
    return 1.0, ''


def generate_alerts_for_store(store):
    """
    Generates PredictionAlert records for a single store.

    Checks all products in the store's inventory and creates alerts
    for any product where:
    1. A seasonal spike is predicted within 15 days.
    2. The predicted demand exceeds current stock.
    3. No duplicate alert already exists for the same product + spike date.

    Args:
        store: A MedicalStore instance

    Returns:
        int: Number of new alerts created for this store
    """
    alert_date = date.today()                          # today (when alert is generated)
    spike_date = alert_date + timedelta(days=15)       # 15 days from now (predicted spike)
    created = 0

    # Loop through every product this store tracks in inventory
    for inv in store.inventory.select_related('product').all():
        multiplier, reason = get_seasonal_multiplier(inv.product.name, spike_date)

        # Skip if no seasonal spike is predicted for this product
        if multiplier <= 1.0:
            continue

        # Estimate how many units will be needed during the spike
        # threshold = normal reorder level, so multiply it by the spike factor
        predicted_demand = int(inv.threshold * multiplier)

        # Only alert if the store doesn't have enough stock to cover the spike
        if predicted_demand <= inv.current_stock:
            continue  # store already has enough stock, no alert needed

        # Avoid creating duplicate alerts for the same product + spike date
        already_exists = PredictionAlert.objects.filter(
            store=store,
            product=inv.product,
            demand_spike_date=spike_date,
        ).exists()

        if not already_exists:
            PredictionAlert.objects.create(
                store=store,
                product=inv.product,
                predicted_demand=predicted_demand,
                alert_date=alert_date,
                demand_spike_date=spike_date,
                reason=reason,
            )
            created += 1

    return created
