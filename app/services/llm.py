from google import genai
from google.genai import types
import os
from typing import List, Optional
from app.core.config import settings

class LLMService:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.model_id = 'gemini-3-flash-preview'

    def _get_knowledge_base(self, product_slug: str) -> str:
        kb_path = f"knowledge/{product_slug}.md"
        if not os.path.exists(kb_path):
            return ""
        
        with open(kb_path, 'r') as f:
            return f.read()

    async def generate_response(
        self, 
        message_body: str, 
        product_slug: Optional[str] = None, 
        knowledge_content: Optional[str] = None,
        system_prompt: Optional[str] = None,
        history: List[dict] = None
    ) -> str:
        # 1. Prepare context from Knowledge Base
        kb_content = knowledge_content or ""
        if not kb_content and product_slug:
            kb_content = self._get_knowledge_base(product_slug)
        
        # 2. Build system instruction
        base_instruction = "You are a helpful customer support AI for a WhatsApp-based business."
        if kb_content:
            base_instruction += f"\n\nHere is the knowledge base for the product:\n{kb_content}"
        
        if system_prompt:
            base_instruction += f"\n\nAdditional instructions: {system_prompt}"
        
        try:
            # 3. Call Gemini using the new SDK (Async)
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=message_body,
                config=types.GenerateContentConfig(
                    system_instruction=base_instruction,
                    temperature=0.7,
                )
            )
            return response.text
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."

llm_service = LLMService(api_key=settings.GOOGLE_API_KEY) if settings.GOOGLE_API_KEY else None
