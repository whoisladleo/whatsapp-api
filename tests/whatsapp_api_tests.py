import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "YOUR_SYSTEM_USER_ACCESS_TOKEN")
APP_ID = os.getenv("WHATSAPP_APP_ID", "YOUR_APP_ID")
WABA_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "YOUR_WABA_ID")
BUSINESS_ID = os.getenv("META_BUSINESS_ID", "YOUR_BUSINESS_ID")

API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

def test_whatsapp_business_management():
    """
    Test call for 'whatsapp_business_management' permission.
    Fetches phone numbers associated with the WhatsApp Business Account.
    """
    print("\n--- Testing whatsapp_business_management ---")
    url = f"{BASE_URL}/{WABA_ID}/phone_numbers"
    response = requests.get(url, headers=headers)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")

def test_business_management():
    """
    Test call for 'business_management' permission.
    Fetches client WhatsApp Business Accounts for a Business Manager.
    """
    print("\n--- Testing business_management ---")
    url = f"{BASE_URL}/{BUSINESS_ID}/client_whatsapp_business_accounts"
    response = requests.get(url, headers=headers)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")

def setup_webhook_api(callback_url, verify_token):
    """
    Subscribes the app to WhatsApp webhooks using the Meta Graph API.
    This demonstrates the 'hooks API for whatsapp'.
    """
    print("\n--- Subscribing to Webhooks via API ---")
    url = f"{BASE_URL}/{APP_ID}/subscriptions"
    
    payload = {
        "object": "whatsapp_business_account",
        "callback_url": callback_url,
        "verify_token": verify_token,
        "fields": "messages,message_template_status_update,phone_number_name_update,phone_number_quality_update,account_review_update,account_alerts"
    }
    
    response = requests.post(url, headers=headers, data=payload)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")

if __name__ == "__main__":
    print("WhatsApp API App Review Tester")
    print("---------------------------------")
    print("Ensure you have set the required environment variables in your .env file or update the script.")
    
    # 1. Test API calls to get green checkmarks in App Dashboard
    test_whatsapp_business_management()
    test_business_management()
    
    # 2. To configure webhooks via API, uncomment and provide your URL/Token:
    # WEBHOOK_URL = "https://your-domain.com/api/v1/whatsapp/webhook"
    # VERIFY_TOKEN = "your_verify_token_here"
    # setup_webhook_api(WEBHOOK_URL, VERIFY_TOKEN)
