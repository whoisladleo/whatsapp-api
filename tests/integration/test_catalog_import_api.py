import httpx
import pytest
import respx

from app.core.config import settings
from app.db.models import Product

IMPORT_URL = "/api/v1/products/import/instagram-shop"
PRODUCTS_URL_RE = r"https://graph\.facebook\.com/.+/merchant_cat_1/products"


@pytest.fixture(autouse=True)
def _catalog_token(monkeypatch):
    # conftest nulls CATALOG_ACCESS_TOKEN (no real Meta in tests) — most
    # import tests need it present; the 503 test below overrides to None.
    monkeypatch.setattr(settings, "CATALOG_ACCESS_TOKEN", "test-catalog-tok")


def _item(item_id, title, price="50000.00 TZS", availability="in stock", **overrides):
    item = {
        "id": item_id,
        "title": title,
        "description": f"Desc for {title}",
        "price": price,
        "availability": availability,
        "image_url": f"https://cdn.meta.com/{item_id}.jpg",
        "link": "https://shop.example.com",
        "brand": "Test Shop",
    }
    item.update(overrides)
    return item


def test_import_requires_auth(client):
    resp = client.post(IMPORT_URL, json={"catalog_id": "merchant_cat_1"})

    assert resp.status_code in (401, 403)


def test_import_blank_catalog_id_is_422(client, auth_user):
    _, headers = auth_user

    resp = client.post(IMPORT_URL, json={"catalog_id": "  "}, headers=headers)

    assert resp.status_code == 422


def test_import_without_catalog_token_is_503(client, auth_user, monkeypatch):
    _, headers = auth_user
    monkeypatch.setattr(settings, "CATALOG_ACCESS_TOKEN", None)

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 503


@respx.mock
def test_import_creates_products_across_pages(client, db_session, auth_user):
    user, headers = auth_user
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [_item("111", "Kettle"), _item("222", "Toaster")],
                    "paging": {"cursors": {"after": "cursor_1"}},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [
                        _item("333", "Blender", availability="out of stock"),
                    ],
                },
            ),
        ]
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_items"] == 3
    assert stats["imported_count"] == 3
    assert stats["skipped_count"] == 0
    assert stats["truncated"] is False

    rows = (
        db_session.query(Product)
        .filter(Product.merchant_id == user.id)
        .order_by(Product.name)
        .all()
    )
    assert [r.name for r in rows] == ["Blender", "Kettle", "Toaster"]
    kettle = next(r for r in rows if r.name == "Kettle")
    assert kettle.max_price == 50000.0
    assert kettle.min_price == 50000.0
    assert kettle.quantity == 1
    assert kettle.is_active is True
    assert kettle.sku == "igshop:111"
    assert kettle.image_url == "https://cdn.meta.com/111.jpg"
    assert kettle.slug == "kettle"
    blender = next(r for r in rows if r.name == "Blender")
    assert blender.quantity == 0
    assert blender.is_active is False


@respx.mock
def test_import_drafts_unmappable_and_skips_existing_without_touching_them(
    client, db_session, auth_user
):
    user, headers = auth_user
    existing = Product(
        name="kettle", slug="kettle", max_price=60000.0, min_price=30000.0,
        quantity=5, sku="SHOP-K1", merchant_id=user.id,
    )
    db_session.add(existing)
    db_session.commit()
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _item("111", "Kettle"),
                    _item("222", "USD Lamp", price="100.00 USD"),
                    _item("333", "Priceless Vase", price="free"),
                    _item("444", "", price="20000.00 TZS"),
                    _item("555", "Radio"),
                ]
            },
        )
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_items"] == 5
    assert stats["imported_count"] == 1
    assert stats["draft_count"] == 2
    assert stats["skipped_count"] == 2
    reasons = {s["name"]: s["reason"] for s in stats["skipped"]}
    assert "already exists" in reasons["Kettle"]
    assert "not TZS" in reasons["USD Lamp"]
    assert "Priceless Vase" in stats["drafts"]

    vase = db_session.query(Product).filter(
        Product.merchant_id == user.id, Product.sku == "igshop:333"
    ).one()
    assert vase.name == "Priceless Vase"
    assert vase.max_price == 0.0
    assert vase.min_price == 0.0
    assert vase.quantity == 0
    assert vase.is_active is False
    assert vase.is_draft is True
    untitled = db_session.query(Product).filter(
        Product.merchant_id == user.id, Product.sku == "igshop:444"
    ).one()
    assert untitled.name == "Untitled import 444"
    assert untitled.is_draft is True

    # The name-matched row keeps its merchant-tuned floor and stock.
    db_session.refresh(existing)
    assert existing.min_price == 30000.0
    assert existing.quantity == 5
    assert existing.sku == "SHOP-K1"
    assert (
        db_session.query(Product).filter(Product.merchant_id == user.id).count()
    ) == 4


@respx.mock
def test_reimport_matches_by_sku_instead_of_duplicating(client, db_session, auth_user):
    user, headers = auth_user
    route = respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"data": [_item("111", "Kettle")]}
        )
    )

    first = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )
    # Merchant renames the row — a name match would no longer catch it.
    row = db_session.query(Product).filter(Product.merchant_id == user.id).one()
    row.name = "My Kettle"
    db_session.commit()
    second = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert first.json()["imported_count"] == 1
    assert second.json()["imported_count"] == 0
    assert second.json()["skipped_count"] == 1
    assert second.json()["skipped"][0]["reason"] == "already imported"
    assert (
        db_session.query(Product).filter(Product.merchant_id == user.id).count()
    ) == 1
    assert route.call_count == 2


@respx.mock
def test_reimport_skips_existing_draft_without_duplicating(
    client, db_session, auth_user
):
    user, headers = auth_user
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"data": [_item("333", "Priceless Vase", price="free")]}
        )
    )

    first = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )
    second = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert first.json()["draft_count"] == 1
    assert second.json()["draft_count"] == 0
    assert second.json()["skipped_count"] == 1
    assert second.json()["skipped"][0]["reason"] == "already imported"
    assert (
        db_session.query(Product).filter(Product.merchant_id == user.id).count()
    ) == 1


@respx.mock
def test_import_skips_in_batch_duplicates(client, db_session, auth_user):
    user, headers = auth_user
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _item("111", "Kettle"),
                    _item("111", "Kettle"),
                    _item("222", "kettle"),
                ]
            },
        )
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["imported_count"] == 1
    assert stats["skipped_count"] == 2
    assert (
        db_session.query(Product).filter(Product.merchant_id == user.id).count()
    ) == 1


@respx.mock
def test_import_empty_page_with_cursor_terminates(client, auth_user):
    _, headers = auth_user
    route = respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [],
                "paging": {"cursors": {"after": "cursor_forever"}},
            },
        )
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["total_items"] == 0
    assert resp.json()["imported_count"] == 0
    # Exactly one fetch — no cursor chase.
    assert route.call_count == 1


@respx.mock
def test_import_non_list_data_is_502(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(200, json={"data": {"not": "a list"}})
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 502


@respx.mock
def test_import_truncates_past_max_items(client, db_session, auth_user, monkeypatch):
    user, headers = auth_user
    monkeypatch.setattr("app.api.v1.products.IMPORT_MAX_ITEMS", 2)
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [_item("111", "Kettle"), _item("222", "Toaster")],
                    "paging": {"cursors": {"after": "cursor_1"}},
                },
            ),
            httpx.Response(200, json={"data": [_item("333", "Blender")]}),
        ]
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["truncated"] is True
    assert stats["total_items"] == 2
    assert stats["imported_count"] == 2
    assert (
        db_session.query(Product).filter(Product.merchant_id == user.id).count()
    ) == 2


@respx.mock
def test_import_trims_overshoot_page(client, db_session, auth_user, monkeypatch):
    user, headers = auth_user
    monkeypatch.setattr("app.api.v1.products.IMPORT_MAX_ITEMS", 2)
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _item("111", "Kettle"),
                    _item("222", "Toaster"),
                    _item("333", "Blender"),
                ]
            },
        )
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["truncated"] is True
    assert stats["total_items"] == 2
    assert stats["imported_count"] == 2


@respx.mock
def test_import_meta_permission_error_is_502(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "(#200) Permissions error"}}
        )
    )

    resp = client.post(
        IMPORT_URL, json={"catalog_id": "merchant_cat_1"}, headers=headers
    )

    assert resp.status_code == 502
    assert "Permissions error" in resp.json()["detail"]
