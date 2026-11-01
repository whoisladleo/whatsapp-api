import logging
from typing import Optional

from app.core.config import settings
from app.services.llm.close import CloseMixin
from app.services.llm.notify import NotifyMixin
from app.services.llm.providers import ClaudeProvider, LLMProvider, MuseProvider
from app.services.llm.replies import ReplyMixin
from app.services.llm.replies_dm import DMReplyMixin
from app.services.llm.replies_wa import WAReplyMixin

logger = logging.getLogger("app.services.llm")


class LLMService(CloseMixin, NotifyMixin, ReplyMixin, DMReplyMixin, WAReplyMixin):
    def __init__(self, api_key: str, provider: Optional[str] = None):
        provider = provider or settings.LLM_PROVIDER
        if provider == MuseProvider.name:
            self.provider: LLMProvider = MuseProvider(api_key=api_key)
        elif provider == ClaudeProvider.name:
            self.provider = ClaudeProvider(api_key=api_key)
        else:
            logger.warning(
                "Unknown LLM_PROVIDER=%r — falling back to 'claude'", provider
            )
            self.provider = ClaudeProvider(api_key=api_key)

    @property
    def client(self):
        """The underlying SDK client (None when unconfigured)."""
        return self.provider.client

    @client.setter
    def client(self, value):
        self.provider.client = value

    @property
    def model_id(self):
        return self.provider.model_id


def _default_llm_service() -> Optional[LLMService]:
    if settings.LLM_PROVIDER == MuseProvider.name:
        return LLMService(api_key=settings.MODEL_API_KEY, provider="muse") if settings.MODEL_API_KEY else None
    return LLMService(api_key=settings.ANTHROPIC_API_KEY) if settings.ANTHROPIC_API_KEY else None


llm_service = _default_llm_service()
