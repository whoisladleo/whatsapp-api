import enum
import uuid
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean, Float, Enum, Uuid
from sqlalchemy.orm import relationship, DeclarativeBase
from datetime import datetime

class Base(DeclarativeBase):
    pass

# Renders as a native Postgres UUID column in prod, and degrades to a
# portable representation (CHAR(32)) on the SQLite test DB — see
# tests/conftest.py — so every id/FK column uses this instead of Integer.
# IDs are generated Python-side (default=uuid.uuid4) rather than via
# server_default=gen_random_uuid(), so no Postgres extension is required.
UUIDType = Uuid(as_uuid=True, native_uuid=True)

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

class InstallmentMode(enum.Enum):
    # Only meaningful for a subscription product (Product.is_subscription)
    # with Product.allow_installments — governs when
    # _activate_subscription_if_linked actually activates the
    # subscription, not anything about regular one-time orders (those have
    # no fulfillment-gating concept in this system; delivery is manual).
    BNPL = "BNPL"  # activates on the first installment payment (default, matches pre-existing behavior)
    SNPL = "SNPL"  # waits until the full installment plan is paid off

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

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    # Nullable: an account with no WhatsApp phone has no username — WhatsApp
    # OTP login and merchant-command routing (matching a WhatsApp sender
    # against account.owner.username) simply never apply to that user.
    username = Column(String, unique=True, index=True, nullable=True)
    otp_code = Column(String, nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)
    business_name = Column(String, nullable=True)
    # Legacy: set by the removed Facebook Login. Kept so existing rows
    # stay valid; nothing writes or reads it for login anymore.
    facebook_id = Column(String, unique=True, index=True, nullable=True)
    email = Column(String, nullable=True)
    name = Column(String, nullable=True)
    # Self-reported phone number for an account with no `username` — the only
    # way app/services/subscription_access.py can match it against the Customer
    # who paid for the platform subscription. OTP merchants don't need this;
    # their phone is already `username`.
    phone_number = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("WhatsAppAccount", back_populates="owner")
    instagram_accounts = relationship("InstagramAccount", back_populates="owner")
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner")
    products = relationship("Product", back_populates="merchant")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    content = Column(String, nullable=False)  # The actual markdown/text content
    user_id = Column(UUIDType, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="knowledge_bases")


class WhatsAppAccount(Base):
    __tablename__ = "whatsapp_accounts"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String, nullable=False)
    phone_number_id = Column(String, unique=True, index=True, nullable=False)
    access_token = Column(String, nullable=False)
    verify_token = Column(String, nullable=True)
    callback_url = Column(String, nullable=True)  # URL to forward incoming messages
    product_slug = Column(String, nullable=True)  # To identify knowledge base
    ai_enabled = Column(Boolean, default=True)
    system_prompt = Column(String, nullable=True)
    kyc_status = Column(Enum(KYCStatus), default=KYCStatus.DRAFT)
    user_id = Column(UUIDType, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    # Marks Taskflow's own onboarding account, where llm.py treats the
    # merchant themselves as the counterparty instead of a customer.
    # Explicit flag rather than matching account.name == "taskflow" — a
    # plain Account Name edit (e.g. from the dashboard settings form)
    # silently broke that in production once already. See #21.
    is_platform_account = Column(Boolean, default=False, nullable=False)

    owner = relationship("User", back_populates="accounts")
    media = relationship("MediaEntry", back_populates="account")
    messages = relationship("Message", back_populates="account")


class PhoneNumberPool(Base):
    __tablename__ = "phone_number_pool"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    phone_number_id = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)  # plain reference label, e.g. "Taskflow Store #4"
    # This number's Cloud API two-step verification PIN, set once during its
    # one-time manual Meta Business Manager provisioning. Required to call
    # POST /{phone_number_id}/register again after each display-name change
    # (see create_whatsapp_account) — without it, a claimed number's display
    # name can be submitted but never actually activated.
    two_step_pin = Column(String, nullable=True)
    status = Column(Enum(PhoneNumberStatus), default=PhoneNumberStatus.AVAILABLE, nullable=False)
    whatsapp_account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    assigned_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)

    whatsapp_account = relationship("WhatsAppAccount")


class PhoneNumberProvisioningAttempt(Base):
    __tablename__ = "phone_number_provisioning_attempts"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
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

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"), unique=True, nullable=False)
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
        UUIDType, ForeignKey("phone_number_provisioning_attempts.id"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("WhatsAppAccount")
    provisioning_attempt = relationship("PhoneNumberProvisioningAttempt")


class MediaEntry(Base):
    __tablename__ = "media_entries"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"))
    media_id = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    mime_type = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("WhatsAppAccount", back_populates="media")


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    whatsapp_message_id = Column(String, unique=True, index=True, nullable=True)
    from_phone = Column(String, nullable=True)
    to_phone = Column(String, nullable=True)
    type = Column(String, nullable=False)  # text, image, template, etc.
    body = Column(String, nullable=True)  # Message content or summary
    raw_data = Column(JSON, nullable=True)  # Full raw payload from Meta
    direction = Column(String, nullable=False)  # inbound, outbound
    status = Column(String, default="sent")  # sent, delivered, read, failed
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"))
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

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    ig_business_id = Column(String, unique=True, index=True, nullable=False)
    access_token = Column(String, nullable=False)
    username = Column(String, nullable=True)
    ai_enabled = Column(Boolean, default=True)
    user_id = Column(UUIDType, ForeignKey("users.id"))
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

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
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
    account_id = Column(UUIDType, ForeignKey("instagram_accounts.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("InstagramAccount", back_populates="comments")


class InstagramCustomer(Base):
    """
    A customer Amara is talking to over Instagram DM — separate from the
    WhatsApp Customer model, which is keyed on phone_number; here the
    identifier is the Instagram-scoped sender id. Instagram gives no phone
    number up front, so closing a deal means Amara has to ask for one during
    negotiation — once she has it, a real (phone-keyed) Customer/Order is
    created the same way as the WhatsApp flow (see
    LLMService._close_instagram_deal), just via Order.instagram_account_id
    instead of Order.account_id.
    """
    __tablename__ = "instagram_customers"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    ig_scoped_id = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    account_id = Column(UUIDType, ForeignKey("instagram_accounts.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("InstagramAccount")
    conversation = relationship(
        "InstagramConversationContext", back_populates="customer", uselist=False
    )


class InstagramConversationContext(Base):
    __tablename__ = "instagram_conversation_contexts"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    customer_id = Column(UUIDType, ForeignKey("instagram_customers.id"), unique=True)
    # Reuses WhatsApp's ConversationState enum (NEGOTIATING/HUMAN_HANDOFF/
    # MANAGEMENT) — same three states apply here.
    state = Column(Enum(ConversationState), default=ConversationState.NEGOTIATING)
    handoff_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("InstagramCustomer", back_populates="conversation")


class InstagramDirectMessage(Base):
    __tablename__ = "instagram_direct_messages"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    ig_message_id = Column(String, unique=True, index=True, nullable=True)
    account_id = Column(UUIDType, ForeignKey("instagram_accounts.id"), nullable=False)
    customer_id = Column(UUIDType, ForeignKey("instagram_customers.id"), nullable=False)
    direction = Column(String, nullable=False)  # inbound, outbound
    body = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("InstagramAccount")
    customer = relationship("InstagramCustomer")


class Product(Base):
    __tablename__ = "products"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
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
    # Opts this product into recurring billing instead of a one-time sale —
    # see ProductSubscription / app/services/product_subscription.py. A deal
    # closed on a subscription product still rides the normal one-time
    # Order/_dispatch_payment flow for its first charge; this flag is what
    # tells _close_deal/_close_instagram_deal to also create the
    # ProductSubscription that drives billing from month 2 onward.
    is_subscription = Column(Boolean, default=False, nullable=False)
    # Whether Amara is allowed to offer installments for this product at
    # all — when False, she never brings it up, only full payment. Default
    # True preserves the pre-existing behavior (every product could always
    # be paid in installments).
    allow_installments = Column(Boolean, default=True, nullable=False)
    # See InstallmentMode — only has any effect when allow_installments and
    # is_subscription are both true. Default BNPL matches the pre-existing
    # behavior (subscription activates on the first installment payment).
    installment_mode = Column(Enum(InstallmentMode), default=InstallmentMode.BNPL, nullable=False)
    merchant_id = Column(UUIDType, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    merchant = relationship("User", back_populates="products")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    delivery_address = Column(String, nullable=True)
    delivery_latitude = Column(Float, nullable=True)
    delivery_longitude = Column(Float, nullable=True)
    # Classified once by the model itself (see llm.py's [LANGUAGE: ..]
    # marker) the first time this customer messages, from whichever
    # channel gets there first — never re-classified afterward, so a
    # customer who switches languages mid-conversation keeps whatever was
    # detected first. NULL until classified; used to pick which language
    # variant of a customer-facing template to send (see
    # app/tasks/order_reminders.py), falling back to
    # settings.WHATSAPP_REMINDER_TEMPLATE_LANGUAGE when still unset.
    language = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    orders = relationship("Order", back_populates="customer")
    conversation = relationship("ConversationContext", back_populates="customer", uselist=False)


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    customer_id = Column(UUIDType, ForeignKey("customers.id"))
    product_id = Column(UUIDType, ForeignKey("products.id"), nullable=True)
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"), nullable=True)
    # An order originates from exactly one channel — this is set instead of
    # account_id for a deal Amara closed over Instagram DM (see
    # LLMService._close_instagram_deal), which needs a real Customer/Order
    # too but has no WhatsAppAccount to hang off (a merchant can be
    # Instagram-only). Never both set on the same order.
    instagram_account_id = Column(UUIDType, ForeignKey("instagram_accounts.id"), nullable=True)
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
    instagram_account = relationship("InstagramAccount")
    subscription = relationship("ProductSubscription", back_populates="order", uselist=False)


class Installment(Base):
    __tablename__ = "installments"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    order_id = Column(UUIDType, ForeignKey("orders.id"))
    tunzaa_installment_id = Column(String, nullable=True)
    amount_due = Column(Float, nullable=False)
    due_date = Column(DateTime, nullable=False)
    status = Column(Enum(InstallmentStatus), default=InstallmentStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order", back_populates="installments")


class ProductSubscriptionStatus(enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"  # first Tunzaa push sent, awaiting the customer's approval
    ACTIVE = "ACTIVE"  # first payment confirmed, billing on schedule
    PAYMENT_FAILED = "PAYMENT_FAILED"  # first payment failed/cancelled — no retry, customer re-negotiates via chat
    PAST_DUE = "PAST_DUE"  # a renewal charge failed — retrying on a schedule, see app/services/product_subscription.py
    CANCELLED = "CANCELLED"  # renewal retries exhausted


class ProductSubscription(Base):
    """
    Recurring billing for a Product with is_subscription=True — created
    alongside the Order that closed the deal (see LLMService._close_deal /
    _close_instagram_deal), and self-reschedules its own renewal charge the
    same way NumberSubscription does (no Celery Beat in this stack — see
    app/tasks/product_subscription_billing.py). The first charge still rides
    the normal one-time Order/_dispatch_payment flow; this row is what turns
    that into recurring billing from month 2 onward.
    """
    __tablename__ = "product_subscriptions"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    order_id = Column(UUIDType, ForeignKey("orders.id"), unique=True, nullable=False)
    customer_id = Column(UUIDType, ForeignKey("customers.id"), nullable=False)
    product_id = Column(UUIDType, ForeignKey("products.id"), nullable=False)
    # Set only when the deal closed over Instagram DM — the phone-keyed
    # Customer above has no IGSID, so this is what lets renewal-failure
    # notifications go out as an Instagram DM instead of (silently failing
    # to send as) a WhatsApp message. NULL means the deal closed on
    # WhatsApp; see app/services/product_subscription.py's channel handling.
    instagram_customer_id = Column(UUIDType, ForeignKey("instagram_customers.id"), nullable=True)
    status = Column(Enum(ProductSubscriptionStatus), default=ProductSubscriptionStatus.PENDING_PAYMENT, nullable=False)
    monthly_amount = Column(Float, nullable=False)
    tunzaa_transaction_id = Column(String, nullable=True)
    next_billing_at = Column(DateTime, nullable=True)
    # 0-10: counts consecutive renewal-charge failures. Hitting
    # product_subscription.MAX_RENEWAL_RETRIES triggers CANCELLED.
    retry_count = Column(Integer, default=0, nullable=False)
    # Set once when status first transitions to PAST_DUE (not touched by
    # later retries within the same past-due streak); cleared back to NULL
    # on a successful renewal. app/services/subscription_access.py denies
    # AI access once this has been set for >= 7 days — see
    # PAST_DUE_ACCESS_GRACE_DAYS.
    past_due_since = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order", back_populates="subscription")
    customer = relationship("Customer")
    product = relationship("Product")
    instagram_customer = relationship("InstagramCustomer")


class ConversationContext(Base):
    __tablename__ = "conversation_contexts"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    customer_id = Column(UUIDType, ForeignKey("customers.id"), unique=True)
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
