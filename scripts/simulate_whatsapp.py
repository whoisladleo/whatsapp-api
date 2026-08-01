import sys
import httpx
import os
import uuid
import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup path so we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.models import WhatsAppAccount, Message
from app.core.config import settings

def main():
    print("========================================")
    print(" WhatsApp Simulation CLI (Local Testing)")
    print("========================================")
    
    # 1. Connect to DB
    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    # Get the first WhatsApp account to simulate sending a message to
    account = db.query(WhatsAppAccount).first()
    if not account:
        print("Error: No WhatsApp account found in the database. Please create one first.")
        return
        
    print(f"[*] Simulating messages to Account: {account.name} (Phone ID: {account.phone_number_id})")
    
    from_phone = input("Enter a test customer phone number (e.g., 255712345678): ").strip()
    if not from_phone:
        from_phone = "255700000000"
        
    print(f"[*] Starting chat as {from_phone}.")
    print("Type your message below and press Enter. Type 'exit' to quit.\n")
    
    with httpx.Client() as client:
        while True:
            try:
                msg = input(f"You ({from_phone}): ").strip()
                if msg.lower() == 'exit':
                    break
                if not msg:
                    continue
                    
                # 2. Construct Webhook Payload (Mocking Meta's structure)
                payload = {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "id": "123456",
                            "changes": [
                                {
                                    "value": {
                                        "messaging_product": "whatsapp",
                                        "metadata": {
                                            "display_phone_number": "123456",
                                            "phone_number_id": account.phone_number_id
                                        },
                                        "contacts": [
                                            {
                                                "profile": {"name": "Test User"},
                                                "wa_id": from_phone
                                            }
                                        ],
                                        "messages": [
                                            {
                                                "from": from_phone,
                                                "id": f"wamid.{uuid.uuid4().hex}",
                                                "timestamp": str(int(time.time())),
                                                "text": {"body": msg},
                                                "type": "text"
                                            }
                                        ]
                                    },
                                    "field": "messages"
                                }
                            ]
                        }
                    ]
                }
                
                # Record the last message ID to know what's new
                last_msg = db.query(Message).filter(
                    Message.to_phone == from_phone,
                    Message.direction == "outbound"
                ).order_by(Message.id.desc()).first()
                last_msg_id = last_msg.id if last_msg else 0
                
                # 3. Post to local Webhook
                response = client.post("http://localhost:8000/api/v1/whatsapp/webhook", json=payload, timeout=30.0)
                
                if response.status_code == 200:
                    # 4. Wait for the LLM to process and write to DB
                    print("... Amara is thinking ...", end="\r")
                    
                    # Poll the DB for a few seconds to get the AI's response
                    ai_reply = None
                    for _ in range(10): # 5 seconds max wait
                        time.sleep(0.5)
                        new_msg = db.query(Message).filter(
                            Message.to_phone == from_phone,
                            Message.direction == "outbound",
                            Message.id > last_msg_id
                        ).order_by(Message.id.desc()).first()
                        
                        if new_msg:
                            ai_reply = new_msg.body
                            break
                            
                    # Clear the "thinking" line
                    print(" " * 30, end="\r")
                    
                    if ai_reply:
                        print(f"Amara: {ai_reply}\n")
                    else:
                        print("[System] No response received in DB. (Check server logs for LLM errors)\n")
                else:
                    print(f"[System Error] {response.status_code}: {response.text}\n")
                    
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error: {e}")

if __name__ == "__main__":
    main()
