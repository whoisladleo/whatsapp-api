import asyncio
import os
import sys

# Running this as `python scripts/provision_pool_number.py` puts scripts/
# (not the project root) on sys.path[0], so `app` can't be imported without
# this — same fix already applied in scripts/simulate_whatsapp.py.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services.pool_provisioning import PoolProvisioningError, provision_pool_number

engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


async def main():
    country_code = sys.argv[1] if len(sys.argv) > 1 else settings.TWILIO_COUNTRY_CODE
    db = SessionLocal()
    try:
        print(f"Provisioning a new pool number in {country_code!r}...")
        pool_number = await provision_pool_number(db, country_code=country_code)
        print(
            f"Done — {pool_number.phone_number_id} added to the pool as AVAILABLE "
            f"({pool_number.display_name})."
        )
    except PoolProvisioningError as e:
        print(f"Provisioning failed at status={e.attempt.status.value}: {e}")
        print(f"See phone_number_provisioning_attempts.id={e.attempt.id} for details.")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
