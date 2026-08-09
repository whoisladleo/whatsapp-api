from google import genai
from google.genai import types
import os
import re
import uuid
from typing import List, Optional
from app.core.config import settings
from sqlalchemy.orm import Session
from app.db.models import Customer, ConversationContext, ConversationState, Product, WhatsAppAccount, Message, User

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

        # 1.5 Determine merchant context
        is_platform_account = (account.name.lower() == 'taskflow')
        
        merchant_user = None
        if is_platform_account:
            # The person texting IS the merchant
            merchant_user = db.query(User).filter(User.username == from_phone).first()
            if not merchant_user:
                merchant_user = User(
                    username=from_phone
                )
                db.add(merchant_user)
                db.commit()
                db.refresh(merchant_user)
            merchant_id = merchant_user.id
        else:
            # The person texting is a customer, the merchant is the account owner
            merchant_id = account.user_id

        # 2. Fetch products for this merchant
        products = db.query(Product).filter(Product.merchant_id == merchant_id).all()
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
        
        # Check if we have ever replied to this user
        outbound_count = db.query(Message).filter(
            Message.to_phone == from_phone,
            Message.account_id == account.id,
            Message.direction == "outbound"
        ).count()
        is_first_message = (outbound_count == 0)

        # Build chat history string for context
        history_msgs = db.query(Message).filter(
            ((Message.from_phone == from_phone) | (Message.to_phone == from_phone)),
            Message.account_id == account.id
        ).order_by(Message.timestamp.asc()).limit(20).all()
        
        chat_history_str = ""
        for m in history_msgs:
            # Skip empty bodies
            if not m.body:
                continue
            sender = "Customer" if m.direction == "inbound" else "Amara"
            chat_history_str += f"{sender}: {m.body}\n"

        # 4. Build system instruction
        
        if is_platform_account:
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for Taskflow OS.
Your goal is to onboard new merchants and explain the value of having an AI agent handle their WhatsApp sales.

{catalog_str}
"""
            
            if is_first_message:
                base_instruction += """
RULES:
1. Welcome Message: This is the very first time we are replying. You MUST reply with EXACTLY the text below, word-for-word, based on the user's language (default to Swahili). DO NOT add any other text or greetings.

[English Welcome]
Habari! My name is *Amara* from *Taskflow OS*.
I am your 24/7 AI Sales Agent on WhatsApp. I handle customer inquiries, negotiate prices, track your inventory, and collect payments via Tunzaa—completely on autopilot.

*How we work together:*
  *Zero Upfront Fee:* You pay 0 TZS to get started.
  *My Monthly Fee:* 100,000 TZS / month (auto-deducted directly from your sales).

To activate your store and add your products, please log in to your merchant dashboard here:
🔗 *https://dashboard-taskflow.whoisladleo.dev/*

Log in using your WhatsApp number to get started!

[Swahili Welcome]
Habari! Naitwa *Amara* kutoka *Taskflow OS*.
Mimi ni Msaidizi wako wa AI wa mauzo kwenye WhatsApp. Nitafanya kazi ya kujibu wateja, kujadiliana bei, kutunza stoki, na kupokea malipo kupitia Tunzaa masaa 24/7—bila wewe kutumia nguvu.

*Jinsi tunavyofanya kazi:*
 *Gharama ya Kuanza:* 0 TZS (Hulipii chochote kuanza).
 *Gharama Yangu:* 100,000 TZS kwa mwezi (inakatwa moja kwa moja kwenye mauzo yako).

Ili nianze kazi rasmi na kuweka bidhaa zako, tafadhali ingia kwenye mfumo wako wa mauzo (dashboard) hapa:
🔗 *https://dashboard-taskflow.whoisladleo.dev/*

Tumia namba yako ya WhatsApp kuingia na kukamilisha usajili!
"""
            else:
                base_instruction += """
RULES:
1. Assistance: Answer any questions they have about Taskflow OS, pricing, or how the AI agent works.
2. Onboarding & Setup: You CANNOT add products or register businesses via WhatsApp. If the user asks you to register their business, add a product, or set up their store, you MUST tell them that you cannot do this here and they MUST use the merchant dashboard: https://dashboard-taskflow.whoisladleo.dev/ . Do NOT pretend or hallucinate that you have added their products or business.
3. Pricing & Negotiation: If they ask about other products, use the catalog info. But your main goal is selling the AI subscription and getting them to log into the dashboard.
4. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
"""
        else:
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for {account.name}.
Your goal is to politely assist customers, answer questions, and sell products from the catalog.

{catalog_str}

RULES:
1. Selling: Be helpful and persuasive. Try to close the sale.
2. Pricing & Negotiation: Use the provided catalog max and min prices to negotiate. Never sell below the minimum price.
3. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
"""
        
        base_instruction += """
Current State:
You are chatting with a customer. Be polite, persuasive, and use Swahili or English depending on their language.

Formatting Rule:
ALWAYS use WhatsApp markdown. For bold text, use a single asterisk (e.g., *bold text*). NEVER use double asterisks (**).
"""
        if kb_content:
            base_instruction += f"\n\nHere is additional knowledge:\n{kb_content}"
        
        if system_prompt:
            base_instruction += f"\n\nAdditional merchant instructions: {system_prompt}"
        
        try:
            # 5. Call Gemini
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=f"Recent Conversation:\n{chat_history_str}\n\nRespond as Amara:",
                config=types.GenerateContentConfig(
                    system_instruction=base_instruction,
                    temperature=0.7,
                )
            )
            
            response_text = response.text
            
            # Post-process response for special triggers
            
            # 2. Human Handoff
            if "[HUMAN_HANDOFF]" in response_text:
                context.state = ConversationState.HUMAN_HANDOFF
                db.commit()
                response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
                
                # Alert the merchant
                if not is_platform_account and account.owner:
                    merchant_phone = account.owner.username
                    alert_message = f"🚨 *HUMAN HANDOFF REQUIRED* 🚨\nA customer at {from_phone} needs your help!\n\n*Last AI message:* {response_text}"
                    from app.core.provider import WhatsAppProvider
                    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
                    import asyncio
                    asyncio.create_task(provider.send_text(merchant_phone, alert_message))

                if not response_text:
                    response_text = "I am transferring you to a human agent who can help you further. Please hold."
            
            # If agreement is reached, we would normally trigger Tunzaa integration here.
            # E.g. "[AGREEMENT_REACHED: 45000]"
            
            return response_text
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."

llm_service = LLMService(api_key=settings.GOOGLE_API_KEY) if settings.GOOGLE_API_KEY else None
