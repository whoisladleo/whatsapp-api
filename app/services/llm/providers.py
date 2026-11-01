import logging
from typing import Optional

import anthropic
import httpx
import openai

logger = logging.getLogger("app.services.llm")


class LLMLengthCutoff(Exception):
    """The model stopped for length (finish_reason=length / stop_reason=max_tokens).

    Partial text is withheld, never returned — a reply cut mid-sentence must
    never reach the buyer as if complete (#103)."""


class LLMUnavailable(Exception):
    """
    A transient LLM failure (connection error, 429, 5xx) that survived
    the SDK's own retries, raised only when the caller asked to retry
    (retry_on_outage=True). Raised before any order, send or state change
    for this turn, so the caller can safely re-run the whole turn (#38).
    """


def _is_transient_llm_error(e: Exception) -> bool:
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError)):
        return True
    if isinstance(e, (anthropic.APIStatusError, openai.APIStatusError)):
        return e.status_code == 429 or e.status_code >= 500
    return False


async def _complete_with_retry(provider, **kwargs) -> str:
    """
    One inline retry when the model returns empty content — a blank
    reply would otherwise end the conversation in silence (#101) — and
    one retry with a doubled cap when it stops for length, since the
    reasoning tokens share the same budget (#103). A length stop that
    survives the bigger cap raises LLMLengthCutoff for the caller to
    hand off; other exceptions propagate to the caller's outage
    handling unchanged.
    """
    try:
        text = await provider.complete(**kwargs)
    except LLMLengthCutoff:
        bigger = {**kwargs, "max_tokens": kwargs.get("max_tokens", 500) * 2}
        logger.warning("LLM reply cut off by max_tokens; retrying once with a doubled cap.")
        text = await provider.complete(**bigger)
    if not (text or "").strip():
        logger.warning("LLM returned empty content; retrying once.")
        text = await provider.complete(**kwargs)
    return text


def _no_keepalive_http_client() -> httpx.AsyncClient:
    """
    Shared HTTP client for both model SDKs with connection reuse
    disabled (#121). Pooled connections bind to the loop that created
    them, and every worker task runs inside its own asyncio.run — so a
    pooled connection outlives its loop and the next task's reuse dies
    with "Event loop is closed" (measured alternating 5/10 failures on
    rapid sequential calls). With keep-alive off every call opens a
    fresh connection: correct at ~100-300ms per call, dwarfed by
    inference time. Timeout mirrors the SDK defaults (600s) so slow
    generations behave exactly as before.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(600.0),
        limits=httpx.Limits(keepalive_expiry=0.0),
    )


class LLMProvider:
    """
    One model backend behind LLMService. complete() takes the same shape
    every caller already builds (a system instruction, a user-messages
    list, a max_tokens cap) and returns the reply as plain text.
    """

    name = ""
    model_id = ""

    def __init__(self, client=None):
        self.client = client

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        raise NotImplementedError


class ClaudeProvider(LLMProvider):
    """Existing behavior, unchanged: Anthropic messages API, same call args."""

    name = "claude"
    model_id = "claude-sonnet-5"

    def __init__(self, api_key: Optional[str]):
        super().__init__(
            client=anthropic.AsyncAnthropic(api_key=api_key, http_client=_no_keepalive_http_client())
            if api_key
            else None
        )

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        response = await self.client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        # Reasoning shares the max_tokens budget, so a length stop can
        # leave only a fragment of visible text — fail rather than send
        # it as if complete (#103).
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMLengthCutoff(f"Claude stop_reason=max_tokens at cap {max_tokens}")
        # This model returns extended-thinking content blocks before the
        # text block non-deterministically (confirmed live — the same
        # prompt sometimes returns [ThinkingBlock, TextBlock] and
        # sometimes just [TextBlock]), so content[0] isn't reliably the
        # reply — filter for the actual text block(s) instead.
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


class MuseProvider(LLMProvider):
    """Meta's Muse API, reached through the OpenAI-compatible SDK."""

    name = "muse"
    model_id = "muse-spark-1.3"
    base_url = "https://api.meta.ai/v1"

    def __init__(self, api_key: Optional[str]):
        super().__init__(
            client=openai.AsyncOpenAI(
                api_key=api_key, base_url=self.base_url, http_client=_no_keepalive_http_client()
            )
            if api_key
            else None
        )

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        response = await self.client.chat.completions.create(
            model=self.model_id,
            max_tokens=max_tokens,
            # Negotiation replies don't need deep reasoning; at the default
            # effort the model spends the whole budget on reasoning tokens
            # and returns content=None with finish_reason=length (#49).
            reasoning_effort="minimal",
            messages=[{"role": "system", "content": system}, *messages],
        )
        choice = response.choices[0]
        # Even at minimal reasoning effort the reasoning tokens share the
        # max_tokens budget, so a length stop can leave a mid-sentence
        # fragment — fail rather than send it as if complete (#103).
        if choice.finish_reason == "length":
            raise LLMLengthCutoff(f"Muse finish_reason=length at cap {max_tokens}")
        content = choice.message.content or ""
        if not content:
            logger.warning(
                "Muse returned empty content (finish_reason=%s)",
                choice.finish_reason,
            )
        return content
