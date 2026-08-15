import pytest
from sqlalchemy.orm import Session

from app.db.session import get_db


def test_get_db_yields_a_session_and_closes_it_on_teardown():
    gen = get_db()

    db = next(gen)
    assert isinstance(db, Session)

    with pytest.raises(StopIteration):
        next(gen)
