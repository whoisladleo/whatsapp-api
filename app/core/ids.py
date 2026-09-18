import uuid
from typing import Optional, Union


def coerce_uuid(value: Union[str, uuid.UUID, None]) -> Optional[uuid.UUID]:
    """
    Celery task args arrive as strings (task_serializer="json" can't encode
    a raw uuid.UUID — see app/core/celery_app.py), but test suites call task
    bodies directly with real model.id values (already uuid.UUID). Accepts
    either and returns None for anything that isn't a valid UUID, rather
    than raising, so callers can treat a malformed/missing id as "not found"
    instead of a 500.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
