import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.core.catalog_client import (
    CATALOG_PRODUCT_FIELDS,
    CatalogError,
    CatalogMappingError,
    MetaCatalogClient,
    catalog_item_to_product_data,
    format_price_tzs,
    parse_catalog_price,
    product_to_catalog_item,
)
from app.core.config import settings
from app.db.models import Product
from app.services.llm import _negotiation_floor

CATALOG_ID = "catalog_123"
ITEMS_BATCH_URL_RE = r"https://graph\.facebook\.com/.+/catalog_123/items_batch$"
STATUS_URL_RE = r"https://graph\.facebook\.com/.+/catalog_123/check_batch_request_status"

# Fixed UUID kept free of the "45000"/"49500" substrings the floor-absence
# test scans for, so a failure can only mean a real leak.
PRODUCT_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")

BRAND = "Test Shop"


def make_product(**overrides):
    attrs = {
        "id": PRODUCT_UUID,
        "slug": "test-kettle",
        "name": "Test Kettle",
        "description": "A great kettle",
        "max_price": 50000.0,
        "min_price": 45000.0,
        "cost_price": 30000.0,
        "quantity": 10,
        "is_active": True,
        "image_url": "https://cdn.example.com/k.jpg",
    }
    attrs.update(overrides)
    return Product(**attrs)


@pytest.fixture
def client():
    return MetaCatalogClient(catalog_id=CATALOG_ID, access_token="catalog-tok")


# Mapping


def test_mapping_field_values():
    item = product_to_catalog_item(make_product(), "255700000001", BRAND)

    assert item["id"] == str(PRODUCT_UUID)
    assert item["title"] == "Test Kettle"
    assert item["description"] == "A great kettle"
    assert item["price"] == "50000.00 TZS"
    assert item["availability"] == "in stock"
    assert item["condition"] == "new"
    assert item["image"] == [{"url": "https://cdn.example.com/k.jpg"}]
    assert item["brand"] == BRAND
    assert item["link"].startswith("https://wa.me/255700000001?text=")
    assert "Test Kettle" in parse_qs(urlparse(item["link"]).query)["text"][0]
    # Old pre-E1-fix keys must be gone: Meta rejects them.
    assert "retailer_id" not in item
    assert "name" not in item
    assert "image_url" not in item


def test_mapping_price_tzs_format():
    assert format_price_tzs(50000) == "50000.00 TZS"
    assert format_price_tzs(99.9) == "99.90 TZS"
    assert format_price_tzs(0) == "0.00 TZS"


def test_mapping_description_falls_back_to_name():
    item = product_to_catalog_item(make_product(description=None), "255700000001", BRAND)

    assert item["description"] == "Test Kettle"


def test_mapping_title_truncated_to_100_chars():
    long_name = "Kettle " * 30  # 210 chars
    item = product_to_catalog_item(
        make_product(name=long_name, description=None), "255700000001", BRAND
    )

    assert len(item["title"]) == 100
    assert item["title"] == long_name[:100]
    # The description fallback keeps the full name; only title is capped.
    assert item["description"] == long_name


def test_mapping_brand_requires_merchant_name():
    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(), "255700000001", None)

    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(), "255700000001", "")

    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(), "255700000001", "   ")


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"quantity": 0}, "out of stock"),
        ({"quantity": None}, "out of stock"),
        ({"is_active": False}, "out of stock"),
        ({"quantity": 0, "is_active": False}, "out of stock"),
        ({"quantity": 5, "is_active": True}, "in stock"),
        # Legacy rows with is_active=None count as in-stock — same as C1's
        # _is_out_of_stock (`is_active is False`, not falsiness).
        ({"quantity": 5, "is_active": None}, "in stock"),
    ],
)
def test_mapping_availability_follows_sellability(overrides, expected):
    item = product_to_catalog_item(make_product(**overrides), "255700000001", BRAND)

    assert item["availability"] == expected


def test_mapping_never_includes_floor_or_cost():
    product = make_product()
    floor = _negotiation_floor(product)
    assert floor == 49501  # guard: the test below is only meaningful if set up right

    item = product_to_catalog_item(product, "255700000001", BRAND)
    dumped = json.dumps(item)

    assert "min_price" not in item
    assert "cost_price" not in item
    assert "45000" not in dumped  # min_price value
    assert "49501" not in dumped  # negotiation floor value
    assert "30000" not in dumped  # cost_price value


def test_mapping_link_normalizes_local_and_messy_numbers():
    item = product_to_catalog_item(make_product(), "0712000001", BRAND)

    assert item["link"].startswith("https://wa.me/255712000001?text=")

    item = product_to_catalog_item(make_product(), "+255 700-000-001", BRAND)

    assert item["link"].startswith("https://wa.me/255700000001?text=")


def test_mapping_link_requires_merchant_phone():
    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(), "", BRAND)

    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(), None, BRAND)


def test_mapping_relative_image_url_uses_public_base(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.example.com/")
    product = make_product(image_url="/static/uploads/products/k.jpg")

    item = product_to_catalog_item(product, "255700000001", BRAND)

    assert item["image"] == [
        {"url": "https://api.example.com/static/uploads/products/k.jpg"}
    ]


def test_mapping_relative_image_url_without_base_raises(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    product = make_product(image_url="/static/uploads/products/k.jpg")

    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(product, "255700000001", BRAND)


def test_mapping_missing_image_raises():
    with pytest.raises(CatalogMappingError):
        product_to_catalog_item(make_product(image_url=None), "255700000001", BRAND)


# Client: batch payload shape (mocked httpx via respx)


@respx.mock
async def test_upsert_items_posts_create_requests_with_allow_upsert(client):
    route = respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(200, json={"handles": ["handle_1"]})
    )
    item = {"id": "p1", "title": "Test Kettle"}

    result = await client.upsert_items([item])

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "requests": [{"method": "CREATE", "data": item}],
        "allow_upsert": True,
        "item_type": "PRODUCT_ITEM",
    }
    assert route.calls.last.request.headers["Authorization"] == "Bearer catalog-tok"
    assert result == {"handles": ["handle_1"]}


@respx.mock
async def test_upsert_items_rejects_empty_without_http_call(client):
    route = respx.post(url__regex=ITEMS_BATCH_URL_RE)

    with pytest.raises(CatalogError):
        await client.upsert_items([])

    assert not route.called


@respx.mock
async def test_upsert_items_raises_on_meta_error_payload(client):
    respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "Invalid price format", "code": 123}}
        )
    )

    with pytest.raises(CatalogError, match="Invalid price format"):
        await client.upsert_items([{"id": "p1"}])


@respx.mock
async def test_upsert_items_raises_on_http_error_status(client):
    respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Bad request"}})
    )

    with pytest.raises(CatalogError, match="HTTP 400"):
        await client.upsert_items([{"id": "p1"}])


@respx.mock
async def test_upsert_items_raises_on_transport_error(client):
    respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(CatalogError, match="request failed"):
        await client.upsert_items([{"id": "p1"}])


@respx.mock
async def test_upsert_items_raises_on_non_dict_payload(client):
    respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )

    with pytest.raises(CatalogError, match="non-JSON payload"):
        await client.upsert_items([{"id": "p1"}])


@respx.mock
async def test_upsert_items_raises_on_invalid_json(client):
    respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(200, text="not json {{{")
    )

    with pytest.raises(CatalogError, match="invalid JSON"):
        await client.upsert_items([{"id": "p1"}])


@respx.mock
async def test_delete_items_posts_delete_requests(client):
    route = respx.post(url__regex=ITEMS_BATCH_URL_RE).mock(
        return_value=httpx.Response(200, json={"handles": ["handle_2"]})
    )

    result = await client.delete_items(["p1", "p2"])

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "requests": [
            {"method": "DELETE", "data": {"id": "p1"}},
            {"method": "DELETE", "data": {"id": "p2"}},
        ],
        "allow_upsert": True,
        "item_type": "PRODUCT_ITEM",
    }
    assert result == {"handles": ["handle_2"]}


@respx.mock
async def test_delete_items_rejects_empty_without_http_call(client):
    route = respx.post(url__regex=ITEMS_BATCH_URL_RE)

    with pytest.raises(CatalogError):
        await client.delete_items([])

    assert not route.called


# Client: status polling


@respx.mock
async def test_get_batch_status_success(client):
    route = respx.get(url__regex=STATUS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "handle": "handle_1",
                        "status": "finished",
                        "errors": [],
                        "errors_total_count": 0,
                    }
                ]
            },
        )
    )

    result = await client.get_batch_status("handle_1")

    assert route.called
    assert route.calls.last.request.url.params["handle"] == "handle_1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer catalog-tok"
    assert result["data"][0]["status"] == "finished"


@respx.mock
async def test_get_batch_status_raises_on_meta_error(client):
    respx.get(url__regex=STATUS_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "Unknown handle", "code": 404}}
        )
    )

    with pytest.raises(CatalogError, match="Unknown handle"):
        await client.get_batch_status("bogus")


@respx.mock
async def test_get_batch_status_raises_on_transport_error(client):
    respx.get(url__regex=STATUS_URL_RE).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(CatalogError, match="check_batch_request_status failed"):
        await client.get_batch_status("handle_1")


@respx.mock
async def test_get_batch_status_raises_on_invalid_json(client):
    respx.get(url__regex=STATUS_URL_RE).mock(
        return_value=httpx.Response(200, text="not json {{{")
    )

    with pytest.raises(CatalogError, match="invalid JSON"):
        await client.get_batch_status("handle_1")


@respx.mock
async def test_get_batch_status_raises_on_http_error_status(client):
    respx.get(url__regex=STATUS_URL_RE).mock(
        return_value=httpx.Response(403, text="forbidden")
    )

    with pytest.raises(CatalogError, match="HTTP 403"):
        await client.get_batch_status("handle_1")


# Shop import: price parsing


def test_parse_catalog_price_valid():
    assert parse_catalog_price("50000.00 TZS") == 50000.0
    assert parse_catalog_price("45,000.50 TZS") == 45000.5
    assert parse_catalog_price("50000.00 tzs") == 50000.0


def test_parse_catalog_price_rejects_foreign_currency():
    # Importing a USD amount as unitless TZS would be a ~2500x error —
    # the same incident class as the $100,000 production quote.
    with pytest.raises(CatalogMappingError, match="not TZS"):
        parse_catalog_price("100.00 USD")


def test_parse_catalog_price_rejects_malformed():
    for bad in ["50000", "TZS", "", None, 50000, "-10.00 TZS", "abc TZS"]:
        with pytest.raises(CatalogMappingError):
            parse_catalog_price(bad)


# Shop import: item mapping


def _catalog_item(**overrides):
    item = {
        "id": "987654321",
        "title": "Leather Handbag",
        "description": "Genuine leather",
        "price": "120000.00 TZS",
        "availability": "in stock",
        "image_url": "https://cdn.meta.com/bag.jpg",
        "link": "https://shop.example.com/bag",
        "brand": "Test Shop",
    }
    item.update(overrides)
    return item


def test_catalog_item_mapping_in_stock():
    data = catalog_item_to_product_data(_catalog_item())

    assert data["name"] == "Leather Handbag"
    assert data["description"] == "Genuine leather"
    assert data["max_price"] == 120000.0
    assert data["min_price"] == 120000.0
    assert data["quantity"] == 1
    assert data["is_active"] is True
    assert data["image_url"] == "https://cdn.meta.com/bag.jpg"
    assert data["sku"] == "igshop:987654321"


def test_catalog_item_mapping_out_of_stock():
    data = catalog_item_to_product_data(_catalog_item(availability="out of stock"))

    assert data["quantity"] == 0
    assert data["is_active"] is False


def test_catalog_item_mapping_missing_optionals():
    data = catalog_item_to_product_data(
        _catalog_item(description="", image_url="", availability="available for order")
    )

    assert data["description"] is None
    assert data["image_url"] is None
    # Any availability that isn't exactly "in stock" counts as out.
    assert data["quantity"] == 0
    assert data["is_active"] is False


def test_catalog_item_mapping_rejects_missing_title_or_id():
    with pytest.raises(CatalogMappingError, match="title"):
        catalog_item_to_product_data(_catalog_item(title="  "))
    with pytest.raises(CatalogMappingError, match="id"):
        catalog_item_to_product_data(_catalog_item(id=""))
    with pytest.raises(CatalogMappingError, match="not an object"):
        catalog_item_to_product_data("not-a-dict")


def test_catalog_item_mapping_rejects_bad_price():
    with pytest.raises(CatalogMappingError, match="not TZS"):
        catalog_item_to_product_data(_catalog_item(price="50.00 USD"))
    with pytest.raises(CatalogMappingError):
        catalog_item_to_product_data(_catalog_item(price="free"))


# Shop import: product listing


PRODUCTS_URL_RE = r"https://graph\.facebook\.com/.+/catalog_123/products"


@respx.mock
async def test_list_products_success(client):
    route = respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_catalog_item()],
                "paging": {"cursors": {"after": "cursor_1"}},
            },
        )
    )

    result = await client.list_products(limit=50)

    assert result["data"][0]["title"] == "Leather Handbag"
    assert result["paging"]["cursors"]["after"] == "cursor_1"
    params = route.calls.last.request.url.params
    assert params["fields"] == CATALOG_PRODUCT_FIELDS
    assert params["limit"] == "50"
    assert "after" not in params


@respx.mock
async def test_list_products_passes_after_cursor(client):
    route = respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    await client.list_products(after="cursor_1")

    assert route.calls.last.request.url.params["after"] == "cursor_1"


@respx.mock
async def test_list_products_raises_on_meta_error(client):
    respx.get(url__regex=PRODUCTS_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "(#200) Permissions error"}}
        )
    )

    with pytest.raises(CatalogError, match="Permissions error"):
        await client.list_products()
