from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, Boolean
from sqlalchemy.orm import relationship, DeclarativeBase
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    accounts = relationship("WhatsAppAccount", back_populates="owner")


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
