"""One-off: rename the admin user and reset their password.

Run after editing ADMIN_EMAILS in .env. Looks up the existing admin
account by its current email, swaps email + password to the new ones.
If no row matches, creates a fresh admin user with the new creds.

Usage:
    cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/backend
    ../.venv/bin/python scripts/swap_admin.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.billing import Subscription
from app.models.user import User

OLD_EMAIL = "antivirys18@gmail.com"
NEW_EMAIL = "gogoflsl@gmail.com"
NEW_PASSWORD = "Oo098765"


def main() -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == OLD_EMAIL).first()
        if user is None:
            user = db.query(User).filter(User.email == NEW_EMAIL).first()

        if user is None:
            user = User(email=NEW_EMAIL, hashed_password=hash_password(NEW_PASSWORD))
            db.add(user)
            db.flush()
            db.add(Subscription(user_id=user.id, tier="free", is_active=True))
            print(f"[+] created new admin user {NEW_EMAIL}")
        else:
            user.email = NEW_EMAIL
            user.hashed_password = hash_password(NEW_PASSWORD)
            print(f"[+] updated user {user.id}: email -> {NEW_EMAIL}, password reset")

        db.commit()
        print("[ok] done. restart the backend so .env ADMIN_EMAILS reloads.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
