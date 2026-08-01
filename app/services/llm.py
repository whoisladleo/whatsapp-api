from google import genai
from google.genai import types
import os
from typing import List, Optional
from app.core.config import settings
from sqlalchemy.orm import Session
from app.db.models import Customer, ConversationContext, ConversationState, Product, WhatsAppAccount

class LLMService:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key) if api_key else None
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
        history: List[dict] = None,
        from_phone: str = None,
        db: Session = None,
        account: WhatsAppAccount = None
    ) -> str:
        if not self.client:
            return "AI service is currently unavailable."
            
        # 1. Fetch customer & context
        customer = db.query(Customer).filter(Customer.phone_number == from_phone).first()
        if not customer:
            customer = Customer(phone_number=from_phone)
            db.add(customer)
            db.commit()
            db.refresh(customer)
            
        context = db.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
        if not context:
            context = ConversationContext(customer_id=customer.id)
            db.add(context)
            db.commit()
            db.refresh(context)

        # If already in handoff, don't generate AI response
        if context.state == ConversationState.HUMAN_HANDOFF:
            # We assume a human agent is dealing with it. 
            return None # Don't send an automated message

        # 2. Fetch products for this merchant
        products = db.query(Product).filter(Product.merchant_id == account.user_id).all()
        if not products:
            catalog_str = "No products currently available."
        else:
            catalog_str = "Available Products:\n"
            for p in products:
                catalog_str += f"- {p.name}: Max Price {p.max_price}, Min Price (SECRET FLOOR) {p.min_price}, Stock: {p.quantity}\n"

        # 3. Prepare knowledge base
        kb_content = knowledge_content or ""
        if not kb_content and product_slug:
            kb_content = self._get_knowledge_base(product_slug)
        
        # 4. Build system instruction
        base_instruction = f"""You are the AI Sales & Negotiation Engine for a WhatsApp-based merchant.
Your goal is to sell the available products to the customer.

{catalog_str}

NEGOTIATION RULES:
1. Pitch: Always anchor your first offer at the Max Price.
2. Counter-Offers: Negotiate down slowly in increments of roughly 10% of the difference. DO NOT jump straight to the Min Price.
3. Hard Floor Rule: NEVER reveal the Min Price outright until absolutely necessary. If a customer offers below the Min Price, decline politely and state the absolute floor price.
4. If a customer agrees to buy at a specific price, say [AGREEMENT_REACHED: price] internally.
5. If the customer is unreasonable after 3 attempts, or asks questions completely unrelated to the catalog, you must include the exact phrase [HUMAN_HANDOFF] in your response.

Current State:
You are chatting with a customer. Be polite, persuasive, and use Swahili or English depending on their language.
"""
        if kb_content:
            base_instruction += f"\n\nHere is additional knowledge:\n{kb_content}"
        
        if system_prompt:
            base_instruction += f"\n\nAdditional merchant instructions: {system_prompt}"
        
        try:
            # 5. Call Gemini
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=message_body,
                config=types.GenerateContentConfig(
                    system_instruction=base_instruction,
                    temperature=0.7,
                )
            )
            
            response_text = response.text
            
            # Post-process response for special triggers
            if "[HUMAN_HANDOFF]" in response_text:
                context.state = ConversationState.HUMAN_HANDOFF
                db.commit()
                response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
                # In a full system, this would trigger an alert to the Operations number.
                if not response_text:
                    response_text = "I am transferring you to a human agent who can help you further. Please hold."
            
            # If agreement is reached, we would normally trigger Tunzaa integration here.
            # E.g. "[AGREEMENT_REACHED: 45000]"
            
            return response_text
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."

llm_service = LLMService(api_key=settings.GOOGLE_API_KEY) if settings.GOOGLE_API_KEY else None
