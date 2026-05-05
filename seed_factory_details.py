"""
seed_factory_details.py
========================
Fills in missing fields on every Factory object with realistic dummy data.
Only blank / null fields are touched — existing data is never overwritten.

Run with:
    python manage.py shell -c "exec(open('seed_factory_details.py', encoding='utf-8').read())"
"""

import random
import string
from core.models import Factory

# ── Seed data pools ───────────────────────────────────────────────────────────

CITIES = ["Ahmedabad", "Mumbai", "Delhi", "Bangalore", "Surat",
          "Pune", "Hyderabad", "Chennai", "Kolkata", "Jaipur"]

# (street number range, street name, area suffix)
STREET_TEMPLATES = [
    ("{n} Industrial Estate, Phase {p}, {area} Road"),
    ("Plot {n}, GIDC {area}, Sector {p}"),
    ("{n}-B, {area} Nagar, Near {p}th Cross"),
    ("Unit {n}, {area} Industrial Park, Block {p}"),
    ("{n}, {area} Compound, Opp. Gate {p}"),
]

AREAS = ["Naroda", "Vatva", "Andheri", "Bhiwandi", "Okhla",
         "Peenya", "Ambattur", "Rajpur", "Sitapura", "Ranjangaon"]

# GSTIN state codes (2-digit prefix) — using a few common ones
GSTIN_STATE_CODES = ["24", "27", "07", "29", "33", "36", "09", "19"]


# ── Generator helpers ─────────────────────────────────────────────────────────

def _rand_digits(n):
    """Return a string of n random decimal digits."""
    return "".join(random.choices(string.digits, k=n))

def _rand_upper(n):
    """Return a string of n random uppercase ASCII letters."""
    return "".join(random.choices(string.ascii_uppercase, k=n))

def fake_city():
    return random.choice(CITIES)

def fake_address(city):
    template = random.choice(STREET_TEMPLATES)
    area     = random.choice(AREAS)
    address  = (
        template
        .replace("{n}", str(random.randint(1, 999)))
        .replace("{p}", str(random.randint(1, 20)))
        .replace("{area}", area)
    )
    return f"{address}, {city}"

def fake_license_no(existing_licenses):
    """
    Generate a unique "DL-MFG-XXXX" style license number.
    Retries until it finds one not already in the DB or used in this run.
    """
    while True:
        candidate = f"DL-MFG-{_rand_digits(4)}"
        if candidate not in existing_licenses:
            existing_licenses.add(candidate)   # reserve it for this run
            return candidate

def fake_gstin(existing_gstins):
    """
    Format: <2-digit state code><5 uppercase letters><4 digits>A1Z5
    Total: 15 characters — matches real GSTIN length.
    Retries until unique.
    """
    while True:
        state   = random.choice(GSTIN_STATE_CODES)
        letters = _rand_upper(5)
        digits  = _rand_digits(4)
        candidate = f"{state}{letters}{digits}A1Z5"
        if candidate not in existing_gstins:
            existing_gstins.add(candidate)
            return candidate

def fake_contact():
    """10-digit Indian mobile number starting with 9 or 8."""
    first_digit = random.choice(["9", "8"])
    return first_digit + _rand_digits(9)


# ── Pre-load existing values to avoid unique-constraint collisions ────────────

existing_licenses = set(
    Factory.objects.exclude(license_no__isnull=True)
                   .exclude(license_no="")
                   .values_list("license_no", flat=True)
)
existing_gstins = set(
    Factory.objects.exclude(gstin="")
                   .values_list("gstin", flat=True)
)

# ── Main loop ─────────────────────────────────────────────────────────────────

factories    = Factory.objects.all().order_by("id")
updated      = 0
already_full = 0

print(f"Processing {factories.count()} factory record(s)...\n")

for factory in factories:
    changed_fields = []

    # city — fill if blank
    if not factory.city:
        factory.city = fake_city()
        changed_fields.append("city")

    # address — fill if blank (use city, which may have just been set)
    if not factory.address:
        factory.address = fake_address(factory.city)
        changed_fields.append("address")

    # license_no — fill if null or blank
    if not factory.license_no:
        factory.license_no = fake_license_no(existing_licenses)
        changed_fields.append("license_no")

    # gstin — fill if blank
    if not factory.gstin:
        factory.gstin = fake_gstin(existing_gstins)
        changed_fields.append("gstin")

    # contact — fill if blank
    if not factory.contact:
        factory.contact = fake_contact()
        changed_fields.append("contact")

    if changed_fields:
        factory.save(update_fields=changed_fields)
        updated += 1
        print(
            f"  ✅  {factory.name:<40} "
            f"updated: {', '.join(changed_fields)}"
        )
    else:
        already_full += 1

print(f"\n{'─' * 60}")
print(f"Done.  {updated} factory record(s) updated, "
      f"{already_full} already complete.")
