import io
import uuid

import pytest

from app.core.security import create_access_token
from app.db.models import KnowledgeBase, User

BASE = "/api/v1/knowledge"


@pytest.fixture
def other_user_headers(db_session):
    other = User(username="255700000050")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def _upload(client, headers, name="Shipping FAQ", slug="shipping-faq", content=b"# FAQ\nWe ship in 2 days."):
    files = {"file": ("kb.md", io.BytesIO(content), "text/markdown")}
    data = {"name": name, "slug": slug}
    return client.post(BASE, data=data, files=files, headers=headers)


def test_create_knowledge_base(client, auth_user):
    _, headers = auth_user

    resp = _upload(client, headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "shipping-faq"
    assert "We ship in 2 days" in body["content"]


def test_create_knowledge_base_requires_auth(client):
    files = {"file": ("kb.md", io.BytesIO(b"content"), "text/markdown")}
    resp = client.post(BASE, data={"name": "x", "slug": "x"}, files=files)

    assert resp.status_code in (401, 403)


def test_create_knowledge_base_duplicate_slug_rejected(client, auth_user):
    _, headers = auth_user
    _upload(client, headers)

    resp = _upload(client, headers)

    assert resp.status_code == 400


def test_create_knowledge_base_rejects_non_utf8_file(client, auth_user):
    _, headers = auth_user
    files = {"file": ("kb.md", io.BytesIO(b"\xff\xfe\x00\x01"), "text/markdown")}

    resp = client.post(BASE, data={"name": "Bad", "slug": "bad"}, files=files, headers=headers)

    assert resp.status_code == 400


def test_list_knowledge_bases_scoped_to_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    _upload(client, headers, name="Mine", slug="mine")
    _upload(client, other_user_headers, name="Theirs", slug="theirs")

    resp = client.get(BASE, headers=headers)

    assert resp.status_code == 200
    slugs = [kb["slug"] for kb in resp.json()]
    assert slugs == ["mine"]


def test_get_knowledge_base(client, auth_user):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.get(f"{BASE}/{created['id']}", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_knowledge_base_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.get(f"{BASE}/{created['id']}", headers=other_user_headers)

    assert resp.status_code == 404


def test_update_knowledge_base_name_only(client, auth_user):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", data={"name": "New Name"}, headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New Name"
    assert body["slug"] == created["slug"]


def test_update_knowledge_base_slug_only(client, auth_user):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", data={"slug": "new-slug"}, headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "new-slug"
    assert body["name"] == created["name"]


def test_update_knowledge_base_rejects_non_utf8_file(client, auth_user):
    _, headers = auth_user
    created = _upload(client, headers).json()
    files = {"file": ("kb2.md", io.BytesIO(b"\xff\xfe\x00\x01"), "text/markdown")}

    resp = client.put(f"{BASE}/{created['id']}", files=files, headers=headers)

    assert resp.status_code == 400


def test_update_knowledge_base_content(client, auth_user):
    _, headers = auth_user
    created = _upload(client, headers).json()
    files = {"file": ("kb2.md", io.BytesIO(b"Updated content"), "text/markdown")}

    resp = client.put(f"{BASE}/{created['id']}", files=files, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["content"] == "Updated content"


def test_update_knowledge_base_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", data={"name": "Hijacked"}, headers=other_user_headers
    )

    assert resp.status_code == 404


def test_delete_knowledge_base(client, auth_user, db_session):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=headers)

    assert resp.status_code == 200
    assert db_session.query(KnowledgeBase).filter(
        KnowledgeBase.id == uuid.UUID(created["id"])
    ).first() is None


def test_delete_knowledge_base_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = _upload(client, headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=other_user_headers)

    assert resp.status_code == 404
