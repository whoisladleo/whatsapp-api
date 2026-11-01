import io
import uuid

import pytest

from app.db.models import Product, User
from app.core.security import create_access_token

BASE = "/api/v1/products"


@pytest.fixture
def other_user_headers(db_session):
    other = User(username="255700000030")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def _product_payload(**overrides):
    payload = {
        "name": "Shoes",
        "max_price": 50000,
        "min_price": 40000,
        "quantity": 10,
    }
    payload.update(overrides)
    return payload


def test_create_product(client, auth_user):
    _, headers = auth_user

    resp = client.post(BASE, json=_product_payload(), headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Shoes"
    assert body["slug"] == "shoes"
    assert body["merchant_id"] == str(auth_user[0].id)


def test_create_product_requires_auth(client):
    resp = client.post(BASE, json=_product_payload())

    assert resp.status_code in (401, 403)


def test_list_products_scoped_to_current_user(client, auth_user, other_user_headers, db_session):
    user, headers = auth_user
    client.post(BASE, json=_product_payload(name="Shoes"), headers=headers)
    client.post(BASE, json=_product_payload(name="Hats"), headers=other_user_headers)

    resp = client.get(BASE, headers=headers)

    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert names == ["Shoes"]


def test_update_product(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"quantity": 99}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["quantity"] == 99


def _draft_row(db_session, user_id):
    p = Product(
        slug="draft-vase", name="Untitled import 333", max_price=0.0, min_price=0.0,
        quantity=0, is_active=False, is_draft=True, merchant_id=user_id,
        sku="igshop:333",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_update_product_completes_draft_on_valid_save(client, auth_user, db_session):
    user, headers = auth_user
    draft = _draft_row(db_session, user.id)

    resp = client.put(
        f"{BASE}/{draft.id}",
        json={"name": "Priceless Vase", "max_price": 80000, "min_price": 70000},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_draft"] is False
    assert body["is_active"] is True
    assert body["quantity"] == 0


def test_update_product_partial_save_keeps_draft(client, auth_user, db_session):
    user, headers = auth_user
    draft = _draft_row(db_session, user.id)

    resp = client.put(
        f"{BASE}/{draft.id}",
        json={"max_price": 80000, "min_price": 70000},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["is_draft"] is True
    assert resp.json()["is_active"] is False


def test_update_product_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"quantity": 1}, headers=other_user_headers
    )

    assert resp.status_code == 404


def test_delete_product(client, auth_user, db_session):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=headers)

    assert resp.status_code == 204
    assert db_session.query(Product).filter(Product.id == uuid.UUID(created["id"])).first() is None


def test_delete_product_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=other_user_headers)

    assert resp.status_code == 404


def test_slug_collision_gets_suffixed(client, auth_user):
    _, headers = auth_user
    first = client.post(BASE, json=_product_payload(name="Shoes"), headers=headers).json()
    second = client.post(BASE, json=_product_payload(name="Shoes"), headers=headers).json()

    assert first["slug"] == "shoes"
    assert second["slug"] != "shoes"
    assert second["slug"].startswith("shoes-")


def test_bulk_template_download(client, auth_user):
    _, headers = auth_user

    resp = client.get(f"{BASE}/bulk/template", headers=headers)

    assert resp.status_code == 200
    assert "name,description" in resp.text


def test_bulk_upload_dry_run_does_not_persist(client, auth_user, db_session):
    _, headers = auth_user
    csv_content = "name,max_price,min_price,quantity,sku\nShirt,20000,15000,3,SKU-1\n"
    files = {"file": ("products.csv", io.BytesIO(csv_content.encode()), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["requires_confirmation"] is True
    assert body["new_products_count"] == 1
    assert db_session.query(Product).count() == 0


def test_bulk_upload_confirm_persists(client, auth_user, db_session):
    _, headers = auth_user
    csv_content = "name,max_price,min_price,quantity,sku\nShirt,20000,15000,3,SKU-1\n"
    files = {"file": ("products.csv", io.BytesIO(csv_content.encode()), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload?confirm=true", files=files, headers=headers)

    assert resp.status_code == 200
    assert db_session.query(Product).filter(Product.sku == "SKU-1").count() == 1


def test_bulk_upload_rejects_non_csv(client, auth_user):
    _, headers = auth_user
    files = {"file": ("products.txt", io.BytesIO(b"not a csv"), "text/plain")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 400


def test_bulk_upload_rejects_invalid_numeric_data(client, auth_user):
    _, headers = auth_user
    csv_content = "name,max_price,min_price,quantity,sku\nShirt,not-a-number,15000,3,SKU-2\n"
    files = {"file": ("products.csv", io.BytesIO(csv_content.encode()), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 400
    assert "numeric" in resp.json()["detail"].lower()


def test_bulk_upload_rejects_missing_name_column(client, auth_user):
    _, headers = auth_user
    csv_content = "sku,quantity\nSKU-9,3\n"
    files = {"file": ("products.csv", io.BytesIO(csv_content.encode()), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 400
    assert "name" in resp.json()["detail"].lower()


def test_bulk_upload_rejects_empty_file(client, auth_user):
    _, headers = auth_user
    files = {"file": ("products.csv", io.BytesIO(b""), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 400


def test_bulk_upload_rejects_non_utf8(client, auth_user):
    _, headers = auth_user
    files = {
        "file": ("products.csv", io.BytesIO(b"name,sku\n\xff\xfe\x00bad\n"), "text/csv")
    }

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 400
    assert "utf-8" in resp.json()["detail"].lower()


def test_bulk_upload_rejects_oversize_file(client, auth_user):
    _, headers = auth_user
    big = b"name,sku\n" + b"Shirt,SKU-BIG\n" * 500_000  # ~7MB, over the 5MB cap
    files = {"file": ("products.csv", io.BytesIO(big), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload", files=files, headers=headers)

    assert resp.status_code == 413


def test_bulk_upload_rejects_missing_filename(db_session, auth_user):
    # Multipart without a filename: UploadFile.filename is None — must be
    # a 400, not an AttributeError 500.
    from fastapi import HTTPException, UploadFile

    from app.api.v1.products import upload_csv_bulk

    user, _ = auth_user
    upload = UploadFile(file=io.BytesIO(b"name,sku\nShirt,SKU-1\n"), filename=None)

    with pytest.raises(HTTPException) as exc_info:
        upload_csv_bulk(confirm=False, file=upload, db=db_session, current_user=user)

    assert exc_info.value.status_code == 400


def test_bulk_upload_updates_existing_product_by_sku(client, auth_user, db_session):
    _, headers = auth_user
    first_csv = "name,max_price,min_price,quantity,sku\nShirt,20000,15000,3,SKU-3\n"
    client.post(
        f"{BASE}/bulk/upload?confirm=true",
        files={"file": ("p.csv", io.BytesIO(first_csv.encode()), "text/csv")},
        headers=headers,
    )

    updated_csv = "name,max_price,min_price,quantity,sku\nShirt V2,25000,18000,7,SKU-3\n"
    resp = client.post(
        f"{BASE}/bulk/upload?confirm=true",
        files={"file": ("p2.csv", io.BytesIO(updated_csv.encode()), "text/csv")},
        headers=headers,
    )

    assert resp.status_code == 200
    stats = resp.json()["stats"]
    assert stats["duplicate_products_count"] == 1
    assert stats["duplicate_skus"] == ["SKU-3"]
    product = db_session.query(Product).filter(Product.sku == "SKU-3").first()
    assert product.quantity == 7
    assert product.name == "Shirt V2"


def test_upload_product_image_local_fallback(client, auth_user, db_session):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()
    files = {"file": ("photo.png", io.BytesIO(b"fakeimagebytes"), "image/png")}

    resp = client.post(
        f"{BASE}/{created['id']}/image", files=files, headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["image_url"].startswith("/static/uploads/products/")

    import os
    saved_path = body["image_url"].lstrip("/")
    assert os.path.exists(saved_path)
    os.remove(saved_path)


def test_upload_product_image_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()
    files = {"file": ("photo.png", io.BytesIO(b"fakeimagebytes"), "image/png")}

    resp = client.post(
        f"{BASE}/{created['id']}/image", files=files, headers=other_user_headers
    )

    assert resp.status_code == 404


def test_upload_product_image_r2_path_uses_mocked_client(client, auth_user, monkeypatch):
    """
    Exercises the R2 branch of upload_product_image with a fully mocked boto3
    client — must never construct a real boto3.client() against production R2.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "R2_ACCOUNT_ID", "test-account")
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(settings, "R2_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(settings, "R2_PUBLIC_URL", "https://cdn.example.test")

    uploaded = {}

    class FakeS3Client:
        def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
            uploaded["bucket"] = bucket
            uploaded["key"] = key
            uploaded["content_type"] = (ExtraArgs or {}).get("ContentType")

    monkeypatch.setattr("boto3.client", lambda *a, **kw: FakeS3Client())

    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()
    files = {"file": ("photo.png", io.BytesIO(b"fakeimagebytes"), "image/png")}

    resp = client.post(f"{BASE}/{created['id']}/image", files=files, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["image_url"].startswith("https://cdn.example.test/")
    assert uploaded["bucket"] == "test-bucket"
    assert uploaded["content_type"] == "image/png"


def test_upload_product_image_rejects_svg(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("evil.svg", io.BytesIO(svg), "image/svg+xml")},
        headers=headers,
    )
    assert resp.status_code == 400

    # A renamed SVG with a spoofed image content type is still rejected:
    # the extension itself must be allowlisted.
    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("evil.svg", io.BytesIO(svg), "image/png")},
        headers=headers,
    )
    assert resp.status_code == 400


def test_upload_product_image_rejects_html(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("evil.html", io.BytesIO(b"<script>alert(1)</script>"), "text/html")},
        headers=headers,
    )
    assert resp.status_code == 400

    # An HTML payload wearing a png filename is still rejected: the
    # content type itself must be allowlisted.
    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("evil.png", io.BytesIO(b"<script>alert(1)</script>"), "text/html")},
        headers=headers,
    )
    assert resp.status_code == 400


def test_upload_product_image_rejects_missing_extension(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("noextension", io.BytesIO(b"fakeimagebytes"), "image/png")},
        headers=headers,
    )
    assert resp.status_code == 400


def test_upload_product_image_rejects_oversize(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()
    big = io.BytesIO(b"x" * (5 * 1024 * 1024 + 1))

    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("huge.png", big, "image/png")},
        headers=headers,
    )
    assert resp.status_code == 413


def test_upload_product_image_derives_extension_from_content_type(
    client, auth_user, tmp_path, monkeypatch
):
    # The local fallback writes to a relative static/ dir — keep it in tmp.
    monkeypatch.chdir(tmp_path)
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.post(
        f"{BASE}/{created['id']}/image",
        files={"file": ("photo.JPEG", io.BytesIO(b"fakeimagebytes"), "image/png")},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["image_url"].startswith("/static/uploads/products/")
    assert body["image_url"].endswith(".png")


def test_patch_stock_sets_quantity(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock", json={"quantity": 25}, headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["quantity"] == 25
    # Untouched stock field keeps its value; non-stock fields untouched too.
    assert body["is_active"] is True
    assert body["name"] == "Shoes"
    assert body["max_price"] == 50000


def test_patch_stock_toggles_is_active(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock", json={"is_active": False}, headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["quantity"] == 10


def test_patch_stock_both_at_once(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock",
        json={"quantity": 0, "is_active": False},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["quantity"] == 0
    assert body["is_active"] is False
    # Narrow write: everything else survives.
    assert body["name"] == "Shoes"
    assert body["min_price"] == 40000


def test_patch_stock_neither_field_errors(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock", json={}, headers=headers
    )

    assert resp.status_code == 422


def test_patch_stock_explicit_null_leaves_unchanged(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock",
        json={"quantity": None, "is_active": False},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["quantity"] == 10
    assert body["is_active"] is False


def test_patch_stock_all_null_errors(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock",
        json={"quantity": None, "is_active": None},
        headers=headers,
    )

    assert resp.status_code == 422


def test_patch_stock_negative_quantity_errors(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock", json={"quantity": -1}, headers=headers
    )

    assert resp.status_code == 422


def test_patch_stock_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}/stock",
        json={"quantity": 1},
        headers=other_user_headers,
    )

    assert resp.status_code == 404


def test_patch_stock_nonexistent_id(client, auth_user):
    _, headers = auth_user

    resp = client.patch(
        f"{BASE}/{uuid.uuid4()}/stock", json={"quantity": 1}, headers=headers
    )

    assert resp.status_code == 404


# --- platform plan SKUs are admin-managed (#90) ---------------------------------


def _as_operator(monkeypatch):
    from app.core.config import settings

    # auth_user's username is 255700000001 — allow-listing it makes every
    # request in the test operator-authorized.
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", "255700000001")


def test_create_plan_product_denied_for_merchant(client, auth_user):
    _, headers = auth_user

    resp = client.post(
        BASE, json=_product_payload(is_platform_plan=True, monthly_reply_limit=5000), headers=headers
    )

    assert resp.status_code == 403


def test_create_limit_alone_denied_for_merchant(client, auth_user):
    _, headers = auth_user

    resp = client.post(BASE, json=_product_payload(monthly_reply_limit=5000), headers=headers)

    assert resp.status_code == 403


def test_create_plan_product_allowed_for_operator(client, auth_user, monkeypatch, db_session):
    _as_operator(monkeypatch)
    _, headers = auth_user

    resp = client.post(
        BASE, json=_product_payload(is_platform_plan=True, monthly_reply_limit=5000), headers=headers
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["is_platform_plan"] is True
    assert body["monthly_reply_limit"] == 5000


def test_update_adding_plan_flag_denied_for_merchant(client, auth_user):
    _, headers = auth_user
    created = client.post(BASE, json=_product_payload(), headers=headers).json()

    resp = client.put(f"{BASE}/{created['id']}", json={"is_platform_plan": True}, headers=headers)

    assert resp.status_code == 403


def test_update_plan_limit_allowed_for_operator(client, auth_user, monkeypatch):
    _as_operator(monkeypatch)
    _, headers = auth_user
    created = client.post(
        BASE, json=_product_payload(is_platform_plan=True, monthly_reply_limit=1000), headers=headers
    ).json()

    resp = client.put(f"{BASE}/{created['id']}", json={"monthly_reply_limit": 9000}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["monthly_reply_limit"] == 9000


def test_delete_plan_product_denied_for_merchant(client, auth_user, db_session):
    user, headers = auth_user
    plan = Product(
        slug="plan-seeded", name="Seeded Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=user.id, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(plan)
    db_session.commit()

    resp = client.delete(f"{BASE}/{plan.id}", headers=headers)

    assert resp.status_code == 403


def test_delete_plan_product_allowed_for_operator(client, auth_user, monkeypatch, db_session):
    _as_operator(monkeypatch)
    user, headers = auth_user
    plan = Product(
        slug="plan-seeded", name="Seeded Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=user.id, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(plan)
    db_session.commit()

    resp = client.delete(f"{BASE}/{plan.id}", headers=headers)

    assert resp.status_code == 204


def test_bulk_upload_strips_plan_columns(client, auth_user, db_session):
    _, headers = auth_user
    csv_content = (
        "name,max_price,min_price,quantity,sku,is_platform_plan,monthly_reply_limit\n"
        "Shirt,20000,15000,3,SKU-1,true,5000\n"
    )
    files = {"file": ("products.csv", io.BytesIO(csv_content.encode()), "text/csv")}

    resp = client.post(f"{BASE}/bulk/upload?confirm=true", files=files, headers=headers)

    assert resp.status_code == 200
    row = db_session.query(Product).filter(Product.sku == "SKU-1").one()
    assert row.is_platform_plan is False
    assert row.monthly_reply_limit is None


# --- POST /generate (#90) ----------------------------------------------------------


def _fake_llm(monkeypatch, reply):
    class FakeProvider:
        async def complete(self, **kwargs):
            return reply

    class FakeLLMService:
        provider = FakeProvider()

    monkeypatch.setattr("app.services.llm.llm_service", FakeLLMService())


def test_generate_requires_auth(client):
    resp = client.post(f"{BASE}/generate", json={"summary": "a long enough summary here"})

    assert resp.status_code in (401, 403)


def test_generate_rejects_short_summary(client, auth_user):
    _, headers = auth_user

    resp = client.post(f"{BASE}/generate", json={"summary": "shoes"}, headers=headers)

    assert resp.status_code == 422


def test_generate_returns_drafts(client, auth_user, monkeypatch, db_session):
    _fake_llm(monkeypatch, '[{"name": "iPhone 12", "max_price": 850000, "min_price": 800000}]')
    _, headers = auth_user

    resp = client.post(
        f"{BASE}/generate", json={"summary": "I sell used iPhones from 800k"}, headers=headers
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "iPhone 12"
    assert body[0]["is_draft"] is True
    row = db_session.query(Product).filter(Product.merchant_id == auth_user[0].id).one()
    assert row.is_draft is True


def test_generate_503_without_llm_client(client, auth_user, monkeypatch):
    monkeypatch.setattr("app.services.llm.llm_service", None)
    _, headers = auth_user

    resp = client.post(
        f"{BASE}/generate", json={"summary": "I sell used iPhones from 800k"}, headers=headers
    )

    assert resp.status_code == 503


def test_generate_502_on_unusable_reply(client, auth_user, monkeypatch):
    _fake_llm(monkeypatch, "sorry, I cannot help with that")
    _, headers = auth_user

    resp = client.post(
        f"{BASE}/generate", json={"summary": "I sell used iPhones from 800k"}, headers=headers
    )

    assert resp.status_code == 502


def test_generate_429_when_daily_limit_hit(client, auth_user, monkeypatch):
    monkeypatch.setattr("app.api.v1.products.check_and_increment", lambda *a, **kw: False)
    _, headers = auth_user

    resp = client.post(
        f"{BASE}/generate", json={"summary": "I sell used iPhones from 800k"}, headers=headers
    )

    assert resp.status_code == 429
