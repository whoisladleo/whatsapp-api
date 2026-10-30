"""Per-request audit for impersonated sessions (#75).

Records one WRITE row per mutating API call made with an impersonated
token (method + path only — never bodies or query strings). START/END
rows are recorded by the impersonate/exit endpoints themselves, so this
middleware skips the exit path to avoid double-logging it. GETs are out
of scope by design: reads are high-volume and the START/END pair bounds
the window; writes are what can harm a merchant.

Never breaks a request: every failure path degrades to a loud log line,
mirroring usage.record_event — except here a gap is a compliance hole,
so the log is an error, not a warning.
"""

import logging
import uuid

import jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.db.models import SupportAccessKind, User
from app.db.session import get_db
from app.services.support_access import (
    IMPERSONATOR_CLAIM,
    operator_match,
    record_support_event,
    target_label,
)

logger = logging.getLogger(__name__)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _request_session(request):
    """
    A DB session resolved the same way endpoint dependencies resolve
    theirs: honors app.dependency_overrides (so tests observe audit rows
    in the test transaction) and falls back to the prod session factory.
    Caller must close the generator.
    """
    factory = request.app.dependency_overrides.get(get_db, get_db)
    gen = factory()
    try:
        yield next(gen)
    finally:
        try:
            gen.close()
        except Exception:
            pass


class SupportAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        try:
            self._audit(request, response.status_code)
        except Exception:
            logger.exception("Support audit failed and was dropped")
        return response

    def _audit(self, request, status_code: int) -> None:
        if request.method not in _WRITE_METHODS:
            return
        path = request.url.path
        if not path.startswith(settings.API_V1_STR + "/"):
            return
        if path == f"{settings.API_V1_STR}/auth/impersonate/exit":
            return
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        try:
            payload = jwt.decode(
                auth[7:], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except jwt.InvalidTokenError:
            return
        raw_operator = payload.get(IMPERSONATOR_CLAIM)
        raw_target = payload.get("sub")
        if not raw_operator or not raw_target:
            return
        try:
            operator_id = uuid.UUID(raw_operator)
            target_id = uuid.UUID(raw_target)
        except ValueError:
            return

        session_gen = _request_session(request)
        db = next(session_gen)
        try:
            operator = db.query(User).filter(User.id == operator_id).first()
            target_user = db.query(User).filter(User.id == target_id).first()
            record_support_event(
                db, operator_id, target_id, SupportAccessKind.WRITE,
                operator_label=(
                    operator_match(db, operator) if operator else None
                ),
                target_label=target_label(target_user) if target_user else None,
                method=request.method,
                path=path,
                detail=f"status={status_code}",
            )
        finally:
            try:
                session_gen.close()
            except Exception:
                pass
