import enum
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean, Float, Enum
from sqlalchemy.orm import relationship, DeclarativeBase
from datetime import datetime

class Base(DeclarativeBase):
    pass

class KYCStatus(enum.Enum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"

class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

class InstallmentStatus(enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    OVERDUE = "OVERDUE"

class PaymentType(enum.Enum):
    FULL = "FULL"
    INSTALLMENT = "INSTALLMENT"

class ConversationState(enum.Enum):
    NEGOTIATING = "NEGOTIATING"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    MANAGEMENT = "MANAGEMENT"

class PhoneNumberStatus(enum.Enum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    RELEASED = "RELEASED"


class ProvisioningStatus(enum.Enum):
    """
    Tracks an in-progress automated pool-number provisioning run (see
    app/services/pool_provisioning.py) — distinct from PhoneNumberStatus,
    which is the pool's own AVAILABLE/ASSIGNED/RELEASED lifecycle for a
    number that's already fully set up. A row here only ever becomes a
    PhoneNumberPool row on reaching COMPLETED.
    """
    PURCHASED = "PURCHASED"
    META_REGISTERED = "META_REGISTERED"
    CODE_REQUESTED = "CODE_REQUESTED"
    VERIFIED = "VERIFIED"
    REGISTERED = "REGISTERED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    otp_code = Column(String, nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)
    business_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("WhatsAppAccount", back_populates="owner")
    instagram_accounts = relationship("InstagramAccount", back_populates="owner")
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner")
    products = relationship("Product", back_populates="merchant")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    content = Column(String, nullable=False)  # The actual markdown/text content
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="knowledge_bases")


class WhatsAppAccount(Base):
    __tablename__ = "whatsapp_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone_number_id = Column(String, unique=True, index=True, nullable=False)
    access_token = Column(String, nullable=False)
    verify_token = Column(String, nullable=True)
    callback_url = Column(String, nullable=True)  # URL to forward incoming messages
    product_slug = Column(String, nullable=True)  # To identify knowledge base
    ai_enabled = Column(Boolean, default=True)
    system_prompt = Column(String, nullable=True)
    kyc_status = Column(Enum(KYCStatus), default=KYCStatus.DRAFT)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="accounts")
    media = relationship("MediaEntry", back_populates="account")
    messages = relationship("Message", back_populates="account")


class PhoneNumberPool(Base):
    __tablename__ = "phone_number_pool"

    id = Column(Integer, primary_key=True, index=True)
    phone_number_id = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)  # plain reference label, e.g. "Taskflow Store #4"
    # This number's Cloud API two-step verification PIN, set once during its
    # one-time manual Meta Business Manager provisioning. Required to call
    # POST /{phone_number_id}/register again after each display-name change
    # (see create_whatsapp_account) — without it, a claimed number's display
    # name can be submitted but never actually activated.
    two_step_pin = Column(String, nullable=True)
    status = Column(Enum(PhoneNumberStatus), default=PhoneNumberStatus.AVAILABLE, nullable=False)
    whatsapp_account_id = Column(Integer, ForeignKey("whatsapp_accounts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    assigned_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)

    whatsapp_account = relationship("WhatsAppAccount")


class PhoneNumberProvisioningAttempt(Base):
    __tablename__ = "phone_number_provisioning_attempts"

    id = Column(Integer, primary_key=True, index=True)
    # Twilio's SID for the purchased number — needed to release/cancel it if
    # a later step fails, so a failed run doesn't leave an orphaned,
    # still-billed Twilio number behind.
    twilio_sid = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)  # E.164, e.g. +12025551234
    phone_number_id = Column(String, nullable=True)  # Meta's ID, set once META_REGISTERED
    two_step_pin = Column(String, nullable=True)
    # Populated by the Twilio inbound-SMS webhook once Meta's verification
    # code arrives (app/api/v1/pool.py) — the provisioning script polls this
    # column rather than receiving the code directly, since the code arrives
    # out-of-band on a separate request.
    verification_code = Column(String, nullable=True)
    status = Column(Enum(ProvisioningStatus), default=ProvisioningStatus.PURCHASED, nullable=False)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NumberSubscriptionStatus(enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"  # Tunzaa push sent, awaiting the merchant's approval
    PROVISIONING = "PROVISIONING"  # payment confirmed, Twilio/Meta pipeline running
    ACTIVE = "ACTIVE"  # number live, billing on schedule
    PAYMENT_FAILED = "PAYMENT_FAILED"  # initial purchase payment failed/cancelled — no number, no retry
    PAST_DUE = "PAST_DUE"  # a renewal charge failed once — one retry scheduled before DEACTIVATED
    DEACTIVATED = "DEACTIVATED"  # renewal retry also failed — ai_enabled turned off, number kept
    PROVISIONING_FAILED = "PROVISIONING_FAILED"  # payment succeeded but Twilio/Meta failed — refunded


class NumberSubscription(Base):
    """
    The billing side of an on-demand purchased number (10,000 TZS/month) —
    deliberately separate from PhoneNumberProvisioningAttempt, which only
    tracks the Twilio/Meta technical steps. One row per WhatsAppAccount that
    has ever purchased a number this way; BYON and ops-pool-assigned
    accounts have no row here at all.
    """
    __tablename__ = "number_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("whatsapp_accounts.id"), unique=True, nullable=False)
    status = Column(Enum(NumberSubscriptionStatus), default=NumberSubscriptionStatus.PENDING_PAYMENT, nullable=False)
    monthly_amount = Column(Float, nullable=False, default=10000)
    tunzaa_transaction_id = Column(String, nullable=True)
    # When the next renewal charge is due (ACTIVE) — checked/used by
    # app/tasks/number_billing.py's self-rescheduling renewal task.
    next_billing_at = Column(DateTime, nullable=True)
    # 0 = no renewal retry used yet; 1 = the one allowed retry has fired.
    # A second consecutive failure at retry_count == 1 triggers DEACTIVATED.
    retry_count = Column(Integer, default=0, nullable=False)
    provisioning_attempt_id = Column(
        Integer, ForeignKey("phone_number_provisioning_attempts.id"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("WhatsAppAccount")
    provisioning_attempt = relationship("PhoneNumberProvisioningAttempt")


class MediaEntry(Base):
    __tablename__ = "media_entries"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("whatsapp_accounts.id"))
    media_id = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    mime_type = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("WhatsAppAccount", back_populates="media")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    whatsapp_message_id = Column(String, unique=True, index=True, nullable=True)
    from_phone = Column(String, nullable=True)
    to_phone = Column(String, nullable=True)
    type = Column(String, nullable=False)  # text, image, template, etc.
    body = Column(String, nullable=True)  # Message content or summary
    raw_data = Column(JSON, nullable=True)  # Full raw payload from Meta
    direction = Column(String, nullable=False)  # inbound, outbound
    status = Column(String, default="sent")  # sent, delivered, read, failed
    account_id = Column(Integer, ForeignKey("whatsapp_accounts.id"))
    timestamp = Column(DateTime, default=datetime.utcnow)

    account = relationship("WhatsAppAccount", back_populates="messages")


class InstagramAccount(Base):
    """
    A merchant's connected Instagram professional account — always
    bring-your-own (unlike WhatsAppAccount, there's no pool: Instagram
    accounts can't be provisioned the way phone numbers can), linked via
    Business Login for Instagram (Instagram API with Instagram Login) OAuth.
    No Facebook Page is involved in this flow — the access token is scoped
    directly to the Instagram professional account itself.
    """
    __tablename__ = "instagram_accounts"

    id = Column(Integer, primary_key=True, index=True)
    ig_business_id = Column(String, unique=True, index=True, nullable=False)
    access_token = Column(String, nullable=False)
    username = Column(String, nullable=True)
    ai_enabled = Column(Boolean, default=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="instagram_accounts")
    comments = relationship("InstagramComment", back_populates="account")


class InstagramComment(Base):
    """
    A comment received on a connected account's media, and Amara's reply
    state for it — deliberately its own table rather than reusing Message,
    which is shaped around WhatsApp's phone-number identifiers.
    """
    __tablename__ = "instagram_comments"

    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(String, unique=True, index=True, nullable=False)
    media_id = Column(String, nullable=True)
    from_username = Column(String, nullable=True)
    # Instagram-scoped user id for the commenter — needed as the `recipient`
    # for a private-reply DM, distinct from their (not always present) username.
    from_ig_scoped_id = Column(String, nullable=True)
    body = Column(String, nullable=True)
    public_reply_sent = Column(Boolean, default=False, nullable=False)
    # Private replies are one-shot per comment (Meta allows exactly one),
    # so this also guards against re-sending on a duplicate webhook delivery.
    private_reply_sent = Column(Boolean, default=False, nullable=False)
    account_id = Column(Integer, ForeignKey("instagram_accounts.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("InstagramAccount", back_populates="comments")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    category = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    sku = Column(String, nullable=True) # Will be unique per merchant logically
    barcode = Column(String, nullable=True)
    max_price = Column(Float, nullable=False)
    min_price = Column(Float, nullable=False)
    cost_price = Column(Float, nullable=True)
    quantity = Column(Integer, default=0)
    weight = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    merchant_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    merchant = relationship("User", back_populates="products")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    delivery_address = Column(String, nullable=True)
    delivery_latitude = Column(Float, nullable=True)
    delivery_longitude = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    orders = relationship("Order", back_populates="customer")
    conversation = relationship("ConversationContext", back_populates="customer", uselist=False)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    account_id = Column(Integer, ForeignKey("whatsapp_accounts.id"), nullable=True)
    quantity = Column(Integer, default=1)
    payment_type = Column(Enum(PaymentType), nullable=True)
    tunzaa_transaction_id = Column(String, nullable=True)
    tunzaa_plan_id = Column(String, nullable=True)
    # Only meaningful for INSTALLMENT orders; synced from Tunzaa's
    # plan-level paid_amount/remaining_balance on each reconciliation
    # (see app/api/v1/tunzaa_webhook.py) rather than computed locally —
    # Tunzaa's ledger is the source of truth for this, not ours.
    paid_amount = Column(Float, nullable=True)
    remaining_balance = Column(Float, nullable=True)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    agreed_price = Column(Float, nullable=True)
    tunzaa_link = Column(String, nullable=True)
    reserved_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    installments = relationship("Installment", back_populates="order")
    product = relationship("Product")
    account = relationship("WhatsAppAccount")


class Installment(Base):
    __tablename__ = "installments"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    tunzaa_installment_id = Column(String, nullable=True)
    amount_due = Column(Float, nullable=False)
    due_date = Column(DateTime, nullable=False)
    status = Column(Enum(InstallmentStatus), default=InstallmentStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order", back_populates="installments")


class ConversationContext(Base):
    __tablename__ = "conversation_contexts"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), unique=True)
    state = Column(Enum(ConversationState), default=ConversationState.NEGOTIATING)
    failed_cycles = Column(Integer, default=0)
    current_product_intent = Column(String, nullable=True)
    last_offer = Column(Float, nullable=True)
    handoff_at = Column(DateTime, nullable=True)
    # Tracks an outstanding price quote the customer hasn't acted on yet, so
    # app/tasks/quote_nudges.py can follow up if they go quiet. Cleared
    # proactively on the customer's next message and when a deal closes —
    # see LLMService.generate_response / _close_deal.
    pending_quote_price = Column(Float, nullable=True)
    pending_quote_at = Column(DateTime, nullable=True)
    quote_nudge_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="conversation")
