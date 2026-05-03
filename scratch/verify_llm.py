import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

try:
    from app.services.llm import LLMService
    from app.core.config import settings
    print("LLM Service imported successfully.")
    
    # Mock settings for test
    if not settings.GOOGLE_API_KEY:
        print("Note: GOOGLE_API_KEY not set in environment.")
    
except Exception as e:
    print(f"Import failed: {e}")
    sys.exit(1)
