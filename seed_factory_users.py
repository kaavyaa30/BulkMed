"""
seed_factory_users.py
=====================
Run with:
    python manage.py shell < seed_factory_users.py

What it does
------------
1. Collects every unique `factory_name` string from the Product table.
2. For each name, skips it if a Factory row with that exact name already exists.
3. For names that are missing a Factory profile:
   - Finds the highest existing "factoryN" username (e.g. factory5)
     and increments N by 1 for the new user.
   - Creates a Django User  (username=factoryN, password="Test@1234").
   - Creates a Factory profile linked to that user.
4. Prints a summary table and a per-row success line.

Safe to re-run — existing Factory rows are never touched.
"""

import re
from django.contrib.auth.models import User
from core.models import Factory, Product

DEFAULT_PASSWORD = "Test@1234"

# ── 1. Collect unique factory names from the Product catalogue ────────────────
all_names = (
    Product.objects
    .exclude(factory_name="")
    .values_list("factory_name", flat=True)
    .distinct()
    .order_by("factory_name")
)

# ── 2. Find which names already have a Factory profile ───────────────────────
existing_names = set(
    Factory.objects.values_list("name", flat=True)
)

missing_names = [n for n in all_names if n not in existing_names]

if not missing_names:
    print("✅  All factory names already have a Factory profile. Nothing to do.")
else:
    print(f"Found {len(missing_names)} factory name(s) without a profile:\n")

    # ── 3. Determine the next available factoryN username ─────────────────────
    # Pull every username that matches the pattern "factory<digits>" and find
    # the highest N so we can continue the sequence without gaps or collisions.
    existing_factory_usernames = User.objects.filter(
        username__regex=r'^factory\d+$'
    ).values_list("username", flat=True)

    used_numbers = []
    for uname in existing_factory_usernames:
        match = re.fullmatch(r'factory(\d+)', uname)
        if match:
            used_numbers.append(int(match.group(1)))

    # Start at 1 if no factoryN users exist yet, otherwise continue from max+1
    next_n = (max(used_numbers) + 1) if used_numbers else 1

    created_count = 0

    for factory_name in missing_names:
        username = f"factory{next_n}"

        # Guard: skip if this username is somehow already taken (edge case)
        if User.objects.filter(username=username).exists():
            print(f"  ⚠️  Username '{username}' already exists — skipping '{factory_name}'")
            next_n += 1
            continue

        # Create the User
        user = User.objects.create_user(
            username=username,
            password=DEFAULT_PASSWORD,
        )

        # Create the Factory profile linked to this user.
        # license_no is left NULL (unique constraint allows multiple NULLs in
        # most databases) — admin can fill it in after verification.
        Factory.objects.create(
            user=user,
            name=factory_name,
        )

        print(
            f"  ✅  Created  |  "
            f"Company: {factory_name:<40}  |  "
            f"Username: {username:<12}  |  "
            f"Password: {DEFAULT_PASSWORD}"
        )

        next_n += 1
        created_count += 1

    print(f"\n{'─' * 70}")
    print(f"Done. {created_count} Factory profile(s) created.")
    print(
        "⚠️  Reminder: these accounts use the default password '{}'.\n"
        "   Ask each factory owner to change it on first login.".format(DEFAULT_PASSWORD)
    )
