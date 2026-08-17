import os
import sys

# Running this as `python scripts/add_pool_number.py` puts scripts/ (not the
# project root) on sys.path[0], so `app` can't be imported without this —
# same fix already applied in scripts/simulate_whatsapp.py.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from app.db.models import PhoneNumberPool, PhoneNumberStatus

load_dotenv()

# Build database URL from .env
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_SERVER = os.getenv("POSTGRES_SERVER")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_SERVER}:{POSTGRES_PORT}/{POSTGRES_DB}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def add_pool_number(phone_number_id: str, display_name: str = None, two_step_pin: str = None):
    """
    Registers an already-provisioned Meta phone_number_id (verified and set
    up in Meta Business Manager outside this codebase) as AVAILABLE in the
    phone number pool, ready to be auto-assigned to the next business that
    signs up without bringing their own WhatsApp credentials.
    """
    db = SessionLocal()
    try:
        existing = (
            db.query(PhoneNumberPool)
            .filter(PhoneNumberPool.phone_number_id == phone_number_id)
            .first()
        )
        if existing:
            print(
                f"Phone number {phone_number_id} is already in the pool "
                f"(status: {existing.status.value})."
            )
            return

        entry = PhoneNumberPool(
            phone_number_id=phone_number_id,
            display_name=display_name,
            two_step_pin=two_step_pin,
            status=PhoneNumberStatus.AVAILABLE,
        )
        db.add(entry)
        db.commit()
        print(f"Added {phone_number_id} to the pool (AVAILABLE).")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    phone_number_id = (
        sys.argv[1] if len(sys.argv) > 1 else input("Meta phone_number_id: ").strip()
    )
    display_name = (
        sys.argv[2]
        if len(sys.argv) > 2
        else input("Display name (optional, press Enter to skip): ").strip() or None
    )
    two_step_pin = (
        sys.argv[3]
        if len(sys.argv) > 3
        else input(
            "Two-step verification PIN set for this number in Meta Business "
            "Manager (required to auto-activate display name changes later; "
            "press Enter to skip): "
        ).strip()
        or None
    )
    add_pool_number(phone_number_id, display_name, two_step_pin)
