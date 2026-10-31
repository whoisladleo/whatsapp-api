"""Shared schema primitives."""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import PlainSerializer


def _serialize_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        # Every DateTime column in models.py defaults to datetime.utcnow,
        # so a naive value reaching a schema IS UTC — say so on the wire
        # instead of emitting an offset-less string the dashboard parses
        # as browser-local (#98).
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


UtcDateTime = Annotated[datetime, PlainSerializer(_serialize_utc, return_type=str)]
