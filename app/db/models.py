import enum
import uuid
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean, Float, Enum, Index, UniqueConstraint, Uuid, text
from sqlalchemy.orm import relationship, DeclarativeBase

from app.db.encrypted import EncryptedString
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

class AmaraEventKind(enum.Enum):
    REPLY = "REPLY"  # billable automated send to a buyer (#41)
    QUOTE = "QUOTE"
    HANDOFF = "HANDOFF"
    PAYMENT = "PAYMENT"
    USAGE_WARNING = "USAGE_WARNING"
    LIMIT_BLOCKED = "LIMIT_BLOCKED"  # first Amara reply refused this period (#90)
    SUBSCRIPTION_NUDGE = "SUBSCRIPTION_NUDGE"  # platform reminder to an unpaid merchant (not billable)

class SupportAccessKind(enum.Enum):
    START = "START"  # operator minted an impersonation token
    END = "END"  # operator exited (or the token expired server-side)
    WRITE = "WRITE"  # one impersonated mutating API call (method+path only)
    TRIAL_GRANTED = "TRIAL_GRANTED"  # operator set/changed a trial window (detail: until)
    TRIAL_REVOKED = "TRIAL_REVOKED"  # operator cleared a trial early

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
    # Explicit merchant-alert override, set in dashboard settings. When set,
    # handoff and usage alerts go here instead of `username` (OTP number) —
    # see crud_user.resolve_handoff_number. No uniqueness: two merchants may
    # share an ops phone, and this is an alert target, not identity.
    handoff_phone_number = Column(String, nullable=True)
    # Operator-granted free trial (demo/trial access): while this is in the
    # future, is_merchant_allowed_ai passes without a paid subscription.
    # NULL = no trial. Expiry is enforced by the daily sweep re-syncing
    # lapsed trials — access reads this live, but ai_enabled flips on sync.
    trial_ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("WhatsAppAccount", back_populates="owner")
    instagram_accounts = relationship("InstagramAccount", back_populates="owner")
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner")
    products = relationship("Product", back_populates="merchant")


class RefreshToken(Base):
    """
    Server-side record for one rotating refresh bearer. Only the sha256 of
    the opaque token is stored (never the bearer itself); POST /auth/refresh
    revokes the presented row and mints a fresh one (single-use rotation),
    POST /auth/logout revokes it. Expired/revoked rows are inert — rejected
    on sight — and left in place (no janitor; a future cleanup can drop rows
    past expiry once volume justifies it).
    """
    __tablename__ = "refresh_tokens"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUIDType, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    # Fernet ciphertext at rest, plaintext in Python (see app/db/encrypted.py).
    access_token = Column(EncryptedString, nullable=False)
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
    # Fernet ciphertext at rest, plaintext in Python (see app/db/encrypted.py).
    two_step_pin = Column(EncryptedString, nullable=True)
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
    # Fernet ciphertext at rest, plaintext in Python (see app/db/encrypted.py).
    two_step_pin = Column(EncryptedString, nullable=True)
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
    # Refund state after a PROVISIONING_FAILED (#110): "MANUAL" means a
    # charge is owed back and support reverses it by hand (Tunzaa
    # dashboard) — refunds are never attempted automatically. A support
    # action can flip MANUAL to "SUCCEEDED" later. NULL means nothing was
    # charged, or the row predates this. The dashboard shows the
    # contact-support copy for anything but SUCCEEDED.
    refund_status = Column(String, nullable=True)
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
    attachments = Column(JSON, nullable=True)  # Normalized media (#87)
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
    # Fernet ciphertext at rest, plaintext in Python (see app/db/encrypted.py).
    # NULL marks a disconnected account — never an empty string, which can't
    # be matched in SQL once values are randomized ciphertext.
    access_token = Column(EncryptedString, nullable=True)
    username = Column(String, nullable=True)
    ai_enabled = Column(Boolean, default=True)
    # Mirrors WhatsAppAccount.is_platform_account: True only for Taskflow's
    # own account, where the merchant is the counterparty (onboarding pitch
    # for platform plans) rather than a customer (#136). Set once by hand,
    # never inferred from the username.
    is_platform_account = Column(Boolean, default=False, nullable=False)
    user_id = Column(UUIDType, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    # Last time access_token was (re)issued. NULL for pre-#80 rows — treated
    # as stale so the first inbound triggers one refresh, then it's stamped.
    token_refreshed_at = Column(DateTime, nullable=True)

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
    # Same one-shot classification as Customer.language, but stored
    # IG-side: pre-close there is no phone-keyed Customer row yet, and a
    # DM-only buyer may never produce one (#136).
    language = Column(String, nullable=True)
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
    # Negotiation/quote state, mirroring ConversationContext column for
    # column: failed-cycle auto-handoff and quote nudges run on IG exactly
    # as on WhatsApp (#136). NULL on pre-migration rows reads as unset.
    failed_cycles = Column(Integer, default=0)
    current_product_intent = Column(String, nullable=True)
    last_offer = Column(Float, nullable=True)
    pending_quote_price = Column(Float, nullable=True)
    pending_quote_at = Column(DateTime, nullable=True)
    quote_nudge_count = Column(Integer, default=0)
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
    attachments = Column(JSON, nullable=True)  # Normalized media (#87)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Only ever set on an inbound row, by process_inbound_dm's atomic claim
    # (app/tasks/instagram_dm_processing.py) before it does any
    # side-effecting work (LLM call, send, order creation) — guards against
    # two concurrent/duplicate executions of the same inbound message both
    # running fully, whether from webhook redelivery or Celery's own
    # at-least-once task redelivery. Confirmed live: without this, a single
    # inbound message produced two separate PAID orders. See #33.
    processed_at = Column(DateTime, nullable=True)

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
    # Shop-import draft (#74): an item that failed mapping (missing title
    # or price) persists as an inactive draft instead of being dropped.
    # Drafts are excluded from Amara's catalog, close-time validation and
    # catalog push until auto-completed by a valid merchant save (see
    # maybe_complete_draft in app/crud/product.py).
    is_draft = Column(Boolean, default=False, nullable=False)
    # Opts this product into recurring billing instead of a one-time sale —
    # see ProductSubscription / app/services/product_subscription.py. A deal
    # closed on a subscription product still rides the normal one-time
    # Order/_dispatch_payment flow for its first charge; this flag is what
    # tells _close_deal/_close_instagram_deal to also create the
    # ProductSubscription that drives billing from month 2 onward.
    is_subscription = Column(Boolean, default=False, nullable=False)
    # A platform plan (Standard, Pro, …): one of the SKUs that gates Amara
    # access and sets the monthly reply meter (#90). Admin-managed only —
    # the flag and the limit are enforced in the product endpoints, not here.
    is_platform_plan = Column(Boolean, default=False, nullable=False)
    monthly_reply_limit = Column(Integer, nullable=True)
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
    # One entry per WhatsApp account this phone has chatted with (see
    # ConversationContext) — a list, not a scalar, since the same phone
    # number can hold separate state with several shops at once.
    conversations = relationship("ConversationContext", back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    # One PENDING order per customer+product+payment-type, enforced by the
    # database — the reclose guard's read-then-write check can't see a
    # concurrent close, and sandbox data showed the same-second double-mint
    # that results in (#114). Deliberately channel-blind: Pay Now orders
    # carry no channel at all, and its own reuse query already treats a
    # chat PENDING order as reusable, so a channel-scoped key would leave
    # the overlap unprotected. payment_type is in the key (not the
    # predicate) so a FULL order can still sit alongside an in-flight
    # installment plan for the same deal — pinned behavior (#110). Mirrored
    # by migration e7f1a3c4d9b2 for prod (tests build the schema via
    # create_all, never migrations).
    __table_args__ = (
        Index(
            "ix_orders_one_pending_per_deal",
            "customer_id",
            "product_id",
            "payment_type",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
    )

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    customer_id = Column(UUIDType, ForeignKey("customers.id"))
    product_id = Column(UUIDType, ForeignKey("products.id"), nullable=True)
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"), nullable=True)
    # Dashboard-native purchases (Pay Now) carry no channel account, so the
    # merchant join can't attribute them — this records the buying user at
    # creation instead, and the merchant scopings OR on it (#16). Chat
    # closes leave it NULL (attributed via their account).
    buyer_user_id = Column(UUIDType, ForeignKey("users.id"), nullable=True, index=True)
    # An order originates from exactly one channel — this is set instead of
    # account_id for a deal Amara closed over Instagram DM (see
    # LLMService._close_instagram_deal), which needs a real Customer/Order
    # too but has no WhatsAppAccount to hang off (a merchant can be
    # Instagram-only). Never both set on the same order.
    instagram_account_id = Column(UUIDType, ForeignKey("instagram_accounts.id"), nullable=True)
    # Set alongside instagram_account_id (see _close_instagram_deal) — the
    # phone-keyed Customer above has no link to the InstagramCustomer that
    # actually messaged Amara, so without this, nothing can find the right
    # DM thread to send a payment confirmation/failure notice to. Mirrors
    # ProductSubscription.instagram_customer_id, which already solved this
    # same problem for renewal notices. Before this field existed,
    # _notify_customer (tunzaa_webhook.py) silently never reached an
    # Instagram customer at all — confirmed live, a real customer was never
    # told their payment succeeded.
    instagram_customer_id = Column(UUIDType, ForeignKey("instagram_customers.id"), nullable=True)
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
    # Refund state after a COMPLETED arrives with no stock to reclaim
    # (#110): "MANUAL" means a charge is owed back and support reverses it
    # by hand (Tunzaa dashboard) — refunds are never attempted
    # automatically. A support action can flip MANUAL to "SUCCEEDED"
    # later. NULL means nothing is owed, or the row predates this. Same
    # convention as NumberSubscription.refund_status.
    refund_status = Column(String, nullable=True)
    reserved_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Touched by every payment dispatch — the resend path throttles repeat
    # prompts off this (see LLMService._resend_payment_for_order, #113).
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    installments = relationship("Installment", back_populates="order")
    product = relationship("Product")
    account = relationship("WhatsAppAccount")
    instagram_account = relationship("InstagramAccount")
    instagram_customer = relationship("InstagramCustomer")
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

    # One row per (customer, WhatsApp account): handoff, quote-nudge, and
    # negotiation state must not leak across shops that share a customer
    # phone number — a handoff at shop A used to silence the bot at shop B,
    # and chatting at B cleared A's pending quote. account_id is NULL only
    # for pre-scoping legacy rows, which the first account to see them
    # adopts (see crud.message.get_or_create_context); Postgres and SQLite
    # both treat NULLs as distinct in a unique constraint, so legacy rows
    # never collide with each other or with scoped rows.
    __table_args__ = (
        UniqueConstraint("customer_id", "account_id", name="uq_conversation_contexts_customer_account"),
    )

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    customer_id = Column(UUIDType, ForeignKey("customers.id"))
    account_id = Column(UUIDType, ForeignKey("whatsapp_accounts.id"), nullable=True, index=True)
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

    customer = relationship("Customer", back_populates="conversations")
    account = relationship("WhatsAppAccount")


class AmaraEvent(Base):
    """
    Append-only ledger of what Amara did for a merchant (#41). REPLY rows
    are the billing record for the plan's monthly reply limit; the rest
    feed GET /stats. Account ids and product name are stored without FKs on
    purpose: WhatsApp accounts and products can be hard-deleted, and a
    billing record must outlive them.
    """
    __tablename__ = "amara_events"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    merchant_id = Column(UUIDType, ForeignKey("users.id"), nullable=False)
    kind = Column(Enum(AmaraEventKind), nullable=False)
    channel = Column(String, nullable=True)  # whatsapp, instagram_dm, instagram_comment
    account_id = Column(UUIDType, nullable=True)
    instagram_account_id = Column(UUIDType, nullable=True)
    product_name = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    detail = Column(String, nullable=True)  # e.g. the USAGE_WARNING threshold
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_amara_events_merchant_kind_created", "merchant_id", "kind", "created_at"),)

class SupportAccessEvent(Base):
    """
    Append-only audit ledger for support impersonation (#75): who acted as
    whom, when, and what they touched. Ids are stored WITHOUT FKs on
    purpose (same rationale as AmaraEvent): users can be hard-deleted, and
    an audit record must outlive them. Labels snapshot the human-readable
    identity at write time for the same reason. No request bodies, ever.
    """
    __tablename__ = "support_access_events"

    id = Column(UUIDType, primary_key=True, default=uuid.uuid4, index=True)
    operator_id = Column(UUIDType, nullable=False)
    operator_label = Column(String, nullable=True)  # verified phone or @handle
    target_id = Column(UUIDType, nullable=False)
    target_label = Column(String, nullable=True)
    kind = Column(Enum(SupportAccessKind), nullable=False)
    method = Column(String, nullable=True)  # WRITE rows only (POST/PUT/...)
    path = Column(String, nullable=True)  # WRITE rows only, no query string
    detail = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_support_access_target_created", "target_id", "created_at"),
        Index("ix_support_access_operator_created", "operator_id", "created_at"),
    )
