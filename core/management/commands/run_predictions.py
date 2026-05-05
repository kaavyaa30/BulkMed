"""
run_predictions.py — Daily Prediction Alert Generator
=======================================================
Management command that runs the AI demand prediction engine
for all registered stores.

Run manually:    python manage.py run_predictions
Run via cron:    0 6 * * * /path/to/env/bin/python manage.py run_predictions
                 (runs every day at 6 AM)

What it does:
1. Fetches all MedicalStore records
2. For each store, calls generate_alerts_for_store() from prediction.py
3. That function checks inventory against seasonal demand rules
4. Creates PredictionAlert records for any predicted spikes
5. Alerts appear on the store's dashboard the next time they log in

Output example:
  Apollo Pharmacy: 2 alert(s) created
  MedPlus Store: 1 alert(s) created
  Done. 3 total alerts generated.
"""

from django.core.management.base import BaseCommand
from core.models import MedicalStore
from core.prediction import generate_alerts_for_store


class Command(BaseCommand):
    """
    Django management command class.
    handle() is called when you run: python manage.py run_predictions
    """
    help = 'Generate demand prediction alerts for all stores (run daily via cron)'

    def handle(self, *args, **kwargs):
        stores = MedicalStore.objects.all()
        total = 0

        for store in stores:
            # generate_alerts_for_store returns the count of new alerts created
            count = generate_alerts_for_store(store)
            total += count
            if count:
                # Only print stores that actually got new alerts
                self.stdout.write(f"  {store.name}: {count} alert(s) created")

        self.stdout.write(
            self.style.SUCCESS(f"Done. {total} total alerts generated.")
        )
