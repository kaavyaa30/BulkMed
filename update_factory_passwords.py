"""
update_factory_passwords.py
============================
Finds all users matching factory6, factory7, ... (N >= 6),
prints their username + linked Factory name, then resets
every password to "test1234".

Run with:
    python manage.py shell -c "exec(open('update_factory_passwords.py', encoding='utf-8').read())"
"""

import re
from django.contrib.auth.models import User

NEW_PASSWORD = "test1234"

# ── 1. Fetch all factoryN users where N >= 6 ─────────────────────────────────
# The regex filter narrows to the right pattern; we then filter N >= 6 in Python
# because Django's ORM can't extract and compare the integer part in one step.
candidates = User.objects.filter(username__regex=r'^factory\d+$').order_by('username')

target_users = [
    u for u in candidates
    if int(re.fullmatch(r'factory(\d+)', u.username).group(1)) >= 6
]

# ── 2. Print total count ──────────────────────────────────────────────────────
print(f"Found {len(target_users)} user(s) matching factory6+:\n")

# ── 3. Print username + linked Factory name ───────────────────────────────────
for user in target_users:
    try:
        factory_name = user.factory.name   # OneToOneField reverse accessor
    except Exception:
        factory_name = "(no Factory profile linked)"
    print(f"  {user.username:<14}  →  {factory_name}")

# ── 4 & 5. Reset passwords and save ──────────────────────────────────────────
print(f"\nUpdating passwords to '{NEW_PASSWORD}'...")

for user in target_users:
    user.set_password(NEW_PASSWORD)
    user.save(update_fields=["password"])

print(f"\n✅  Done. Password updated to '{NEW_PASSWORD}' for {len(target_users)} user(s).")
