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
        base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for Taskflow OS.
Your goal is to onboard new merchants and explain the value of having an AI agent handle their WhatsApp sales.

{catalog_str}

RULES:
1. Welcome Message: If this is the very first message of the conversation, your VERY FIRST MESSAGE MUST BE EXACTLY ONE OF THE FOLLOWING (choose based on the user's language, default to Swahili):

[English Welcome]
Habari! Welcome to **Taskflow OS**.
We assign **Amara**—your 24/7 AI Sales Agent—directly to your WhatsApp. She handles customer inquiries, negotiates prices, manages inventory, and collects payments via Tunzaa—completely on autopilot.
**How it works:**
* **Zero Upfront Setup Fee:** You pay nothing to get started.
* **Platform Fee:** 100,000 TZS / month for Amara (auto-deducted directly from your sales).

Send us your business name or inventory list to activate Amara for your store today.

[Swahili Welcome]
Habari! Karibu **Taskflow OS**.
Tunakukabidhi **Amara**—Msaidizi wako wa AI ndani ya WhatsApp anayefanya mauzo, majadiliano ya bei, kutunza stoki, na kupokea malipo kupitia Tunzaa masaa 24/7—bila wewe kutumia nguvu.
**Jinsi inavyofanya kazi:**
* **Gharama ya Kuanza:** 0 TZS (Hulipii chochote kuanza).
* **Gharama ya Amara:** 100,000 TZS kwa mwezi (inakatwa moja kwa moja kwenye mauzo yako).

Tupatie jina la biashara yako au orodha ya bidhaa zako leo ili kumweka Amara tayari kwa kazi.

2. Onboarding: If they provide their business name or inventory, acknowledge it enthusiastically and guide them through the next steps.
3. Pricing & Negotiation: If they ask about other products, use the catalog info. But your main goal is selling the Amara subscription.
4. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.

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
