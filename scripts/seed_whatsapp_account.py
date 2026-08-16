import os
import sys

# Running this as `python scripts/seed_whatsapp_account.py` puts scripts/
# (not the project root) on sys.path[0], so `app` can't be imported without
# this — same fix already applied in scripts/simulate_whatsapp.py.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from app.db.models import Base, User, WhatsAppAccount

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

def seed_test_account():
    db = SessionLocal()
    try:
        # 1. Create a dummy user if none exists
        user = db.query(User).first()
        if not user:
            print("No user found. Creating a dummy owner user...")
            user = User(username="admin")
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created user ID: {user.id}")

        # 2. Check if the WhatsApp account exists
        phone_number_id = os.getenv("PHONE_NUMBER_ID")
        access_token = os.getenv("WHATSAPP_TOKEN")
        
        account = db.query(WhatsAppAccount).filter(WhatsAppAccount.phone_number_id == phone_number_id).first()
        
        if account:
            print(f"Account with Phone Number ID {phone_number_id} already exists!")
            
            # Update the token just in case
            if account.access_token != access_token:
                account.access_token = access_token
                db.commit()
                print("Updated access token for existing account.")
        else:
            print(f"Creating WhatsApp Account entry for {phone_number_id}...")
            new_account = WhatsAppAccount(
                name="Test Account",
                phone_number_id=phone_number_id,
                access_token=access_token,
                ai_enabled=True,
                user_id=user.id
            )
            db.add(new_account)
            db.commit()
            print("Successfully added the WhatsApp account to the database!")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_test_account()
