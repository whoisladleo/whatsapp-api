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

class ConversationState(enum.Enum):
    NEGOTIATING = "NEGOTIATING"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    MANAGEMENT = "MANAGEMENT"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    otp_code = Column(String, nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)
    business_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("WhatsAppAccount", back_populates="owner")
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
    ai_enabled = Column(Boolean, default=False)
    system_prompt = Column(String, nullable=True)
    kyc_status = Column(Enum(KYCStatus), default=KYCStatus.DRAFT)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="accounts")
    media = relationship("MediaEntry", back_populates="account")
    messages = relationship("Message", back_populates="account")


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


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    max_price = Column(Float, nullable=False)
    min_price = Column(Float, nullable=False)
    quantity = Column(Integer, default=0)
    merchant_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    merchant = relationship("User", back_populates="products")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    delivery_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    orders = relationship("Order", back_populates="customer")
    conversation = relationship("ConversationContext", back_populates="customer", uselist=False)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    agreed_price = Column(Float, nullable=True)
    tunzaa_link = Column(String, nullable=True)
    reserved_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    installments = relationship("Installment", back_populates="order")


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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="conversation")
